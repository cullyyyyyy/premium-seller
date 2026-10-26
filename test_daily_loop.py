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


async def _instant_wait(*args, **kwargs):
    return None
