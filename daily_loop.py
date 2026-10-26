"""
daily_loop.py -- ties iron_butterfly.py (construction) and
condor_order_executor.py (submission) into the once-a-day entry/exit
cycle: wait for the entry window, build+submit the butterfly, monitor
live quotes for the profit-target exit, force-close at the backstop
time otherwise.

should_exit() and contracts_for() are the real decision logic and are
fully unit-tested as pure functions (test_daily_loop.py). run_day()
itself HAS been run live against the real sandbox+production APIs
(2026-10-26/27) -- entry, monitoring, the backstop-retry escalation,
and a real fee-aware sizing all confirmed working, including catching
and fixing a real fill-vs-cancel race and a real sign-convention bug
along the way (see condor_order_executor.py's history).

Alerting (alerts.py) is wired into every real decision point as of
2026-10-27: entry success/failure, every exit, and critically the
backstop-exhausted-retries case (previously a silent gap -- a position
could be left open with nobody told). What's still NOT covered: a
crash in this process itself (not a failed order, the whole run_day()
coroutine dying) still has no recovery or alert -- see run_trade_day.py
for the top-level try/except that at least turns a crash into one
alert, which is the closest thing to a mitigation that exists yet.
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
import fees as _fees
import alerts as _alerts

logger = logging.getLogger(__name__)
et_tz = pytz.timezone("US/Eastern")

MONITOR_POLL_SEC = 15
BACKSTOP_PRICE_CONCESSION = 0.05   # added to the planning debit per failed backstop retry
BACKSTOP_MAX_ATTEMPTS = 10         # bounds how far price escalation can go


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
    capital at the structure's OWN max loss, NET of real round-trip
    fees (fees.py) -- not premium paid, unlike spy_0dte_strat's
    directional strategies, and not gross-only (closed 2026-10-26,
    user: "yes close gap" -- the sizing side of the same fee-netting
    fix the gain/loss targets already got).

    Fees depend on contracts (the $10/leg commission cap, plus linear
    clearing/regulatory), so there's no closed-form N -- this starts
    from the gross-only estimate (a safe upper bound: fees only ever
    make the real per-contract cost >= the gross one) and decrements
    until the true NET total (gross risk + round_trip_fees(N)) actually
    fits the budget."""
    if max_loss_per_share <= 0:
        return 0
    budget = capital * max_pct
    gross_per_contract_risk = max_loss_per_share * multiplier
    if gross_per_contract_risk <= 0:
        return 0
    n = max(0, int(budget // gross_per_contract_risk))
    while n > 0 and (n * gross_per_contract_risk + _fees.round_trip_fees(n)) > budget:
        n -= 1
    return n


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
        _alerts.notify_no_entry(f"could not build a structure: {e}")
        return None

    contracts = contracts_for(prelim.max_loss, CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT)
    if contracts <= 0:
        logger.error(
            "[CONDOR] Sized to 0 contracts (max_loss=$%.2f/share vs capital budget) -- skipping today.",
            prelim.max_loss,
        )
        _alerts.notify_no_entry(f"sized to 0 contracts (max_loss=${prelim.max_loss:.2f}/share vs budget)")
        return None

    try:
        result = _build_sized_result(chain, contracts=contracts)
    except IronButterflyError as e:
        logger.error("[CONDOR] Could not build a structure today at sized contracts: %s", e)
        _alerts.notify_no_entry(f"could not build at sized contracts: {e}")
        return None

    order_result, position = executor.submit_entry(result)
    if position is None:
        logger.error("[CONDOR] Entry failed: %s", order_result.message)
        _alerts.notify_no_entry(f"entry order did not fill: {order_result.message}")
        return None
    logger.info(
        "[CONDOR] Entered %dx short $%.2f straddle | net credit $%.2f/share | max loss $%.2f/share",
        contracts, result.short_strike, position.entry_net_credit, position.max_loss,
    )
    _alerts.notify_entry(result.short_strike, contracts, position.entry_net_credit,
                          position.max_loss * position.multiplier * contracts)

    backstop_time = dt_time(CONDOR_BACKSTOP_HOUR, CONDOR_BACKSTOP_MINUTE)
    backstop_attempts = 0
    # Worst-case sane ceiling on how much to pay to close: the debit implied
    # by the structure's OWN defined max_loss (max_loss = wing_width -
    # net_credit, so wing_width == net_credit + max_loss) -- never escalate
    # past what the position could lose anyway by just expiring at the wing.
    backstop_price_ceiling = position.entry_net_credit + position.max_loss

    while True:
        quotes = fetch_leg_quotes(options_chain, position)
        current_debit = mid_debit_to_close(position, quotes) if quotes else None
        now = datetime.now(et_tz)
        reason = should_exit(position, current_debit, now, backstop_time)
        if not reason:
            await asyncio.sleep(MONITOR_POLL_SEC)
            continue

        price_override = None
        if reason == "backstop" and backstop_attempts > 0:
            # A hard safety exit that gives up after one unfilled attempt
            # isn't actually a safety exit -- found live 2026-10-26 (the
            # exit didn't cross at the mid-based price within
            # ORDER_FILL_TIMEOUT_SEC and run_day returned with the position
            # still open). Escalate price aggression on each retry instead.
            # `current_debit if ... is not None` (not `or`) -- a real debit
            # of exactly 0.0 is falsy but legitimate, "or" would wrongly
            # fall back to the ceiling for it.
            base_debit = current_debit if current_debit is not None else backstop_price_ceiling
            concession = min(BACKSTOP_PRICE_CONCESSION * backstop_attempts,
                              max(0.0, backstop_price_ceiling - base_debit))
            price_override = base_debit + concession

        exit_result = executor.submit_exit(position, quotes or {}, reason=reason, price_override=price_override)

        if position.status == "closed":
            logger.info(
                "[CONDOR] Exited (%s): debit=$%.2f/share, realized P&L=$%.2f",
                reason, position.exit_debit, position.realized_pnl,
            )
            _alerts.notify_exit(reason, position.exit_debit, position.realized_pnl)
            return position

        if reason != "backstop":
            # No urgency to force a profit-target fill -- just keep
            # monitoring; the next poll recomputes a fresh planning price.
            logger.warning("[CONDOR] Exit attempt (%s) did not fill: %s -- will retry next poll",
                            reason, exit_result.message)
            await asyncio.sleep(MONITOR_POLL_SEC)
            continue

        backstop_attempts += 1
        if backstop_attempts >= BACKSTOP_MAX_ATTEMPTS:
            logger.error(
                "[CONDOR] Backstop exit FAILED to close after %d attempts (last price_override=$%.2f) -- "
                "position is still OPEN -- manual intervention needed.",
                backstop_attempts, price_override or 0.0,
            )
            _alerts.notify_backstop_failure(backstop_attempts, price_override or 0.0)
            return position
        logger.error("[CONDOR] Backstop exit attempt %d did not fill: %s -- retrying immediately with more aggression",
                      backstop_attempts, exit_result.message)
        # No sleep here -- a hard safety exit retries as fast as possible,
        # not on the normal monitoring cadence.
