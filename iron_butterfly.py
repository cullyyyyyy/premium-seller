"""
iron_butterfly.py -- construction + economics for the 0DTE SPY premium-
selling structure: sell the ATM straddle (call + put at the SAME strike),
buy a put wing and a call wing each selected by delta (closest to 0.10),
not by fixed width. Terminology note: this is an IRON BUTTERFLY (body
width = 0, short call and short put share one strike), not the iron
CONDOR (OTM short strangle + fixed-width wings) locked in
spy_0dte_strat's project memory on 2026-10-22 -- this spec, given
directly by the user on 2026-10-26, supersedes that one.

Pure selection/economics logic lives here with no network calls, so it
can be unit-tested against a synthetic chain snapshot. See
fetch_chain_snapshot() for the one function that touches the live
OptionsChain (options.py, already ported from spy_0dte_strat).

Fees (2026-10-26, user: "We need to account for this in the premium
selling strategy especially when considering the gain/loss targets.
These will need to be net"): every dollar figure on IronButterflyResult
is labeled GROSS or NET in its own comment. `profit_target_debit` --
the number should_exit()/daily_loop.py actually compares a live quote
against -- is NET: it's the debit price that locks in target_pct of
the structure's max profit AFTER real round-trip commission/clearing/
regulatory fees (fees.py, verified against real dry-run data the same
day). `gross_profit_target_debit` is kept alongside it only for
reference/debugging, not for any real exit decision. max_loss/
capital_at_risk stay GROSS (the options-market price side of the
risk); net_max_loss adds the round-trip fees on top, since losing the
max still costs you the fees too. Position SIZING (contracts_for, in
daily_loop.py) is also fee-aware as of the same day (closed on
request, "yes close gap") -- it sizes off max_loss per share PLUS the
real round-trip fees at the resulting contract count, not gross alone.
"""
import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from typing import List, Optional

import fees as _fees

_log = logging.getLogger(__name__)


class IronButterflyError(ValueError):
    """Raised when the chain data can't support a valid, sane structure --
    callers should treat this as "abort the trade," not a bug to catch."""


@dataclass
class LegQuote:
    """One option's strike, OCC symbol, signed delta (calls positive,
    puts negative -- same convention as options.py's _select_by_delta),
    and bid/ask. bid/ask are None when the chain had no quote for this
    leg at all (distinct from a real, quoted zero bid)."""
    strike: float
    symbol: str
    delta: float
    bid: Optional[float]
    ask: Optional[float]

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass
class ChainSnapshot:
    """calls/puts: every strike's quote for that side, any order --
    build_iron_butterfly sorts them itself. Each side need not have a
    quote at every strike (missing legs just aren't candidates)."""
    underlying_price: float
    calls: List[LegQuote]
    puts: List[LegQuote]


@dataclass
class IronButterflyResult:
    short_strike: float
    short_call: LegQuote
    short_put: LegQuote
    long_call: LegQuote
    long_put: LegQuote

    net_credit: float            # per share, GROSS (no fees)
    max_profit: float            # per share == net_credit, GROSS
    call_side_max_loss: float    # per share, GROSS
    put_side_max_loss: float     # per share, GROSS
    max_loss: float              # per share == max(call_side, put_side), GROSS
    call_wing_width: float       # per share
    put_wing_width: float        # per share
    breakeven_lower: float       # GROSS (ignores fees -- the true breakeven is fee-shifted, see net_* below)
    breakeven_upper: float       # GROSS
    reward_to_risk: float        # GROSS
    credit_to_width_call: float
    credit_to_width_put: float
    target_pct: float
    gross_profit_target_debit: float  # per share -- target_pct of GROSS max profit, no fees (kept for reference)
    profit_target_debit: float        # per share -- target_pct of NET max profit, fees included (see module docstring)
    contracts: int
    multiplier: int
    capital_at_risk: float       # dollars = max_loss * multiplier * contracts (GROSS -- see net_max_loss)
    entry_fees: float            # dollars, total commission+clearing+regulatory to open at `contracts`
    exit_fees: float             # dollars, estimated total to close (fees.py's schedule, price-independent)
    round_trip_fees: float       # dollars == entry_fees + exit_fees
    net_max_profit: float        # dollars == max_profit*multiplier*contracts - round_trip_fees
    net_max_loss: float          # dollars == max_loss*multiplier*contracts + round_trip_fees (fees hurt on a loss too)
    net_reward_to_risk: float    # net_max_profit / net_max_loss -- the economically real ratio
    warnings: List[str]


def _distance_to_half(delta_call: float, delta_put: float) -> float:
    """Combined distance of this strike's call+put |delta| from 0.50 --
    the ATM tie-break rule ("closest to 0.50"), smaller is better."""
    return abs(delta_call - 0.50) + abs(abs(delta_put) - 0.50)


def recompute_profit_target_debit(net_credit: float, target_pct: float, multiplier: int,
                                   contracts: int, round_trip_fees: float) -> float:
    """Debit price that locks in target_pct of max profit AFTER fees:
    (net_credit - D)*multiplier*contracts - round_trip_fees == target_pct*net_max_profit.
    Pulled out of build_iron_butterfly so condor_order_executor.py can
    re-run it against the ACTUAL as-filled net_credit -- build_iron_butterfly
    only ever sees the pre-fill planning mid-price, which the entry chase
    (config.py's ENTRY_CHASE_*) almost always walks away from to get
    filled. Bug found 2026-11-04: the position's profit_target_debit was
    being left at this pre-fill value, so a trade filled below planning
    credit exited at less than CONDOR_PROFIT_TARGET_PCT of what it
    actually captured (a live $3.33 fill vs a $3.45 plan locked in ~22.6%
    instead of 25%)."""
    net_max_profit = net_credit * multiplier * contracts - round_trip_fees
    net_target_profit_dollars = target_pct * net_max_profit
    return _floor_to_cent(net_credit - (
        (net_target_profit_dollars + round_trip_fees) / (multiplier * contracts)
    ))


def _floor_to_cent(x: float) -> float:
    """Round DOWN to the nearest cent, never up or to-nearest. Both
    profit-target debits are "the most you may pay and still lock in
    AT LEAST target_pct of profit" -- rounding up (or Python's default
    round-to-nearest/-even) could land on a debit that pays slightly
    MORE than the true target price, capturing slightly LESS than
    target_pct. Flooring never does. User 2026-10-27: "you should also
    round down or truncate (i.e. 1.1553 should distill to 1.15)." The
    tiny epsilon before flooring guards against float noise (a true
    1.15 landing as 1.1499999999996 and getting floored down an extra
    cent it shouldn't)."""
    return math.floor(x * 100 + 1e-9) / 100.0


def select_short_strike(chain: ChainSnapshot) -> tuple:
    """Nearest strike to the underlying price; ties broken toward the
    strike whose call+put |delta| are jointly closest to 0.50. Requires
    a quote on BOTH sides at the chosen strike (it's sold as a straddle).
    Returns (strike, short_call: LegQuote, short_put: LegQuote)."""
    calls_by_strike = {c.strike: c for c in chain.calls}
    puts_by_strike = {p.strike: p for p in chain.puts}
    common_strikes = sorted(set(calls_by_strike) & set(puts_by_strike))
    if not common_strikes:
        raise IronButterflyError("No strike has both a call and a put quote -- cannot sell a straddle.")

    min_dist = min(abs(k - chain.underlying_price) for k in common_strikes)
    EPS = 1e-9
    tied = [k for k in common_strikes if abs(abs(k - chain.underlying_price) - min_dist) <= EPS]

    if len(tied) == 1:
        chosen = tied[0]
    else:
        chosen = min(
            tied,
            key=lambda k: _distance_to_half(calls_by_strike[k].delta, puts_by_strike[k].delta),
        )
    return chosen, calls_by_strike[chosen], puts_by_strike[chosen]


def select_wing(legs: List[LegQuote], short_strike: float, side: str,
                 target_delta: float = 0.10) -> LegQuote:
    """Pick the leg closest to target_delta, restricted to strikes
    strictly below (side="put") or above (side="call") the short
    strike. Wings are chosen by delta, not fixed width -- the caller
    must not assume call/put wing widths match.

    2026-10-27, user: "I don't want a delta less than 10 but I'll
    accept more than 10. Closest to 10 without going under." -- NEVER
    pick a wing with |delta| < target_delta while any candidate has
    |delta| >= target_delta; among those, pick whichever is closest to
    target_delta (least excess above it). A wing under the target is
    weaker protection than intended, so if every candidate is under
    target_delta, this aborts (IronButterflyError) rather than quietly
    accept one -- same "skip the day" posture as the far-wing-tolerance
    check in build_iron_butterfly."""
    if side == "put":
        candidates = [l for l in legs if l.strike < short_strike]
    elif side == "call":
        candidates = [l for l in legs if l.strike > short_strike]
    else:
        raise ValueError(f"side must be 'put' or 'call', got {side!r}")

    candidates = [l for l in candidates if l.delta is not None]
    if not candidates:
        raise IronButterflyError(
            f"No {side} strikes {'below' if side == 'put' else 'above'} "
            f"the short strike (${short_strike:.2f}) with a usable delta."
        )

    at_or_above = [l for l in candidates if abs(l.delta) >= target_delta]
    if not at_or_above:
        closest_available = max(abs(l.delta) for l in candidates)
        raise IronButterflyError(
            f"No {side} strike has |delta| >= target {target_delta:.2f} "
            f"(closest available is |delta|={closest_available:.3f}) -- "
            f"refusing to pick an under-protected wing."
        )
    return min(at_or_above, key=lambda l: abs(l.delta) - target_delta)


def build_iron_butterfly(
    chain: ChainSnapshot,
    *,
    target_wing_delta: float = 0.10,
    target_pct: float = 0.25,
    contracts: int = 1,
    multiplier: int = 100,
) -> IronButterflyResult:
    """2026-10-27, user: "I don't need a 3 point buffer. I don't want to
    get below 10 and I want the closest strike to 10." -- there used to
    be a second check here that aborted whenever the wing select_wing()
    picked was more than wing_delta_tolerance away from target, with an
    allow_far_wings escape hatch. Removed: select_wing() already
    enforces exactly what's wanted (never under target_delta, closest
    available at or above it) and already aborts on its own if NO
    candidate clears target_delta at all -- a second, separate
    distance-based abort on top of that was rejecting valid picks
    (e.g. a 16-delta wing, the closest available above 10 that day)
    for being numerically far from 10, which is not what "closest to
    10 without going under" means."""
    short_strike, short_call, short_put = select_short_strike(chain)
    long_put = select_wing(chain.puts, short_strike, "put", target_wing_delta)
    long_call = select_wing(chain.calls, short_strike, "call", target_wing_delta)

    warnings: List[str] = []

    # --- missing-quote / zero-bid-on-a-long-wing checks -------------------
    for leg, label in (
        (short_call, "short call"), (short_put, "short put"),
        (long_call, "long call wing"), (long_put, "long put wing"),
    ):
        if leg.bid is None or leg.ask is None:
            raise IronButterflyError(f"{label} (strike ${leg.strike:.2f}) has no bid/ask quote.")
    for leg, label in ((long_call, "long call wing"), (long_put, "long put wing")):
        if leg.bid == 0:
            raise IronButterflyError(
                f"{label} (strike ${leg.strike:.2f}) has a zero bid -- "
                f"likely illiquid or a stale quote, refusing to price the hedge off it."
            )

    net_credit = (short_call.mid + short_put.mid) - (long_call.mid + long_put.mid)
    if net_credit <= 0:
        raise IronButterflyError(f"Net credit is non-positive (${net_credit:.2f}) -- refusing to build it.")

    call_wing_width = long_call.strike - short_strike
    put_wing_width = short_strike - long_put.strike

    if net_credit >= call_wing_width or net_credit >= put_wing_width:
        raise IronButterflyError(
            f"Net credit (${net_credit:.2f}) >= a wing width "
            f"(call=${call_wing_width:.2f}, put=${put_wing_width:.2f}) -- "
            f"one side would have zero or negative max loss, which is not a sane structure."
        )

    max_profit = net_credit
    call_side_max_loss = call_wing_width - net_credit
    put_side_max_loss = put_wing_width - net_credit
    max_loss = max(call_side_max_loss, put_side_max_loss)

    breakeven_lower = short_strike - net_credit
    breakeven_upper = short_strike + net_credit

    reward_to_risk = net_credit / max_loss
    credit_to_width_call = net_credit / call_wing_width
    credit_to_width_put = net_credit / put_wing_width

    gross_profit_target_debit = _floor_to_cent(net_credit * (1 - target_pct))
    capital_at_risk = max_loss * multiplier * contracts

    entry_fee_dollars = _fees.entry_fees(contracts)
    exit_fee_dollars = _fees.exit_fees(contracts)
    round_trip_fee_dollars = entry_fee_dollars + exit_fee_dollars

    gross_max_profit_dollars = max_profit * multiplier * contracts
    net_max_profit = gross_max_profit_dollars - round_trip_fee_dollars
    net_max_loss = capital_at_risk + round_trip_fee_dollars
    net_reward_to_risk = net_max_profit / net_max_loss if net_max_loss > 0 else 0.0

    profit_target_debit = recompute_profit_target_debit(
        net_credit, target_pct, multiplier, contracts, round_trip_fee_dollars,
    )

    return IronButterflyResult(
        short_strike=short_strike, short_call=short_call, short_put=short_put,
        long_call=long_call, long_put=long_put,
        net_credit=net_credit, max_profit=max_profit,
        call_side_max_loss=call_side_max_loss, put_side_max_loss=put_side_max_loss,
        max_loss=max_loss, call_wing_width=call_wing_width, put_wing_width=put_wing_width,
        breakeven_lower=breakeven_lower, breakeven_upper=breakeven_upper,
        reward_to_risk=reward_to_risk,
        credit_to_width_call=credit_to_width_call, credit_to_width_put=credit_to_width_put,
        target_pct=target_pct, gross_profit_target_debit=gross_profit_target_debit,
        profit_target_debit=profit_target_debit,
        contracts=contracts, multiplier=multiplier, capital_at_risk=capital_at_risk,
        entry_fees=entry_fee_dollars, exit_fees=exit_fee_dollars, round_trip_fees=round_trip_fee_dollars,
        net_max_profit=net_max_profit, net_max_loss=net_max_loss, net_reward_to_risk=net_reward_to_risk,
        warnings=warnings,
    )


@dataclass
class UntestedSideRoll:
    """The new short+wing pair for rolling an open position's UNTESTED
    side closer to the money -- see select_untested_short/
    build_untested_side_roll."""
    side: str                  # "call" or "put" -- the side being rolled
    new_short: LegQuote
    new_wing: LegQuote
    new_short_strike: float
    new_wing_strike: float
    new_wing_width: float      # per share
    roll_credit: float         # per share, GROSS, mid-based -- THIS NEW SPREAD'S
                                # own planning credit only. Does NOT net out what it
                                # costs to close the old spread being replaced -- the
                                # caller combines this with a live quote on the old
                                # legs to get the roll's real net cash flow.


def select_untested_short(legs: List[LegQuote], side: str, current_price: float,
                           target_delta: float = 0.30, *,
                           opposite_strike: Optional[float] = None) -> LegQuote:
    """Pick a NEW short strike for the untested side when rolling it
    closer for extra credit (the tastytrade-style defense, not a
    recenter of the whole structure). Same 'closest to target_delta
    without going under' rule as select_wing. OTM is defined against
    the CURRENT underlying price (call strikes above it, put strikes
    below) -- AND, when `opposite_strike` is given, against the short
    strike still open on the OTHER (tested) side, so a roll can never
    cross the two spreads over each other. `opposite_strike` is the
    tested side's own short strike (e.g. the still-open short put when
    rolling the call side): the new call short must stay above it,
    the new put short must stay below it -- not just OTM of spot,
    which alone let a roll pick a strike inside the other spread when
    price had already moved past it (see Nov 5 2026 incident: call
    rolled to $743, UNDER the open $754 short put)."""
    if side == "put":
        floor = current_price if opposite_strike is None else min(current_price, opposite_strike)
        candidates = [l for l in legs if l.strike < floor]
    elif side == "call":
        floor = current_price if opposite_strike is None else max(current_price, opposite_strike)
        candidates = [l for l in legs if l.strike > floor]
    else:
        raise ValueError(f"side must be 'put' or 'call', got {side!r}")

    candidates = [l for l in candidates if l.delta is not None]
    if not candidates:
        bound_desc = f"the current price (${current_price:.2f})"
        if opposite_strike is not None:
            bound_desc += f" and the open short {'call' if side == 'put' else 'put'} (${opposite_strike:.2f})"
        raise IronButterflyError(
            f"No {side} strikes {'below' if side == 'put' else 'above'} "
            f"{bound_desc} with a usable delta."
        )

    at_or_above = [l for l in candidates if abs(l.delta) >= target_delta]
    if not at_or_above:
        closest_available = max(abs(l.delta) for l in candidates)
        raise IronButterflyError(
            f"No {side} strike has |delta| >= target {target_delta:.2f} "
            f"(closest available is |delta|={closest_available:.3f}) -- "
            f"refusing to roll to an under-targeted strike."
        )
    return min(at_or_above, key=lambda l: abs(l.delta) - target_delta)


def build_untested_side_roll(
    chain: ChainSnapshot, *, side: str,
    short_target_delta: float = 0.30,
    wing_target_delta: float = 0.10,
    opposite_strike: Optional[float] = None,
) -> UntestedSideRoll:
    """Build a new, closer short+wing pair for the UNTESTED side of an
    already-open iron butterfly/condor -- the tastytrade-style defense
    of rolling the safe side in for more credit, rather than touching
    the side that's actually losing. `side` is "call" or "put": the
    side NOT being tested (the caller decides which, from the open
    position's own breakeven math -- see daily_loop.evaluate_defend).
    `opposite_strike` is the tested side's own still-open short strike
    (pass position.short_put.strike when side="call", or
    position.short_call.strike when side="put") -- it's forwarded to
    select_untested_short as the hard floor/ceiling that keeps the
    roll from crossing into the other spread.

    The new short is picked off current OTM delta, bounded by both
    the underlying price and `opposite_strike` (select_untested_short),
    then given its own protective wing the same way entry does
    (select_wing, reusing the same target-delta discipline -- never
    under target, closest available at or above it)."""
    legs = chain.calls if side == "call" else chain.puts
    new_short = select_untested_short(legs, side, chain.underlying_price, short_target_delta,
                                       opposite_strike=opposite_strike)
    new_wing = select_wing(legs, new_short.strike, side, wing_target_delta)

    for leg, label in ((new_short, "new short"), (new_wing, "new wing")):
        if leg.bid is None or leg.ask is None:
            raise IronButterflyError(f"{label} (strike ${leg.strike:.2f}) has no bid/ask quote.")

    roll_credit = new_short.mid - new_wing.mid
    new_wing_width = (new_wing.strike - new_short.strike) if side == "call" else (new_short.strike - new_wing.strike)

    if roll_credit >= new_wing_width:
        raise IronButterflyError(
            f"Roll credit (${roll_credit:.2f}) >= new wing width (${new_wing_width:.2f}) -- "
            f"would leave zero or negative max loss on the rolled side, refusing."
        )

    return UntestedSideRoll(
        side=side, new_short=new_short, new_wing=new_wing,
        new_short_strike=new_short.strike, new_wing_strike=new_wing.strike,
        new_wing_width=new_wing_width, roll_credit=roll_credit,
    )


def chain_snapshot_from_dict(data: dict) -> ChainSnapshot:
    """Load a ChainSnapshot from the plain-JSON shape used by --input
    files and by fetch_chain_snapshot()'s eventual live output:
    {"underlying_price": 760.0,
     "calls": [{"strike":..., "symbol":..., "delta":..., "bid":..., "ask":...}, ...],
     "puts": [...]}"""
    def to_legs(items):
        return [LegQuote(strike=float(i["strike"]), symbol=i["symbol"], delta=float(i["delta"]),
                          bid=(None if i.get("bid") is None else float(i["bid"])),
                          ask=(None if i.get("ask") is None else float(i["ask"])))
                for i in items]
    return ChainSnapshot(
        underlying_price=float(data["underlying_price"]),
        calls=to_legs(data["calls"]),
        puts=to_legs(data["puts"]),
    )


CHAIN_WINDOW_PCT = 0.05    # only quote strikes within this fraction of the underlying price --
                            # relative, not a fixed $ amount, so it scales across symbols at very
                            # different price levels (SPY ~$770 vs SPX ~$7700). 5% matches the old
                            # fixed $40 window at SPY's price (40/770 ~= 5.2%) and comfortably covers
                            # the wider (~$70, ~0.9%) 0.10-delta wing distance seen on a live SPX
                            # chain check (2026-10-27) with real margin to spare.
QUOTE_CHUNK_SIZE = 50      # symbols per batch-quote REST call


def _chunked_batch_quotes(options_chain, symbols: list) -> dict:
    """options_chain._get_batch_quotes puts every symbol into ONE query
    string -- fine for the ~14-strike window _select_by_delta uses, but
    a real 0DTE chain has 100+ strikes per side and blows past the
    server's URL length limit (confirmed live 2026-10-26: a ~300-symbol
    request got a real 414 Request-URI Too Large from the sandbox's
    nginx). Chunk it instead of trusting any one request to fit."""
    merged: dict = {}
    for i in range(0, len(symbols), QUOTE_CHUNK_SIZE):
        chunk = symbols[i:i + QUOTE_CHUNK_SIZE]
        merged.update(options_chain._get_batch_quotes(chunk))
    return merged


def fetch_chain_snapshot(options_chain, symbol: str = "SPY") -> ChainSnapshot:
    """Build a ChainSnapshot from the live OptionsChain (options.py),
    fetching bid/ask/delta for strikes within CHAIN_WINDOW_PCT of the
    underlying price in today's 0DTE expiration, chunked across several
    batch-quote REST calls (see _chunked_batch_quotes).

    IMPORTANT, confirmed live 2026-10-26: the SANDBOX (cert) environment
    has no real market-data feed at all -- both the equity-quote and
    option batch-quote endpoints returned plain 502s from sandbox, while
    the chain STRUCTURE endpoint (strikes/symbols, no prices) works fine
    there. This mirrors spy_0dte_strat's own live_runner.py, which always
    builds its OptionsChain against PRODUCTION via OAuth
    (create_production_options_chain()) for real chain/quote data, and
    only uses a sandbox session for actual order submission. Callers of
    this function MUST pass an OptionsChain built the same way -- a
    sandbox-session OptionsChain will fail here with "no current price."
    """
    underlying_price = options_chain.get_equity_mid_price(symbol)
    if underlying_price is None:
        raise IronButterflyError(
            f"Could not get a current price for {symbol} -- if this OptionsChain "
            f"is on a sandbox session, it has no real market data; build it against "
            f"production instead (see this function's docstring)."
        )

    exp_data = options_chain.get_0dte_strikes(symbol)
    if not exp_data:
        raise IronButterflyError(f"Could not fetch a 0DTE chain for {symbol}.")
    strikes = exp_data.get("strikes") or exp_data.get("items") or []
    if not strikes:
        raise IronButterflyError(f"0DTE chain for {symbol} has no strikes.")

    def strike_price(s):
        val = s.get("strike-price") or s.get("strikePrice") or s.get("strike_price") or 0
        return float(val) if val else 0.0

    chain_window_points = underlying_price * CHAIN_WINDOW_PCT
    call_symbols, put_symbols = [], []
    by_call_sym, by_put_sym = {}, {}
    for s in strikes:
        px = strike_price(s)
        if abs(px - underlying_price) > chain_window_points:
            continue
        call_sym = s.get("call") or s.get("call-streamer-symbol")
        put_sym = s.get("put") or s.get("put-streamer-symbol")
        if call_sym:
            call_symbols.append(call_sym)
            by_call_sym[call_sym] = px
        if put_sym:
            put_symbols.append(put_sym)
            by_put_sym[put_sym] = px

    quotes = _chunked_batch_quotes(options_chain, call_symbols + put_symbols)

    def build_legs(symbols, by_sym):
        legs = []
        for sym in symbols:
            q = quotes.get(sym)
            if not q or q.get("delta") is None:
                continue
            legs.append(LegQuote(
                strike=by_sym[sym], symbol=sym, delta=float(q["delta"]),
                bid=(None if q.get("bid") is None else float(q["bid"])),
                ask=(None if q.get("ask") is None else float(q["ask"])),
            ))
        return legs

    return ChainSnapshot(
        underlying_price=underlying_price,
        calls=build_legs(call_symbols, by_call_sym),
        puts=build_legs(put_symbols, by_put_sym),
    )


def format_report(r: IronButterflyResult) -> str:
    lines = [
        f"Short straddle @ ${r.short_strike:.2f}  "
        f"(call {r.short_call.symbol} delta={r.short_call.delta:+.3f}, "
        f"put {r.short_put.symbol} delta={r.short_put.delta:+.3f})",
        f"Long put wing  @ ${r.long_put.strike:.2f}  delta={r.long_put.delta:+.3f}  "
        f"(width ${r.put_wing_width:.2f})",
        f"Long call wing @ ${r.long_call.strike:.2f}  delta={r.long_call.delta:+.3f}  "
        f"(width ${r.call_wing_width:.2f})",
        "",
        f"Net credit:            ${r.net_credit:.2f}/share  (${r.net_credit * r.multiplier * r.contracts:.2f} total)",
        f"Max profit:            ${r.max_profit:.2f}/share",
        f"Call-side max loss:    ${r.call_side_max_loss:.2f}/share",
        f"Put-side max loss:     ${r.put_side_max_loss:.2f}/share",
        f"Max loss (binding):    ${r.max_loss:.2f}/share  (${r.capital_at_risk:.2f} total, {r.contracts} contract(s))",
        f"Breakevens:            ${r.breakeven_lower:.2f} / ${r.breakeven_upper:.2f}",
        f"Reward:risk (gross):   {r.reward_to_risk:.3f}",
        f"Credit:width (call):   {r.credit_to_width_call:.3f}",
        f"Credit:width (put):    {r.credit_to_width_put:.3f}",
        "",
        f"Est. round-trip fees:  ${r.round_trip_fees:.2f}  (entry ${r.entry_fees:.2f} + exit ${r.exit_fees:.2f})",
        f"Net max profit:        ${r.net_max_profit:.2f}  (gross ${r.max_profit * r.multiplier * r.contracts:.2f})",
        f"Net max loss:          ${r.net_max_loss:.2f}  (gross ${r.capital_at_risk:.2f})",
        f"Reward:risk (net):     {r.net_reward_to_risk:.3f}",
        f"Profit target ({r.target_pct:.0%} of NET max profit): close for a debit of ${r.profit_target_debit:.2f}/share "
        f"(gross-only target would be ${r.gross_profit_target_debit:.2f}/share)",
    ]
    if r.warnings:
        lines.append("")
        lines.extend(r.warnings)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and price a 0DTE SPY iron butterfly.")
    parser.add_argument("--input", required=True, help="JSON chain snapshot file (see chain_snapshot_from_dict).")
    parser.add_argument("--target-pct", type=float, default=0.25, help="Profit target as a fraction of net credit (default 0.25).")
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--multiplier", type=int, default=100)
    args = parser.parse_args(argv)

    with open(args.input) as f:
        chain = chain_snapshot_from_dict(json.load(f))

    try:
        result = build_iron_butterfly(
            chain, target_pct=args.target_pct,
            contracts=args.contracts, multiplier=args.multiplier,
        )
    except IronButterflyError as e:
        print(f"ABORTED: {e}", file=sys.stderr)
        return 1

    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
