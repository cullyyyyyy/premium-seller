"""
Slack-command response logic for slack_bot.py -- deliberately separated
from the slack_bolt wiring itself (mirrors covered_call_weekly's own
slack_bot.py/slack_commands.py split) so this module never imports
slack_bolt and can be unit-tested with zero Slack SDK involved.

SAFETY BOUNDARY -- read this before adding anything here. Per explicit
user scope decision 2026-10-28, this bot is READ-ONLY STATUS/HISTORY
PLUS EXACTLY ONE TRADING ACTION: a manual override close of whatever's
open right now ("close"). That is a DELIBERATE, NARROWER exception than
covered_call_weekly's bot (which is structurally barred from any trading
function at all) -- not a precedent to widen later without the same kind
of explicit, scoped decision. "close" reaches manual_close.py and
nothing else does; this module must NEVER import or call anything from
daily_loop.py or run_trade_day.py (the autonomous scheduled trading
loop) -- that loop's own process is the only thing that may drive entry
decisions, sizing, or the profit-target/backstop monitoring cycle. See
test_slack_commands.py::TestSafetyBoundary, which scans this file's own
source text for those names and fails if any ever appear.

This module may import ONLY:
  check_position   (TastytradeAuth re-export, fetch_open_option_positions,
                     compute_position_pnl, create_production_options_chain,
                     fetch_account_balance)
  trade_history     (build_history) -- parses trade_day_<date>.log
  intraday_history  (load_intraday, summarize) -- parses intraday_pnl_<date>.jsonl
  manual_close      (close_open_position_now) -- the one trading action
  condor_order_executor (CondorOrderExecutor) -- to construct a live
                     executor for manual_close; never to call submit_entry/
                     submit_resting_exit/_chase_entry directly from here
  fees, config      -- static schedule/constants only, to explain (not
                     decide) a day's sizing math after the fact
"""
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import check_position
import trade_history
import intraday_history
import manual_close
import fees as _fees
from condor_order_executor import CondorOrderExecutor
from config import SYMBOL, CAPITAL_PER_SYMBOL, MAX_TRADE_CAPITAL_PCT

_ET = ZoneInfo("America/New_York")


def is_authorized(user_id: Optional[str], allowed_ids) -> bool:
    """Fail-closed: no allowlist (None or empty) means nobody is
    authorized, not everybody. True only for an exact match."""
    if not allowed_ids:
        return False
    return user_id in allowed_ids


def _login_and_fetch_positions():
    """Shared by status/positions/close -- returns (auth, positions)
    or (None, error_message) on any login failure."""
    auth = check_position.TastytradeAuth()
    if not auth.login():
        return None, "Sandbox login failed."
    positions = check_position.fetch_open_option_positions(auth.session_token)
    return auth, positions


def _today_net_credit() -> Optional[float]:
    """net_credit * 100 * contracts for TODAY's trade_day_<date>.log
    specifically (not just "the most recent log") -- same net_credit_total
    basis as history's "% of net credit" (trade_history.pnl_pct_of_credit).
    None if today has no log yet, or it's an older-format entry line
    without a logged net credit (same graceful-omit pattern as that one).

    User correction 2026-11-06: percent gain/loss is reported against the
    premium actually COLLECTED (net credit), not the structural max-loss/
    capital_at_risk figure -- a day that nets $137.04 against $548
    collected is a 25% win, not the ~18% capital_at_risk-basis number
    this used to compute."""
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    record = trade_history.parse_trade_day_log(trade_history.LOG_DIR / f"trade_day_{today}.log")
    contracts = record.get("contracts")
    net_credit = record.get("net_credit")
    if contracts is None or net_credit is None:
        return None
    return net_credit * 100 * contracts


def build_status_response() -> str:
    auth, positions = _login_and_fetch_positions()
    if auth is None:
        return positions  # the error message, per _login_and_fetch_positions

    if not positions:
        return f"No open {SYMBOL} position right now."

    chain = check_position.create_production_options_chain()
    if chain is None:
        return f"{len(positions)} open {SYMBOL} leg(s), but production login failed -- can't fetch live quotes."

    result = check_position.compute_position_pnl(positions, chain)
    partial = " _(partial -- a leg is missing a quote)_" if result["any_missing_quote"] else ""

    net_credit = _today_net_credit()
    pct_str = f" ({result['net_pnl'] / net_credit * 100:+.1f}% of net credit)" if net_credit else ""

    return (
        f"*{SYMBOL} position:* OPEN, {len(positions)} leg(s){partial}\n"
        f"*Gross P&L:* ${result['gross_pnl']:+,.2f}\n"
        f"*Round-trip fees (est.):* ${result['round_trip_fees']:.2f}\n"
        f"*Net P&L:* ${result['net_pnl']:+,.2f}{pct_str}"
    )


def build_positions_response() -> str:
    auth, positions = _login_and_fetch_positions()
    if auth is None:
        return positions

    if not positions:
        return f"No open {SYMBOL} position right now."

    chain = check_position.create_production_options_chain()
    if chain is None:
        return "Production login failed -- can't fetch live quotes."

    result = check_position.compute_position_pnl(positions, chain)
    lines = [f"*{len(positions)} open {SYMBOL} leg(s):*"]
    for leg in result["legs"]:
        if leg["mid"] is None:
            lines.append(f"• {leg['symbol']}: no live quote available")
            continue
        lines.append(
            f"• {leg['direction']} {leg['quantity']}x {leg['symbol']} -- "
            f"opened @ ${leg['open_price']:.2f}, now ${leg['mid']:.2f}, P&L ${leg['pnl']:+.2f}"
        )
    return "\n".join(lines)


def build_history_response(limit: int = 10) -> str:
    records = trade_history.build_history(limit=limit)
    if not records:
        return "No trade_day_*.log files found yet."

    lines = [f"*Last {len(records)} trading day(s):*"]
    for r in records:
        date = r["date"]
        if r["status"] == "closed":
            icon = "✅" if r["realized_pnl"] > 0 else "❌" if r["realized_pnl"] < 0 else "➖"
            pct = r.get("pnl_pct_of_credit")
            pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
            lines.append(f"{icon} {date}: ${r['realized_pnl']:+,.2f}{pct_str}")
        elif r["status"] == "open":
            lines.append(f"🔵 {date}: still open")
        elif r["status"] == "no_entry":
            lines.append(f"⚪ {date}: no trade")
        else:
            lines.append(f"⚠️ {date}: no run ({r.get('detail', 'unknown')})")
    return "\n".join(lines)


def build_total_response() -> str:
    records = trade_history.build_history(limit=None)
    closed = [r for r in records if r["status"] == "closed"]
    if not closed:
        return "No closed trading days yet."

    total_pnl = sum(r["realized_pnl"] for r in closed)
    wins = sum(1 for r in closed if r["realized_pnl"] > 0)
    losses = sum(1 for r in closed if r["realized_pnl"] < 0)
    flat = len(closed) - wins - losses
    record_str = f"{wins}W / {losses}L" + (f" / {flat} flat" if flat else "")

    return (
        f"*All-time ({len(closed)} closed trading day(s)):*\n"
        f"*Total realized P&L:* ${total_pnl:+,.2f}\n"
        f"*Record:* {record_str}\n"
        f"*Avg P&L per day:* ${total_pnl / len(closed):+,.2f}"
    )


def build_balance_response() -> str:
    auth = check_position.TastytradeAuth()
    if not auth.login():
        return "Sandbox login failed."
    balance = check_position.fetch_account_balance(auth.session_token)
    if balance is None:
        return "Could not fetch account balance."
    return (
        f"*Sandbox account balance:*\n"
        f"*Net liquidating value:* ${balance['net_liq']:,.2f}\n"
        f"*Cash balance:* ${balance['cash_balance']:,.2f}\n"
        f"*Buying power (derivatives):* ${balance['buying_power']:,.2f}"
    )


def _fmt_et(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S ET")


def build_trend_response() -> str:
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    summary = intraday_history.summarize(intraday_history.load_intraday(today))
    if summary is None:
        return f"No intraday P&L log for {today} yet."

    first, last, low, high = summary["first"], summary["last"], summary["low"], summary["high"]
    return (
        f"*Today's P&L trend ({today}), mark-to-market while open "
        f"-- see `history` for the realized exit P&L:*\n"
        f"*Entry* ({_fmt_et(first['timestamp'])}): ${first['net_pnl_mark']:+,.2f} (SPY ${first['spy_price']:.2f})\n"
        f"*Session low* ({_fmt_et(low['timestamp'])}): ${low['net_pnl_mark']:+,.2f} (SPY ${low['spy_price']:.2f})\n"
        f"*Session high* ({_fmt_et(high['timestamp'])}): ${high['net_pnl_mark']:+,.2f} (SPY ${high['spy_price']:.2f})\n"
        f"*Last mark* ({_fmt_et(last['timestamp'])}): ${last['net_pnl_mark']:+,.2f} (SPY ${last['spy_price']:.2f})"
    )


def build_structure_response() -> str:
    records = trade_history.build_history(limit=1)
    if not records:
        return "No trade_day_*.log files found yet."

    r = records[-1]
    date = r["date"]
    contracts = r.get("contracts")
    short_strike = r.get("short_strike")
    net_credit = r.get("net_credit")
    if contracts is None or short_strike is None or net_credit is None:
        return f"{date}: no trade entered, or structure couldn't be parsed from the log."

    max_gain_gross = net_credit * 100 * contracts
    lines = [
        f"*{date} structure:* {contracts}x short ${short_strike:.2f} straddle",
        f"*Net credit:* ${net_credit:.2f}/share (${max_gain_gross:,.2f} gross max gain)",
    ]

    max_loss_per_share = r.get("max_loss_per_share")
    if max_loss_per_share is not None:
        max_loss_gross = max_loss_per_share * 100 * contracts
        round_trip = _fees.round_trip_fees(contracts)
        breakeven_lower = short_strike - net_credit
        breakeven_upper = short_strike + net_credit
        lines += [
            f"*Max loss (binding side):* ${max_loss_per_share:.2f}/share (${max_loss_gross:,.2f} gross)",
            f"*Net of ~${round_trip:.2f} fees:* max gain ${max_gain_gross - round_trip:,.2f} "
            f"/ max loss ${max_loss_gross + round_trip:,.2f}",
            f"*Breakeven range:* ${breakeven_lower:.2f} - ${breakeven_upper:.2f}",
        ]
    else:
        lines.append("_(older log format -- max loss/breakeven not available for this day.)_")

    if r["status"] == "closed":
        lines.append(f"*Actual outcome:* exited ({r['exit_reason']}), realized P&L ${r['realized_pnl']:+,.2f}")
    else:
        lines.append("*Status:* still open")
    return "\n".join(lines)


def build_sizing_response() -> str:
    records = trade_history.build_history(limit=1)
    if not records:
        return "No trade_day_*.log files found yet."

    r = records[-1]
    date = r["date"]
    contracts = r.get("contracts")
    max_loss_per_share = r.get("max_loss_per_share")
    if contracts is None or max_loss_per_share is None:
        return f"{date}: sizing detail not available for this day's log format."

    # Mirrors daily_loop.py's contracts_for() math exactly, recomputed
    # here after the fact to explain the day's sizing -- never imports
    # daily_loop.py itself (see this module's safety-boundary docstring).
    budget = CAPITAL_PER_SYMBOL * MAX_TRADE_CAPITAL_PCT
    gross_per_contract = max_loss_per_share * 100
    fees_used = _fees.round_trip_fees(contracts)
    total_used = contracts * gross_per_contract + fees_used
    remaining = budget - total_used

    next_contracts = contracts + 1
    next_total = next_contracts * gross_per_contract + _fees.round_trip_fees(next_contracts)
    over_by = next_total - budget

    lines = [
        f"*{date} sizing:* {contracts} contract(s)",
        f"*Budget:* ${CAPITAL_PER_SYMBOL:,.2f} x {MAX_TRADE_CAPITAL_PCT:.0%} = ${budget:,.2f}",
        f"*Risk per contract:* ${max_loss_per_share:.2f}/share x 100 = ${gross_per_contract:,.2f}",
        f"*{contracts}x used:* ${total_used:,.2f} (incl. ${fees_used:.2f} fees), ${remaining:,.2f} left over",
    ]
    if over_by > 0:
        lines.append(f"*{next_contracts}x would've needed:* ${next_total:,.2f} -- ${over_by:,.2f} over budget")
    else:
        lines.append(f"*{next_contracts}x would've fit too* (${next_total:,.2f}) -- unexpected, check contracts_for().")
    return "\n".join(lines)


def trigger_close() -> str:
    """The one trading action this bot can take -- see this module's
    own docstring for the scope decision behind it. Reconstructs
    whatever's open straight from the broker (manual_close.py), never
    from any daily_loop.py state."""
    auth, positions = _login_and_fetch_positions()
    if auth is None:
        return positions

    chain = check_position.create_production_options_chain()
    if chain is None:
        return "Production login failed -- can't fetch live quotes to close with."

    executor = CondorOrderExecutor(session_token=auth.session_token, auth=auth)
    result = manual_close.close_open_position_now(executor, chain, positions, reason="manual")
    return result["message"]


def build_help_response() -> str:
    lines = [
        "*Available commands:*",
        "• `status` -- open position summary + live net P&L (default if no subcommand given)",
        "• `positions` -- per-leg detail of the open position",
        "• `history` -- last 10 trading days' entry/exit/realized P&L",
        "• `total` -- all-time realized P&L across every logged trading day",
        "• `balance` -- live sandbox account balance (net-liq, cash, buying power)",
        "• `trend` -- today's intraday P&L trend: entry/low/high/last mark, each with SPY price",
        "• `structure` -- most recent trade's strikes, net credit, max loss, breakeven range",
        "• `sizing` -- why the most recent trade was sized the way it was (budget vs. fees vs. max loss)",
        "• `close` -- *manually close the open position right now* (real order, escalates price like the backstop retry)",
        "• `help` -- this message",
        "",
        "Every command but `close` is read-only. `close` is the one deliberate exception -- "
        "it never touches the scheduled daily_loop.py cycle, only submits a real exit order "
        "for whatever's open on the broker right now.",
    ]
    return "\n".join(lines)


COMMAND_TABLE = {
    "status": {"handler": build_status_response, "kind": "slow",
               "description": "Open position summary + live net P&L"},
    "positions": {"handler": build_positions_response, "kind": "slow",
                  "description": "Per-leg detail of the open position"},
    "history": {"handler": build_history_response, "kind": "fast",
                "description": "Last 10 trading days' entry/exit/realized P&L"},
    "total": {"handler": build_total_response, "kind": "fast",
              "description": "All-time realized P&L across every logged trading day"},
    "balance": {"handler": build_balance_response, "kind": "slow",
                "description": "Live sandbox account balance (net-liq, cash, buying power)"},
    "trend": {"handler": build_trend_response, "kind": "fast",
              "description": "Today's intraday P&L trend: entry/low/high/last mark"},
    "structure": {"handler": build_structure_response, "kind": "fast",
                  "description": "Most recent trade's strikes, net credit, max loss, breakeven range"},
    "sizing": {"handler": build_sizing_response, "kind": "fast",
               "description": "Why the most recent trade was sized the way it was"},
    "close": {"handler": trigger_close, "kind": "slow",
              "description": "Manually close the open position right now (real order)"},
    "help": {"handler": build_help_response, "kind": "fast",
             "description": "List available commands"},
}


def dispatch(subcommand: str) -> str:
    """Never raises on an unrecognized subcommand -- returns help text
    instead. A genuine handler exception is the caller's (slack_bot.py's)
    responsibility to catch."""
    entry = COMMAND_TABLE.get(subcommand)
    if entry is None:
        return f"Unknown command: {subcommand!r}\n\n{build_help_response()}"
    return entry["handler"]()
