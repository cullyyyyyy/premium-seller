"""
run_trade_day.py -- the standalone entry point for one real trading
day. Meant to be invoked by an OS-level scheduler (Windows Task
Scheduler) at CONDOR_ENTRY_HOUR:MINUTE ET, not by this chat session --
see project memory for why: a scheduled prompt in this session only
fires if the session stays open overnight, which isn't a real
guarantee. This script is a real, independent process.

Does exactly what every scratchpad test script this project has run
today did, cleaned up into one place:
  1. Log in to SANDBOX (order submission) and PRODUCTION (chain/quote
     data -- sandbox has no live market data, confirmed 2026-10-26).
  2. Set EXECUTE_ORDERS=True for this process only -- config.py on
     disk stays False, this script is the one place that flips it.
  3. Run daily_loop.run_day() for real.
  4. Any unhandled exception is caught at the top level and sent as a
     CRITICAL alert (alerts.notify_crash) before re-raising -- there's
     still no crash-recovery mechanism (a crash mid-day still leaves
     whatever position was open unmanaged), but a crash is no longer
     SILENT. That gap is real; this is a mitigation, not a fix.

Logs to logs/trade_day_<date>.log as well as stdout, since this runs
unattended and nobody is watching a terminal.
"""
import asyncio
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _setup_logging() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"trade_day_{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )


async def _main() -> int:
    from streamer import TastytradeAuth
    from options import create_production_options_chain
    import condor_order_executor as coe
    import daily_loop as dl
    import alerts

    log = logging.getLogger("run_trade_day")

    log.info("Logging in to sandbox (order submission)...")
    sandbox_auth = TastytradeAuth()
    if not sandbox_auth.login():
        log.error("Sandbox login failed -- aborting.")
        alerts.notify_alert("\U0001F6A8 run_trade_day: sandbox login FAILED, no trade attempted today.")
        return 1

    log.info("Logging in to production (chain/quote data)...")
    chain_api = create_production_options_chain()
    if chain_api is None:
        log.error("Production login failed -- aborting.")
        alerts.notify_alert("\U0001F6A8 run_trade_day: production login FAILED, no trade attempted today.")
        return 1

    coe.EXECUTE_ORDERS = True
    log.warning("EXECUTE_ORDERS=True for this process -- real orders will be submitted to the sandbox.")

    executor = coe.CondorOrderExecutor(session_token=sandbox_auth.session_token, auth=sandbox_auth)
    position = await dl.run_day(chain_api, executor)

    if position is None:
        log.info("No trade today.")
        return 0

    log.info(
        "Day complete: status=%s reason=%s exit_debit=%s realized_pnl=%s",
        position.status, position.exit_reason, position.exit_debit, position.realized_pnl,
    )
    return 0


def main() -> int:
    _setup_logging()
    log = logging.getLogger("run_trade_day")
    try:
        return asyncio.run(_main())
    except Exception as e:
        log.error("UNHANDLED EXCEPTION: %s\n%s", e, traceback.format_exc())
        try:
            import alerts
            alerts.notify_crash(f"{type(e).__name__}: {e}")
        except Exception:
            log.error("Could not even send the crash alert.", exc_info=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
