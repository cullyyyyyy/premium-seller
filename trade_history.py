"""
trade_history.py -- day-by-day trade history, parsed straight from the
unstructured trade_day_<date>.log files run_trade_day.py already writes.
No structured trade_history.jsonl exists yet (flagged 2026-10-28, not yet
built) -- this is the "query the logs on demand" version instead, built
for slack_commands.py's "history" command.

Pure text parsing, no network/broker calls, so it's cheap to call on
every Slack "history" request and easy to unit test with real log text.
"""
import re
from pathlib import Path
from typing import List, Optional

LOG_DIR = Path(__file__).parent / "logs"

_ENTRY_RE = re.compile(
    r"Entered (\d+)x short \$([\d.]+) straddle \| net credit \$([\d.]+)/share"
    r"(?: \| max loss \$([\d.]+)/share)?"
    r"(?: \| put wing \$([\d.]+) \(side max loss \$([\d.]+)/share\)"
    r" \| call wing \$([\d.]+) \(side max loss \$([\d.]+)/share\))?"
)
_EXIT_RE = re.compile(
    r"Exited \(([^)]+)\): debit=\$([\d.]+)/share, realized P&L=\$(-?[\d.]+)"
)
_NO_ENTRY_RE = re.compile(r"\[CONDOR\] (?:Sized to 0 contracts|Could not build|Skipping entry)")
_LOGIN_FAIL_RE = re.compile(r"(?:Sandbox|Production) login FAILED")


def parse_trade_day_log(path: Path) -> dict:
    """One day's outcome from its trade_day_<date>.log text. Always
    returns a dict with at least {date, status} -- status is "closed",
    "open" (entered today, no exit logged yet -- e.g. mid-day, or a
    crash left it unmanaged), "no_entry" (ran, chose not to trade), or
    "no_run" (never got past login -- the log says so explicitly, or
    the file is otherwise unreadable)."""
    date = path.stem.replace("trade_day_", "")
    try:
        text = path.read_text()
    except OSError as e:
        return {"date": date, "status": "no_run", "detail": f"log unreadable: {e}"}

    entry_m = _ENTRY_RE.search(text)
    exit_m = _EXIT_RE.search(text)

    record = {"date": date}
    if entry_m:
        record.update(
            contracts=int(entry_m.group(1)),
            short_strike=float(entry_m.group(2)),
            net_credit=float(entry_m.group(3)),
        )
        if entry_m.group(4):
            record["max_loss_per_share"] = float(entry_m.group(4))
        # Wing strikes/per-side max loss -- added 2026-10-30 after the binding
        # side's single max_loss figure (above) turned out to be ambiguous:
        # max_loss = max(call_side, put_side), with no record of WHICH side
        # bound, so a tested side's wing width couldn't be reconstructed from
        # it even on a day where that side lost (see the "wing width"
        # conversation -- deriving put_wing_width from the binding max_loss
        # silently assumed put was binding, which isn't always true). Absent
        # on any log written before this change.
        if entry_m.group(5):
            record["put_wing_strike"] = float(entry_m.group(5))
            record["put_side_max_loss"] = float(entry_m.group(6))
            record["call_wing_strike"] = float(entry_m.group(7))
            record["call_side_max_loss"] = float(entry_m.group(8))
        if "net_credit" in record:
            record["breakeven_lower"] = record["short_strike"] - record["net_credit"]
            record["breakeven_upper"] = record["short_strike"] + record["net_credit"]
    if exit_m:
        # daily_loop.py's resting-order exit path logs "(profit_target,
        # resting order)" -- the comma-suffixed note, not just a bare
        # \w+ reason -- so normalize to the leading token here rather
        # than leak that phrasing into history/total output.
        exit_reason = exit_m.group(1).split(",")[0].strip()
        record.update(
            status="closed",
            exit_reason=exit_reason,
            exit_debit=float(exit_m.group(2)),
            realized_pnl=float(exit_m.group(3)),
        )
        # capital_at_risk: max-loss/share (the binding side, already
        # max()'d between call/put wings in iron_butterfly.py) * 100 *
        # contracts -- the sizing-side dollar figure, kept for reference.
        # Omitted when an older/short-form entry line didn't log a
        # max-loss figure, rather than guessing at a denominator.
        if "max_loss_per_share" in record:
            capital_at_risk = record["max_loss_per_share"] * 100 * record["contracts"]
            if capital_at_risk:
                record["capital_at_risk"] = capital_at_risk
        # pnl_pct_of_credit: the percent gain/loss actually reported
        # (history, status) -- realized P&L as a fraction of the
        # premium COLLECTED (net_credit * 100 * contracts), not of the
        # structural max-loss figure above. User correction 2026-11-06:
        # a day that nets $137.04 against $548 collected is a 25% win,
        # not the ~18% capital_at_risk-basis number this used to report.
        if "net_credit" in record:
            net_credit_total = record["net_credit"] * 100 * record["contracts"]
            if net_credit_total:
                record["net_credit_total"] = net_credit_total
                record["pnl_pct_of_credit"] = record["realized_pnl"] / net_credit_total * 100
        return record
    if entry_m:
        record["status"] = "open"
        return record
    if _LOGIN_FAIL_RE.search(text):
        return {"date": date, "status": "no_run", "detail": "login failed"}
    if _NO_ENTRY_RE.search(text):
        return {"date": date, "status": "no_entry"}
    return {"date": date, "status": "no_run", "detail": "no entry/exit/login-failure line found"}


def build_history(log_dir: Optional[Path] = None, limit: Optional[int] = 10) -> List[dict]:
    """Most recent `limit` trading days, oldest first (so a Slack message
    reads top-to-bottom as a timeline). Sorted by the date in the
    filename, not file mtime -- a log can be rewritten/re-touched
    without changing which trading day it represents. `limit=None`
    returns every logged day (used by the "total" command's cumulative
    sum, where truncating to 10 would silently under-count)."""
    log_dir = log_dir or LOG_DIR
    paths = sorted(log_dir.glob("trade_day_*.log"))
    selected = paths if limit is None else paths[-limit:]
    return [parse_trade_day_log(p) for p in selected]
