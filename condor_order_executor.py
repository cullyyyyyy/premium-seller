"""
condor_order_executor.py -- submits and monitors the 4-leg iron
butterfly order iron_butterfly.py builds.

Paper mode (EXECUTE_ORDERS=False, the default) simulates an instant
marketable fill -- bid for each leg you're selling, ask for each leg
you're buying -- and never posts anything real, same convention as
spy_0dte_strat's order_executor.py (_simulate_buy_fill/_simulate_sell_fill).
Live mode posts one real multi-leg order (dry-run first if asked),
with the retry/backoff/401-refresh logic ported near-verbatim from
spy_0dte_strat's order_executor.py -- that plumbing is generic HTTP
handling, not single-leg-specific.

The net fill-price/quantity extraction (_extract_net_fill) was wrong
twice against real fills before being fixed 2026-10-26 (wrong quantity
key, missing string->number casts) -- see test_condor_order_executor.py
and project memory for what was actually seen in a filled 4-leg order's
JSON.

Entry now DOES limit-chase (added 2026-10-27, after a real scheduled
run that morning submitted one Day order at the net-credit mid, got no
fill in ORDER_FILL_TIMEOUT_SEC, and gave up): CondorOrderExecutor._chase_entry
submits a sequence of IOC orders, stepping the net credit DOWN by
ENTRY_CHASE_STEP per miss, same shape as spy_0dte_strat's single-leg
_limit_chase but one 4-leg combo order per step and walking the
opposite direction (giving up credit, not paying more). Confirmed live
that IOC is accepted on a 4-leg combo order and self-cancels cleanly
with zero fills on any leg when non-marketable. Exit still uses one Day
limit + the backstop's escalating-price retry (daily_loop.py) -- not
yet converted to the same IOC-chase shape as entry.
"""
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import requests

from config import (
    BASE_URL, ACCOUNT_NUMBER, EXECUTE_ORDERS,
    ORDER_POST_MAX_RETRIES, ORDER_POST_BACKOFF_BASE_SEC, ORDER_POST_BACKOFF_MAX_SEC,
    ORDER_FILL_TIMEOUT_SEC,
    ENTRY_CHASE_STEP, ENTRY_CHASE_SETTLE_TIMEOUT_SEC, ENTRY_CHASE_SETTLE_POLL_SEC,
    ENTRY_CHASE_MAX_WALK_TICKS, ENTRY_CHASE_ABORT_MISS_STREAK,
)
from iron_butterfly import IronButterflyResult, UntestedSideRoll, recompute_profit_target_debit

logger = logging.getLogger(__name__)

_REVERSE_ACTION = {
    "Sell to Open": "Buy to Close",
    "Buy to Open": "Sell to Close",
}


def new_external_id() -> str:
    """Same purpose as spy_0dte_strat's new_external_id(): one unique
    tracking ID per distinct order intent, resubmitted unchanged across
    every retry of THAT submission so an ambiguous failure can never
    create a duplicate order."""
    return f"premiumseller-{uuid.uuid4().hex[:24]}"


class OrderStatus(Enum):
    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class OrderResult:
    status: OrderStatus
    order_id: Optional[str] = None
    net_price: Optional[float] = None   # per share; credit positive on entry, debit positive on exit
    filled_quantity: int = 0
    message: str = ""


@dataclass
class CondorLegRecord:
    """One leg's symbol + the action it was ENTERED with -- exit always
    mirrors this (Sell to Open -> Buy to Close, Buy to Open -> Sell to
    Close), so the position doesn't need to remember a whole
    IronButterflyResult to be closed out later."""
    symbol: str
    strike: float
    entry_action: str


@dataclass
class CondorPosition:
    short_call: CondorLegRecord
    short_put: CondorLegRecord
    long_call: CondorLegRecord
    long_put: CondorLegRecord
    short_strike: float
    contracts: int
    multiplier: int
    entry_net_credit: float          # per share, AS FILLED (not the planning mid-based figure)
    max_loss: float                  # per share, GROSS, from the IronButterflyResult at entry time
    profit_target_debit: float       # per share, NET of real round-trip fees (fees.py) -- see iron_butterfly.py
    target_pct: float                # CONDOR_PROFIT_TARGET_PCT at entry -- kept on the position so an
                                      # untested-side roll can recompute profit_target_debit off the new,
                                      # larger entry_net_credit (see _finalize_untested_roll)
    round_trip_fees: float           # dollars, estimated at entry (fees.py; price-independent, contracts-based)
    entry_time: datetime
    entry_order_id: Optional[str] = None
    status: str = "open"             # "open" | "closed"
    exit_time: Optional[datetime] = None
    exit_debit: Optional[float] = None    # per share, AS FILLED
    exit_order_id: Optional[str] = None
    exit_reason: Optional[str] = None     # "profit_target" | "backstop" | "manual" | "error" |
                                           # "defend_reversal" | "vix_recross" | "roll_unavailable"
                                           # ("roll_unavailable": daily_loop._attempt_untested_roll
                                           # found no strike that both hit the roll's delta target
                                           # and cleared the other side's open short strike --
                                           # see iron_butterfly.select_untested_short's
                                           # opposite_strike guard -- and force-closed the whole
                                           # position rather than leave it unrolled and unwatched)
    gross_pnl: Optional[float] = None     # dollars, before fees
    realized_pnl: Optional[float] = None  # dollars, NET of round_trip_fees -- the real economic result
    rolled_side: Optional[str] = None     # "call" | "put" | None -- set once the untested side has
                                           # been rolled (see submit_untested_roll); None means the
                                           # position is still the original, as-entered structure
    rolls_used: int = 0                   # how many untested-side rolls this position has had --
                                           # compared against CONDOR_MAX_UNTESTED_ROLLS_PER_DAY

    @property
    def legs(self) -> List[CondorLegRecord]:
        return [self.short_call, self.short_put, self.long_call, self.long_put]


def _entry_legs_payload(result: IronButterflyResult) -> list:
    return [
        {"instrument-type": "Equity Option", "symbol": result.short_call.symbol,
         "quantity": result.contracts, "action": "Sell to Open"},
        {"instrument-type": "Equity Option", "symbol": result.short_put.symbol,
         "quantity": result.contracts, "action": "Sell to Open"},
        {"instrument-type": "Equity Option", "symbol": result.long_call.symbol,
         "quantity": result.contracts, "action": "Buy to Open"},
        {"instrument-type": "Equity Option", "symbol": result.long_put.symbol,
         "quantity": result.contracts, "action": "Buy to Open"},
    ]


def _exit_legs_payload(position: CondorPosition) -> list:
    return [
        {"instrument-type": "Equity Option", "symbol": leg.symbol,
         "quantity": position.contracts, "action": _REVERSE_ACTION[leg.entry_action]}
        for leg in position.legs
    ]


def build_entry_payload(result: IronButterflyResult, price: float, external_id: Optional[str] = None,
                         time_in_force: str = "Day") -> dict:
    return {
        "time-in-force": time_in_force,
        "order-type": "Limit",
        "price": round(price, 2),
        "price-effect": "Credit",
        "external-identifier": external_id or new_external_id(),
        "legs": _entry_legs_payload(result),
    }


def build_exit_payload(position: CondorPosition, price: float, external_id: Optional[str] = None) -> dict:
    return {
        "time-in-force": "Day",
        "order-type": "Limit",
        "price": round(price, 2),
        "price-effect": "Debit",
        "external-identifier": external_id or new_external_id(),
        "legs": _exit_legs_payload(position),
    }


def _untested_roll_legs_payload(position: CondorPosition, roll: UntestedSideRoll) -> list:
    old_short = position.short_call if roll.side == "call" else position.short_put
    old_wing = position.long_call if roll.side == "call" else position.long_put
    return [
        {"instrument-type": "Equity Option", "symbol": old_short.symbol,
         "quantity": position.contracts, "action": "Buy to Close"},
        {"instrument-type": "Equity Option", "symbol": old_wing.symbol,
         "quantity": position.contracts, "action": "Sell to Close"},
        {"instrument-type": "Equity Option", "symbol": roll.new_short.symbol,
         "quantity": position.contracts, "action": "Sell to Open"},
        {"instrument-type": "Equity Option", "symbol": roll.new_wing.symbol,
         "quantity": position.contracts, "action": "Buy to Open"},
    ]


def build_untested_roll_payload(position: CondorPosition, roll: UntestedSideRoll, price: float,
                                 external_id: Optional[str] = None) -> dict:
    """One 4-leg combo order: close the untested side's old short+wing,
    open the new closer pair -- same single-ticket shape as entry/exit.
    Priced as a credit since a roll closer to the money should always
    collect more than it costs to unwind the old spread (build_untested_side_roll
    already refuses to build a roll that wouldn't)."""
    return {
        "time-in-force": "Day",
        "order-type": "Limit",
        "price": round(price, 2),
        "price-effect": "Credit",
        "external-identifier": external_id or new_external_id(),
        "legs": _untested_roll_legs_payload(position, roll),
    }


def _position_from_result(result: IronButterflyResult, net_credit: float, order_id: Optional[str]) -> CondorPosition:
    # Recomputed off the ACTUAL as-filled net_credit, not result.profit_target_debit
    # (which is only the pre-fill planning-mid figure) -- see recompute_profit_target_debit's
    # docstring. The entry chase (config.py's ENTRY_CHASE_*) almost always fills away from
    # that planning price, so using it directly understated/overstated the real target_pct.
    profit_target_debit = recompute_profit_target_debit(
        net_credit, result.target_pct, result.multiplier, result.contracts, result.round_trip_fees,
    )
    return CondorPosition(
        short_call=CondorLegRecord(result.short_call.symbol, result.short_call.strike, "Sell to Open"),
        short_put=CondorLegRecord(result.short_put.symbol, result.short_put.strike, "Sell to Open"),
        long_call=CondorLegRecord(result.long_call.symbol, result.long_call.strike, "Buy to Open"),
        long_put=CondorLegRecord(result.long_put.symbol, result.long_put.strike, "Buy to Open"),
        short_strike=result.short_strike,
        contracts=result.contracts,
        multiplier=result.multiplier,
        entry_net_credit=net_credit,
        max_loss=result.max_loss,
        profit_target_debit=profit_target_debit,
        target_pct=result.target_pct,
        round_trip_fees=result.round_trip_fees,
        entry_time=datetime.now(),
        entry_order_id=order_id,
    )


class CondorOrderExecutor:
    """Mirrors spy_0dte_strat's OrderExecutor shape (same _post_order/
    _check_order_once retry semantics) but priced/filled as ONE net
    order across 4 legs instead of one leg at a time."""

    def __init__(self, session_token: str, auth=None):
        self.session_token = session_token
        self.auth = auth
        self.headers = {"Authorization": session_token, "Content-Type": "application/json"}
        self._session = requests.Session()

    @property
    def is_live(self) -> bool:
        return EXECUTE_ORDERS

    # --- entry -----------------------------------------------------------

    def submit_entry(self, result: IronButterflyResult, *, dry_run: bool = False) -> Tuple[OrderResult, Optional[CondorPosition]]:
        if not EXECUTE_ORDERS:
            net_credit = self._simulate_entry_fill(result)
            logger.info("[PAPER] Iron butterfly entry | short=$%.2f contracts=%d | "
                        "simulated net credit=$%.2f (planning mid=$%.2f)",
                        result.short_strike, result.contracts, net_credit, result.net_credit)
            position = _position_from_result(result, net_credit, order_id=None)
            return OrderResult(status=OrderStatus.FILLED, net_price=net_credit,
                                filled_quantity=result.contracts,
                                message="Paper fill at bid/ask (immediate, no walk)"), position

        if dry_run:
            payload = build_entry_payload(result, price=result.net_credit)
            order_id = self._post_order(payload, dry_run=True)
            if order_id is None:
                return OrderResult(status=OrderStatus.ERROR, message="Dry-run rejected"), None
            return OrderResult(status=OrderStatus.NOT_SUBMITTED, order_id=order_id,
                                message="Dry-run accepted, nothing opened"), None

        return self._chase_entry(result)

    def _chase_entry(self, result: IronButterflyResult) -> Tuple[OrderResult, Optional[CondorPosition]]:
        """IOC limit-chase: step the net credit DOWN by ENTRY_CHASE_STEP on
        each miss, same shape as spy_0dte_strat's single-leg _limit_chase
        but for one 4-leg combo order per step (confirmed live 2026-10-27
        that IOC is accepted on a 4-leg combo -- self-cancels cleanly,
        zero fills on any leg, when non-marketable). Direction is credit
        DOWN (giving up edge to get filled), the opposite of the exit
        backstop's debit-UP escalation -- bounded the same way: a fixed
        tick cap (ENTRY_CHASE_MAX_WALK_TICKS) plus a miss-streak abort
        (ENTRY_CHASE_ABORT_MISS_STREAK) rather than chasing a market that
        has moved away entirely."""
        mid_credit = result.net_credit
        floor_credit = round(mid_credit - ENTRY_CHASE_MAX_WALK_TICKS * ENTRY_CHASE_STEP, 2)
        limit_price = round(mid_credit, 2)
        attempt = 0
        miss_streak = 0

        while limit_price >= floor_credit:
            attempt += 1
            payload = build_entry_payload(result, price=limit_price, time_in_force="IOC")
            order_id = self._post_order(payload)
            if order_id is None:
                return OrderResult(status=OrderStatus.ERROR, message="Entry order submission failed"), None

            status, net_price, filled_qty = self._poll_ioc_settle(order_id)
            if status == OrderStatus.FILLED and filled_qty >= result.contracts:
                logger.info("[ORDER] Entry chase filled on attempt %d at $%.2f (started at $%.2f)",
                            attempt, net_price, mid_credit)
                position = _position_from_result(result, net_price, order_id=order_id)
                return OrderResult(status=OrderStatus.FILLED, order_id=order_id, net_price=net_price,
                                    filled_quantity=filled_qty), position

            miss_streak += 1
            if miss_streak >= ENTRY_CHASE_ABORT_MISS_STREAK:
                logger.warning("[ORDER] Entry chase aborting: %d consecutive misses (started at $%.2f, last tried $%.2f)",
                                miss_streak, mid_credit, limit_price)
                break
            limit_price = round(limit_price - ENTRY_CHASE_STEP, 2)

        return OrderResult(status=OrderStatus.TIMEOUT,
                            message=f"Entry chase exhausted after {attempt} attempt(s), "
                                    f"started $%.2f, last tried $%.2f" % (mid_credit, limit_price)), None

    def _poll_ioc_settle(self, order_id: str) -> Tuple[OrderStatus, Optional[float], int]:
        """IOC resolves almost instantly for SPY (confirmed live 2026-10-27:
        ~0.4s for a 4-leg combo to go Routed -> Cancelled). This does NOT
        hold for every symbol: a live check on SPXW (2026-10-27) left an
        IOC order sitting in "Received" indefinitely -- never self-cancelled,
        needed an explicit DELETE. So this always explicitly cancels on
        timeout rather than assuming self-cancellation; _cancel_order
        already treats an already-terminal order as a no-op success, so
        this is safe even on a symbol where IOC genuinely does self-cancel."""
        deadline = time.time() + ENTRY_CHASE_SETTLE_TIMEOUT_SEC
        while time.time() < deadline:
            status, net_price, filled_qty = self._check_order_once(order_id)
            if status in ("filled",) and net_price is not None:
                return OrderStatus.FILLED, net_price, filled_qty
            if status in ("cancelled", "rejected", "expired", "error"):
                return OrderStatus.TIMEOUT, None, 0
            time.sleep(ENTRY_CHASE_SETTLE_POLL_SEC)
        logger.warning("[ORDER] IOC %s did not settle within %.1fs -- cancelling explicitly, treating as a miss",
                        order_id, ENTRY_CHASE_SETTLE_TIMEOUT_SEC)
        self._cancel_order(order_id)
        # Same fill-vs-cancel race _poll_fill already guards against (see its
        # comment for the real incident that found it): the order can fill in
        # the gap between the last poll and the cancel landing. Don't trust
        # TIMEOUT without one more check.
        final_status, final_net_price, final_filled_qty = self._check_order_once(order_id)
        if final_status == "filled" and final_net_price is not None:
            logger.warning("[ORDER] IOC %s actually filled in the cancel race -- NOT a miss", order_id)
            return OrderStatus.FILLED, final_net_price, final_filled_qty
        return OrderStatus.TIMEOUT, None, 0

    # --- exit --------------------------------------------------------------

    def submit_exit(self, position: CondorPosition, quotes: Dict[str, Tuple[float, float]],
                     *, reason: str, dry_run: bool = False,
                     price_override: Optional[float] = None) -> OrderResult:
        """quotes: {symbol: (bid, ask)} for all 4 of position.legs, current
        as of the moment the caller wants to close -- fetched by the
        caller (streamer or REST poll), not by this method, so this
        module stays testable without a live connection.

        price_override: submit at this debit instead of the mid-based
        planning figure -- used by daily_loop.py's backstop-retry path
        to escalate price aggression on a reason="backstop" exit that
        didn't fill the first time. A hard safety exit that gives up
        after one unfilled attempt isn't actually a safety exit; see
        daily_loop.run_day's docstring for the real gap this fixes
        (found live 2026-10-26: the exit order simply didn't cross at
        the mid-based price within ORDER_FILL_TIMEOUT_SEC and run_day
        returned with the position still open)."""
        if not EXECUTE_ORDERS:
            debit = self._simulate_exit_fill(position, quotes)
            self._finalize_exit(position, debit, order_id=None, reason=reason)
            logger.info("[PAPER] Iron butterfly exit (%s) | simulated net debit=$%.2f | realized P&L=$%.2f",
                        reason, debit, position.realized_pnl)
            return OrderResult(status=OrderStatus.FILLED, net_price=debit,
                                filled_quantity=position.contracts,
                                message=f"Paper exit fill ({reason})")

        planning_debit = price_override if price_override is not None else self._planning_exit_debit(position, quotes)
        payload = build_exit_payload(position, price=planning_debit)
        order_id = self._post_order(payload, dry_run=dry_run)
        if dry_run:
            return OrderResult(status=OrderStatus.NOT_SUBMITTED, order_id=order_id, message="Dry-run exit accepted")
        if order_id is None:
            return OrderResult(status=OrderStatus.ERROR, message="Exit order submission failed")

        status, raw_net_price, filled_qty = self._poll_fill(order_id, position.contracts)
        # _extract_net_fill's convention is direction-agnostic (Sell actions
        # positive, Buy actions negative, regardless of Open/Close) -- correct
        # as "net credit received (+) vs net debit paid (-) on THIS order," and
        # matches entry_net_credit directly for an entry. For an EXIT, the
        # actions are reversed (Buy to Close on the shorts, Sell to Close on
        # the wings), so that same convention comes back NEGATIVE for what
        # costs you money -- the opposite sign from "debit" everywhere else in
        # this module (_simulate_exit_fill, _planning_exit_debit/
        # mid_debit_to_close all return a POSITIVE cost-to-close). Confirmed
        # against a real filled sandbox close 2026-10-26: raw net_price came
        # back -1.21 for a real $1.21 debit. Flip it here, once, rather than
        # changing _extract_net_fill's otherwise-correct generic convention.
        debit = None if raw_net_price is None else -raw_net_price
        if status == OrderStatus.FILLED:
            self._finalize_exit(position, debit, order_id=order_id, reason=reason)
        return OrderResult(status=status, order_id=order_id, net_price=debit, filled_quantity=filled_qty)

    def submit_resting_exit(self, position: CondorPosition, price: float, *, reason: str,
                             dry_run: bool = False) -> OrderResult:
        """Submit one Day limit exit order and return immediately WITHOUT
        waiting for a fill or cancelling on a timeout -- unlike
        submit_exit, this is meant to sit on the book until the market
        comes to it. Added 2026-10-27, user: "After an order gets
        filled, you can add a day limit order to sell at whatever value
        is 25% profit[,] that would be most efficient" -- resting the
        profit-target order at the exchange the instant entry fills
        means the order matches the moment the market reaches it,
        instead of waiting for the next quote poll to notice it already
        crossed and only THEN submitting. Live-mode only: paper mode's
        simulated fills are already instant, so there's no latency to
        save there, and this module has no real order book to rest
        anything on in paper mode. Caller owns the lifecycle: poll with
        poll_resting_exit(), cancel with cancel_resting_exit() if it's
        no longer wanted (e.g. the backstop time arrives unfilled)."""
        if not EXECUTE_ORDERS:
            raise RuntimeError("submit_resting_exit is live-mode only -- paper mode has no real order book to rest on")

        payload = build_exit_payload(position, price=price)
        order_id = self._post_order(payload, dry_run=dry_run)
        if dry_run:
            return OrderResult(status=OrderStatus.NOT_SUBMITTED, order_id=order_id,
                                message=f"Dry-run resting {reason} exit accepted")
        if order_id is None:
            return OrderResult(status=OrderStatus.ERROR, message=f"Resting {reason} exit order submission failed")
        return OrderResult(status=OrderStatus.SUBMITTED, order_id=order_id,
                            message=f"Resting {reason} exit order resting at ${price:.2f}")

    def poll_resting_exit(self, position: CondorPosition, order_id: str, *, reason: str) -> bool:
        """Check ONCE whether a resting exit order (from submit_resting_exit)
        has filled. If so, finalizes the position (same sign-flip
        submit_exit's live path applies -- _extract_net_fill's raw
        convention comes back negative for a genuine exit debit) and
        returns True. Returns False if still live, cancelled, or
        errored -- caller keeps polling or cancels."""
        status, raw_net_price, filled_qty = self._check_order_once(order_id)
        if status == "filled" and raw_net_price is not None and filled_qty >= position.contracts:
            debit = -raw_net_price
            self._finalize_exit(position, debit, order_id=order_id, reason=reason)
            return True
        return False

    def cancel_resting_exit(self, order_id: str) -> bool:
        """Cancel a still-open resting exit order (e.g. the backstop
        time arrived before the profit-target order filled). Thin
        public wrapper around the same _cancel_order used internally by
        _poll_fill, kept public here since daily_loop.py (outside this
        module) needs to call it directly for a resting order's
        lifecycle."""
        return self._cancel_order(order_id)

    # --- defend: roll the untested side ------------------------------------

    def submit_untested_roll(self, position: CondorPosition, roll: UntestedSideRoll,
                              old_quotes: Dict[str, Tuple[float, float]], *,
                              dry_run: bool = False) -> OrderResult:
        """Close the untested side's existing short+wing and open the new,
        closer pair from build_untested_side_roll -- one 4-leg combo order
        (2 closes + 2 opens). On a real fill, mutates `position` in place
        (see _finalize_untested_roll): the rolled side's legs are replaced,
        the roll's net credit is folded into entry_net_credit so every
        downstream P&L figure (profit-target check, alerts, history) keeps
        reading off the same field unchanged, and max_loss is recomputed
        from the now-possibly-asymmetric wing widths. profit_target_debit
        is also recomputed off the new, larger entry_net_credit (fixed
        2026-11-04 -- previously it stayed at the ORIGINAL entry's
        target_pct of the ORIGINAL, smaller credit, so it triggered the
        profit-target exit earlier than CONDOR_PROFIT_TARGET_PCT of the
        new total credit really intends; same root cause as the plain
        entry-chase case recompute_profit_target_debit was pulled out for)."""
        if not EXECUTE_ORDERS:
            net_credit = self._simulate_untested_roll_fill(position, roll, old_quotes)
            self._finalize_untested_roll(position, roll, net_credit, order_id=None)
            logger.info(
                "[PAPER] Untested-side roll (%s) | new short=$%.2f new wing=$%.2f | "
                "simulated net credit=$%.2f (planning mid=$%.2f)",
                roll.side, roll.new_short_strike, roll.new_wing_strike, net_credit, roll.roll_credit,
            )
            return OrderResult(status=OrderStatus.FILLED, net_price=net_credit,
                                filled_quantity=position.contracts,
                                message=f"Paper roll fill ({roll.side})")

        planning_price = roll.roll_credit
        payload = build_untested_roll_payload(position, roll, price=planning_price)
        order_id = self._post_order(payload, dry_run=dry_run)
        if dry_run:
            return OrderResult(status=OrderStatus.NOT_SUBMITTED, order_id=order_id, message="Dry-run roll accepted")
        if order_id is None:
            return OrderResult(status=OrderStatus.ERROR, message="Roll order submission failed")

        status, net_price, filled_qty = self._poll_fill(order_id, position.contracts)
        if status == OrderStatus.FILLED and net_price is not None:
            self._finalize_untested_roll(position, roll, net_price, order_id=order_id)
        return OrderResult(status=status, order_id=order_id, net_price=net_price, filled_quantity=filled_qty)

    def _simulate_untested_roll_fill(self, position: CondorPosition, roll: UntestedSideRoll,
                                      old_quotes: Dict[str, Tuple[float, float]]) -> float:
        """Realistic marketable fill, same convention as
        _simulate_entry_fill/_simulate_exit_fill: buy back the old short at
        ask, sell the old wing at bid (the cost to unwind), sell the new
        short at bid, buy the new wing at ask (the new spread's proceeds)."""
        old_short = position.short_call if roll.side == "call" else position.short_put
        old_wing = position.long_call if roll.side == "call" else position.long_put
        old_short_bid, old_short_ask = old_quotes[old_short.symbol]
        old_wing_bid, old_wing_ask = old_quotes[old_wing.symbol]
        close_cost = old_short_ask - old_wing_bid
        open_credit = roll.new_short.bid - roll.new_wing.ask
        return open_credit - close_cost

    def _finalize_untested_roll(self, position: CondorPosition, roll: UntestedSideRoll,
                                 net_credit: float, order_id: Optional[str]) -> None:
        new_short_leg = CondorLegRecord(roll.new_short.symbol, roll.new_short.strike, "Sell to Open")
        new_wing_leg = CondorLegRecord(roll.new_wing.symbol, roll.new_wing.strike, "Buy to Open")
        if roll.side == "call":
            position.short_call = new_short_leg
            position.long_call = new_wing_leg
        else:
            position.short_put = new_short_leg
            position.long_put = new_wing_leg

        position.entry_net_credit += net_credit
        position.rolled_side = roll.side
        position.rolls_used += 1

        # Recompute max_loss from each side's OWN current short strike --
        # after this roll, short_call.strike and short_put.strike may no
        # longer match (it's effectively an iron condor now, not a
        # butterfly, on whichever side rolled). Same formula build_iron_butterfly
        # uses at entry, just evaluated off the live leg strikes.
        call_wing_width = position.long_call.strike - position.short_call.strike
        put_wing_width = position.short_put.strike - position.long_put.strike
        call_side_max_loss = call_wing_width - position.entry_net_credit
        put_side_max_loss = put_wing_width - position.entry_net_credit
        position.max_loss = max(call_side_max_loss, put_side_max_loss)

        position.profit_target_debit = recompute_profit_target_debit(
            position.entry_net_credit, position.target_pct, position.multiplier,
            position.contracts, position.round_trip_fees,
        )

    def _finalize_exit(self, position: CondorPosition, debit: float, order_id: Optional[str], reason: str) -> None:
        position.status = "closed"
        position.exit_time = datetime.now()
        position.exit_debit = debit
        position.exit_order_id = order_id
        position.exit_reason = reason
        position.gross_pnl = (position.entry_net_credit - debit) * position.multiplier * position.contracts
        # 2026-10-26, user: "We need to account for this ... These will need
        # to be net" -- the recorded result for a closed trade is the real
        # economic one (after commission/clearing/regulatory fees), not the
        # options-market price move alone.
        position.realized_pnl = position.gross_pnl - position.round_trip_fees

    # --- paper-mode fill simulation ----------------------------------------

    def _simulate_entry_fill(self, result: IronButterflyResult) -> float:
        """Realistic marketable fill: sell the shorts at bid, buy the
        wings at ask -- NOT the mid-based planning figure (result.net_credit),
        which is what justified the trade, not what you'd actually get."""
        return ((result.short_call.bid + result.short_put.bid)
                - (result.long_call.ask + result.long_put.ask))

    def _simulate_exit_fill(self, position: CondorPosition, quotes: Dict[str, Tuple[float, float]]) -> float:
        """Closing: buy back the shorts at ask, sell the wings at bid."""
        total = 0.0
        for leg in position.legs:
            bid, ask = quotes[leg.symbol]
            if leg.entry_action == "Sell to Open":
                total += ask   # buying it back
            else:
                total -= bid   # selling it off
        return total

    def _planning_exit_debit(self, position: CondorPosition, quotes: Dict[str, Tuple[float, float]]) -> float:
        return mid_debit_to_close(position, quotes)

    # --- HTTP plumbing (ported from spy_0dte_strat's order_executor.py;
    #     generic retry/backoff/401-refresh, not leg-count-specific) -------

    @staticmethod
    def _backoff_delay(retry_count: int) -> float:
        base = ORDER_POST_BACKOFF_BASE_SEC * (2 ** retry_count)
        capped = min(base, ORDER_POST_BACKOFF_MAX_SEC)
        return max(0.1, capped * random.uniform(0.8, 1.2))

    def _post_order(self, payload: dict, dry_run: bool = False) -> Optional[str]:
        suffix = "/dry-run" if dry_run else ""
        url = f"{BASE_URL}/accounts/{ACCOUNT_NUMBER}/orders{suffix}"
        token_refreshed = False
        retry_count = 0
        ext_id = payload.get("external-identifier", "?")

        while True:
            try:
                response = self._session.post(url, json=payload, headers=self.headers, timeout=5)
            except requests.exceptions.Timeout:
                if retry_count >= ORDER_POST_MAX_RETRIES:
                    logger.error("[ORDER] POST timed out after %d retries -- giving up | external_id=%s",
                                 retry_count, ext_id)
                    return None
                delay = self._backoff_delay(retry_count)
                logger.warning("[ORDER] POST timed out, retrying in %.1fs (attempt %d/%d, safe: external_id=%s)",
                                delay, retry_count + 1, ORDER_POST_MAX_RETRIES, ext_id)
                time.sleep(delay)
                retry_count += 1
                continue
            except Exception as e:
                logger.error(f"[ORDER] POST error: {e}")
                return None

            if response.status_code == 401 and not token_refreshed and self.auth is not None:
                logger.warning("[ORDER] 401 Unauthorized -- refreshing session token and retrying")
                token_refreshed = True
                if self.auth.login():
                    self.session_token = self.auth.session_token
                    self.headers["Authorization"] = self.session_token
                    continue
                logger.error("[ORDER] Token refresh failed after 401 -- aborting order")
                return None

            if response.status_code == 429 or response.status_code >= 500:
                if retry_count >= ORDER_POST_MAX_RETRIES:
                    logger.error("[ORDER] POST got %d after %d retries -- giving up | external_id=%s | %s",
                                 response.status_code, retry_count, ext_id, response.text)
                    return None
                delay = self._backoff_delay(retry_count)
                logger.warning("[ORDER] POST got %d, backing off %.1fs (attempt %d/%d, safe: external_id=%s)",
                                response.status_code, delay, retry_count + 1, ORDER_POST_MAX_RETRIES, ext_id)
                time.sleep(delay)
                retry_count += 1
                continue

            if response.status_code not in (200, 201):
                logger.error(f"[ORDER] POST failed: {response.status_code} - {response.text}")
                return None

            order_data = response.json().get("data", {}).get("order", {})
            order_id = str(order_data.get("id", ""))
            logger.info(f"[ORDER] Submitted | order_id={order_id} | external_id={ext_id} | dry_run={dry_run}")
            return order_id or None

    def _check_order_once(self, order_id: str) -> Tuple[str, Optional[float], int]:
        """Returns (status_str, net_price_or_None, filled_qty). Single GET,
        one 401-refresh retry -- same pattern as _post_order."""
        token_refreshed = False
        while True:
            try:
                response = self._session.get(
                    f"{BASE_URL}/accounts/{ACCOUNT_NUMBER}/orders/{order_id}",
                    headers=self.headers, timeout=5,
                )
            except Exception as e:
                logger.warning(f"[ORDER] Status check error: {e}")
                return ("error", None, 0)

            if response.status_code == 401 and not token_refreshed and self.auth is not None:
                token_refreshed = True
                logger.warning("[ORDER] Status check 401 -- refreshing token and retrying")
                if self.auth.login():
                    self.session_token = self.auth.session_token
                    self.headers["Authorization"] = self.session_token
                    continue
                return ("error", None, 0)

            if response.status_code != 200:
                return ("error", None, 0)

            order_data = response.json().get("data", {})
            status = order_data.get("status", "").lower()
            net_price, filled_qty = _extract_net_fill(order_data)
            return (status, net_price, filled_qty)

    def _poll_fill(self, order_id: str, expected_contracts: int,
                    timeout_sec: float = ORDER_FILL_TIMEOUT_SEC, poll_interval: float = 0.5) -> Tuple[OrderStatus, Optional[float], int]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status, net_price, filled_qty = self._check_order_once(order_id)
            if status in ("filled",) and net_price is not None:
                return OrderStatus.FILLED, net_price, filled_qty
            if status in ("rejected", "cancelled", "expired", "error"):
                return OrderStatus.REJECTED if status == "rejected" else OrderStatus.ERROR, None, 0
            time.sleep(poll_interval)
        logger.warning("[ORDER] %s did not fill within %.0fs -- cancelling", order_id, timeout_sec)
        self._cancel_order(order_id)
        # Real race found live 2026-10-26: the order can genuinely fill in
        # the gap between the last poll above and the cancel request landing
        # at the broker -- the cancel then correctly reports "already
        # terminal" (400/422), but that's ambiguous: terminal-because-filled
        # looks identical to terminal-because-already-expired from
        # _cancel_order's return value alone. One more status check settles
        # it -- do NOT trust TIMEOUT without this. Confirmed concretely: an
        # 8-contract order reported TIMEOUT here, was treated as "nothing
        # opened," and was actually sitting filled in the account the whole
        # time until found and closed manually.
        final_status, final_net_price, final_filled_qty = self._check_order_once(order_id)
        if final_status == "filled" and final_net_price is not None:
            logger.warning("[ORDER] %s actually filled in the cancel race -- NOT a timeout", order_id)
            return OrderStatus.FILLED, final_net_price, final_filled_qty
        return OrderStatus.TIMEOUT, None, 0

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open Day order via DELETE .../orders/{id}, so a
        timed-out multi-leg order never rests on the book unattended.
        Ported from spy_0dte_strat's order_executor.py (same retry/401
        pattern as _post_order/_check_order_once)."""
        token_refreshed = False
        retry_count = 0
        while True:
            try:
                response = self._session.delete(
                    f"{BASE_URL}/accounts/{ACCOUNT_NUMBER}/orders/{order_id}",
                    headers=self.headers, timeout=5,
                )
            except Exception as e:
                if retry_count >= ORDER_POST_MAX_RETRIES:
                    logger.error("[ORDER] Cancel error for %s after %d retries: %s -- giving up "
                                 "(order may still be LIVE at the broker)", order_id, retry_count, e)
                    return False
                delay = self._backoff_delay(retry_count)
                time.sleep(delay)
                retry_count += 1
                continue

            if response.status_code == 401 and not token_refreshed and self.auth is not None:
                token_refreshed = True
                if self.auth.login():
                    self.session_token = self.auth.session_token
                    self.headers["Authorization"] = self.session_token
                    continue
                logger.error("[ORDER] Token refresh failed -- cancel of %s aborted", order_id)
                return False

            if response.status_code in (200, 201):
                logger.info("[ORDER] Cancelled open order %s", order_id)
                return True
            if response.status_code in (400, 422):
                logger.info("[ORDER] Cancel of %s returned %s (already terminal)", order_id, response.status_code)
                return True
            if response.status_code == 429 or response.status_code >= 500:
                if retry_count >= ORDER_POST_MAX_RETRIES:
                    logger.error("[ORDER] Cancel of %s got %s after %d retries -- giving up "
                                 "(order may still be LIVE at the broker)",
                                 order_id, response.status_code, retry_count)
                    return False
                delay = self._backoff_delay(retry_count)
                time.sleep(delay)
                retry_count += 1
                continue

            logger.warning("[ORDER] Cancel of %s returned unexpected %s", order_id, response.status_code)
            return False


def mid_debit_to_close(position: CondorPosition, quotes: Dict[str, Tuple[float, float]]) -> float:
    """Mid-based cost to close right now -- same role result.net_credit
    plays on entry (the fair-value planning figure, not a slippage
    simulation). Used both as the live exit order's limit price
    (CondorOrderExecutor._planning_exit_debit) and, in daily_loop.py,
    as the monitoring signal for the profit-target exit -- a
    module-level function so the monitoring loop doesn't need an
    executor instance just to evaluate it."""
    total = 0.0
    for leg in position.legs:
        bid, ask = quotes[leg.symbol]
        mid = (bid + ask) / 2.0
        if leg.entry_action == "Sell to Open":
            total += mid
        else:
            total -= mid
    return total


def _extract_net_fill(order_data: dict) -> Tuple[Optional[float], int]:
    """Net price across every leg (credit positive, debit positive --
    caller already knows which this order was), and the filled quantity
    as the MINIMUM filled across legs (conservative: bounds how many
    complete 4-leg units are actually done if one leg only partially
    filled).

    VERIFIED against two real filled sandbox orders, 2026-10-26. The
    real shape differs from what this function originally assumed in
    two ways, both fixed here:
      - each fill's quantity key is "quantity", NOT "filled-quantity"
        (the key spy_0dte_strat's single-leg _extract_filled_quantity
        uses -- this function generalized that name without checking
        it against a real multi-leg response, and it was wrong).
      - "fill-price" and "quantity" both come back as JSON STRINGS
        ("0.55", not 0.55) -- multiplying a string by an int doesn't
        raise, it just does Python's string-repeat, silently producing
        garbage instead of an error. Both are now cast explicitly."""
    legs = order_data.get("legs") or order_data.get("order-legs") or []
    if not legs:
        return None, 0
    net_total = 0.0
    min_filled_qty = None
    for leg in legs:
        fills = leg.get("fills", [])
        if not fills:
            return None, 0
        leg_qty = sum(int(f.get("quantity") or 0) for f in fills)
        if leg_qty <= 0:
            return None, 0
        leg_value = sum(float(f.get("fill-price") or 0) * int(f.get("quantity") or 0) for f in fills)
        avg_leg_price = leg_value / leg_qty
        action = leg.get("action", "")
        sign = 1 if action.startswith("Sell") else -1
        net_total += sign * avg_leg_price
        min_filled_qty = leg_qty if min_filled_qty is None else min(min_filled_qty, leg_qty)
    return round(net_total, 4), (min_filled_qty or 0)


def fetch_leg_quotes(options_chain, position: CondorPosition) -> Optional[Dict[str, Tuple[float, float]]]:
    """Current bid/ask for all 4 of a position's legs, via the same
    batch-quote REST call options.py's _select_by_delta already uses.
    Returns None (not a partial dict) if any leg is missing a quote --
    callers should treat that as "can't evaluate the exit right now,"
    not as 0/None prices for the missing leg. UNVERIFIED against a
    real connection, same caveat as iron_butterfly.fetch_chain_snapshot."""
    symbols = [leg.symbol for leg in position.legs]
    quotes = options_chain._get_batch_quotes(symbols)
    result: Dict[str, Tuple[float, float]] = {}
    for sym in symbols:
        q = quotes.get(sym)
        if not q or q.get("bid") is None or q.get("ask") is None:
            return None
        result[sym] = (float(q["bid"]), float(q["ask"]))
    return result
