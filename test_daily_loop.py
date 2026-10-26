import asyncio
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

import pytest

import daily_loop as dl
import condor_order_executor as coe
from test_iron_butterfly import _base_chain
from iron_butterfly import build_iron_butterfly


def _dt(hour, minute, second=0):
    return datetime(2026, 10, 26, hour, minute, second)


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
        patches = [
            patch("daily_loop.wait_until", new=_instant_wait),
            patch("daily_loop.fetch_chain_snapshot", return_value=chain),
            patch("daily_loop.fetch_leg_quotes", side_effect=quotes_sequence),
        ]
        return options_chain, patches

    def test_full_day_exits_on_profit_target(self):
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

    def test_zero_sized_contracts_skips_entry(self):
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

    def test_gives_up_after_max_attempts_leaves_position_open_not_hidden(self):
        chain, options_chain, executor, quotes = self._setup()
        executor.submit_exit = MagicMock(
            return_value=coe.OrderResult(status=coe.OrderStatus.TIMEOUT, message="never fills"))

        position = self._run(options_chain, executor, chain, quotes)

        assert position is not None
        assert position.status == "open"  # never silently marked closed
        assert executor.submit_exit.call_count == dl.BACKSTOP_MAX_ATTEMPTS

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
