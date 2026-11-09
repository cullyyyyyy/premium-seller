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
import json
import logging
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytz

from dataclasses import dataclass

from config import (
    CONDOR_ENTRY_HOUR, CONDOR_ENTRY_MINUTE,
    CONDOR_BACKSTOP_HOUR, CONDOR_BACKSTOP_MINUTE,
    CONDOR_WING_TARGET_DELTA, CONDOR_PROFIT_TARGET_PCT,
    CONDOR_DEFEND_ENABLED, CONDOR_DEFEND_CONFIRM_POLLS,
    CONDOR_ROLL_SHORT_TARGET_DELTA, CONDOR_MAX_UNTESTED_ROLLS_PER_DAY,
    CONDOR_VIX_RECROSS_EXIT_ENABLED, CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT,
    CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT, SYMBOL,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE,
)
from iron_butterfly import (
    build_iron_butterfly, build_untested_side_roll, fetch_chain_snapshot, IronButterflyError,
)
from condor_order_executor import (
    CondorOrderExecutor, CondorPosition, OrderStatus, mid_debit_to_close, fetch_leg_quotes,
)
import fees as _fees
import alerts as _alerts

logger = logging.getLogger(__name__)
et_tz = pytz.timezone("US/Eastern")

MONITOR_POLL_SEC = 15
BACKSTOP_PRICE_CONCESSION = 0.05   # added to the planning debit per failed backstop retry
BACKSTOP_MAX_ATTEMPTS = 10         # bounds how far price escalation can go
POST_CLOSE_TRACK_POLL_SEC = 60     # coarser cadence -- this is for hindsight analysis, not trading

LOG_DIR = Path(__file__).parent / "logs"


def _default_intraday_log_path(_now_fn=None) -> Path:
    now_fn = _now_fn or (lambda: datetime.now(et_tz))
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"intraday_pnl_{now_fn().strftime('%Y-%m-%d')}.jsonl"


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


@dataclass(frozen=True)
class DefendStreaks:
    """How many CONSECUTIVE polls in a row each breach condition has
    held true -- the debounce behind evaluate_defend (user 2026-10-30:
    "One minute, so four polls," confirming over CONDOR_DEFEND_CONFIRM_POLLS
    rather than acting on a single flickering tick). Immutable: each poll
    produces a fresh DefendStreaks rather than mutating one in place, same
    "pure function, caller owns the loop variable" split as should_exit/
    contracts_for elsewhere in this module."""
    put_breach_streak: int = 0
    call_breach_streak: int = 0
    rolled_side_breach_streak: int = 0


def evaluate_defend(position: CondorPosition, spy_price: Optional[float], streaks: DefendStreaks,
                     *, rolled_side: Optional[str], rolls_used: int,
                     confirm_polls: int, max_rolls: int) -> Tuple[Optional[str], DefendStreaks]:
    """Pure decision function (same style as should_exit/contracts_for):
    returns ("roll_call" | "roll_put" | "close_reversal" | None, next_streaks).

    rolled_side/rolls_used are the CALLER's responsibility to track and
    pass in -- they only change on a CONFIRMED successful roll order, not
    as a pure function of price, so they don't belong in the (pure)
    DefendStreaks the way the poll-to-poll breach counters do. A roll
    attempt that fails to fill must not have already committed to "this
    position has been rolled."

    Each side's breakeven is computed from that side's OWN current short
    strike (position.short_call.strike / position.short_put.strike), not
    position.short_strike (the original ATM entry strike) -- these differ
    once a roll moves one side's short strike (the structure becomes an
    asymmetric iron condor, not a true butterfly, on whichever side
    rolled). Pre-roll the two are identical, so this is correct either way.

    Once a side has been rolled (rolled_side is not None), this ONLY
    watches that rolled side for a re-breach -- per user 2026-10-30
    ("If the SPY reverses and tests the opposite side, close it
    completely"), there is no second roll, just a full close."""
    if spy_price is None:
        return None, streaks

    put_breakeven = position.short_put.strike - position.entry_net_credit
    call_breakeven = position.short_call.strike + position.entry_net_credit

    if rolled_side is not None:
        breached = (spy_price >= call_breakeven) if rolled_side == "call" else (spy_price <= put_breakeven)
        next_streak = streaks.rolled_side_breach_streak + 1 if breached else 0
        action = "close_reversal" if next_streak >= confirm_polls else None
        return action, DefendStreaks(streaks.put_breach_streak, streaks.call_breach_streak, next_streak)

    put_next = streaks.put_breach_streak + 1 if spy_price <= put_breakeven else 0
    call_next = streaks.call_breach_streak + 1 if spy_price >= call_breakeven else 0
    next_streaks = DefendStreaks(put_next, call_next, streaks.rolled_side_breach_streak)

    if rolls_used >= max_rolls:
        return None, next_streaks
    if put_next >= confirm_polls:
        return "roll_call", next_streaks   # put side tested -> roll the untested CALL side closer
    if call_next >= confirm_polls:
        return "roll_put", next_streaks    # call side tested -> roll the untested PUT side closer
    return None, next_streaks


def _attempt_untested_roll(executor: CondorOrderExecutor, options_chain, position: CondorPosition,
                            side: str, symbol: str) -> bool:
    """Fetch a fresh chain, build the new closer spread for `side` (the
    untested side), and submit the 4-leg roll. Best-effort: a quotes
    fetch failure or an unfilled roll order is logged and alerted but
    never raises -- should_exit()'s existing profit-target/backstop
    path is the real safety net regardless of whether this succeeds,
    and a missed roll just means defending didn't happen this time, not
    that the position is unmanaged.

    The one failure that's NOT left for the backstop to catch later is
    build_untested_side_roll() refusing to build a roll at all (no
    strike clears both the delta target and the tested side's own open
    short strike -- see its opposite_strike guard, added after the
    Nov 5 2026 incident where a call-side roll landed UNDER the open
    short put). If there's no safe strike to roll to, there's no
    partial defense available either, so this closes the WHOLE position
    immediately (reason="roll_unavailable") rather than leaving a
    structure that's already breached its untested side to sit
    unrolled and unwatched until the backstop time arrives.

    Returns True if this call force-closed the position (caller must
    treat it exactly like any other exit and stop monitoring), False if
    the position is still open (rolled, or roll attempted/skipped and
    left for the next poll)."""
    try:
        chain = fetch_chain_snapshot(options_chain, symbol)
        opposite_strike = position.short_put.strike if side == "call" else position.short_call.strike
        roll = build_untested_side_roll(
            chain, side=side,
            short_target_delta=CONDOR_ROLL_SHORT_TARGET_DELTA,
            wing_target_delta=CONDOR_WING_TARGET_DELTA,
            opposite_strike=opposite_strike,
        )
    except IronButterflyError as e:
        logger.error("[CONDOR] Could not build untested-side roll (%s): %s -- closing position instead",
                      side, e)
        _alerts.notify_roll_failed(side, str(e))
        quotes = fetch_leg_quotes(options_chain, position)
        executor.submit_exit(position, quotes or {}, reason="roll_unavailable")
        if position.status == "closed":
            logger.info(
                "[CONDOR] Exited (roll_unavailable): debit=$%.2f/share, realized P&L=$%.2f",
                position.exit_debit, position.realized_pnl,
            )
            _alerts.notify_exit("roll_unavailable", position.exit_debit, position.realized_pnl,
                                position.entry_net_credit * position.multiplier * position.contracts)
            return True
        logger.error("[CONDOR] Forced close after roll_unavailable (%s) did not fill -- position still open", side)
        return False

    old_quotes = fetch_leg_quotes(options_chain, position)
    if old_quotes is None:
        logger.error("[CONDOR] Untested-side roll (%s) aborted: could not fetch current quotes on existing legs", side)
        _alerts.notify_roll_failed(side, "could not fetch current quotes on existing legs")
        return False

    result = executor.submit_untested_roll(position, roll, old_quotes)
    if result.status == OrderStatus.FILLED:
        logger.info(
            "[CONDOR] Rolled untested %s side | new short $%.2f, new wing $%.2f | "
            "roll net credit $%.2f/share | total net credit now $%.2f/share",
            side, roll.new_short_strike, roll.new_wing_strike, result.net_price, position.entry_net_credit,
        )
        _alerts.notify_roll(side, roll.new_short_strike, roll.new_wing_strike,
                             result.net_price, position.entry_net_credit)
        return False
    else:
        logger.warning("[CONDOR] Untested-side roll (%s) order did not fill: %s", side, result.message)
        _alerts.notify_roll_failed(side, result.message)
        return False


def _rest_profit_target_order(executor: CondorOrderExecutor, position: CondorPosition) -> Optional[str]:
    """Submit (or re-submit) the resting Day-limit profit-target exit
    order for `position`'s current profit_target_debit, returning its
    order_id, or None if not live or submission failed (reactive polling
    covers that case regardless -- see run_day's initial rest). Pulled
    out so a post-roll re-rest (profit_target_debit may have changed)
    uses the exact same submit+log behavior as the initial one at entry."""
    if not executor.is_live:
        return None
    resting_result = executor.submit_resting_exit(position, position.profit_target_debit, reason="profit_target")
    if resting_result.order_id is not None:
        logger.info(
            "[CONDOR] Resting profit-target order submitted | order_id=%s | target debit $%.2f/share",
            resting_result.order_id, position.profit_target_debit,
        )
        return resting_result.order_id
    logger.warning(
        "[CONDOR] Could not submit resting profit-target order (%s) -- falling back to reactive polling",
        resting_result.message,
    )
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
        target_pct=CONDOR_PROFIT_TARGET_PCT,
        contracts=contracts,
    )


def _log_intraday_snapshot(
    log_path: Path, position: CondorPosition,
    quotes: Optional[Dict[str, Tuple[float, float]]], current_debit: Optional[float],
    symbol: str, options_chain, now: datetime, vix_change_pct: Optional[float] = None,
) -> None:
    """Append one mark-to-market snapshot of the OPEN position to
    logs/intraday_pnl_<date>.jsonl -- closes the gap found 2026-10-28
    building a live status dashboard: run_day's own monitoring loop
    already polls quotes every MONITOR_POLL_SEC but never persisted what
    it saw, so there was no way to show today's P&L over time, only a
    single right-now reading pulled straight from the broker. Reuses the
    same quotes/current_debit the caller already fetched this iteration
    for should_exit -- never fetches a second time just to log.

    vix_change_pct: the same day-change figure run_day computes each poll
    for the recross-exit check (get_vix_day_change_pct) -- logged here too
    so a day's VIX path survives after the fact. Added 2026-11-06: the
    bot was computing this every poll and discarding it, so the only way
    to check what the VIX rule would have done on a past day was
    reconstructing it from a separately-pulled Yahoo Finance history
    (see the 10/30, 11/02, 11/05 analysis) instead of the bot's own data.

    Best-effort and never allowed to affect the real trading loop: any
    failure (a bad quote shape, a disk error) is caught and logged, never
    raised -- this is observability, not a decision input."""
    try:
        net_pnl = hypothetical_net_pnl(position, quotes) if quotes else None
        spy_price = options_chain.get_equity_mid_price(symbol)
        record = {
            "timestamp": now.isoformat(),
            "spy_price": spy_price,
            "debit_to_close": current_debit,
            "net_pnl_mark": net_pnl,
            "short_strike": position.short_strike,
            "contracts": position.contracts,
            "vix_change_pct": vix_change_pct,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.warning("[CONDOR] intraday snapshot logging failed -- continuing without it", exc_info=True)


async def run_day(options_chain, executor: CondorOrderExecutor, symbol: str = SYMBOL,
                   *, intraday_log_path: Optional[Path] = None) -> Optional[CondorPosition]:
    """One full day's cycle. Returns the closed CondorPosition, or None
    if entry never happened (sanity checks aborted it, or it sized to
    zero contracts).

    intraday_log_path: where each monitoring poll's mark-to-market
    snapshot is appended (see _log_intraday_snapshot). Defaults to
    logs/intraday_pnl_<today>.jsonl; tests override it to stay off the
    real filesystem."""
    await wait_until(CONDOR_ENTRY_HOUR, CONDOR_ENTRY_MINUTE)
    intraday_log_path = intraday_log_path or _default_intraday_log_path()

    # Skip entry entirely when VIX is already up too much at the entry poll --
    # the risk CONDOR_VIX_RECROSS_EXIT_ENABLED doesn't cover (that rule only
    # arms after a negative dip post-entry). 2026-10-30 (+9.24%) and
    # 2026-11-05 (+26.67%) both never dipped and rode to a backstop loss;
    # neither would have passed this filter. See config.py.
    entry_vix_change_pct = options_chain.get_vix_day_change_pct()
    if entry_vix_change_pct is not None and entry_vix_change_pct >= CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT:
        logger.info(
            "[CONDOR] Skipping entry -- VIX already up %.2f%% at entry (threshold %.2f%%)",
            entry_vix_change_pct, CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT,
        )
        _alerts.notify_no_entry(
            f"VIX already up {entry_vix_change_pct:.2f}% at entry (threshold {CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT:.2f}%)"
        )
        return None

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
        "[CONDOR] Entered %dx short $%.2f straddle | net credit $%.2f/share | max loss $%.2f/share"
        " | put wing $%.2f (side max loss $%.2f/share) | call wing $%.2f (side max loss $%.2f/share)",
        contracts, result.short_strike, position.entry_net_credit, position.max_loss,
        result.long_put.strike, result.put_side_max_loss,
        result.long_call.strike, result.call_side_max_loss,
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

    # Rest a Day limit profit-target order on the book immediately instead
    # of waiting for a quote poll to notice it already crossed the target
    # and only THEN submitting -- user 2026-10-27: "you can add a day
    # limit order to sell at whatever value is 25% profit[,] that would
    # be most efficient." Live-mode only: paper mode's simulated fills are
    # already instant (no latency to save), and there's no real order
    # book to rest anything on there.
    resting_order_id = _rest_profit_target_order(executor, position)

    defend_streaks = DefendStreaks()
    vix_dipped_negative = False  # tracks whether VIX has been seen BELOW its own
    # prior close at any poll since entry -- a later recross above that same
    # level is only the signal found live 2026-11-02 (VIX recovering off a dip)
    # if it actually dipped first; a day where VIX is already elevated at
    # entry (2026-10-30, 2026-11-05) never sets this, so vix_recross_exit
    # below can never fire for that day -- matches the backtest exactly.

    while True:
        now = datetime.now(et_tz)
        quotes = fetch_leg_quotes(options_chain, position)
        current_debit = mid_debit_to_close(position, quotes) if quotes else None
        # Fetched unconditionally (not gated behind CONDOR_VIX_RECROSS_EXIT_ENABLED)
        # so every day's VIX path gets logged regardless of whether the
        # recross-exit decision is turned on -- see _log_intraday_snapshot.
        vix_change_pct = options_chain.get_vix_day_change_pct()
        _log_intraday_snapshot(intraday_log_path, position, quotes, current_debit, symbol, options_chain, now,
                                vix_change_pct)

        vix_recross_exit = False
        if CONDOR_VIX_RECROSS_EXIT_ENABLED and vix_change_pct is not None:
            if vix_change_pct <= 0:
                vix_dipped_negative = True
            elif vix_dipped_negative:
                vix_recross_exit = True

        defend_action = None
        if CONDOR_DEFEND_ENABLED:
            spy_price = options_chain.get_equity_mid_price(symbol)
            defend_action, defend_streaks = evaluate_defend(
                position, spy_price, defend_streaks,
                rolled_side=position.rolled_side, rolls_used=position.rolls_used,
                confirm_polls=CONDOR_DEFEND_CONFIRM_POLLS, max_rolls=CONDOR_MAX_UNTESTED_ROLLS_PER_DAY,
            )
            if defend_action in ("roll_call", "roll_put"):
                # The resting profit-target order (if any) already holds a
                # closing order against all 4 current legs, including the
                # short leg this roll needs to close -- the broker rejects
                # the roll's own closing leg with cannot_close_against_more_
                # than_existing_position until that's cancelled. Found live
                # 2026-11-05: every roll attempt failed identically, forever,
                # because this cancel was missing (the backstop/close_reversal
                # path below already does this correctly).
                can_attempt_roll = True
                if resting_order_id is not None:
                    if executor.cancel_resting_exit(resting_order_id):
                        logger.info(
                            "[CONDOR] Cancelled resting profit-target order %s before untested-side roll attempt (%s)",
                            resting_order_id, defend_action,
                        )
                        resting_order_id = None
                    else:
                        logger.error(
                            "[CONDOR] Could not cancel resting profit-target order %s -- skipping roll "
                            "attempt this cycle, it would only collide with the still-open closing order",
                            resting_order_id,
                        )
                        can_attempt_roll = False
                if can_attempt_roll:
                    closed = _attempt_untested_roll(executor, options_chain, position,
                                                     "call" if defend_action == "roll_call" else "put", symbol)
                    if closed:
                        return position
                    resting_order_id = _rest_profit_target_order(executor, position)

        if resting_order_id is not None:
            if executor.poll_resting_exit(position, resting_order_id, reason="profit_target"):
                logger.info(
                    "[CONDOR] Exited (profit_target, resting order): debit=$%.2f/share, realized P&L=$%.2f",
                    position.exit_debit, position.realized_pnl,
                )
                _alerts.notify_exit("profit_target", position.exit_debit, position.realized_pnl,
                                    position.entry_net_credit * position.multiplier * position.contracts)
                return position
            defend_forces_exit = defend_action == "close_reversal"
            if now.time() >= backstop_time or defend_forces_exit or vix_recross_exit:
                executor.cancel_resting_exit(resting_order_id)
                logger.warning(
                    "[CONDOR] %s -- cancelled resting profit-target order %s, switching to %s exit",
                    "Defend reversal" if defend_forces_exit else ("VIX recrossed flat" if vix_recross_exit else "Backstop time reached"),
                    resting_order_id,
                    "defend_reversal" if defend_forces_exit else ("vix_recross" if vix_recross_exit else "backstop"),
                )
                resting_order_id = None
                # No `continue` -- fall straight into the exit-reason check
                # below this same iteration, not waiting another full
                # MONITOR_POLL_SEC just to notice the cancel happened.
            else:
                await asyncio.sleep(MONITOR_POLL_SEC)
                continue

        reason = should_exit(position, current_debit, now, backstop_time) or \
            ("defend_reversal" if defend_action == "close_reversal" else None) or \
            ("vix_recross" if vix_recross_exit else None)
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
            _alerts.notify_exit(reason, position.exit_debit, position.realized_pnl,
                                position.entry_net_credit * position.multiplier * position.contracts)
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


def hypothetical_net_pnl(position: CondorPosition, quotes) -> float:
    """What the net P&L WOULD be right now if this (already-closed)
    position were still open and closed at these quotes -- pure
    hindsight data. Never mutates the position; position.realized_pnl
    stays the one real, already-recorded result."""
    debit = mid_debit_to_close(position, quotes)
    gross = (position.entry_net_credit - debit) * position.multiplier * position.contracts
    return gross - position.round_trip_fees


async def track_post_close(
    options_chain, position: CondorPosition, symbol: str = SYMBOL,
    *,
    until_hour: int = MARKET_CLOSE_HOUR, until_minute: int = MARKET_CLOSE_MINUTE,
    poll_sec: float = POST_CLOSE_TRACK_POLL_SEC,
    log_path: Optional[Path] = None,
    _now_fn=None,
) -> List[dict]:
    """After a position closes (profit target or backstop), keep
    recording what it WOULD have been worth had it stayed open, through
    the rest of the trading day -- pure hindsight evaluation, never
    feeds back into any live decision (should_exit/submit_exit are
    never called here). Built 2026-10-27 on request ("check pricing
    after 2:30 cutoff to evaluate P/L in the future").

    Appends one JSON line per poll to log_path (default:
    logs/post_close_eval_<today>.jsonl) and also returns the same
    records in memory, so this is testable without touching the
    filesystem (pass log_path=None and a tmp_path in tests, or just
    inspect the return value).

    No-op (returns []) if the position isn't actually closed yet --
    this is explicitly post-close tracking, not a replacement for the
    real monitoring loop in run_day()."""
    if position.status != "closed":
        logger.warning(
            "[CONDOR] track_post_close called on a position that isn't closed (status=%s) -- skipping.",
            position.status,
        )
        return []

    now_fn = _now_fn or (lambda: datetime.now(et_tz))
    until_time = dt_time(until_hour, until_minute)

    if log_path is None:
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"post_close_eval_{now_fn().strftime('%Y-%m-%d')}.jsonl"

    records: List[dict] = []
    while True:
        now = now_fn()
        if now.time() >= until_time:
            break
        quotes = fetch_leg_quotes(options_chain, position)
        if quotes is not None:
            spy_price = options_chain.get_equity_mid_price(symbol)
            record = {
                "timestamp": now.isoformat(),
                "spy_price": spy_price,
                "hypothetical_debit_to_close": mid_debit_to_close(position, quotes),
                "hypothetical_net_pnl_if_held": hypothetical_net_pnl(position, quotes),
                "actual_realized_pnl": position.realized_pnl,
                "actual_exit_reason": position.exit_reason,
                "actual_exit_time": position.exit_time.isoformat() if position.exit_time else None,
            }
            records.append(record)
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        else:
            logger.warning("[CONDOR] track_post_close: no quotes available at %s -- skipping this poll", now)
        await asyncio.sleep(poll_sec)

    logger.info("[CONDOR] Post-close tracking finished (%d records) -> %s", len(records), log_path)
    return records
