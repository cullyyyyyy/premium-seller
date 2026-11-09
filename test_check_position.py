import math
from unittest.mock import MagicMock, patch

import pytest

import check_position as cp


def _position(symbol="SPY   261027C00769000", quantity=6, direction="Short",
               avg_open=1.17, multiplier=100.0, underlying="SPY",
               instrument_type="Equity Option"):
    return {
        "symbol": symbol,
        "underlying-symbol": underlying,
        "instrument-type": instrument_type,
        "quantity": quantity,
        "quantity-direction": direction,
        "average-open-price": str(avg_open),
        "multiplier": multiplier,
    }


class TestLegPnl:
    def test_short_leg_profits_as_price_falls(self):
        pos = _position(direction="Short", avg_open=1.17, quantity=6)
        pnl = cp.leg_pnl(pos, current_mid=1.00)
        assert math.isclose(pnl, (1.17 - 1.00) * 6 * 100, abs_tol=1e-9)
        assert pnl > 0

    def test_short_leg_loses_as_price_rises(self):
        pos = _position(direction="Short", avg_open=1.17, quantity=6)
        pnl = cp.leg_pnl(pos, current_mid=1.50)
        assert pnl < 0

    def test_long_leg_profits_as_price_rises(self):
        pos = _position(direction="Long", avg_open=0.16, quantity=6)
        pnl = cp.leg_pnl(pos, current_mid=0.30)
        assert math.isclose(pnl, (0.30 - 0.16) * 6 * 100, abs_tol=1e-9)
        assert pnl > 0

    def test_matches_the_aggregate_mid_debit_formula_for_a_full_butterfly(self):
        """Summing all 4 legs' individual P&L via leg_pnl must equal the
        project's existing aggregate formula: (entry_net_credit -
        current_debit) * multiplier * contracts -- this is deriving the
        same economics from the broker's own records instead of this
        project's in-memory CondorPosition, so it has to agree exactly."""
        short_call = _position("SC", direction="Short", avg_open=1.17, quantity=6)
        short_put = _position("SP", direction="Short", avg_open=0.75, quantity=6)
        long_call = _position("LC", direction="Long", avg_open=0.17, quantity=6)
        long_put = _position("LP", direction="Long", avg_open=0.16, quantity=6)

        mids = {"SC": 0.95, "SP": 0.60, "LC": 0.10, "LP": 0.09}
        total = sum(cp.leg_pnl(p, mids[p["symbol"]])
                    for p in (short_call, short_put, long_call, long_put))

        entry_net_credit = 1.17 + 0.75 - 0.17 - 0.16
        current_debit = mids["SC"] + mids["SP"] - mids["LC"] - mids["LP"]
        expected = (entry_net_credit - current_debit) * 100 * 6
        assert math.isclose(total, expected, abs_tol=1e-9)


class TestComputePositionPnl:
    def test_aggregates_legs_and_handles_a_missing_quote(self):
        positions = [
            _position("SC", direction="Short", avg_open=1.17, quantity=6),
            _position("SP", direction="Short", avg_open=0.75, quantity=6),
            _position("LC", direction="Long", avg_open=0.17, quantity=6),
            _position("LP", direction="Long", avg_open=0.16, quantity=6),
        ]
        mock_chain = MagicMock()
        mock_chain._get_batch_quotes.return_value = {
            "SC": {"bid": "0.94", "ask": "0.96"},
            "SP": {"bid": "0.59", "ask": "0.61"},
            "LC": {"bid": "0.09", "ask": "0.11"},
            # LP intentionally missing -- exercises the no-quote branch
        }
        result = cp.compute_position_pnl(positions, mock_chain)

        assert result["any_missing_quote"] is True
        lp_leg = next(leg for leg in result["legs"] if leg["symbol"] == "LP")
        assert lp_leg["mid"] is None
        assert lp_leg["pnl"] is None
        sc_leg = next(leg for leg in result["legs"] if leg["symbol"] == "SC")
        assert math.isclose(sc_leg["mid"], 0.95, abs_tol=1e-9)
        assert math.isclose(sc_leg["pnl"], (1.17 - 0.95) * 6 * 100, abs_tol=1e-9)
        # gross_pnl only sums the 3 legs that had a quote
        expected_gross = sc_leg["pnl"] + next(l for l in result["legs"] if l["symbol"] == "SP")["pnl"] \
            + next(l for l in result["legs"] if l["symbol"] == "LC")["pnl"]
        assert math.isclose(result["gross_pnl"], expected_gross, abs_tol=1e-9)
        assert result["net_pnl"] == pytest.approx(result["gross_pnl"] - result["round_trip_fees"])


class TestFetchOpenOptionPositions:
    def test_filters_to_the_target_symbol_and_instrument_type(self):
        """Real shape verified live 2026-10-27 against a real (tiny,
        single-leg) sandbox position."""
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"items": [
            _position("SPY   261027P00766000", underlying="SPY"),
            {"symbol": "QQQ   261027C00500000", "underlying-symbol": "QQQ",
             "instrument-type": "Equity Option", "quantity": 1,
             "quantity-direction": "Long", "average-open-price": "1.00", "multiplier": 100.0},
            {"symbol": "SPY", "underlying-symbol": "SPY",
             "instrument-type": "Equity", "quantity": 100,
             "quantity-direction": "Long", "average-open-price": "600.00", "multiplier": 1.0},
        ]}}
        with patch.object(cp.requests, "get", return_value=resp) as mock_get:
            positions = cp.fetch_open_option_positions("tok", symbol="SPY")

        assert len(positions) == 1
        assert positions[0]["symbol"] == "SPY   261027P00766000"
        called_url = mock_get.call_args.args[0]
        assert called_url.endswith("/positions")

    def test_returns_empty_list_when_flat(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"items": []}}
        with patch.object(cp.requests, "get", return_value=resp):
            positions = cp.fetch_open_option_positions("tok", symbol="SPY")
        assert positions == []


class TestMain:
    def test_reports_no_positions_when_flat(self, capsys):
        with patch("check_position.TastytradeAuth") as MockAuth, \
             patch.object(cp, "fetch_open_option_positions", return_value=[]):
            MockAuth.return_value.login.return_value = True
            rc = cp.main()
        assert rc == 0
        assert "No open" in capsys.readouterr().out

    def test_computes_net_pnl_for_an_open_four_leg_butterfly(self, capsys):
        positions = [
            _position("SC", direction="Short", avg_open=1.17, quantity=6),
            _position("SP", direction="Short", avg_open=0.75, quantity=6),
            _position("LC", direction="Long", avg_open=0.17, quantity=6),
            _position("LP", direction="Long", avg_open=0.16, quantity=6),
        ]
        mock_chain = MagicMock()
        mock_chain._get_batch_quotes.return_value = {
            "SC": {"bid": "0.94", "ask": "0.96"},
            "SP": {"bid": "0.59", "ask": "0.61"},
            "LC": {"bid": "0.09", "ask": "0.11"},
            "LP": {"bid": "0.08", "ask": "0.10"},
        }
        with patch("check_position.TastytradeAuth") as MockAuth, \
             patch.object(cp, "fetch_open_option_positions", return_value=positions), \
             patch.object(cp, "create_production_options_chain", return_value=mock_chain):
            MockAuth.return_value.login.return_value = True
            rc = cp.main()

        out = capsys.readouterr().out
        assert rc == 0
        assert "Net P&L:" in out
        assert "Gross P&L:" in out

    def test_login_failure_returns_nonzero(self, capsys):
        with patch("check_position.TastytradeAuth") as MockAuth:
            MockAuth.return_value.login.return_value = False
            rc = cp.main()
        assert rc == 1
