"""
manual_close.py -- closes whatever iron butterfly is open on SYMBOL
right now, reconstructed straight from the broker's OWN /positions
records -- NOT from any in-memory daily_loop.py state, because there is
none outside the life of run_trade_day.py's own process. Built
2026-10-28 for slack_commands.py's "close" command, per explicit user
scope decision that day: unlike covered_call_weekly's slack bot (status
+ safe triggers only, structurally barred from any trading function),
this project's bot gets exactly ONE trading action -- a manual override
close -- deliberately, not by accident.

SAFETY NOTES:
  - refuses to act if there's no open SYMBOL position (never invents one)
  - refuses to act if the open legs don't resolve to a clean 4-leg
    short_call/short_put/long_call/long_put iron butterfly shape (two
    shorts at the same strike, two longs at different strikes, one
    call/one put each side, matching quantities) -- never guesses at a
    structure it can't verify
  - escalates price on a miss the same way daily_loop's backstop retry
    does (same BACKSTOP_PRICE_CONCESSION per attempt, bounded), so a
    manual close has a real chance of actually filling, not just one
    shot at the mid
  - never retries silently forever -- always returns exactly what
    happened, filled or not

KNOWN RACE, accepted rather than engineered around: if the scheduled
run_trade_day.py process is ALSO trying to exit the same position at
the moment this runs (e.g. it just hit the profit target or backstop
the instant before), both processes could submit a closing order for
the same legs. The broker-side failure mode is a clean rejection (there's
nothing left to Buy/Sell to Close once the other order fills first), not
a double-fill or a corrupted book -- acceptable for a single-position,
single-account bot at this scale. Not solved with distributed locking;
just documented.
"""
import logging
from datetime import datetime
from typing import List

import fees as _fees
from config import SYMBOL
from condor_order_executor import (
    CondorOrderExecutor, CondorPosition, CondorLegRecord,
    fetch_leg_quotes, mid_debit_to_close,
)
from daily_loop import BACKSTOP_PRICE_CONCESSION

logger = logging.getLogger(__name__)

MANUAL_CLOSE_MAX_ATTEMPTS = 5  # fewer than daily_loop's backstop (10) -- this is a one-off manual
                                # action in a Slack response, not an unattended day-long safety net


def _parse_occ_symbol(symbol: str) -> tuple:
    """(option_type, strike) from a standard 21-char OCC option symbol,
    e.g. "SPY   261028C00770000" -- 6-char root (space-padded), 6-digit
    date, C/P, 8-digit strike (implied 3 decimals). Confirmed against
    real symbols in this project's own order/position logs (2026-10-27/28),
    not just the spec."""
    tail = symbol[6:]
    option_type = "call" if tail[6] == "C" else "put"
    strike = int(tail[7:]) / 1000.0
    return option_type, strike


def reconstruct_open_position(positions: List[dict]) -> CondorPosition:
    """positions: raw items from check_position.fetch_open_option_positions,
    already filtered to SYMBOL's open option legs. Raises ValueError (never
    guesses) if they don't resolve to exactly one clean iron butterfly."""
    roles = {}
    for p in positions:
        option_type, _ = _parse_occ_symbol(p["symbol"])
        key = (p["quantity-direction"], option_type)
        if key in roles:
            raise ValueError(
                f"more than one {key[0]} {key[1]} leg open -- not a clean iron butterfly, refusing to touch it"
            )
        roles[key] = p

    required = {
        ("Short", "call"): "short_call", ("Short", "put"): "short_put",
        ("Long", "call"): "long_call", ("Long", "put"): "long_put",
    }
    missing = [label for key, label in required.items() if key not in roles]
    if missing:
        raise ValueError(
            f"open {SYMBOL} legs don't resolve to a clean 4-leg iron butterfly -- missing {missing}, refusing to touch it"
        )

    contracts_seen = {int(p["quantity"]) for p in roles.values()}
    if len(contracts_seen) != 1:
        raise ValueError(f"legs have mismatched quantities {contracts_seen} -- refusing to touch it")
    contracts = contracts_seen.pop()

    def leg_record(key: tuple) -> CondorLegRecord:
        p = roles[key]
        _, strike = _parse_occ_symbol(p["symbol"])
        action = "Sell to Open" if key[0] == "Short" else "Buy to Open"
        return CondorLegRecord(symbol=p["symbol"], strike=strike, entry_action=action)

    short_call_p = roles[("Short", "call")]
    short_put_p = roles[("Short", "put")]
    long_call_p = roles[("Long", "call")]
    long_put_p = roles[("Long", "put")]

    entry_net_credit = (
        float(short_call_p["average-open-price"]) + float(short_put_p["average-open-price"])
        - float(long_call_p["average-open-price"]) - float(long_put_p["average-open-price"])
    )
    multiplier = int(float(short_call_p["multiplier"]))
    _, short_strike = _parse_occ_symbol(short_call_p["symbol"])

    return CondorPosition(
        short_call=leg_record(("Short", "call")),
        short_put=leg_record(("Short", "put")),
        long_call=leg_record(("Long", "call")),
        long_put=leg_record(("Long", "put")),
        short_strike=short_strike,
        contracts=contracts,
        multiplier=multiplier,
        entry_net_credit=entry_net_credit,
        max_loss=0.0,              # unused by submit_exit/_finalize_exit -- only daily_loop's own
        profit_target_debit=0.0,   # run_day() loop reads these, and this function is never called by it
        target_pct=0.0,            # only read by an untested-side roll, which never happens on a
                                    # manual close -- same "unused" placeholder as the two fields above
        round_trip_fees=_fees.round_trip_fees(contracts),
        entry_time=datetime.now(),  # approximate -- not read by anything this function calls
    )


def close_open_position_now(executor: CondorOrderExecutor, options_chain, positions: List[dict],
                             *, reason: str = "manual") -> dict:
    """Does the real work once the caller (slack_commands.py) has already
    logged in and fetched `positions` (check_position.fetch_open_option_positions)
    -- kept out of this function so it stays unit-testable with plain
    mocks, same split check_position.py and daily_loop.py already use.

    Returns {"ok": bool, "message": str} -- never raises; every failure
    path is a clean, explained ok=False."""
    if not positions:
        return {"ok": False, "message": f"No open {SYMBOL} position right now -- nothing to close."}

    try:
        position = reconstruct_open_position(positions)
    except ValueError as e:
        return {"ok": False, "message": f"Refusing to close: {e}"}

    if not executor.is_live:
        return {"ok": False, "message": (
            "EXECUTE_ORDERS is False on this process -- a close here would only paper-simulate, "
            "not touch the real open position. Refusing to report a fake result."
        )}

    quotes = fetch_leg_quotes(options_chain, position)
    if not quotes:
        return {"ok": False, "message": "Could not get a live quote for all 4 legs -- refusing to submit blind."}

    # Ceiling matches daily_loop's backstop_price_ceiling concept: never pay
    # more to close than the position could lose anyway by expiring at the
    # wing (wing_width == the farther of the two long strikes from the short).
    price_ceiling = max(
        position.long_call.strike - position.short_strike,
        position.short_strike - position.long_put.strike,
    )

    base_debit = mid_debit_to_close(position, quotes)
    exit_result = executor.submit_exit(position, quotes, reason=reason)
    attempts = 1
    while position.status != "closed" and attempts < MANUAL_CLOSE_MAX_ATTEMPTS:
        concession = min(BACKSTOP_PRICE_CONCESSION * attempts, max(0.0, price_ceiling - base_debit))
        price_override = base_debit + concession
        logger.warning("[MANUAL CLOSE] attempt %d did not fill: %s -- retrying at $%.2f",
                        attempts, exit_result.message, price_override)
        exit_result = executor.submit_exit(position, quotes, reason=reason, price_override=price_override)
        attempts += 1

    if position.status == "closed":
        logger.info("[MANUAL CLOSE] Closed %dx short $%.2f straddle after %d attempt(s): "
                    "debit=$%.2f/share, realized P&L=$%.2f",
                    position.contracts, position.short_strike, attempts,
                    position.exit_debit, position.realized_pnl)
        return {"ok": True, "message": (
            f"Closed {position.contracts}x short ${position.short_strike:.2f} straddle "
            f"({attempts} attempt(s)) -- debit ${position.exit_debit:.2f}/share, "
            f"realized P&L ${position.realized_pnl:+.2f}"
        )}

    logger.error("[MANUAL CLOSE] FAILED to close after %d attempts (last: %s) -- position still OPEN",
                 attempts, exit_result.message)
    return {"ok": False, "message": (
        f"Tried {attempts} time(s), still not filled (last attempt: {exit_result.message}) -- "
        f"the position is STILL OPEN. Check the broker directly."
    )}
