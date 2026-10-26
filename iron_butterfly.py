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
"""
import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import List, Optional

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

    net_credit: float            # per share
    max_profit: float            # per share == net_credit
    call_side_max_loss: float    # per share
    put_side_max_loss: float     # per share
    max_loss: float              # per share == max(call_side, put_side)
    call_wing_width: float       # per share
    put_wing_width: float        # per share
    breakeven_lower: float
    breakeven_upper: float
    reward_to_risk: float
    credit_to_width_call: float
    credit_to_width_put: float
    target_pct: float
    profit_target_debit: float   # per share
    contracts: int
    multiplier: int
    capital_at_risk: float       # dollars = max_loss * multiplier * contracts
    warnings: List[str]


def _distance_to_half(delta_call: float, delta_put: float) -> float:
    """Combined distance of this strike's call+put |delta| from 0.50 --
    the ATM tie-break rule ("closest to 0.50"), smaller is better."""
    return abs(delta_call - 0.50) + abs(abs(delta_put) - 0.50)


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
    """Pick the leg whose |delta| is closest to target_delta, restricted
    to strikes strictly below (side="put") or above (side="call") the
    short strike. Wings are chosen by delta, not fixed width -- the
    caller must not assume call/put wing widths match."""
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
    return min(candidates, key=lambda l: abs(abs(l.delta) - target_delta))


def build_iron_butterfly(
    chain: ChainSnapshot,
    *,
    target_wing_delta: float = 0.10,
    wing_delta_tolerance: float = 0.03,
    allow_far_wings: bool = False,
    target_pct: float = 0.25,
    contracts: int = 1,
    multiplier: int = 100,
) -> IronButterflyResult:
    short_strike, short_call, short_put = select_short_strike(chain)
    long_put = select_wing(chain.puts, short_strike, "put", target_wing_delta)
    long_call = select_wing(chain.calls, short_strike, "call", target_wing_delta)

    warnings: List[str] = []

    # --- wing-delta closeness check (abort, unless allow_far_wings) ------
    for leg, side in ((long_put, "put"), (long_call, "call")):
        diff = abs(abs(leg.delta) - target_wing_delta)
        if diff > wing_delta_tolerance:
            msg = (
                f"{side} wing (strike ${leg.strike:.2f}) has |delta|={abs(leg.delta):.3f}, "
                f"{diff:.3f} away from target {target_wing_delta:.2f} "
                f"(tolerance {wing_delta_tolerance:.2f})"
            )
            if allow_far_wings:
                warnings.append(f"WARNING: {msg} -- proceeding (--allow-far-wings)")
                _log.warning(msg)
            else:
                raise IronButterflyError(
                    f"{msg}. Pass --allow-far-wings to proceed anyway."
                )

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

    profit_target_debit = net_credit * (1 - target_pct)
    capital_at_risk = max_loss * multiplier * contracts

    return IronButterflyResult(
        short_strike=short_strike, short_call=short_call, short_put=short_put,
        long_call=long_call, long_put=long_put,
        net_credit=net_credit, max_profit=max_profit,
        call_side_max_loss=call_side_max_loss, put_side_max_loss=put_side_max_loss,
        max_loss=max_loss, call_wing_width=call_wing_width, put_wing_width=put_wing_width,
        breakeven_lower=breakeven_lower, breakeven_upper=breakeven_upper,
        reward_to_risk=reward_to_risk,
        credit_to_width_call=credit_to_width_call, credit_to_width_put=credit_to_width_put,
        target_pct=target_pct, profit_target_debit=profit_target_debit,
        contracts=contracts, multiplier=multiplier, capital_at_risk=capital_at_risk,
        warnings=warnings,
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


def fetch_chain_snapshot(options_chain, symbol: str = "SPY") -> ChainSnapshot:
    """Build a ChainSnapshot from the live OptionsChain (options.py),
    fetching bid/ask/delta for every strike in today's 0DTE expiration
    via the same batch-quote REST call _select_by_delta already uses.
    Not yet exercised against a real connection in this session --
    build_iron_butterfly itself is fully tested against synthetic data
    (test_iron_butterfly.py); this is the thin live-data adapter."""
    exp_data = options_chain.get_0dte_strikes(symbol)
    if not exp_data:
        raise IronButterflyError(f"Could not fetch a 0DTE chain for {symbol}.")
    strikes = exp_data.get("strikes") or exp_data.get("items") or []
    if not strikes:
        raise IronButterflyError(f"0DTE chain for {symbol} has no strikes.")

    def strike_price(s):
        val = s.get("strike-price") or s.get("strikePrice") or s.get("strike_price") or 0
        return float(val) if val else 0.0

    call_symbols, put_symbols = [], []
    by_call_sym, by_put_sym = {}, {}
    for s in strikes:
        px = strike_price(s)
        call_sym = s.get("call") or s.get("call-streamer-symbol")
        put_sym = s.get("put") or s.get("put-streamer-symbol")
        if call_sym:
            call_symbols.append(call_sym)
            by_call_sym[call_sym] = px
        if put_sym:
            put_symbols.append(put_sym)
            by_put_sym[put_sym] = px

    quotes = options_chain._get_batch_quotes(call_symbols + put_symbols)

    underlying_price = options_chain.get_equity_mid_price(symbol)
    if underlying_price is None:
        raise IronButterflyError(f"Could not get a current price for {symbol}.")

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
        f"Reward:risk:           {r.reward_to_risk:.3f}",
        f"Credit:width (call):   {r.credit_to_width_call:.3f}",
        f"Credit:width (put):    {r.credit_to_width_put:.3f}",
        f"Profit target ({r.target_pct:.0%}): close for a debit of ${r.profit_target_debit:.2f}/share",
    ]
    if r.warnings:
        lines.append("")
        lines.extend(r.warnings)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and price a 0DTE SPY iron butterfly.")
    parser.add_argument("--input", required=True, help="JSON chain snapshot file (see chain_snapshot_from_dict).")
    parser.add_argument("--target-pct", type=float, default=0.25, help="Profit target as a fraction of net credit (default 0.25).")
    parser.add_argument("--allow-far-wings", action="store_true", help="Warn instead of abort when a wing's delta is >0.03 from 0.10.")
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--multiplier", type=int, default=100)
    args = parser.parse_args(argv)

    with open(args.input) as f:
        chain = chain_snapshot_from_dict(json.load(f))

    try:
        result = build_iron_butterfly(
            chain, allow_far_wings=args.allow_far_wings, target_pct=args.target_pct,
            contracts=args.contracts, multiplier=args.multiplier,
        )
    except IronButterflyError as e:
        print(f"ABORTED: {e}", file=sys.stderr)
        return 1

    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
