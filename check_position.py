"""
check_position.py -- on-demand live P&L for whatever premium_seller
position is actually open right now. Added 2026-10-27 on request
("If I want to know P/l at any given point can I do that?") -- the
answer was no: the open CondorPosition only lives in the memory of the
running run_trade_day.py process, nothing persists it to disk, so
every prior check this session was a one-off script hand-built from
log-file order IDs. This derives everything fresh from the broker's
own position records instead (ground truth), not from any local
record of what premium_seller thinks it did.

Verified live 2026-10-27 against a real (tiny, single-leg, immediately
closed) sandbox position to confirm the /positions response shape --
in particular quantity-direction ("Long"/"Short") and average-open-price,
which this script's P&L math depends on.
"""
import logging
import sys
from typing import Dict, List, Optional

import requests

from streamer import TastytradeAuth
from options import create_production_options_chain
from config import BASE_URL, ACCOUNT_NUMBER, SYMBOL
import fees as _fees

logging.basicConfig(level=logging.WARNING)


def fetch_open_option_positions(session_token: str, symbol: str = SYMBOL) -> List[dict]:
    """Every currently open option position on `symbol`, straight from
    the broker -- not filtered to "legs premium_seller thinks it owns,"
    since nothing on disk records that. If something unexpected is
    open, this will show it rather than hide it."""
    resp = requests.get(
        f"{BASE_URL}/accounts/{ACCOUNT_NUMBER}/positions",
        headers={"Authorization": session_token, "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("data", {}).get("items", [])
    return [
        p for p in items
        if p.get("instrument-type") == "Equity Option" and p.get("underlying-symbol") == symbol
    ]


def fetch_account_balance(session_token: str) -> Optional[dict]:
    """Live account snapshot straight from the broker (sandbox) --
    net-liq, cash, and derivative buying power. Added 2026-10-29 for
    slack_commands.py's "balance" command; no caller derives sizing
    from this, it's informational only."""
    resp = requests.get(
        f"{BASE_URL}/accounts/{ACCOUNT_NUMBER}/balances",
        headers={"Authorization": session_token, "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    if not data:
        return None
    return {
        "net_liq": float(data.get("net-liquidating-value", 0)),
        "cash_balance": float(data.get("cash-balance", 0)),
        "buying_power": float(data.get("derivative-buying-power", 0)),
    }


def leg_pnl(position: dict, current_mid: float) -> float:
    """Dollar P&L for one leg at the given current mid price. Long
    profits as price rises, Short profits as price falls -- same
    economics as condor_order_executor.py's mid_debit_to_close, just
    derived from the broker's own average-open-price instead of this
    project's own entry_net_credit record."""
    quantity = int(position["quantity"])
    multiplier = float(position["multiplier"])
    avg_open = float(position["average-open-price"])
    if position["quantity-direction"] == "Long":
        return (current_mid - avg_open) * quantity * multiplier
    return (avg_open - current_mid) * quantity * multiplier


def compute_position_pnl(positions: List[dict], chain) -> dict:
    """Shared by main()'s CLI output and slack_commands.py's "positions"/
    "pnl" commands, so the two can never disagree about the same broker
    data -- added 2026-10-28 when the Slack bot needed this same
    aggregation. Returns {"legs": [...], "gross_pnl", "round_trip_fees",
    "net_pnl", "any_missing_quote"}; each leg dict carries {"symbol",
    "direction", "quantity", "open_price", "mid" (None if no live quote),
    "pnl" (None to match)}."""
    symbols = [p["symbol"] for p in positions]
    quotes = chain._get_batch_quotes(symbols)

    legs = []
    gross_pnl = 0.0
    contracts: Optional[int] = None
    any_missing_quote = False
    for p in positions:
        sym = p["symbol"]
        q = quotes.get(sym)
        if not q or q.get("bid") is None or q.get("ask") is None:
            legs.append({
                "symbol": sym, "direction": p["quantity-direction"], "quantity": int(p["quantity"]),
                "open_price": float(p["average-open-price"]), "mid": None, "pnl": None,
            })
            any_missing_quote = True
            continue
        mid = (float(q["bid"]) + float(q["ask"])) / 2.0
        pnl = leg_pnl(p, mid)
        gross_pnl += pnl
        contracts = int(p["quantity"]) if contracts is None else contracts
        legs.append({
            "symbol": sym, "direction": p["quantity-direction"], "quantity": int(p["quantity"]),
            "open_price": float(p["average-open-price"]), "mid": mid, "pnl": pnl,
        })

    round_trip_fees = _fees.round_trip_fees(contracts) if contracts else 0.0
    return {
        "legs": legs, "gross_pnl": gross_pnl, "round_trip_fees": round_trip_fees,
        "net_pnl": gross_pnl - round_trip_fees, "any_missing_quote": any_missing_quote,
    }


def main() -> int:
    auth = TastytradeAuth()
    if not auth.login():
        print("Sandbox login failed.")
        return 1

    positions = fetch_open_option_positions(auth.session_token)
    if not positions:
        print(f"No open {SYMBOL} option positions right now.")
        return 0

    chain = create_production_options_chain()
    if chain is None:
        print("Production login failed -- can't fetch live quotes.")
        return 1

    result = compute_position_pnl(positions, chain)

    print(f"{len(positions)} open {SYMBOL} option leg(s):\n")
    for leg in result["legs"]:
        if leg["mid"] is None:
            print(f"  {leg['symbol']}: no live quote available -- excluded from P&L")
            continue
        print(
            f"  {leg['direction']:5s} {leg['quantity']:>3}x {leg['symbol']}  "
            f"opened @ ${leg['open_price']:.2f}  now mid ${leg['mid']:.2f}  "
            f"P&L ${leg['pnl']:+.2f}"
        )

    if result["any_missing_quote"]:
        print("\n(Some legs had no live quote -- totals below are partial.)")

    print(f"\nGross P&L:              ${result['gross_pnl']:+.2f}")
    print(f"Round-trip fees (est.): -${result['round_trip_fees']:.2f}")
    print(f"Net P&L:                ${result['net_pnl']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
