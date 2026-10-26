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
SYMBOL = "SPY"
STRIKES_OTM = 1      # fallback only -- unused once target_delta is passed to select_strike
TARGET_DELTA = 0.50  # fallback default for options.py's select_strike(); condor calls override this

# -- 0DTE Iron Condor spec, locked 2026-10-22 (see spy_0dte_strat project memory
#    "0DTE Iron Condor Spec" for the full origin/reasoning) --
CONDOR_SHORT_DELTA = 0.25     # abs(delta) target for both short strikes (call and put)
CONDOR_WING_WIDTH = 2.0       # $ distance from each short strike to its long wing
CONDOR_PROFIT_TARGET_PCT = 0.50  # close at this fraction of max credit captured
CONDOR_ENTRY_HOUR = 10        # single daily entry time (ET) -- after opening vol settles
CONDOR_ENTRY_MINUTE = 30
CONDOR_BACKSTOP_HOUR = 14     # force-close by this time regardless of P&L --
CONDOR_BACKSTOP_MINUTE = 30   # never ride into the final-hour gamma/pin window

# Capital / position sizing -- sized off CONDOR_WING_WIDTH's defined max loss, not premium paid
CAPITAL_PER_SYMBOL = 1000.0
MAX_TRADE_CAPITAL_PCT = 0.98

# Email Configuration (end-of-day reports via Gmail App Password)
EMAIL_ENABLED = True
EMAIL_FROM = "your_email@gmail.com"
EMAIL_TO = "your_email@gmail.com"
EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"  # Gmail App Password, NOT your regular password

# Slack alerting (optional)
ALERT_SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Order Execution -- KEEP FALSE until ready for live trading
EXECUTE_ORDERS = False
BALANCE_CHECK_ENABLED = EXECUTE_ORDERS

# Order submission retry
ORDER_POST_MAX_RETRIES      = 3
ORDER_POST_BACKOFF_BASE_SEC = 0.5
ORDER_POST_BACKOFF_MAX_SEC  = 8.0
ORDER_FILL_TIMEOUT_SEC      = 10

# Streamer Health / Emergency Close
EMERGENCY_CLOSE_TIMEOUT = 60

# Market Hours
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0
