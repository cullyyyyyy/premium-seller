import math
from unittest.mock import MagicMock

import pytest

import manual_close as mc
import condor_order_executor as coe
import fees


def _pos(symbol, direction, avg_open, quantity=3, multiplier=100.0):
    return {
        "symbol": symbol,
        "quantity": quantity,
        "quantity-direction": direction,
        "average-open-price": str(avg_open),
        "multiplier": multiplier,
    }


def _butterfly_positions(quantity=3):
    return [
        _pos("SPY   261028C00770000", "Short", 2.54, quantity),  # short call, strike 770
        _pos("SPY   261028P00770000", "Short", 2.11, quantity),  # short put, strike 770
        _pos("SPY   261028C00777000", "Long", 0.30, quantity),   # long call wing, strike 777
        _pos("SPY   261028P00763000", "Long", 0.39, quantity),   # long put wing, strike 763
    ]


class TestParseOccSymbol:
    def test_call(self):
        assert mc._parse_occ_symbol("SPY   261028C00770000") == ("call", 770.0)

    def test_put(self):
        assert mc._parse_occ_symbol("SPY   261028P00763000") == ("put", 763.0)


class TestReconstructOpenPosition:
    def test_builds_a_clean_butterfly(self):
        position = mc.reconstruct_open_position(_butterfly_positions(quantity=3))
        assert position.short_strike == 770.0
        assert position.contracts == 3
        assert position.multiplier == 100
        assert position.short_call.symbol == "SPY   261028C00770000"
        assert position.short_call.entry_action == "Sell to Open"
        assert position.long_call.symbol == "SPY   261028C00777000"
        assert position.long_call.entry_action == "Buy to Open"
        assert position.long_put.strike == 763.0
        expected_credit = 2.54 + 2.11 - 0.30 - 0.39
        assert math.isclose(position.entry_net_credit, expected_credit, abs_tol=1e-9)
        assert position.round_trip_fees == pytest.approx(fees.round_trip_fees(3))

    def test_raises_on_missing_leg(self):
        positions = _butterfly_positions()[:3]  # drop the long put
        with pytest.raises(ValueError, match="missing"):
            mc.reconstruct_open_position(positions)

    def test_raises_on_duplicate_role(self):
        positions = _butterfly_positions() + [_pos("SPY   261028C00780000", "Short", 0.10, 3)]
        with pytest.raises(ValueError, match="more than one"):
            mc.reconstruct_open_position(positions)

    def test_raises_on_mismatched_quantities(self):
        positions = _butterfly_positions(quantity=3)
        positions[0]["quantity"] = 5  # one leg disagrees with the other three
        with pytest.raises(ValueError, match="mismatched quantities"):
            mc.reconstruct_open_position(positions)


class TestCloseOpenPositionNow:
    def _executor(self, is_live=True):
        executor = MagicMock()
        executor.is_live = is_live
        return executor

    def _quotes_for(self, positions):
        return {p["symbol"]: (float(p["average-open-price"]) - 0.05, float(p["average-open-price"]) + 0.05)
                for p in positions}

    def test_no_open_position(self):
        executor = self._executor()
        result = mc.close_open_position_now(executor, MagicMock(), [], reason="manual")
        assert result == {"ok": False, "message": "No open SPY position right now -- nothing to close."}

    def test_refuses_an_unresolvable_structure(self):
        executor = self._executor()
        positions = _butterfly_positions()[:3]
        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is False
        assert "Refusing to close" in result["message"]

    def test_refuses_when_not_live(self):
        executor = self._executor(is_live=False)
        positions = _butterfly_positions()
        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is False
        assert "EXECUTE_ORDERS is False" in result["message"]

    def test_no_quotes_available(self, monkeypatch):
        executor = self._executor()
        positions = _butterfly_positions()
        monkeypatch.setattr(mc, "fetch_leg_quotes", lambda chain, pos: None)
        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is False
        assert "live quote" in result["message"]

    def test_fills_on_first_attempt(self, monkeypatch):
        positions = _butterfly_positions(quantity=3)
        quotes = self._quotes_for(positions)
        monkeypatch.setattr(mc, "fetch_leg_quotes", lambda chain, pos: quotes)

        executor = self._executor()

        def fake_submit_exit(position, quotes_arg, *, reason, price_override=None):
            position.status = "closed"
            position.exit_debit = 3.60
            position.realized_pnl = 100.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=3.60, filled_quantity=position.contracts)
        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is True
        assert "1 attempt" in result["message"]
        assert "realized P&L $+100.00" in result["message"]
        executor.submit_exit.assert_called_once()

    def test_fills_after_escalating_retries(self, monkeypatch):
        positions = _butterfly_positions(quantity=3)
        quotes = self._quotes_for(positions)
        monkeypatch.setattr(mc, "fetch_leg_quotes", lambda chain, pos: quotes)

        executor = self._executor()
        seen_overrides = []

        def fake_submit_exit(position, quotes_arg, *, reason, price_override=None):
            seen_overrides.append(price_override)
            if len(seen_overrides) < 3:
                return coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="didn't fill")
            position.status = "closed"
            position.exit_debit = 3.70
            position.realized_pnl = 50.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=3.70, filled_quantity=position.contracts)
        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is True
        assert "3 attempt" in result["message"]
        assert seen_overrides[0] is None  # first attempt: mid-based planning price, no override
        assert seen_overrides[1] < seen_overrides[2]  # price escalates between misses

    def test_gives_up_after_max_attempts(self, monkeypatch):
        positions = _butterfly_positions(quantity=3)
        quotes = self._quotes_for(positions)
        monkeypatch.setattr(mc, "fetch_leg_quotes", lambda chain, pos: quotes)

        executor = self._executor()
        executor.submit_exit = MagicMock(
            return_value=coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="didn't fill")
        )

        result = mc.close_open_position_now(executor, MagicMock(), positions, reason="manual")
        assert result["ok"] is False
        assert "STILL OPEN" in result["message"]
        assert executor.submit_exit.call_count == mc.MANUAL_CLOSE_MAX_ATTEMPTS
