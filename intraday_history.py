"""
intraday_history.py -- per-15-second P&L/SPY-price snapshots from
logs/intraday_pnl_<date>.jsonl, parsed on demand for slack_commands.py's
"trend" command. Mirrors trade_history.py's read-only, pure-parsing
shape (no network/broker calls), so it's cheap to call on every Slack
request and easy to unit test with real log text.
"""
import json
from pathlib import Path
from typing import List, Optional

LOG_DIR = Path(__file__).parent / "logs"


def load_intraday(date: str, log_dir: Optional[Path] = None) -> List[dict]:
    """Every snapshot row logged for `date` (YYYY-MM-DD), oldest first.
    Empty list if the file doesn't exist or has no rows -- never raises
    on a missing/empty day, same contract as trade_history.build_history."""
    log_dir = log_dir or LOG_DIR
    path = log_dir / f"intraday_pnl_{date}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def summarize(records: List[dict]) -> Optional[dict]:
    """{"first", "last", "low", "high"} snapshot rows -- low/high picked
    by net_pnl_mark. None if there's nothing to summarize (no rows)."""
    if not records:
        return None
    low = min(records, key=lambda r: r["net_pnl_mark"])
    high = max(records, key=lambda r: r["net_pnl_mark"])
    return {"first": records[0], "last": records[-1], "low": low, "high": high}
