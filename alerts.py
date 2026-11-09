"""
alerts.py -- operational alerts to Slack, ported from spy_0dte_strat's
slack_notifier.py (same fire-and-forget pattern: a failed POST is
logged as a warning and never raises, so a Slack outage can't affect
order execution or crash an unattended run).

Built 2026-10-27 specifically to close the alerting gap found while
testing daily_loop.py live: the backstop exit retry can exhaust its
attempts and leave a position open with nobody told. Every call site
in daily_loop.py that used to just log an error now also alerts.
"""
import json
import logging
import urllib.request
from typing import Optional

from config import ALERT_SLACK_WEBHOOK_URL

_log = logging.getLogger(__name__)


def _post(text: str) -> None:
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            ALERT_SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        _log.warning("Slack notification failed: %s", exc)


def notify_alert(text: str) -> None:
    """Post a plain-text operational alert. Fire-and-forget -- never raises."""
    _post(text)


def notify_entry(short_strike: float, contracts: int, net_credit: float, capital_at_risk: float) -> None:
    _post(
        f"\U0001F7E2 *Iron butterfly entered* · short ${short_strike:.2f} · {contracts}ct\n"
        f"  Net credit: ${net_credit * 100 * contracts:.2f}  ·  Capital at risk: ${capital_at_risk:.2f}"
    )


def notify_no_entry(reason: str) -> None:
    _post(f"⚪ *No trade today* · {reason}")


def notify_exit(reason: str, exit_debit: float, realized_pnl: float,
                 net_credit: Optional[float] = None) -> None:
    """net_credit: total premium collected at entry (entry_net_credit *
    multiplier * contracts), dollars -- the percentage is realized P&L
    as a fraction of what was actually collected, not of the structural
    max-loss/capital_at_risk figure (user correction 2026-11-06: a day
    that nets $137.04 against $548 collected is a 25% win, not ~18%)."""
    icon = "✅" if realized_pnl > 0 else "❌" if realized_pnl < 0 else "➖"
    sign = "+" if realized_pnl >= 0 else ""
    pct_str = f" ({sign}{realized_pnl / net_credit * 100:.1f}%)" if net_credit else ""
    _post(
        f"{icon} *Iron butterfly closed* · {reason} · debit ${exit_debit:.2f}/share\n"
        f"  Net P&L: *{sign}${realized_pnl:.2f}*{pct_str}"
    )


def notify_roll(side: str, new_short_strike: float, new_wing_strike: float,
                 roll_net_credit: float, total_net_credit: float) -> None:
    _post(
        f"\U0001F504 *Rolled untested {side} side* closer for extra credit\n"
        f"  New short ${new_short_strike:.2f} / wing ${new_wing_strike:.2f}  ·  "
        f"Roll credit: ${roll_net_credit:.2f}/share  ·  Total net credit now: ${total_net_credit:.2f}/share"
    )


def notify_roll_failed(side: str, reason: str) -> None:
    _post(f"⚠️ *Untested {side} side roll did not go through* · {reason}")


def notify_backstop_failure(attempts: int, last_price_override: float) -> None:
    """CRITICAL -- the exact gap found live 2026-10-26: the backstop exit
    retry exhausted all attempts and the position is still open, with
    nothing else watching it. This is the one alert that must never be
    silently skipped."""
    _post(
        f"\U0001F6A8 *CRITICAL: backstop exit FAILED to close after {attempts} attempts* "
        f"(last price_override=${last_price_override:.2f}) · "
        f"POSITION IS STILL OPEN · manual intervention needed NOW"
    )


def notify_crash(error: str) -> None:
    """CRITICAL -- the run crashed with an unhandled exception. There is
    no crash-recovery mechanism yet, so this is the only signal anyone
    gets that the day's position (if one was open) is unmanaged."""
    _post(f"\U0001F6A8 *CRITICAL: premium_seller run crashed* · {error}")
