# Tastytrade API Configuration
# Copy this file to config.py and fill in your credentials.
# config.py is gitignored and must NEVER be committed.

# Tastytrade sandbox credentials (for testing orders without real money)
USERNAME = "your_tastytrade_username"
PASSWORD = "your_tastytrade_password"
ACCOUNT_NUMBER = "your_account_number"

# OAuth 2.0 credentials (production -- refresh token never expires)
OAUTH_CLIENT_SECRET = "your_oauth_client_secret"
OAUTH_REFRESH_TOKEN = "your_oauth_refresh_token"

# API URLs
BASE_URL = "https://api.cert.tastyworks.com"  # Used for orders/account (sandbox)
PRICING_URL = "https://api.tastyworks.com"    # Used for options chain data (production)
PRODUCTION_URL = "https://api.tastyworks.com"  # switch BASE_URL to this when going live

# Account Streamer WebSocket (position/order fills)
ACCOUNT_STREAMER_URL      = "wss://streamer.cert.tastyworks.com"  # sandbox
ACCOUNT_STREAMER_PROD_URL = "wss://streamer.tastyworks.com"       # production

# Strategy Parameters
SYMBOL = "SPY"       # considered switching to SPX 2026-10-27 (cash-settled, no assignment risk,
                      # 1256 tax treatment) but a live check that day found it needs ~3x the capital
                      # per contract ($2,855 max loss vs SPY's $875.78 for 6 contracts) and wider
                      # absolute spreads (worse execution for the penny-chase entry) -- stayed on SPY.
STRIKES_OTM = 1      # fallback only -- unused once target_delta is passed to select_strike
TARGET_DELTA = 0.50  # fallback default for options.py's select_strike(); condor calls override this

# -- 0DTE Iron BUTTERFLY spec, given directly by the user 2026-10-26,
#    superseding the looser "iron condor" spec from 2026-10-22 (OTM
#    short strangle + fixed-width wings -- see spy_0dte_strat project
#    memory "0DTE Iron Condor Spec", now marked superseded). The short
#    strike here is ATM (nearest the underlying price), NOT delta-
#    targeted -- only the wings are delta-selected. --
CONDOR_WING_TARGET_DELTA = 0.10      # |delta| target for both long wings, selected independently --
                                      # select_wing() never picks under this, picks closest available
                                      # at or above it (user 2026-10-27: "I don't want to get below 10
                                      # and I want the closest strike to 10" -- no separate distance
                                      # tolerance/abort on top of that, removed same day)
CONDOR_PROFIT_TARGET_PCT = 0.25      # close at this fraction of net credit captured (user's default)
CONDOR_ENTRY_HOUR = 10        # single daily entry time (ET), user-set 2026-10-27 --
CONDOR_ENTRY_MINUTE = 0       # matches tastylive's own 0DTE iron fly research
                              # (10:00-10:30am ET window tested best, see project memory)
CONDOR_BACKSTOP_HOUR = 14     # force-close by this time regardless of P&L --
CONDOR_BACKSTOP_MINUTE = 30   # never ride into the final-hour gamma/pin window

# Defend: roll the untested side closer for extra credit when breakeven is
# breached (confirmed), once per day; full close if the rolled side is
# later breached too -- see config.py for the full rationale.
CONDOR_DEFEND_ENABLED = False
CONDOR_DEFEND_CONFIRM_POLLS = 4
CONDOR_ROLL_SHORT_TARGET_DELTA = 0.30
CONDOR_MAX_UNTESTED_ROLLS_PER_DAY = 1

# Force-exit when VIX, after dipping below its own previous close post-entry,
# recrosses back above it -- see config.py for the full rationale (2026-11-05
# analysis). Defaults off here, same pattern as CONDOR_DEFEND_ENABLED above.
CONDOR_VIX_RECROSS_EXIT_ENABLED = False

# Skip entry entirely when VIX is already up this much (day-change vs its own
# prior settled close) at the entry poll -- see config.py for the full
# rationale (2026-11-06).
CONDOR_VIX_ENTRY_SKIP_THRESHOLD_PCT = 5.0

# Capital / position sizing -- sized off the structure's OWN defined max loss
# (iron_butterfly.py's IronButterflyResult.max_loss), not premium paid
CAPITAL_PER_SYMBOL = 1000.0
MAX_TRADE_CAPITAL_PCT = 0.98

# Email Configuration (end-of-day reports via Gmail App Password)
EMAIL_ENABLED = True
EMAIL_FROM = "your_email@gmail.com"
EMAIL_TO = "your_email@gmail.com"
EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"  # Gmail App Password, NOT your regular password

# Slack alerting (optional)
ALERT_SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Slack bot (slack_bot.py) -- interactive status/history/close commands.
# Needs a Slack App with Socket Mode enabled (separate from the Incoming
# Webhook above): a bot token (xoxb-...), an app-level token (xapp-...,
# needs the connections:write scope), and two Slash Commands registered
# on that app -- "/premium-seller" and "/prem" -- both pointed at Socket
# Mode (no Request URL needed). SLACK_ALLOWED_USER_IDS gates every
# command: empty means every command is denied, not "anyone can use it."
SLACK_APP_TOKEN = "xapp-your-app-level-token"
SLACK_BOT_TOKEN = "xoxb-your-bot-token"
SLACK_ALLOWED_USER_IDS = ["UYOUR_SLACK_USER_ID"]

# Order Execution -- KEEP FALSE until ready for live trading
EXECUTE_ORDERS = False
BALANCE_CHECK_ENABLED = EXECUTE_ORDERS

# Order submission retry
ORDER_POST_MAX_RETRIES      = 3
ORDER_POST_BACKOFF_BASE_SEC = 0.5
ORDER_POST_BACKOFF_MAX_SEC  = 8.0
ORDER_FILL_TIMEOUT_SEC      = 10

# Entry limit-chase (IOC, stepping the net credit down 1 tick per miss) --
# same shape as spy_0dte_strat's single-leg LIMIT_CHASE_*, confirmed live
# 2026-10-27 that IOC is accepted on a 4-leg combo order (self-cancels
# cleanly in ~0.4s with zero fills when non-marketable). Direction is
# opposite spy_0dte_strat's buy-side walk: this GIVES UP CREDIT (walks
# down), since premium_seller is selling net premium to open.
ENTRY_CHASE_STEP               = 0.01  # move net credit by this per miss (user: "it has to be 1 cent")
ENTRY_CHASE_SETTLE_TIMEOUT_SEC = 2.0   # bound on how long one IOC attempt is given to settle
ENTRY_CHASE_SETTLE_POLL_SEC    = 0.1
ENTRY_CHASE_MAX_WALK_TICKS     = 50    # never give up more than 50c of credit total
ENTRY_CHASE_ABORT_MISS_STREAK  = 15    # abort the chase after this many consecutive misses

# Streamer Health / Emergency Close
EMERGENCY_CLOSE_TIMEOUT = 60

# Market Hours
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0
