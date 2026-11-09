"""
Persistent Slack bot for premium_seller: status/history queries plus
exactly one trading action ("close" -- manual override), per explicit
user scope decision 2026-10-28. See slack_commands.py's own docstring
for the full safety-boundary explanation; that module never imports
slack_bolt and is what makes "this bot cannot reach daily_loop.py's
autonomous trading cycle" a structural fact, not just a comment here.

This file is I/O wiring ONLY (the Socket Mode connection, ack()/respond(),
per-command authorization, crash alerting) -- it holds no trading logic
and no response-formatting logic of its own, mirroring
covered_call_weekly's slack_bot.py/slack_commands.py split exactly.

Uses Socket Mode (an OUTBOUND WebSocket connection FROM this process TO
Slack) for the same reason covered_call_weekly's bot does: this runs on
a home machine behind NAT, no public URL/inbound port needed.

Reuses the SAME Slack App as covered_call_weekly's bot (same workspace,
same SLACK_BOT_TOKEN/SLACK_APP_TOKEN -- confirmed by the user 2026-10-28
rather than standing up a second Slack App) -- this just registers a
DIFFERENT slash command (/premium-seller, alias /prem) on that app. Both
bots can hold their own Socket Mode connection on the same app token at
once; Slack routes a slash-command invocation by command name, not by
connection, so there's no collision as long as the command names differ.
That slash command registration itself (api.slack.com/apps -> this app
-> Slash Commands -> New Command) is a one-time manual step outside this
code -- SEE README note left in config.example.py.

SLACK_ALLOWED_USER_IDS gates every command: empty/missing means every
command is denied (fail-closed), not "anyone in the workspace can use it."
"""
import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import alerts
import slack_commands
from config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_ALLOWED_USER_IDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("slack_bot")


def handle_command(ack, respond, command):
    """Registered as the listener for BOTH /premium-seller and /prem --
    same handler, same subcommand dispatch either way. Parameter names
    (ack/respond/command) are Bolt's own dependency-injection convention."""
    ack()  # must happen within Slack's 3-second window, before any real work

    user_id = command.get("user_id")
    if not slack_commands.is_authorized(user_id, SLACK_ALLOWED_USER_IDS):
        log.warning("unauthorized command attempt: user_id=%s text=%r", user_id, command.get("text"))
        alerts.notify_alert(
            f"\U0001F6A8 Unauthorized Slack command attempt on premium_seller: "
            f"user_id={user_id!r} tried {command.get('text')!r}"
        )
        respond("Not authorized.")
        return

    raw_text = (command.get("text") or "status").strip()
    subcommand = raw_text.split()[0].lower() if raw_text else "status"

    log.info("command received: user_id=%s subcommand=%s", user_id, subcommand)
    try:
        reply = slack_commands.dispatch(subcommand)
    except Exception:
        log.error("command failed: subcommand=%s", subcommand, exc_info=True)
        respond("Something went wrong running that -- check this process's log for details.")
        return
    log.info("command completed: subcommand=%s", subcommand)
    respond(reply)


def build_app() -> App:
    app = App(token=SLACK_BOT_TOKEN)
    app.command("/premium-seller")(handle_command)
    # Short alias, same handler -- requires /prem to ALSO be registered as
    # its own Slash Command in the Slack app config (api.slack.com/apps),
    # pointing at this same app/Socket Mode connection; this code-side
    # registration alone does nothing until that's done.
    app.command("/prem")(handle_command)
    return app


def run() -> None:
    app = build_app()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()  # blocks forever until the connection dies


def main() -> int:
    log.info("premium_seller slack_bot starting")
    try:
        run()
    except Exception as e:
        log.error("UNHANDLED EXCEPTION in slack_bot", exc_info=True)
        try:
            alerts.notify_crash(f"slack_bot.py: {type(e).__name__}: {e}")
        except Exception:
            log.error("Could not even send the crash alert.", exc_info=True)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
