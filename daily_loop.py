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
    CONDOR_DEFEND_ENABLED, CONDOR_DEFEND_CONFIRM_POLLS, CONDOR_DEFEND_CAPTURE_PCT,
    CONDOR_VIX_RECROSS_EXIT_ENABLED, CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT,
    CONDOR_VIX_RECROSS_CONFIRM_POLLS,
    CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT, SYMBOL,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE,
)
from iron_butterfly import (
    build_iron_butterfly, fetch_chain_snapshot, IronButterflyError,
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


def evaluate_defend(position: CondorPosition, spy_price: Optional[float], streaks: DefendStreaks,
                     *, confirm_polls: int, capture_threshold: float,
                     call_capture_pct: Optional[float], put_capture_pct: Optional[float],
                     ) -> Tuple[Optional[str], DefendStreaks]:
    """Pure decision function (same style as should_exit/contracts_for):
    returns ("flatten_call" | "flatten_put" | None, next_streaks).

    On a confirmed breach of one side (put_next/call_next >= confirm_polls,
    same debounce as before), the UNTESTED side is flattened -- closed on
    its own, 2 legs -- but ONLY if it has already captured >= capture_threshold
    of its own planning-time credit (call_capture_pct/put_capture_pct,
    computed by the caller from a live quote -- this stays a pure function
    and doesn't fetch anything itself). Otherwise this returns None and
    the position just rides as a whole on the ordinary should_exit path,
    same as if CONDOR_DEFEND_ENABLED were off.

    This replaces rolling the untested side closer (retired 2026-11-12):
    confirmed against two real trades (2026-11-11, 2026-11-12) that on a
    shared-strike iron butterfly, the roll's own anti-crossing guard left
    no safe strike to roll to once breakeven was actually breached, so it
    always degenerated into closing the whole position anyway. Flattening
    only the side that's already near its own max profit, and leaving the
    tested side open on its own betting on a reversion before backstop,
    is the adjustment that actually has room to work on this structure
    (user 2026-11-12: "you close the position that's like at 100% profit
    and hope the other one mean reverts").

    Each side's breakeven is computed from position.short_call.strike/
    position.short_put.strike directly, so this still reads correctly
    even if called again later for whatever reason."""
    if spy_price is None:
        return None, streaks

    put_breakeven = position.short_put.strike - position.entry_net_credit
    call_breakeven = position.short_call.strike + position.entry_net_credit

    put_next = streaks.put_breach_streak + 1 if spy_price <= put_breakeven else 0
    call_next = streaks.call_breach_streak + 1 if spy_price >= call_breakeven else 0
    next_streaks = DefendStreaks(put_next, call_next)

    if put_next >= confirm_polls and call_capture_pct is not None and call_capture_pct >= capture_threshold:
        return "flatten_call", next_streaks   # put side tested -> flatten the untested CALL side
    if call_next >= confirm_polls and put_capture_pct is not None and put_capture_pct >= capture_threshold:
        return "flatten_put", next_streaks    # call side tested -> flatten the untested PUT side
    return None, next_streaks


def _side_capture_pct(position: CondorPosition, side: str,
                       quotes: Optional[Dict[str, Tuple[float, float]]]) -> Optional[float]:
    """How much of `side`'s OWN planning-time credit (call_side_entry_credit/
    put_side_entry_credit) has already been captured, as a fraction --
    1.0 means it could be closed for zero debit right now, 0.0 means it's
    worth exactly what it was sold for. None if there's no quote to
    evaluate it with. Only meaningful pre-flatten, while both sides'
    legs/quotes are still available (position.legs/fetch_leg_quotes both
    narrow to one side once flattened_side is set)."""
    short_leg = position.short_call if side == "call" else position.short_put
    wing_leg = position.long_call if side == "call" else position.long_put
    entry_credit = position.call_side_entry_credit if side == "call" else position.put_side_entry_credit
    if quotes is None or entry_credit <= 0:
        return None
    if short_leg.symbol not in quotes or wing_leg.symbol not in quotes:
        return None
    short_bid, short_ask = quotes[short_leg.symbol]
    wing_bid, wing_ask = quotes[wing_leg.symbol]
    mid_debit = (short_bid + short_ask) / 2.0 - (wing_bid + wing_ask) / 2.0
    return (entry_credit - mid_debit) / entry_credit


def _attempt_flatten(executor: CondorOrderExecutor, options_chain, position: CondorPosition,
                      side: str, symbol: str) -> None:
    """Fetch a fresh quote and close just `side`'s 2 legs. Best-effort:
    a quotes fetch failure or an unfilled flatten order is logged and
    alerted but never raises -- should_exit()'s existing profit-target/
    backstop path is the real safety net regardless of whether this
    succeeds, and a missed flatten just means it'll be retried (or
    re-evaluated) next poll, not that the position is unmanaged. Unlike
    the old roll attempt, there's no "no valid strike" failure mode here
    to force a full close over -- closing 2 already-identified legs at
    whatever they're quoted at always succeeds or just doesn't fill yet."""
    quotes = fetch_leg_quotes(options_chain, position)
    if quotes is None:
        logger.error("[CONDOR] Flatten (%s) aborted: could not fetch current quotes on existing legs", side)
        _alerts.notify_flatten_failed(side, "could not fetch current quotes on existing legs")
        return

    result = executor.close_vertical(position, side, quotes)
    if result.status == OrderStatus.FILLED:
        logger.info(
            "[CONDOR] Flattened untested %s side | debit=$%.2f/share | banked P&L=$%.2f | "
            "remaining side's new profit target $%.2f/share",
            side, position.flatten_debit, position.flatten_pnl, position.profit_target_debit,
        )
        _alerts.notify_flatten(side, position.flatten_debit, position.flatten_pnl)
    else:
        logger.warning("[CONDOR] Flatten (%s) order did not fill: %s", side, result.message)
        _alerts.notify_flatten_failed(side, result.message)


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
    # Logged unconditionally -- previously this was only ever visible in a
    # log line when it triggered the skip below; on a normal entry it was
    # computed and then silently discarded, so there was no record of where
    # VIX actually stood at entry to check against later (see 2026-11-10:
    # asked whether VIX was still up ~5% at entry, had no logged number to
    # check it against).
    logger.info(
        "[CONDOR] VIX at entry poll: %s",
        f"{entry_vix_change_pct:.2f}% vs prior close" if entry_vix_change_pct is not None else "unavailable",
    )
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
    vix_dipped_negative = False  # tracks whether VIX has been CONFIRMED below its
    # own prior close at some poll since entry -- a later confirmed recross above
    # that same level is only the signal found live 2026-11-02 (VIX recovering off
    # a dip) if it actually dipped first; a day where VIX is already elevated at
    # entry (2026-10-30, 2026-11-05) never sets this, so vix_recross_exit
    # below can never fire for that day -- matches the backtest exactly.
    #
    # "Confirmed" means CONDOR_VIX_RECROSS_CONFIRM_POLLS consecutive polls on
    # one side of zero, not a single poll -- found live 2026-11-10: vix_change_pct
    # wobbled -0.10%/+0.10%/-0.35% around zero for a few polls right after entry
    # (VIX's own print is noisy right after the open) and the un-debounced
    # single-poll sign check force-closed the position on that noise, not a
    # real dip-and-recover, for a -$5.96 loss.
    vix_negative_poll_streak = 0
    vix_positive_poll_streak_since_dip = 0

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
                vix_negative_poll_streak += 1
                vix_positive_poll_streak_since_dip = 0
                if vix_negative_poll_streak >= CONDOR_VIX_RECROSS_CONFIRM_POLLS:
                    vix_dipped_negative = True
            else:
                vix_negative_poll_streak = 0
                if vix_dipped_negative:
                    vix_positive_poll_streak_since_dip += 1
                    if (vix_positive_poll_streak_since_dip >= CONDOR_VIX_RECROSS_CONFIRM_POLLS
                            and current_debit is not None
                            and current_debit < position.entry_net_credit):
                        # Gate added 2026-11-18: the recross itself is only ever
                        # the SIGNAL this was built around (2026-11-02, position
                        # already +$165 when it fired, protecting that gain from
                        # a backstop-time loss) -- it was never meant to force an
                        # exit on its own. Live 2026-11-18 it fired at -$204 on
                        # the mark (SPY had just dropped toward the put wing) and
                        # locked in a -$199.96 loss; post-close hindsight showed
                        # SPY reverted and the position would have been +$100 to
                        # +$128 for most of the rest of the session. Requiring
                        # current_debit < entry_net_credit (i.e. closing now is
                        # actually a gain) keeps the exit doing only what it was
                        # designed to do. current_debit is None whenever the quote
                        # fetch fails that poll -- no data means no forced exit;
                        # the backstop is still the real safety net either way.
                        vix_recross_exit = True

        if CONDOR_DEFEND_ENABLED and position.flattened_side is None:
            spy_price = options_chain.get_equity_mid_price(symbol)
            call_capture_pct = _side_capture_pct(position, "call", quotes)
            put_capture_pct = _side_capture_pct(position, "put", quotes)
            defend_action, defend_streaks = evaluate_defend(
                position, spy_price, defend_streaks,
                confirm_polls=CONDOR_DEFEND_CONFIRM_POLLS, capture_threshold=CONDOR_DEFEND_CAPTURE_PCT,
                call_capture_pct=call_capture_pct, put_capture_pct=put_capture_pct,
            )
            if defend_action in ("flatten_call", "flatten_put"):
                # The resting profit-target order (if any) already holds a
                # closing order against all 4 current legs, including the
                # 2 legs this flatten needs to close -- the broker rejects
                # the flatten's own closing legs with cannot_close_against_
                # more_than_existing_position until that's cancelled. Same
                # issue the old roll attempt hit (found live 2026-11-05).
                side = "call" if defend_action == "flatten_call" else "put"
                can_attempt_flatten = True
                if resting_order_id is not None:
                    if executor.cancel_resting_exit(resting_order_id):
                        logger.info(
                            "[CONDOR] Cancelled resting profit-target order %s before flattening untested %s side",
                            resting_order_id, side,
                        )
                        resting_order_id = None
                    else:
                        logger.error(
                            "[CONDOR] Could not cancel resting profit-target order %s -- skipping flatten "
                            "attempt this cycle, it would only collide with the still-open closing order",
                            resting_order_id,
                        )
                        can_attempt_flatten = False
                if can_attempt_flatten:
                    _attempt_flatten(executor, options_chain, position, side, symbol)
                    if position.flattened_side is not None:
                        # entry_net_credit/max_loss just shrank to the remaining
                        # side alone (see CondorOrderExecutor._finalize_flatten) --
                        # the backstop price ceiling has to shrink with it.
                        backstop_price_ceiling = position.entry_net_credit + position.max_loss
                    resting_order_id = _rest_profit_target_order(executor, position)

        if resting_order_id is not None:
            if executor.poll_resting_exit(position, resting_order_id, reason="profit_target"):
                logger.info(
                    "[CONDOR] Exited (profit_target, resting order): debit=$%.2f/share, realized P&L=$%.2f (total incl. flatten: $%.2f)",
                    position.exit_debit, position.realized_pnl, _total_realized_pnl(position),
                )
                _alerts.notify_exit("profit_target", position.exit_debit, _total_realized_pnl(position),
                                    position.entry_net_credit * position.multiplier * position.contracts)
                return position
            if now.time() >= backstop_time or vix_recross_exit:
                executor.cancel_resting_exit(resting_order_id)
                logger.warning(
                    "[CONDOR] %s -- cancelled resting profit-target order %s, switching to %s exit",
                    "VIX recrossed flat" if vix_recross_exit else "Backstop time reached",
                    resting_order_id,
                    "vix_recross" if vix_recross_exit else "backstop",
                )
                resting_order_id = None
                # No `continue` -- fall straight into the exit-reason check
                # below this same iteration, not waiting another full
                # MONITOR_POLL_SEC just to notice the cancel happened.
            else:
                await asyncio.sleep(MONITOR_POLL_SEC)
                continue

        reason = should_exit(position, current_debit, now, backstop_time) or \
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
                "[CONDOR] Exited (%s): debit=$%.2f/share, realized P&L=$%.2f (total incl. flatten: $%.2f)",
                reason, position.exit_debit, position.realized_pnl, _total_realized_pnl(position),
            )
            _alerts.notify_exit(reason, position.exit_debit, _total_realized_pnl(position),
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
    """What the net P&L WOULD be right now if whatever's still open on
    this position were closed at these quotes -- pure hindsight/mark
    data, used both for live monitoring (_log_intraday_snapshot) and
    post-close hindsight tracking. Never mutates the position;
    position.realized_pnl stays the one real, already-recorded result.

    Includes position.flatten_pnl (already banked, if one side has
    been flattened) PLUS the mark on whatever's left open --
    entry_net_credit/round_trip_fees/legs have already narrowed to just
    that remaining side by this point (see CondorOrderExecutor._finalize_flatten),
    so this adds, never double-counts, the flattened side's result."""
    debit = mid_debit_to_close(position, quotes)
    gross = (position.entry_net_credit - debit) * position.multiplier * position.contracts
    open_side_pnl = gross - position.round_trip_fees
    return open_side_pnl + (position.flatten_pnl or 0.0)


def _total_realized_pnl(position: CondorPosition) -> float:
    """The real, final day total once the position is fully closed:
    whatever was banked by flattening (flatten_pnl, or 0 if that never
    happened) plus the final close's own realized_pnl."""
    return (position.realized_pnl or 0.0) + (position.flatten_pnl or 0.0)


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
