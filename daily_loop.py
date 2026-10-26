"""
daily_loop.py -- ties iron_butterfly.py (construction) and
condor_order_executor.py (submission) into the once-a-day entry/exit
cycle: wait for the entry window, build+submit the butterfly, monitor
live quotes for the profit-target exit, force-close at the backstop
time otherwise.

should_exit() and contracts_for() are the real decision logic and are
fully unit-tested as pure functions (test_daily_loop.py). run_day()'s
async orchestration (waiting for wall-clock time, the monitoring poll
loop) is thin glue code and, like fetch_chain_snapshot() and the live
order path in condor_order_executor.py, is UNTESTED against a real
connection -- there is no sandbox account wired into this session to
exercise it against. Treat it as unverified until it has run once for
real.
"""
import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import Optional

import pytz

from config import (
    CONDOR_ENTRY_HOUR, CONDOR_ENTRY_MINUTE,
    CONDOR_BACKSTOP_HOUR, CONDOR_BACKSTOP_MINUTE,
    CONDOR_WING_TARGET_DELTA, CONDOR_WING_DELTA_TOLERANCE, CONDOR_PROFIT_TARGET_PCT,
    CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT, SYMBOL,
)
from iron_butterfly import build_iron_butterfly, fetch_chain_snapshot, IronButterflyError
from condor_order_executor import CondorOrderExecutor, CondorPosition, mid_debit_to_close, fetch_leg_quotes

logger = logging.getLogger(__name__)
et_tz = pytz.timezone("US/Eastern")

MONITOR_POLL_SEC = 15


def should_exit(position: CondorPosition, current_debit: Optional[float],
                 now: datetime, backstop_time: dt_time) -> Optional[str]:
    """Pure decision function: "profit_target", "backstop", or None.
    now/backstop_time are both ET wall-clock -- caller handles timezone.
    Backstop is checked even when current_debit is unavailable (a
    missing quote must never block the time-based safety exit)."""
    if current_debit is not None and current_debit <= position.profit_target_debit:
        return "profit_target"
    if now.time() >= backstop_time:
        return "backstop"
    return None


def contracts_for(max_loss_per_share: float, capital: float, max_pct: float, multiplier: int = 100) -> int:
    """Defined-risk sizing: as many contracts as fit within max_pct of
    capital at the structure's OWN max loss -- not premium paid, unlike
    spy_0dte_strat's directional strategies."""
    if max_loss_per_share <= 0:
        return 0
    budget = capital * max_pct
    per_contract_risk = max_loss_per_share * multiplier
    if per_contract_risk <= 0:
        return 0
    return max(0, int(budget // per_contract_risk))


async def wait_until(hour: int, minute: int, *, _now_fn=None) -> None:
    now_fn = _now_fn or (lambda: datetime.now(et_tz))
    while True:
        now = now_fn()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            return
        await asyncio.sleep(min(30, (target - now).total_seconds()))


def _build_sized_result(chain, contracts: int):
    return build_iron_butterfly(
        chain,
        target_wing_delta=CONDOR_WING_TARGET_DELTA,
        wing_delta_tolerance=CONDOR_WING_DELTA_TOLERANCE,
        target_pct=CONDOR_PROFIT_TARGET_PCT,
        contracts=contracts,
    )


async def run_day(options_chain, executor: CondorOrderExecutor, symbol: str = SYMBOL) -> Optional[CondorPosition]:
    """One full day's cycle. Returns the closed CondorPosition, or None
    if entry never happened (sanity checks aborted it, or it sized to
    zero contracts)."""
    await wait_until(CONDOR_ENTRY_HOUR, CONDOR_ENTRY_MINUTE)

    chain = fetch_chain_snapshot(options_chain, symbol)
    try:
        prelim = _build_sized_result(chain, contracts=1)
    except IronButterflyError as e:
        logger.error("[CONDOR] Could not build a structure today: %s", e)
        return None

    contracts = contracts_for(prelim.max_loss, CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT)
    if contracts <= 0:
        logger.error(
            "[CONDOR] Sized to 0 contracts (max_loss=$%.2f/share vs capital budget) -- skipping today.",
            prelim.max_loss,
        )
        return None

    try:
        result = _build_sized_result(chain, contracts=contracts)
    except IronButterflyError as e:
        logger.error("[CONDOR] Could not build a structure today at sized contracts: %s", e)
        return None

    order_result, position = executor.submit_entry(result)
    if position is None:
        logger.error("[CONDOR] Entry failed: %s", order_result.message)
        return None
    logger.info(
        "[CONDOR] Entered %dx short $%.2f straddle | net credit $%.2f/share | max loss $%.2f/share",
        contracts, result.short_strike, position.entry_net_credit, position.max_loss,
    )

    backstop_time = dt_time(CONDOR_BACKSTOP_HOUR, CONDOR_BACKSTOP_MINUTE)
    while True:
        quotes = fetch_leg_quotes(options_chain, position)
        current_debit = mid_debit_to_close(position, quotes) if quotes else None
        now = datetime.now(et_tz)
        reason = should_exit(position, current_debit, now, backstop_time)
        if reason:
            exit_result = executor.submit_exit(position, quotes or {}, reason=reason)
            if position.status == "closed":
                logger.info(
                    "[CONDOR] Exited (%s): debit=$%.2f/share, realized P&L=$%.2f",
                    reason, position.exit_debit, position.realized_pnl,
                )
            else:
                logger.error("[CONDOR] Exit attempt (%s) did not close the position: %s", reason, exit_result.message)
            return position
        await asyncio.sleep(MONITOR_POLL_SEC)
