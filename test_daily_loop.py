import asyncio
import json
import math
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

import pytest

import daily_loop as dl
import condor_order_executor as coe
from test_iron_butterfly import _base_chain
from iron_butterfly import build_iron_butterfly


def _dt(hour, minute, second=0):
    return datetime(2026, 10, 26, hour, minute, second)


@pytest.fixture(autouse=True)
def mock_alerts():
    """alerts.py fires real (fire-and-forget) HTTP POSTs -- every test in
    this file goes through run_day() paths that now call it. Patch it out
    everywhere so the suite stays hermetic and fast, not dependent on a
    real (and in CI, nonexistent) webhook URL. Yields the mock so tests
    can assert the right alert actually fired, not just that nothing
    crashed."""
    with patch("daily_loop._alerts", MagicMock()) as m:
        yield m


@pytest.fixture(autouse=True)
def isolate_intraday_log(tmp_path, monkeypatch):
    """run_day() now appends a mark-to-market snapshot to
    logs/intraday_pnl_<date>.jsonl on every monitoring poll (see
    _log_intraday_snapshot). Every test in this file calls run_day()
    without passing intraday_log_path, so without this they'd all write
    into the REAL logs/ directory under today's actual date -- on a day
    this bot is really trading, that would interleave fake test rows into
    the same file real production snapshots land in. Redirect the
    no-argument default to a throwaway path instead."""
    monkeypatch.setattr(dl, "_default_intraday_log_path", lambda *_a, **_kw: tmp_path / "intraday_test.jsonl")


class TestShouldExit:
    def _position(self, profit_target_debit=4.20):
        chain = _base_chain()
        result = build_iron_butterfly(chain)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        position.profit_target_debit = profit_target_debit
        return position

    def test_exits_on_profit_target_before_backstop(self):
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=4.00, now=_dt(11, 0),
                                 backstop_time=dt_time(14, 30))
        assert reason == "profit_target"

    def test_exits_on_profit_target_at_exact_boundary(self):
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=4.20, now=_dt(11, 0),
                                 backstop_time=dt_time(14, 30))
        assert reason == "profit_target"

    def test_no_exit_before_target_and_before_backstop(self):
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=5.00, now=_dt(11, 0),
                                 backstop_time=dt_time(14, 30))
        assert reason is None

    def test_exits_on_backstop_even_without_a_quote(self):
        """A missing/stale quote (current_debit=None) must never block
        the time-based safety exit."""
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=None, now=_dt(14, 30),
                                 backstop_time=dt_time(14, 30))
        assert reason == "backstop"

    def test_backstop_fires_even_if_debit_still_above_target(self):
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=5.50, now=_dt(14, 31),
                                 backstop_time=dt_time(14, 30))
        assert reason == "backstop"

    def test_profit_target_checked_before_backstop_when_both_true(self):
        """At/after backstop time, profit target still wins if it's
        ALSO satisfied -- same exit either way in that case, but this
        pins the precedence so a future change notices if it flips."""
        position = self._position(profit_target_debit=4.20)
        reason = dl.should_exit(position, current_debit=4.00, now=_dt(14, 31),
                                 backstop_time=dt_time(14, 30))
        assert reason == "profit_target"


class TestEvaluateDefend:
    def _position(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        return position
        # entry_net_credit = (3.00+2.90) - (0.25+0.20) = 5.45
        # breakeven_lower = 760 - 5.45 = 754.55; breakeven_upper = 760 + 5.45 = 765.45

    def test_no_action_inside_the_profit_zone(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        action, next_streaks = dl.evaluate_defend(
            position, 760.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
        )
        assert action is None
        assert next_streaks == dl.DefendStreaks(0, 0, 0)

    def test_single_breach_does_not_fire_before_confirm_polls(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        for _ in range(3):
            action, streaks = dl.evaluate_defend(
                position, 750.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
            )
            assert action is None
        assert streaks.put_breach_streak == 3

    def test_put_breach_confirmed_for_four_polls_rolls_the_call_side(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        action = None
        for _ in range(4):
            action, streaks = dl.evaluate_defend(
                position, 750.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
            )
        assert action == "roll_call"

    def test_call_breach_confirmed_for_four_polls_rolls_the_put_side(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        action = None
        for _ in range(4):
            action, streaks = dl.evaluate_defend(
                position, 770.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
            )
        assert action == "roll_put"

    def test_streak_resets_if_price_recovers_before_confirming(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        for _ in range(3):
            _, streaks = dl.evaluate_defend(
                position, 750.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
            )
        assert streaks.put_breach_streak == 3
        action, streaks = dl.evaluate_defend(
            position, 760.0, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
        )
        assert action is None
        assert streaks.put_breach_streak == 0

    def test_no_roll_once_the_daily_cap_is_used(self):
        position = self._position()
        streaks = dl.DefendStreaks()
        action = None
        for _ in range(4):
            action, streaks = dl.evaluate_defend(
                position, 750.0, streaks, rolled_side=None, rolls_used=1, confirm_polls=4, max_rolls=1,
            )
        assert action is None

    def test_missing_price_is_a_no_op(self):
        position = self._position()
        streaks = dl.DefendStreaks(2, 0, 0)
        action, next_streaks = dl.evaluate_defend(
            position, None, streaks, rolled_side=None, rolls_used=0, confirm_polls=4, max_rolls=1,
        )
        assert action is None
        assert next_streaks == streaks  # untouched, not reset

    def test_rolled_side_only_watches_the_rolled_side_for_reversal(self):
        """After a call-side roll, the new call breakeven is derived from
        the ROLLED short_call.strike, not the stale single short_strike --
        simulate the roll by replacing short_call directly, same shape
        _finalize_untested_roll leaves the position in."""
        position = self._position()
        position.short_call = coe.CondorLegRecord(symbol="rolled_call", strike=764.0, entry_action="Sell to Open")
        position.entry_net_credit = 7.05  # 5.45 original + 1.60 roll credit
        # new call breakeven = 764 + 7.05 = 771.05
        streaks = dl.DefendStreaks()
        # Deep put-side move must NOT register at all once a side is rolled.
        action, streaks = dl.evaluate_defend(
            position, 700.0, streaks, rolled_side="call", rolls_used=1, confirm_polls=4, max_rolls=1,
        )
        assert action is None
        assert streaks.rolled_side_breach_streak == 0

        action = None
        for _ in range(4):
            action, streaks = dl.evaluate_defend(
                position, 772.0, streaks, rolled_side="call", rolls_used=1, confirm_polls=4, max_rolls=1,
            )
        assert action == "close_reversal"


class TestAttemptUntestedRoll:
    """build_untested_side_roll/select_untested_short are unit-tested
    directly in test_iron_butterfly.py (the opposite_strike guard
    itself). These exercise _attempt_untested_roll's own job: forwarding
    the tested side's open short strike as opposite_strike, and -- the
    behavior added after the Nov 5 2026 incident -- force-closing the
    WHOLE position when no safe roll strike exists, instead of leaving
    a structure that's already breached its untested side unrolled and
    unwatched until the backstop time."""

    def _position(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        return position

    def test_build_failure_force_closes_the_position(self, mock_alerts):
        position = self._position()
        executor = MagicMock()

        def _fake_submit_exit(pos, quotes, *, reason):
            pos.status = "closed"
            pos.exit_debit = 9.99
            pos.exit_reason = reason
            pos.realized_pnl = -123.45
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=9.99,
                                    filled_quantity=pos.contracts)
        executor.submit_exit.side_effect = _fake_submit_exit

        with patch("daily_loop.fetch_chain_snapshot", return_value=_base_chain()), \
             patch("daily_loop.build_untested_side_roll",
                   side_effect=dl.IronButterflyError("no call strike clears the open short put")), \
             patch("daily_loop.fetch_leg_quotes", return_value={"x": (1.0, 1.1)}):
            closed = dl._attempt_untested_roll(executor, MagicMock(), position, "call", "SPY")

        assert closed is True
        assert position.status == "closed"
        executor.submit_exit.assert_called_once()
        _, kwargs = executor.submit_exit.call_args
        assert kwargs["reason"] == "roll_unavailable"
        mock_alerts.notify_exit.assert_called_once()
        mock_alerts.notify_roll_failed.assert_called_once()

    def test_build_failure_but_close_order_does_not_fill_returns_false(self, mock_alerts):
        """submit_exit left the position open (order didn't fill) -- the
        caller must NOT treat this as closed and stop monitoring."""
        position = self._position()
        executor = MagicMock()
        executor.submit_exit.return_value = coe.OrderResult(status=coe.OrderStatus.TIMEOUT)

        with patch("daily_loop.fetch_chain_snapshot", return_value=_base_chain()), \
             patch("daily_loop.build_untested_side_roll",
                   side_effect=dl.IronButterflyError("no call strike clears the open short put")), \
             patch("daily_loop.fetch_leg_quotes", return_value={"x": (1.0, 1.1)}):
            closed = dl._attempt_untested_roll(executor, MagicMock(), position, "call", "SPY")

        assert closed is False
        assert position.status == "open"
        mock_alerts.notify_exit.assert_not_called()

    def test_opposite_strike_passed_through_as_the_tested_sides_open_short_strike(self, mock_alerts):
        position = self._position()
        executor = MagicMock()
        executor.submit_untested_roll.return_value = coe.OrderResult(
            status=coe.OrderStatus.FILLED, net_price=1.0, filled_quantity=position.contracts)

        with patch("daily_loop.fetch_chain_snapshot", return_value=_base_chain()), \
             patch("daily_loop.build_untested_side_roll") as mock_build, \
             patch("daily_loop.fetch_leg_quotes", return_value={"x": (1.0, 1.1)}):
            mock_build.return_value = MagicMock(new_short_strike=764.0, new_wing_strike=766.0)
            closed = dl._attempt_untested_roll(executor, MagicMock(), position, "call", "SPY")

        assert closed is False
        _, kwargs = mock_build.call_args
        assert kwargs["opposite_strike"] == position.short_put.strike

        executor_put_side = MagicMock()
        executor_put_side.submit_untested_roll.return_value = coe.OrderResult(
            status=coe.OrderStatus.FILLED, net_price=1.0, filled_quantity=position.contracts)
        with patch("daily_loop.fetch_chain_snapshot", return_value=_base_chain()), \
             patch("daily_loop.build_untested_side_roll") as mock_build_put, \
             patch("daily_loop.fetch_leg_quotes", return_value={"x": (1.0, 1.1)}):
            mock_build_put.return_value = MagicMock(new_short_strike=756.0, new_wing_strike=752.0)
            dl._attempt_untested_roll(executor_put_side, MagicMock(), position, "put", "SPY")
        _, kwargs_put = mock_build_put.call_args
        assert kwargs_put["opposite_strike"] == position.short_call.strike


class TestContractsFor:
    def test_basic_sizing(self):
        # $1000 budget * 0.98 = $980; max_loss=$2.40/share -> $240/contract -> 4 contracts (980//240=4)
        n = dl.contracts_for(max_loss_per_share=2.40, capital=1000.0, max_pct=0.98)
        assert n == 4

    def test_zero_when_max_loss_exceeds_budget(self):
        n = dl.contracts_for(max_loss_per_share=50.0, capital=1000.0, max_pct=0.98)
        assert n == 0

    def test_zero_or_negative_max_loss_is_zero_contracts(self):
        assert dl.contracts_for(max_loss_per_share=0.0, capital=1000.0, max_pct=0.98) == 0
        assert dl.contracts_for(max_loss_per_share=-1.0, capital=1000.0, max_pct=0.98) == 0

    def test_scales_with_capital(self):
        n1 = dl.contracts_for(max_loss_per_share=2.40, capital=1000.0, max_pct=0.98)
        n2 = dl.contracts_for(max_loss_per_share=2.40, capital=5000.0, max_pct=0.98)
        assert n2 > n1

    def test_steps_down_when_fees_would_push_a_tight_budget_over(self):
        """2026-10-26, user: "yes close gap" -- sizing must account for
        the real round-trip fees at the resulting contract count, not
        just the gross per-share max_loss. Chosen so the gross-only
        answer (5 contracts, exactly $980.00 of gross risk, zero
        headroom) would overshoot the budget once real fees
        (fees.round_trip_fees(5) ~= $24.82) are added -- the fee-aware
        answer must step down to 4."""
        gross_only_n = int((1000.0 * 0.98) // 196.0)
        assert gross_only_n == 5  # confirms the test setup actually creates zero headroom

        n = dl.contracts_for(max_loss_per_share=1.96, capital=1000.0, max_pct=0.98)
        assert n == 4

        # And the true total at that size genuinely fits the budget.
        from fees import round_trip_fees
        assert n * 1.96 * 100 + round_trip_fees(n) <= 1000.0 * 0.98

    def test_never_sizes_larger_than_the_gross_only_estimate(self):
        """Fees only ever add cost -- the fee-aware answer must never
        exceed what gross-only sizing alone would have allowed."""
        for max_loss, capital in [(1.09, 1000.0), (0.50, 1000.0), (5.00, 10000.0), (0.25, 500.0)]:
            budget = capital * 0.98
            gross_only_n = int(budget // (max_loss * 100))
            n = dl.contracts_for(max_loss_per_share=max_loss, capital=capital, max_pct=0.98)
            assert n <= gross_only_n


class TestWaitUntil:
    def test_returns_immediately_once_target_time_reached(self):
        times = [_dt(10, 29, 59), _dt(10, 30, 0)]

        async def run():
            calls = {"n": 0}

            def now_fn():
                t = times[min(calls["n"], len(times) - 1)]
                calls["n"] += 1
                return t

            with patch("daily_loop.asyncio.sleep", new=_fast_sleep):
                await dl.wait_until(10, 30, _now_fn=now_fn)
            return calls["n"]

        n_calls = asyncio.run(run())
        assert n_calls >= 2  # looped at least once before returning

    def test_returns_immediately_if_already_past_target(self):
        async def run():
            def now_fn():
                return _dt(11, 0)
            await dl.wait_until(10, 30, _now_fn=now_fn)
        asyncio.run(run())  # should not hang


async def _fast_sleep(_seconds):
    return None


class TestRunDay:
    """End-to-end orchestration smoke test, fully mocked: no real
    waiting, no real network. Verifies the pieces are wired together
    correctly in paper mode."""

    def _mock_options_chain_and_patches(self, chain, quotes_sequence):
        options_chain = MagicMock()
        # Every run_day poll now checks this (CONDOR_VIX_RECROSS_EXIT_ENABLED
        # is True in real config.py) -- default to "no VIX signal available"
        # so existing tests that don't care about it aren't tripped up by an
        # unconfigured MagicMock failing a `<= 0` comparison.
        options_chain.get_vix_day_change_pct.return_value = None
        patches = [
            patch("daily_loop.wait_until", new=_instant_wait),
            patch("daily_loop.fetch_chain_snapshot", return_value=chain),
            patch("daily_loop.fetch_leg_quotes", side_effect=quotes_sequence),
        ]
        return options_chain, patches

    def test_full_day_exits_on_profit_target(self, mock_alerts):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        # Quotes sequence: first poll still expensive to close, second poll hits target.
        expensive_quotes = {leg.symbol: (5.00, 5.10) for leg in
                             [result.short_call, result.short_put, result.long_call, result.long_put]}
        cheap_quotes = {
            result.short_call.symbol: (0.50, 0.55),
            result.short_put.symbol: (0.40, 0.45),
            result.long_call.symbol: (0.01, 0.02),
            result.long_put.symbol: (0.01, 0.02),
        }
        options_chain, patches = self._mock_options_chain_and_patches(
            chain, quotes_sequence=[expensive_quotes, cheap_quotes],
        )
        executor = coe.CondorOrderExecutor(session_token="tok")

        async def run():
            with patches[0], patches[1], patches[2], \
                 patch("daily_loop.asyncio.sleep", new=_fast_sleep), \
                 patch.object(coe, "EXECUTE_ORDERS", False):
                return await dl.run_day(options_chain, executor)

        position = asyncio.run(run())
        assert position is not None
        assert position.status == "closed"
        assert position.exit_reason == "profit_target"
        mock_alerts.notify_entry.assert_called_once()
        mock_alerts.notify_exit.assert_called_once()
        mock_alerts.notify_backstop_failure.assert_not_called()

    def test_logs_an_intraday_snapshot_per_poll(self, mock_alerts, tmp_path):
        """2026-10-28, found while building a live status dashboard: the
        monitoring loop already polls quotes every MONITOR_POLL_SEC but
        never persisted what it saw, so there was no way to show the
        day's P&L over time -- only a single right-now reading pulled
        straight from the broker. Confirms run_day now appends one
        record per poll with the fields the dashboard needs."""
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        expensive_quotes = {leg.symbol: (5.00, 5.10) for leg in
                             [result.short_call, result.short_put, result.long_call, result.long_put]}
        cheap_quotes = {
            result.short_call.symbol: (0.50, 0.55),
            result.short_put.symbol: (0.40, 0.45),
            result.long_call.symbol: (0.01, 0.02),
            result.long_put.symbol: (0.01, 0.02),
        }
        options_chain, patches = self._mock_options_chain_and_patches(
            chain, quotes_sequence=[expensive_quotes, cheap_quotes],
        )
        options_chain.get_equity_mid_price.return_value = 770.50
        executor = coe.CondorOrderExecutor(session_token="tok")
        log_path = tmp_path / "intraday.jsonl"

        async def run():
            with patches[0], patches[1], patches[2], \
                 patch("daily_loop.asyncio.sleep", new=_fast_sleep), \
                 patch.object(coe, "EXECUTE_ORDERS", False):
                return await dl.run_day(options_chain, executor, intraday_log_path=log_path)

        position = asyncio.run(run())
        assert position.status == "closed"

        # The mocked quotes price every leg identically, so the very first
        # poll's mid-based debit is already 0.0 -- below any positive
        # profit target -- and the loop exits on that one poll. One
        # record is exactly what should have been logged for it.
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["spy_price"] == 770.50
        assert record["debit_to_close"] == pytest.approx(coe.mid_debit_to_close(position, expensive_quotes))
        assert record["short_strike"] == result.short_strike
        assert record["contracts"] == position.contracts
        assert record["net_pnl_mark"] is not None

    def test_zero_sized_contracts_skips_entry(self, mock_alerts):
        chain = _base_chain()
        options_chain, patches = self._mock_options_chain_and_patches(chain, quotes_sequence=[])
        executor = coe.CondorOrderExecutor(session_token="tok")

        async def run():
            with patches[0], patches[1], \
                 patch("daily_loop.CAPITAL_PER_SYMBOL", 1.0), \
                 patch.object(coe, "EXECUTE_ORDERS", False):
                return await dl.run_day(options_chain, executor)

        position = asyncio.run(run())
        assert position is None
        mock_alerts.notify_no_entry.assert_called_once()

    def test_skips_entry_when_vix_already_elevated_at_entry(self, mock_alerts):
        """CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT, added 2026-11-06: the risk
        CONDOR_VIX_RECROSS_EXIT_ENABLED doesn't cover, since that rule only
        arms after a negative dip post-entry. 10/30 (+9.24% at entry) and
        11/05 (+26.67% at entry) both never dipped and rode to a backstop
        loss -- this reproduces that condition directly and checks entry
        is skipped before the chain is even fetched."""
        chain = _base_chain()
        options_chain, patches = self._mock_options_chain_and_patches(chain, quotes_sequence=[])
        options_chain.get_vix_day_change_pct.return_value = 9.24
        executor = coe.CondorOrderExecutor(session_token="tok")

        async def run():
            with patches[0], patch("daily_loop.fetch_chain_snapshot") as mock_fetch, \
                 patch.object(coe, "EXECUTE_ORDERS", False):
                position = await dl.run_day(options_chain, executor)
                mock_fetch.assert_not_called()
                return position

        position = asyncio.run(run())
        assert position is None
        mock_alerts.notify_no_entry.assert_called_once()
        reason = mock_alerts.notify_no_entry.call_args.args[0]
        assert "9.24" in reason
        assert "5.00" in reason

    def test_does_not_skip_entry_when_vix_is_below_threshold(self, mock_alerts):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        flat_quotes = {leg.symbol: (0.01, 0.02) for leg in
                       [result.short_call, result.short_put, result.long_call, result.long_put]}
        options_chain, patches = self._mock_options_chain_and_patches(chain, quotes_sequence=[flat_quotes])
        options_chain.get_vix_day_change_pct.return_value = 4.99
        options_chain.get_equity_mid_price.return_value = 770.0
        executor = coe.CondorOrderExecutor(session_token="tok")

        async def run():
            with patches[0], patches[1], patches[2], \
                 patch("daily_loop.asyncio.sleep", new=_fast_sleep), \
                 patch.object(coe, "EXECUTE_ORDERS", False):
                return await dl.run_day(options_chain, executor)

        position = asyncio.run(run())
        assert position is not None
        mock_alerts.notify_no_entry.assert_not_called()


class TestRunDayLiveRestingProfitTarget:
    """2026-10-27, user: "After an order gets filled, you can add a day
    limit order to sell at whatever value is 25% profit[,] that would
    be most efficient." In LIVE mode, run_day rests a Day limit
    profit-target order on the book right after entry instead of
    waiting for a quote poll to notice the target was already crossed.
    Paper mode is untouched (TestRunDay above) -- its simulated fills
    are already instant, so there's no latency to save there."""

    def _entered_position(self, chain):
        result = build_iron_butterfly(chain, contracts=1)
        real_executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = real_executor.submit_entry(result)
        return position

    def _mock_live_executor(self, position):
        executor = MagicMock()
        executor.is_live = True
        executor.submit_entry.return_value = (
            coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=position.entry_net_credit,
                             filled_quantity=position.contracts, order_id="entry-1"),
            position,
        )
        return executor

    def test_rests_a_profit_target_order_immediately_after_entry_and_fills(self, mock_alerts):
        chain = _base_chain()
        position = self._entered_position(chain)
        executor = self._mock_live_executor(position)
        executor.submit_resting_exit.return_value = coe.OrderResult(
            status=coe.OrderStatus.SUBMITTED, order_id="resting-1",
        )

        def fake_poll_resting_exit(pos, order_id, *, reason):
            pos.status = "closed"
            pos.exit_debit = 0.10
            pos.exit_reason = reason
            pos.realized_pnl = 100.0
            return True
        executor.poll_resting_exit = MagicMock(side_effect=fake_poll_resting_exit)

        options_chain = MagicMock()
        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            result_position = asyncio.run(dl.run_day(options_chain, executor))

        executor.submit_resting_exit.assert_called_once_with(
            position, position.profit_target_debit, reason="profit_target",
        )
        executor.poll_resting_exit.assert_called_once_with(position, "resting-1", reason="profit_target")
        executor.cancel_resting_exit.assert_not_called()
        assert result_position is position
        assert result_position.status == "closed"
        mock_alerts.notify_exit.assert_called_once_with(
            "profit_target", 0.10, 100.0, position.max_loss * position.multiplier * position.contracts,
        )

    def test_cancels_resting_order_and_falls_back_to_backstop_when_unfilled(self, mock_alerts):
        chain = _base_chain()
        position = self._entered_position(chain)
        executor = self._mock_live_executor(position)
        executor.submit_resting_exit.return_value = coe.OrderResult(
            status=coe.OrderStatus.SUBMITTED, order_id="resting-1",
        )
        executor.poll_resting_exit.return_value = False  # never fills
        executor.cancel_resting_exit.return_value = True

        def fake_submit_exit(pos, quotes_arg, *, reason, dry_run=False, price_override=None):
            pos.status = "closed"
            pos.exit_debit = 0.05
            pos.exit_reason = reason
            pos.realized_pnl = 90.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=0.05, filled_quantity=pos.contracts)
        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        options_chain = MagicMock()
        # CONDOR_BACKSTOP_HOUR/MINUTE=0,0 so the loop's direct
        # now.time() >= backstop_time check is true on the very first
        # iteration (real wall clock is always past midnight) --
        # forces the cancel-and-fallback path without mocking datetime.
        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value={}), \
             patch("daily_loop.should_exit", return_value="backstop"), \
             patch("daily_loop.CONDOR_BACKSTOP_HOUR", 0), \
             patch("daily_loop.CONDOR_BACKSTOP_MINUTE", 0), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            result_position = asyncio.run(dl.run_day(options_chain, executor))

        executor.cancel_resting_exit.assert_called_once_with("resting-1")
        executor.submit_exit.assert_called_once()
        assert executor.submit_exit.call_args.kwargs["reason"] == "backstop"
        assert result_position is position
        assert result_position.status == "closed"
        mock_alerts.notify_exit.assert_called_once_with(
            "backstop", 0.05, 90.0, position.max_loss * position.multiplier * position.contracts,
        )

    def test_falls_back_to_reactive_polling_if_resting_order_submission_fails(self, mock_alerts):
        """submit_resting_exit can fail (network error, rejection) --
        run_day must not crash or silently drop monitoring; it should
        fall back to the pre-existing reactive quote-polling behavior."""
        chain = _base_chain()
        position = self._entered_position(chain)
        executor = self._mock_live_executor(position)
        executor.submit_resting_exit.return_value = coe.OrderResult(
            status=coe.OrderStatus.ERROR, message="Resting exit order submission failed",
        )

        def fake_submit_exit(pos, quotes_arg, *, reason, dry_run=False, price_override=None):
            pos.status = "closed"
            pos.exit_debit = 0.10
            pos.exit_reason = reason
            pos.realized_pnl = 95.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=0.10, filled_quantity=pos.contracts)
        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        options_chain = MagicMock()
        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value={}), \
             patch("daily_loop.should_exit", return_value="profit_target"), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            result_position = asyncio.run(dl.run_day(options_chain, executor))

        executor.poll_resting_exit.assert_not_called()
        executor.cancel_resting_exit.assert_not_called()
        executor.submit_exit.assert_called_once()
        assert result_position is position
        assert result_position.status == "closed"


class TestRunDayVixRecrossExit:
    """2026-11-05 analysis (see config.py's CONDOR_VIX_RECROSS_EXIT_ENABLED):
    force-exit the moment VIX, having dipped below its own previous close
    at some point after entry, recrosses back above it. Isolated from
    CONDOR_DEFEND_ENABLED here (patched off) since that path's own
    spy_price handling is covered by its own tests, not this one."""

    def _entered_position(self, chain):
        result = build_iron_butterfly(chain, contracts=1)
        real_executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = real_executor.submit_entry(result)
        return position

    def _mock_live_executor(self, position):
        executor = MagicMock()
        executor.is_live = True
        executor.submit_entry.return_value = (
            coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=position.entry_net_credit,
                             filled_quantity=position.contracts, order_id="entry-1"),
            position,
        )
        return executor

    def test_fires_only_after_a_negative_poll_is_followed_by_a_positive_one(self, mock_alerts):
        chain = _base_chain()
        position = self._entered_position(chain)
        executor = self._mock_live_executor(position)
        executor.submit_resting_exit.return_value = coe.OrderResult(
            status=coe.OrderStatus.SUBMITTED, order_id="resting-1",
        )
        executor.poll_resting_exit.return_value = False  # never fills
        executor.cancel_resting_exit.return_value = True

        def fake_submit_exit(pos, quotes_arg, *, reason, dry_run=False, price_override=None):
            pos.status = "closed"
            pos.exit_debit = 0.20
            pos.exit_reason = reason
            pos.realized_pnl = 165.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=0.20, filled_quantity=pos.contracts)
        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        options_chain = MagicMock()
        # Leading 0.0 is the separate entry-time fetch (run_day's VIX
        # entry-skip check, CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT) --
        # below threshold, so entry proceeds and the monitoring loop
        # gets to its own sequence unchanged: Poll 1 VIX down -2% (dip
        # recorded), Poll 2 VIX up +1% -- the recross. If it fires
        # here, the loop must never reach a 3rd poll.
        options_chain.get_vix_day_change_pct.side_effect = [0.0, -2.0, 1.0, 999.0]

        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value={}), \
             patch("daily_loop.should_exit", return_value=None), \
             patch("daily_loop.CONDOR_DEFEND_ENABLED", False), \
             patch("daily_loop.CONDOR_BACKSTOP_HOUR", 23), \
             patch("daily_loop.CONDOR_BACKSTOP_MINUTE", 59), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            result_position = asyncio.run(dl.run_day(options_chain, executor))

        executor.cancel_resting_exit.assert_called_once_with("resting-1")
        executor.submit_exit.assert_called_once()
        assert executor.submit_exit.call_args.kwargs["reason"] == "vix_recross"
        assert result_position.status == "closed"
        assert result_position.realized_pnl == 165.0

    def test_never_fires_if_vix_is_already_positive_at_entry(self, mock_alerts):
        """2026-10-30 and 2026-11-05 live: VIX was already above its prior
        close on the very first poll -- there's no dip to recross FROM, so
        this must never fire, no matter how high VIX goes from there."""
        chain = _base_chain()
        position = self._entered_position(chain)
        executor = self._mock_live_executor(position)
        executor.submit_resting_exit.return_value = coe.OrderResult(
            status=coe.OrderStatus.SUBMITTED, order_id="resting-1",
        )
        executor.poll_resting_exit.return_value = False

        options_chain = MagicMock()
        # Leading 3.0 is the separate entry-time fetch -- below
        # CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT (5.0) so entry still
        # proceeds here (that filter is covered by its own tests in
        # TestRunDay); this test is isolating the recross rule itself,
        # which must not fire on these all-positive, never-dipping
        # monitoring polls no matter how high VIX goes.
        options_chain.get_vix_day_change_pct.side_effect = [3.0, 9.0, 15.0, 27.0]

        class _Bail(Exception):
            pass

        call_count = {"n": 0}

        async def _sleep_then_stop(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 3:
                raise _Bail  # bail out after exhausting the side_effect list
            return None

        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value={}), \
             patch("daily_loop.should_exit", return_value=None), \
             patch("daily_loop.CONDOR_DEFEND_ENABLED", False), \
             patch("daily_loop.CONDOR_BACKSTOP_HOUR", 23), \
             patch("daily_loop.CONDOR_BACKSTOP_MINUTE", 59), \
             patch("daily_loop.asyncio.sleep", new=_sleep_then_stop), \
             pytest.raises(_Bail):
            asyncio.run(dl.run_day(options_chain, executor))

        executor.cancel_resting_exit.assert_not_called()
        executor.submit_exit.assert_not_called()


class TestRunDayBackstopRetry:
    """Regression tests for a real gap found live 2026-10-26: run_day
    used to give up after exactly ONE exit attempt regardless of
    outcome -- for a "backstop" (the hard safety exit), that defeats
    its entire purpose. Now it retries with escalating price
    aggression, bounded by BACKSTOP_MAX_ATTEMPTS and a price ceiling
    tied to the structure's own defined max_loss."""

    def _setup(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        quotes = {leg.symbol: (0.10, 0.15) for leg in
                  [result.short_call, result.short_put, result.long_call, result.long_put]}
        options_chain = MagicMock()
        executor = coe.CondorOrderExecutor(session_token="tok")
        return chain, options_chain, executor, quotes

    def _run(self, options_chain, executor, chain, quotes):
        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value=quotes), \
             patch("daily_loop.should_exit", return_value="backstop"), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep), \
             patch.object(coe, "EXECUTE_ORDERS", False):
            return asyncio.run(dl.run_day(options_chain, executor))

    def test_retries_with_escalating_price_until_filled(self):
        chain, options_chain, executor, quotes = self._setup()
        seen_overrides = []

        def fake_submit_exit(position, quotes_arg, *, reason, dry_run=False, price_override=None):
            seen_overrides.append(price_override)
            if len(seen_overrides) < 3:
                return coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="didn't fill")
            position.status = "closed"
            position.exit_reason, position.exit_debit, position.realized_pnl = reason, 1.0, 10.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=1.0, filled_quantity=position.contracts)

        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)
        position = self._run(options_chain, executor, chain, quotes)

        assert position.status == "closed"
        assert len(seen_overrides) == 3
        assert seen_overrides[0] is None          # first attempt: planning price, no override
        assert seen_overrides[1] is not None
        assert seen_overrides[2] > seen_overrides[1]   # escalating

    def test_gives_up_after_max_attempts_leaves_position_open_not_hidden(self, mock_alerts):
        chain, options_chain, executor, quotes = self._setup()
        executor.submit_exit = MagicMock(
            return_value=coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="never fills"))

        position = self._run(options_chain, executor, chain, quotes)

        assert position is not None
        assert position.status == "open"  # never silently marked closed
        assert executor.submit_exit.call_count == dl.BACKSTOP_MAX_ATTEMPTS
        # The exact gap this session closed: a position left open after
        # exhausting retries must trigger the critical alert, not just a
        # log line nobody's watching.
        mock_alerts.notify_backstop_failure.assert_called_once()
        call_args = mock_alerts.notify_backstop_failure.call_args.args
        assert call_args[0] == dl.BACKSTOP_MAX_ATTEMPTS

    def test_price_never_escalates_past_the_structural_ceiling(self):
        chain, options_chain, executor, quotes = self._setup()
        seen_overrides = []

        def fake_submit_exit(position, q, *, reason, dry_run=False, price_override=None):
            seen_overrides.append(price_override)
            return coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="never fills")

        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)
        self._run(options_chain, executor, chain, quotes)

        # Ceiling = entry_net_credit + max_loss (the debit implied by the
        # structure's own defined max_loss) -- recomputed independently
        # from the same fixture to check run_day's escalation against it.
        result = build_iron_butterfly(chain, contracts=1)
        from condor_order_executor import _position_from_result
        pos = _position_from_result(result, result.net_credit, order_id=None)
        ceiling = pos.entry_net_credit + pos.max_loss
        for override in seen_overrides:
            if override is not None:
                assert override <= ceiling + 1e-9


class TestRunDayProfitTargetRetry:
    def test_profit_target_failure_keeps_polling_without_escalating_price(self):
        """Unlike backstop, a profit-target exit that doesn't fill isn't
        urgent -- run_day should keep monitoring (never pass a
        price_override for this reason) rather than chasing a worse
        price just to force a fill."""
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        quotes = {leg.symbol: (0.10, 0.15) for leg in
                  [result.short_call, result.short_put, result.long_call, result.long_put]}
        options_chain = MagicMock()
        executor = coe.CondorOrderExecutor(session_token="tok")
        seen_overrides = []

        def fake_submit_exit(position, quotes_arg, *, reason, dry_run=False, price_override=None):
            seen_overrides.append(price_override)
            if len(seen_overrides) < 2:
                return coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="didn't fill")
            position.status = "closed"
            position.exit_reason, position.exit_debit, position.realized_pnl = reason, 1.0, 10.0
            return coe.OrderResult(status=coe.OrderStatus.FILLED, net_price=1.0, filled_quantity=position.contracts)

        executor.submit_exit = MagicMock(side_effect=fake_submit_exit)

        with patch("daily_loop.wait_until", new=_instant_wait), \
             patch("daily_loop.fetch_chain_snapshot", return_value=chain), \
             patch("daily_loop.fetch_leg_quotes", return_value=quotes), \
             patch("daily_loop.should_exit", return_value="profit_target"), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep), \
             patch.object(coe, "EXECUTE_ORDERS", False):
            position = asyncio.run(dl.run_day(options_chain, executor))

        assert position.status == "closed"
        assert all(o is None for o in seen_overrides)


async def _instant_wait(*args, **kwargs):
    return None


class TestHypotheticalNetPnl:
    def _closed_position(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
            quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}
            executor.submit_exit(position, quotes, reason="profit_target")
        return position

    def test_matches_gross_minus_fees_formula(self):
        position = self._closed_position()
        quotes = {leg.symbol: (0.20, 0.25) for leg in position.legs}
        hypo = dl.hypothetical_net_pnl(position, quotes)
        debit = coe.mid_debit_to_close(position, quotes)
        expected = ((position.entry_net_credit - debit) * position.multiplier * position.contracts
                    - position.round_trip_fees)
        assert math.isclose(hypo, expected, abs_tol=1e-9)

    def test_does_not_mutate_the_position(self):
        """Pure hindsight calculation -- must never touch the real,
        already-recorded realized_pnl."""
        position = self._closed_position()
        original_realized = position.realized_pnl
        quotes = {leg.symbol: (0.01, 0.02) for leg in position.legs}
        dl.hypothetical_net_pnl(position, quotes)
        assert position.realized_pnl == original_realized


class TestLogIntradaySnapshot:
    """vix_change_pct added 2026-11-06: run_day was computing this every
    poll for the recross-exit check and discarding it, so the only way
    to check what the VIX rule would have done on a past day was
    reconstructing it from a separately-pulled price history instead of
    the bot's own logs. Now it's persisted alongside the rest of the
    mark-to-market snapshot."""

    def _position(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        return position

    def test_records_vix_change_pct_alongside_the_rest_of_the_snapshot(self, tmp_path):
        position = self._position()
        options_chain = MagicMock()
        options_chain.get_equity_mid_price.return_value = 760.0
        log_path = tmp_path / "intraday_test.jsonl"
        quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}

        dl._log_intraday_snapshot(log_path, position, quotes, 1.0, "SPY", options_chain,
                                   _dt(10, 15), vix_change_pct=-3.86)

        record = json.loads(log_path.read_text().strip())
        assert record["vix_change_pct"] == -3.86

    def test_defaults_to_none_when_not_passed(self, tmp_path):
        """Keeps the old 5-positional-arg call site (if anything still
        uses it) working without a crash -- just logs vix_change_pct as
        null rather than requiring every caller to pass it."""
        position = self._position()
        options_chain = MagicMock()
        options_chain.get_equity_mid_price.return_value = 760.0
        log_path = tmp_path / "intraday_test.jsonl"

        dl._log_intraday_snapshot(log_path, position, None, None, "SPY", options_chain, _dt(10, 15))

        record = json.loads(log_path.read_text().strip())
        assert record["vix_change_pct"] is None


class TestTrackPostClose:
    """2026-10-27, user: "check pricing after 2:30 cutoff to evaluate
    P/L in the future" -- track what an already-closed position would
    have been worth had it stayed open, through market close."""

    def _closed_position(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
            quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}
            executor.submit_exit(position, quotes, reason="profit_target")
        return position

    def test_noop_if_position_is_not_actually_closed(self):
        chain = _base_chain()
        result = build_iron_butterfly(chain, contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)  # still open, never exited

        options_chain = MagicMock()
        records = asyncio.run(dl.track_post_close(options_chain, position, _now_fn=lambda: _dt(14, 35)))
        assert records == []

    def test_records_until_market_close_then_stops(self, tmp_path):
        position = self._closed_position()
        options_chain = MagicMock()
        options_chain.get_equity_mid_price.return_value = 765.00
        quotes = {leg.symbol: (0.10, 0.15) for leg in position.legs}

        times = iter([_dt(15, 58), _dt(15, 59), _dt(16, 0)])  # two polls, then 16:00 stops the loop

        def now_fn():
            return next(times, _dt(16, 1))  # safety net, can't hang if logic is wrong

        log_file = tmp_path / "post_close.jsonl"
        with patch("daily_loop.fetch_leg_quotes", return_value=quotes), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            records = asyncio.run(dl.track_post_close(
                options_chain, position, until_hour=16, until_minute=0,
                log_path=log_file, _now_fn=now_fn,
            ))

        assert len(records) == 2
        assert records[0]["spy_price"] == 765.00
        assert "hypothetical_net_pnl_if_held" in records[0]
        assert records[0]["actual_realized_pnl"] == position.realized_pnl
        assert records[0]["actual_exit_reason"] == "profit_target"

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["timestamp"] == records[0]["timestamp"]

    def test_skips_a_poll_with_no_quotes_but_keeps_going(self, tmp_path):
        position = self._closed_position()
        options_chain = MagicMock()
        options_chain.get_equity_mid_price.return_value = 765.0
        ok_quotes = {leg.symbol: (0.10, 0.15) for leg in position.legs}

        times = iter([_dt(15, 58), _dt(15, 59), _dt(16, 0)])

        def now_fn():
            return next(times, _dt(16, 1))

        with patch("daily_loop.fetch_leg_quotes", side_effect=[None, ok_quotes]), \
             patch("daily_loop.asyncio.sleep", new=_fast_sleep):
            records = asyncio.run(dl.track_post_close(
                options_chain, position, until_hour=16, until_minute=0,
                log_path=tmp_path / "x.jsonl", _now_fn=now_fn,
            ))

        assert len(records) == 1  # first poll had no quotes (skipped), second recorded
