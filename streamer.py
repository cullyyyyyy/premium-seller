"""DXLink websocket streamer for Tastytrade market data."""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable, Optional
import pytz
import websockets
import requests

from config import (BASE_URL, PRICING_URL, USERNAME, PASSWORD,
                    OAUTH_CLIENT_SECRET, OAUTH_REFRESH_TOKEN)
from models import Candle

logger = logging.getLogger(__name__)
et_tz = pytz.timezone("US/Eastern")


class TastytradeAuth:
    """Handles Tastytrade authentication."""

    def __init__(self, username: str = None, password: str = None, base_url: str = None):
        self.username = username or USERNAME
        self.password = password or PASSWORD
        self.base_url = base_url or BASE_URL
        self.session_token: Optional[str] = None
        self.streamer_url: Optional[str] = None
        self.streamer_token: Optional[str] = None
        self._oauth_expiry: float = 0.0  # Unix timestamp when OAuth token expires
        self._is_oauth: bool = False

    def login(self) -> bool:
        """Login and get session token. Uses OAuth for production, session-token for sandbox."""
        if self.base_url in (PRICING_URL, "https://api.tastyworks.com"):
            return self._oauth_login()

        # Sandbox: session-token login
        logger.info(f"Logging in to Tastytrade ({self.base_url})...")
        response = requests.post(
            f"{self.base_url}/sessions",
            json={"login": self.username, "password": self.password},
            headers={"Content-Type": "application/json"},
            timeout=10
        )

        if response.status_code not in (200, 201):
            logger.error(f"Login failed: {response.status_code} - {response.text}")
            return False

        data = response.json()["data"]
        self.session_token = data["session-token"]
        logger.info("Login successful!")
        return True

    def _oauth_login(self) -> bool:
        """Authenticate via OAuth 2.0 refresh token (production only)."""
        logger.info(f"OAuth login to Tastytrade ({self.base_url})...")
        try:
            response = requests.post(
                f"{self.base_url}/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_secret": OAUTH_CLIENT_SECRET,
                    "refresh_token": OAUTH_REFRESH_TOKEN,
                },
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if response.status_code not in (200, 201):
                logger.error(f"OAuth login failed: {response.status_code} - {response.text[:300]}")
                return False

            data = response.json()
            if "data" in data:
                data = data["data"]

            access_token = data.get("access-token") or data.get("access_token")
            token_type = data.get("token-type") or data.get("token_type", "Bearer")
            expires_in = data.get("expires-in") or data.get("expires_in", 900)

            if not access_token:
                logger.error(f"OAuth response missing access token. Keys: {list(data.keys())}")
                return False

            self.session_token = f"{token_type} {access_token}"
            self._oauth_expiry = time.time() + expires_in
            self._is_oauth = True
            logger.info(f"OAuth login successful! Token expires in {expires_in}s")
            return True
        except Exception as e:
            logger.error(f"OAuth login error: {e}")
            return False

    def _check_oauth_refresh(self):
        """Refresh OAuth token if it expires within 2 minutes."""
        if not self._is_oauth:
            return
        if time.time() > self._oauth_expiry - 120:
            logger.info("OAuth token expiring soon, refreshing...")
            self._oauth_login()

    def get_streamer_token(self) -> bool:
        """Get DXLink streamer token."""
        if not self.session_token:
            logger.error("Must login first!")
            return False

        self._check_oauth_refresh()

        logger.info("Fetching streamer token...")
        try:
            response = requests.get(
                f"{self.base_url}/api-quote-tokens",
                headers={
                    "Authorization": self.session_token,
                    "Content-Type": "application/json"
                },
                timeout=10
            )

            if response.status_code != 200:
                logger.error(f"Failed to get streamer token: {response.status_code} - {response.text}")
                return False

            data = response.json()["data"]
            self.streamer_url = data["dxlink-url"]
            self.streamer_token = data["token"]
            logger.info(f"Streamer URL: {self.streamer_url}")
            return True
        except Exception as e:
            logger.error(f"Error fetching streamer token: {e}")
            return False


class DXLinkStreamer:
    """Handles DXLink websocket connection for market data streaming."""

    # Reconnection settings
    MAX_RECONNECT_ATTEMPTS = 10
    INITIAL_RECONNECT_DELAY = 1.0  # seconds
    MAX_RECONNECT_DELAY = 60.0  # seconds
    RECONNECT_BACKOFF_MULTIPLIER = 2.0

    def __init__(self, auth: TastytradeAuth):
        self.auth = auth
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.channel_id = 1
        self.quote_channel_id = 3  # dedicated low-latency channel for Quote/Greeks (see _setup_quote_channel) — DXLink requires odd channel IDs for client-requested channels
        self.candle_callback: Optional[Callable[[Candle], None]] = None
        self.candle_5m_callback: Optional[Callable[[Candle], None]] = None
        self.option_quote_callback: Optional[Callable[[str, float, float], None]] = None
        self.underlying_quote_callback: Optional[Callable[[str, float], None]] = None
        self._option_symbols: set = set()  # Currently subscribed option symbols
        self._option_greeks: dict = {}  # Store latest greeks data per symbol
        self._running = False
        self._last_keepalive = 0
        self._last_message_time: float = 0.0  # Timestamp of last received message
        # Per-symbol quote candle tracking (used for synthetic candles from quotes)
        self._last_quote_times: dict = {}  # symbol -> last minute timestamp
        self._quote_candle_datas: dict = {}  # symbol -> OHLCV dict

        # Reconnection state
        self._reconnect_attempts = 0
        self._subscribed_candle_symbols: list = []  # 1m candle symbols for reconnect
        self._subscribed_5m_symbols: list = []      # 5m candle symbols for reconnect
        self._is_reconnecting = False

        # Dynamic field indices from FEED_SETUP response
        # Maps event type -> field name -> array index
        # e.g., {"Quote": {"bidPrice": 1, "askPrice": 2}, "Greeks": {"price": 1, "delta": 3}}
        self._field_indices: dict = {}

    # Seconds without a message before the streamer is considered unhealthy.
    # 10s is the right threshold for 0DTE intraday: a 30s window was too wide —
    # tick stops are blind the entire time, and the candle fallback was also
    # suppressed because the streamer still appeared "healthy".
    HEALTH_TIMEOUT = 10

    @property
    def is_healthy(self) -> bool:
        """Whether the websocket is connected and actively receiving data."""
        if not self._running or self.ws is None:
            return False
        if self._last_message_time == 0:
            return False  # Never received a message yet
        return (time.time() - self._last_message_time) < self.HEALTH_TIMEOUT

    async def connect(self):
        """Establish websocket connection to DXLink."""
        if not self.auth.streamer_url or not self.auth.streamer_token:
            raise ValueError("Must get streamer token first!")

        logger.info(f"Connecting to DXLink: {self.auth.streamer_url}")
        self.ws = await websockets.connect(self.auth.streamer_url)
        self._running = True

        # Step 1: SETUP
        await self._send({
            "type": "SETUP",
            "channel": 0,
            "version": "0.1-DXF-JS/0.3.0",
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60
        })

        # Wait for AUTH_STATE
        await self._receive_until("AUTH_STATE")

        # Step 2: AUTHORIZE
        await self._send({
            "type": "AUTH",
            "channel": 0,
            "token": self.auth.streamer_token
        })

        # Wait for authorized state
        await self._receive_until("AUTH_STATE")
        logger.info("DXLink authorized!")

        # Dedicated low-latency channel for live Quote/Greeks data (SPY underlying +
        # options). Kept separate from self.channel_id so 1m/5m candle bucketing's
        # slower aggregation period doesn't throttle tick-level price monitoring.
        await self._setup_quote_channel()

    async def _setup_quote_channel(self):
        """Open a second FEED channel requesting the fastest available update rate.

        Used for live SPY/option Quote+Greeks data (fills, stop monitoring).
        Separate from self.channel_id (candles) — candles want slow, batched
        aggregation; tick-level pricing wants the opposite.
        """
        await self._send({
            "type": "CHANNEL_REQUEST",
            "channel": self.quote_channel_id,
            "service": "FEED",
            "parameters": {
                "contract": "AUTO"
            }
        })

        await self._receive_until("CHANNEL_OPENED")
        logger.info("Channel opened for fast quote FEED service")

        await self._send({
            "type": "FEED_SETUP",
            "channel": self.quote_channel_id,
            "acceptAggregationPeriod": 0,  # no batching — deliver every update as it arrives
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {
                "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
                "Greeks": ["eventSymbol", "price", "volatility", "delta", "gamma", "theta", "rho", "vega"]
            }
        })

        await self._receive_until("FEED_CONFIG")
        self._build_field_indices({
            "acceptEventFields": {
                "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
                "Greeks": ["eventSymbol", "price", "volatility", "delta", "gamma", "theta", "rho", "vega"]
            }
        })

    async def subscribe_candles(self, symbol: str, interval: str = "1m", callback: Callable[[Candle], None] = None):
        """Subscribe to candle data for a symbol (first symbol sets up the channel)."""
        self.candle_callback = callback
        self._subscribed_candle_symbols = [symbol]  # First symbol resets the list

        # Step 3: CHANNEL_REQUEST for candle data
        await self._send({
            "type": "CHANNEL_REQUEST",
            "channel": self.channel_id,
            "service": "FEED",
            "parameters": {
                "contract": "AUTO"
            }
        })

        await self._receive_until("CHANNEL_OPENED")
        logger.info("Channel opened for FEED service")

        # Step 4: FEED_SETUP - specify what data we want.
        # Quote/Greeks are handled on the dedicated fast channel (_setup_quote_channel) —
        # this channel only carries Candle data now, so its slow aggregation period
        # (appropriate for 1m/5m bar building) doesn't throttle tick-level pricing.
        await self._send({
            "type": "FEED_SETUP",
            "channel": self.channel_id,
            "acceptAggregationPeriod": 15000,  # 15 seconds in ms
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {
                "Candle": ["eventSymbol", "time", "open", "high", "low", "close", "volume"]
            }
        })

        # Wait for FEED_CONFIG response (server's acknowledgment of our FEED_SETUP)
        # Note: Server sends FEED_CONFIG, not FEED_SETUP echo. Field indices use fallback positions.
        await self._receive_until("FEED_CONFIG")
        # Build field indices from what WE sent (server doesn't echo acceptEventFields)
        self._build_field_indices({
            "acceptEventFields": {
                "Candle": ["eventSymbol", "time", "open", "high", "low", "close", "volume"]
            }
        })

        # Step 5: FEED_SUBSCRIPTION - candles on the slow channel, quotes on the fast one
        from_time = int(time.time() * 1000) - (4 * 60 * 60 * 1000)  # 4 hours back

        candle_symbol = f"{symbol}{{=15s}}"  # 15-second candles

        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.channel_id,
            "reset": True,
            "add": [
                {"type": "Candle", "symbol": candle_symbol, "fromTime": from_time}
            ]
        })
        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.quote_channel_id,
            "reset": False,
            "add": [
                {"type": "Quote", "symbol": symbol}
            ]
        })

        logger.info(f"Subscribed to {candle_symbol} candles and {symbol} quotes (fast channel)")

    async def subscribe_additional_candles(self, symbol: str):
        """Subscribe to candle data for an additional symbol without resetting existing subscriptions."""
        if symbol in self._subscribed_candle_symbols:
            logger.info(f"Already subscribed to {symbol} candles, skipping")
            return

        self._subscribed_candle_symbols.append(symbol)

        from_time = int(time.time() * 1000) - (4 * 60 * 60 * 1000)  # 4 hours back
        candle_symbol = f"{symbol}{{=15s}}"

        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.channel_id,
            "reset": False,  # Don't drop existing subscriptions
            "add": [
                {"type": "Candle", "symbol": candle_symbol, "fromTime": from_time}
            ]
        })
        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.quote_channel_id,
            "reset": False,
            "add": [
                {"type": "Quote", "symbol": symbol}
            ]
        })

        logger.info(f"Added subscription: {candle_symbol} candles and {symbol} quotes (fast channel)")

    async def subscribe_5m_candles(self, symbol: str, callback: Callable[[Candle], None] = None):
        """Subscribe to 5-minute candle data for a symbol.

        Uses the same FEED channel as 1m candles with reset=False so existing
        1m subscriptions are preserved.  The DXLink symbol format for 5-minute
        candles is '{symbol}{=5m}' (e.g. 'SPY{=5m}').

        The routing in _handle_feed_data distinguishes 5m events from 1m events
        because '{=m}' is NOT a substring of '{=5m}'.
        """
        if callback:
            self.candle_5m_callback = callback

        if symbol not in self._subscribed_5m_symbols:
            self._subscribed_5m_symbols.append(symbol)

        from_time = int(time.time() * 1000) - (4 * 60 * 60 * 1000)  # 4 hours back
        candle_symbol = f"{symbol}{{=5m}}"

        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.channel_id,
            "reset": False,  # Don't drop existing 1m subscriptions
            "add": [
                {"type": "Candle", "symbol": candle_symbol, "fromTime": from_time},
            ]
        })

        logger.info(f"Subscribed to 5m candles: {candle_symbol}")

    async def subscribe_option_quote(self, option_symbol: str, callback: Callable[[str, float, float], None]):
        """Subscribe to real-time bid/ask quotes for an option symbol.

        Subscribes to both Quote (real bid/ask — the only pricing source; drives
        `callback`) and Greeks (delta/gamma/theta/etc — metadata only, stored in
        _option_greeks, never used as a price) on the fast no-aggregation channel.
        """
        self.option_quote_callback = callback
        self._option_symbols.add(option_symbol)

        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.quote_channel_id,
            "reset": False,
            "add": [
                {"type": "Quote", "symbol": option_symbol},
                {"type": "Greeks", "symbol": option_symbol}
            ]
        })
        logger.info(f"Subscribed to option quotes and Greeks: {option_symbol}")

    async def unsubscribe_option_quote(self, option_symbol: str):
        """Unsubscribe from an option symbol's quotes."""
        self._option_symbols.discard(option_symbol)
        # Clean up cached greeks to prevent memory leak
        self._option_greeks.pop(option_symbol, None)

        await self._send({
            "type": "FEED_SUBSCRIPTION",
            "channel": self.quote_channel_id,
            "reset": False,
            "remove": [
                {"type": "Quote", "symbol": option_symbol}
            ]
        })
        logger.info(f"Unsubscribed from option quotes: {option_symbol}")

    async def run(self):
        """Main loop to receive and process messages with automatic reconnection."""
        self._last_keepalive = time.time()
        msg_count = 0

        logger.info("Starting message receive loop...")

        while self._running:
            try:
                # Send keepalive every 30 seconds. Logged at INFO (was DEBUG,
                # invisible at this project's configured level) -- 2026-10-21,
                # found while investigating a 4h24m silent outage (2026-10-20)
                # where this loop kept looping and sending keepalives the
                # whole time with zero visible log output, because every
                # active code path here was either silent (the TimeoutError
                # branch below) or debug-only (this one).
                if time.time() - self._last_keepalive > 30:
                    await self._send({"type": "KEEPALIVE", "channel": 0})
                    self._last_keepalive = time.time()
                    logger.info(f"Sent keepalive, total messages received: {msg_count}")

                # Receive with timeout so we can send keepalives
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                    msg_count += 1
                    self._reconnect_attempts = 0  # Reset on successful message
                    self._last_message_time = time.time()
                    await self._handle_message(json.loads(msg))
                except asyncio.TimeoutError:
                    # A dead-but-not-closed ("zombie") connection times out
                    # here forever without ever raising ConnectionClosed --
                    # exactly what happened 2026-10-20 (silent 4h24m outage,
                    # zero log output, never detected). Self-heal instead of
                    # waiting on an exception that may never come: once
                    # we've gone HEALTH_TIMEOUT past the last real message,
                    # force a reconnect ourselves.
                    if (
                        self._last_message_time
                        and time.time() - self._last_message_time > self.HEALTH_TIMEOUT
                        and not self._is_reconnecting
                    ):
                        logger.warning(
                            f"No message received for over {self.HEALTH_TIMEOUT}s "
                            f"(connection never raised ConnectionClosed) -- "
                            f"forcing a reconnect"
                        )
                        if await self._reconnect():
                            logger.info("Self-healed reconnect successful, resuming message loop")
                            msg_count = 0
                        else:
                            logger.error("Self-healed reconnect failed after max attempts, stopping")
                            self._running = False
                    continue

            except websockets.ConnectionClosed as e:
                logger.error(f"Connection closed: {e}")
                # Don't reconnect if intentionally closed (market close or shutdown)
                if not self._running:
                    logger.info("Connection closed intentionally, stopping run loop")
                    break
                if await self._reconnect():
                    logger.info("Reconnected successfully, resuming message loop")
                    msg_count = 0
                    continue
                else:
                    logger.error("Failed to reconnect after max attempts, stopping")
                    self._running = False
            except Exception as e:
                logger.error(f"Error in run loop: {e}", exc_info=True)
                # Don't reconnect if intentionally stopped
                if not self._running:
                    break
                # Try to reconnect on unexpected errors
                if await self._reconnect():
                    continue
                else:
                    self._running = False

    async def force_reconnect(self) -> bool:
        """Public entry point for an external caller (live_runner.py's
        monitor_streamer_health) to proactively force a reconnect -- added
        2026-10-21 so a stale-but-not-exception-raising connection can be
        recovered without waiting on this object's own receive loop to
        notice. Safe to call even while the receive loop's own self-heal
        (see run()'s TimeoutError branch) is also trying -- _reconnect()
        itself guards against concurrent attempts via _is_reconnecting."""
        return await self._reconnect()

    async def _reconnect(self) -> bool:
        """Attempt to reconnect with exponential backoff. Returns True if successful."""
        if self._is_reconnecting:
            return False

        self._is_reconnecting = True

        try:
            while self._reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS:
                self._reconnect_attempts += 1

                # Calculate delay with exponential backoff
                delay = min(
                    self.INITIAL_RECONNECT_DELAY * (self.RECONNECT_BACKOFF_MULTIPLIER ** (self._reconnect_attempts - 1)),
                    self.MAX_RECONNECT_DELAY
                )

                logger.warning(
                    f"Reconnection attempt {self._reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS} "
                    f"in {delay:.1f}s..."
                )

                await asyncio.sleep(delay)

                try:
                    # Close existing connection if any
                    if self.ws:
                        try:
                            await self.ws.close()
                        except Exception:
                            pass
                        self.ws = None

                    # Refresh streamer token (may have expired).
                    # get_streamer_token() is a synchronous blocking requests.get call.
                    # Wrapping in asyncio.to_thread() keeps the event loop responsive
                    # during the network round-trip so stop monitoring keeps running.
                    logger.info("Refreshing streamer token...")
                    token_ok = await asyncio.to_thread(self.auth.get_streamer_token)
                    if not token_ok:
                        logger.error("Failed to refresh streamer token")
                        continue

                    # Reconnect
                    await self.connect()

                    # Re-subscribe to candles if we were subscribed
                    if self._subscribed_candle_symbols:
                        symbols_to_restore = list(self._subscribed_candle_symbols)
                        logger.info(f"Re-subscribing to candles: {symbols_to_restore}")
                        # First symbol uses subscribe_candles (sets up channel)
                        await self.subscribe_candles(
                            symbols_to_restore[0],
                            callback=self.candle_callback
                        )
                        # Additional symbols use additive subscription
                        for extra_symbol in symbols_to_restore[1:]:
                            await self.subscribe_additional_candles(extra_symbol)

                    # Re-subscribe to 5m candles
                    if self._subscribed_5m_symbols:
                        logger.info(f"Re-subscribing to 5m candles: {self._subscribed_5m_symbols}")
                        for sym in self._subscribed_5m_symbols:
                            await self.subscribe_5m_candles(sym)

                    # Re-subscribe to option quotes
                    if self._option_symbols:
                        logger.info(f"Re-subscribing to {len(self._option_symbols)} option symbols")
                        for symbol in list(self._option_symbols):
                            await self._send({
                                "type": "FEED_SUBSCRIPTION",
                                "channel": self.quote_channel_id,
                                "reset": False,
                                "add": [
                                    {"type": "Quote", "symbol": symbol},
                                    {"type": "Greeks", "symbol": symbol}
                                ]
                            })

                    logger.info("Reconnection successful!")
                    self._reconnect_attempts = 0
                    return True

                except Exception as e:
                    logger.error(f"Reconnection attempt failed: {e}")

            # Max attempts reached
            logger.error(f"Max reconnection attempts ({self.MAX_RECONNECT_ATTEMPTS}) reached")
            return False

        finally:
            self._is_reconnecting = False

    async def _send(self, msg: dict):
        """Send a message to DXLink."""
        logger.debug(f"Sending: {msg.get('type')} on channel {msg.get('channel')}")
        await self.ws.send(json.dumps(msg))

    async def _receive_until(self, msg_type: str, timeout: float = 30.0) -> dict:
        """Receive messages until we get the expected type, with keepalive handling.

        Args:
            msg_type: The message type to wait for
            timeout: Total timeout in seconds (default 30s)
        """
        start_time = time.time()
        last_keepalive = time.time()

        while True:
            # Check total timeout
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Timed out waiting for {msg_type} after {timeout}s")

            # Send keepalive every 20 seconds during setup
            if time.time() - last_keepalive > 20:
                await self._send({"type": "KEEPALIVE", "channel": 0})
                last_keepalive = time.time()
                logger.debug("Sent keepalive during setup")

            try:
                # Use short timeout so we can send keepalives
                msg = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                data = json.loads(msg)
                logger.debug(f"Received during setup: {data.get('type')}")

                if data.get("type") == "ERROR":
                    logger.error(f"DXLink error during setup: {data}")
                    # Raise on fatal errors
                    if data.get("error") == "TIMEOUT":
                        raise ConnectionError(f"DXLink error: {data.get('message')}")

                if data.get("type") == msg_type:
                    logger.info(f"Got expected message type: {msg_type}")
                    return data

            except asyncio.TimeoutError:
                # No message received, loop back to send keepalive if needed
                continue

    def _build_field_indices(self, setup_msg: dict):
        """Build field name -> index mappings from FEED_SETUP response.

        This allows dynamic parsing of arrays based on the actual field order
        returned by DXLink, rather than assuming fixed positions.
        """
        accept_fields = setup_msg.get("acceptEventFields", {})

        for event_type, fields in accept_fields.items():
            if isinstance(fields, list):
                # Build index lookup: field_name -> position in array
                self._field_indices[event_type] = {
                    field: idx for idx, field in enumerate(fields)
                }
                logger.info(f"Field indices for {event_type}: {self._field_indices[event_type]}")

        # Warn only for event types that were actually requested on this channel but
        # somehow came back without indices. The fast channel legitimately has no Candle
        # fields (it only handles Quote/Greeks), so we must not warn about Candle there.
        for expected in accept_fields:
            if expected not in self._field_indices:
                logger.warning(f"No field indices for {expected} - using fallback positions")

    def _get_field(self, item: list, event_type: str, field_name: str, offset: int = 0):
        """Get a field value from an array using dynamic field indices.

        Args:
            item: The data array
            event_type: Event type (Quote, Greeks, Candle)
            field_name: Field name to extract
            offset: Offset for multi-record arrays (e.g., second symbol starts at offset 5 for Quote)

        Returns:
            The field value, or None if not found
        """
        indices = self._field_indices.get(event_type, {})
        idx = indices.get(field_name)

        if idx is not None:
            actual_idx = offset + idx
            if 0 <= actual_idx < len(item):
                return item[actual_idx]
        return None

    def _get_record_size(self, event_type: str) -> int:
        """Get the number of fields per record for an event type."""
        indices = self._field_indices.get(event_type, {})
        if indices:
            return len(indices)
        # Fallback sizes
        return {"Quote": 5, "Greeks": 8, "Candle": 7}.get(event_type, 1)

    def _parse_quote_data(self, item: list):
        """Parse Quote data array which may contain multiple symbols.

        Quote format: [symbol, bid, ask, bidSize, askSize, symbol2, bid2, ask2, ...]
        Each symbol has N fields (determined by FEED_SETUP response).
        The array can contain multiple symbols concatenated.
        """
        record_size = self._get_record_size("Quote")
        i = 0

        while i < len(item):
            # Look for an option symbol (string starting with '.' or containing space)
            if isinstance(item[i], str) and (item[i].startswith('.') or ' ' in item[i] or len(item[i]) > 10):
                # Check we have enough fields for a complete record
                if i + record_size <= len(item):
                    symbol = self._get_field(item, "Quote", "eventSymbol", i)
                    bid = self._get_field(item, "Quote", "bidPrice", i)
                    ask = self._get_field(item, "Quote", "askPrice", i)

                    # Fallback to positional if dynamic lookup failed
                    if symbol is None:
                        symbol = item[i]
                    if bid is None and i + 1 < len(item):
                        bid = item[i + 1]
                    if ask is None and i + 2 < len(item):
                        ask = item[i + 2]

                    # Handle NaN values - treat as 0
                    if isinstance(bid, str) and bid == 'NaN':
                        bid = 0.0
                    if isinstance(ask, str) and ask == 'NaN':
                        ask = 0.0

                    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                        logger.info(f"Option Quote: {symbol} bid=${bid:.2f} ask=${ask:.2f}")

                        # Trigger callback with real bid/ask
                        if symbol in self._option_symbols and self.option_quote_callback:
                            try:
                                self.option_quote_callback(symbol, bid, ask)
                            except Exception as cb_err:
                                logger.error(f"Option quote callback error: {cb_err}", exc_info=True)

                    i += record_size  # Move to next symbol
                else:
                    break  # Not enough fields remaining
            else:
                i += 1  # Skip non-symbol elements

    def _parse_greeks_data(self, item: list):
        """Parse Greeks data array which may contain multiple symbols.

        Greeks format: [symbol, price, volatility, delta, gamma, theta, rho, vega, symbol2, ...]
        Each symbol has N fields (determined by FEED_SETUP response).
        The array can contain multiple symbols concatenated.

        Stored for delta/gamma metadata only (e.g. trades.json entry_delta/entry_gamma).
        theo_price is a theoretical model output, not a real bid/ask — it must never be
        used as a pricing source. Only genuine Quote events (real bid/ask) drive
        option_quote_callback; see _parse_quote_data and the single-symbol Quote branch
        in _handle_feed_data.
        """
        record_size = self._get_record_size("Greeks")
        i = 0

        while i < len(item):
            # Look for a symbol (string starting with '.' or containing space)
            if isinstance(item[i], str) and (item[i].startswith('.') or ' ' in item[i] or len(item[i]) > 10):
                # Check we have enough fields for a complete record
                if i + record_size <= len(item):
                    symbol = self._get_field(item, "Greeks", "eventSymbol", i)
                    theo_price = self._get_field(item, "Greeks", "price", i)
                    volatility = self._get_field(item, "Greeks", "volatility", i)
                    delta = self._get_field(item, "Greeks", "delta", i)
                    gamma = self._get_field(item, "Greeks", "gamma", i)
                    theta = self._get_field(item, "Greeks", "theta", i)
                    rho = self._get_field(item, "Greeks", "rho", i)
                    vega = self._get_field(item, "Greeks", "vega", i)

                    # Fallback to positional if dynamic lookup failed
                    if symbol is None:
                        symbol = item[i]
                    if theo_price is None and i + 1 < len(item):
                        theo_price = item[i + 1]

                    if theo_price and isinstance(theo_price, (int, float)):
                        self._option_greeks[symbol] = {
                            'price': theo_price,
                            'volatility': volatility,
                            'delta': delta,
                            'gamma': gamma,
                            'theta': theta,
                            'rho': rho,
                            'vega': vega
                        }
                        delta_val = delta if isinstance(delta, (int, float)) else 0
                        vol_val = volatility if isinstance(volatility, (int, float)) else 0
                        logger.info(f"Greeks: {symbol} theo=${theo_price:.2f} delta={delta_val:.3f} IV={vol_val:.2%}")

                    i += record_size  # Move to next symbol
                else:
                    break  # Not enough fields remaining
            else:
                i += 1  # Skip non-symbol elements

    async def _handle_message(self, msg: dict):
        """Handle incoming DXLink messages."""
        msg_type = msg.get("type")

        if msg_type == "FEED_DATA":
            await self._handle_feed_data(msg)
        elif msg_type == "KEEPALIVE":
            pass  # Just a keepalive response
        elif msg_type == "FEED_CONFIG":
            logger.info(f"Feed config received: {msg}")
        elif msg_type == "ERROR":
            logger.error(f"DXLink error: {msg}")
        else:
            logger.debug(f"DXLink message: {msg_type}")

    async def _handle_feed_data(self, msg: dict):
        """Process incoming candle data.

        DXLink COMPACT format sends data as flat arrays:
        - Candles: ['SPY{=m}', timestamp, open, high, low, close, volume, 'SPY{=m}', timestamp2, ...]
        - Quotes: ['SPY', bid, ask, bidSize, askSize]

        The first element is the event symbol, which tells us the event type.
        """
        data = msg.get("data", [])

        if not data:
            return

        for item in data:
            if not isinstance(item, list) or len(item) < 2:
                continue

            event_symbol = item[0]

            # Check if it's a 5-minute candle event (must be checked BEFORE 1m check)
            if isinstance(event_symbol, str) and "{=5m}" in event_symbol:
                record_size = self._get_record_size("Candle")
                i = 0
                while i < len(item):
                    if isinstance(item[i], str) and "{=5m}" in item[i]:
                        if i + record_size <= len(item):
                            symbol = self._get_field(item, "Candle", "eventSymbol", i) or item[i]
                            ts = self._get_field(item, "Candle", "time", i)
                            o = self._get_field(item, "Candle", "open", i)
                            h = self._get_field(item, "Candle", "high", i)
                            l = self._get_field(item, "Candle", "low", i)
                            c = self._get_field(item, "Candle", "close", i)
                            v = self._get_field(item, "Candle", "volume", i)

                            if ts is None:
                                ts, o, h, l, c, v = item[i+1], item[i+2], item[i+3], item[i+4], item[i+5], item[i+6]

                            if isinstance(c, (int, float)) and not (isinstance(c, float) and str(c) == 'nan'):
                                underlying = symbol.split("{")[0] if "{" in symbol else symbol
                                candle = Candle(
                                    timestamp=ts,
                                    open=o,
                                    high=h,
                                    low=l,
                                    close=c,
                                    volume=v or 0,
                                    symbol=underlying
                                )
                                # Real exchange-provided candle time, not this log line's own
                                # write time — the write time can lag or batch arbitrarily during
                                # a reconnect/replay backlog (proven 2026-10-06: wall-clock-only
                                # matching silently mismatched 204 distinct bars to one write
                                # timestamp on 2026-09-16). Embedding the real time lets any
                                # downstream reconstruction do a true timestamp join instead of
                                # fragile nearest-price matching.
                                candle_et = datetime.fromtimestamp(candle.timestamp / 1000, tz=et_tz)
                                logger.info(f"5m Candle: {underlying} @ {candle.close:.2f} "
                                            f"(O={candle.open:.2f} H={candle.high:.2f} L={candle.low:.2f}) "
                                            f"t={candle_et.strftime('%H:%M:%S')}")

                                if self.candle_5m_callback:
                                    try:
                                        self.candle_5m_callback(candle)
                                    except Exception as cb_err:
                                        logger.error(f"5m candle callback error: {cb_err}", exc_info=True)
                            i += record_size
                        else:
                            break
                    else:
                        i += 1

            # Check if it's a 15-second candle event
            elif isinstance(event_symbol, str) and "{=15s}" in event_symbol:
                # Candle data in COMPACT format: flat array with repeating pattern
                # Field order determined by FEED_SETUP response
                record_size = self._get_record_size("Candle")
                i = 0
                while i < len(item):
                    if isinstance(item[i], str) and "{=15s}" in item[i]:
                        # Found a candle symbol, extract fields dynamically
                        if i + record_size <= len(item):
                            symbol = self._get_field(item, "Candle", "eventSymbol", i) or item[i]
                            ts = self._get_field(item, "Candle", "time", i)
                            o = self._get_field(item, "Candle", "open", i)
                            h = self._get_field(item, "Candle", "high", i)
                            l = self._get_field(item, "Candle", "low", i)
                            c = self._get_field(item, "Candle", "close", i)
                            v = self._get_field(item, "Candle", "volume", i)

                            # Fallback to positional if dynamic lookup failed
                            if ts is None:
                                ts, o, h, l, c, v = item[i+1], item[i+2], item[i+3], item[i+4], item[i+5], item[i+6]

                            # Skip NaN values
                            if isinstance(c, (int, float)) and not (isinstance(c, float) and str(c) == 'nan'):
                                # Extract underlying symbol from candle symbol (e.g. "SPY{=15s}" -> "SPY")
                                underlying = symbol.split("{")[0] if "{" in symbol else symbol
                                candle = Candle(
                                    timestamp=ts,
                                    open=o,
                                    high=h,
                                    low=l,
                                    close=c,
                                    volume=v or 0,
                                    symbol=underlying
                                )
                                # See the 5m-candle log line above for why the real exchange
                                # timestamp (not this line's write time) is embedded here.
                                candle_et = datetime.fromtimestamp(candle.timestamp / 1000, tz=et_tz)
                                logger.info(f"Candle: {underlying} @ {candle.close:.2f} "
                                            f"(O={candle.open:.2f} H={candle.high:.2f} L={candle.low:.2f}) "
                                            f"t={candle_et.strftime('%H:%M:%S')}")

                                if self.candle_callback:
                                    try:
                                        self.candle_callback(candle)
                                    except Exception as cb_err:
                                        logger.error(f"Candle callback error: {cb_err}", exc_info=True)
                            i += record_size
                        else:
                            break
                    else:
                        i += 1

            # Check if this is multi-symbol Quote data for options
            # Must check BEFORE Greeks since both start with option symbols
            # Use dynamic record size (default 5 for Quote)
            elif isinstance(event_symbol, str) and len(item) == self._get_record_size("Quote") * 2 and (
                event_symbol.startswith('.') or ' ' in event_symbol or len(event_symbol) > 10
            ):
                # Multi-symbol Quote data - 2 symbols x N fields each
                self._parse_quote_data(item)

            # Check if this is Greeks data for options
            # Greeks format determined by FEED_SETUP (default 8 fields per symbol)
            # Option symbols start with '.' (e.g., .SPY260130C689) or contain spaces
            elif isinstance(event_symbol, str) and len(item) >= self._get_record_size("Greeks") * 2 and (
                event_symbol.startswith('.') or ' ' in event_symbol or len(event_symbol) > 10
            ):
                # Greeks data - can contain multiple symbols
                self._parse_greeks_data(item)

            # Check if this is single-symbol Greeks data
            elif isinstance(event_symbol, str) and len(item) == self._get_record_size("Greeks") and (
                event_symbol.startswith('.') or ' ' in event_symbol or len(event_symbol) > 10
            ):
                self._parse_greeks_data(item)

            # Check if it's a quote event
            # This handles BOTH underlying quotes (SPY) and single option quotes
            elif isinstance(event_symbol, str) and "{" not in event_symbol and len(item) >= self._get_record_size("Quote") and len(item) < self._get_record_size("Greeks"):
                # Use dynamic field indices
                symbol = self._get_field(item, "Quote", "eventSymbol", 0) or item[0]
                bid = self._get_field(item, "Quote", "bidPrice", 0)
                ask = self._get_field(item, "Quote", "askPrice", 0)

                # Fallback to positional if dynamic lookup failed
                if bid is None:
                    bid = item[1] if len(item) > 1 else None
                if ask is None:
                    ask = item[2] if len(item) > 2 else None

                if bid and ask and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                    # Check if this is an option symbol
                    is_option = symbol.startswith('.') or ' ' in symbol or len(symbol) > 10

                    if is_option:
                        # Option Quote - route to option callback with real bid/ask
                        if symbol in self._option_symbols and self.option_quote_callback:
                            logger.info(f"Option Quote: {symbol} bid=${bid:.2f} ask=${ask:.2f}")
                            try:
                                self.option_quote_callback(symbol, bid, ask)
                            except Exception as cb_err:
                                logger.error(f"Option quote callback error: {cb_err}", exc_info=True)
                        continue

                    # Underlying quote (SPY, QQQ, etc.)
                    mid_price = (bid + ask) / 2
                    current_minute = int(time.time() // 60)

                    logger.info(f"Quote: {symbol} bid={bid:.2f} ask={ask:.2f} mid={mid_price:.2f}")

                    # Notify runner for intrabar RSI calculation
                    if self.underlying_quote_callback:
                        try:
                            self.underlying_quote_callback(symbol, mid_price)
                        except Exception as cb_err:
                            logger.error(f"Underlying quote callback error: {cb_err}", exc_info=True)

                    # Per-symbol OHLC tracking for synthetic candles
                    last_minute = self._last_quote_times.get(symbol, 0)
                    candle_data = self._quote_candle_datas.get(symbol)

                    # Track OHLC for the current minute
                    if current_minute != last_minute:
                        # New minute - emit previous candle if we have data
                        if last_minute > 0 and candle_data and candle_data["open"] is not None:
                            candle = Candle(
                                timestamp=last_minute * 60 * 1000,
                                open=candle_data["open"],
                                high=candle_data["high"],
                                low=candle_data["low"],
                                close=candle_data["close"],
                                volume=candle_data["volume"],
                                symbol=symbol
                            )
                            logger.info(f"Synthetic candle from quotes: {symbol} O={candle.open:.2f} H={candle.high:.2f} L={candle.low:.2f} C={candle.close:.2f}")
                            if self.candle_callback:
                                try:
                                    self.candle_callback(candle)
                                except Exception as cb_err:
                                    logger.error(f"Candle callback error (synthetic): {cb_err}", exc_info=True)

                        # Reset for new minute
                        self._last_quote_times[symbol] = current_minute
                        self._quote_candle_datas[symbol] = {
                            "open": mid_price,
                            "high": mid_price,
                            "low": mid_price,
                            "close": mid_price,
                            "volume": 0
                        }
                    else:
                        # Same minute - update OHLC
                        if candle_data and candle_data["high"] is not None:
                            candle_data["high"] = max(candle_data["high"], mid_price)
                            candle_data["low"] = min(candle_data["low"], mid_price)
                            candle_data["close"] = mid_price
                        else:
                            # First quote of this minute - initialize OHLC
                            self._quote_candle_datas[symbol] = {
                                "open": mid_price,
                                "high": mid_price,
                                "low": mid_price,
                                "close": mid_price,
                                "volume": 0
                            }

            # Fallback: try parsing as Greeks for any remaining format with 8+ fields
            elif isinstance(event_symbol, str) and len(item) >= 8:
                self._parse_greeks_data(item)
            else:
                logger.debug(f"Unknown event format: {item[:3] if len(item) > 3 else item}")

    def get_option_greeks(self, symbol: str) -> Optional[dict]:
        """Get the latest Greeks data for an option symbol."""
        return self._option_greeks.get(symbol)

    async def close(self):
        """Close the websocket connection."""
        self._running = False
        if self.ws:
            await self.ws.close()


class AccountStreamer:
    """
    WebSocket consumer for Tastytrade account events (real-time order fill notifications).

    Delivers order status changes to _limit_chase_async via per-order asyncio.Queue objects,
    eliminating REST polling on the hot order-execution path.

    Protocol: Tastytrade account streamer
      Sandbox:    wss://streamer.cert.tastyworks.com
      Production: wss://streamer.tastyworks.com

    Auth: the session token from /sessions POST (NOT the DXLink market-data token).

    Message formats handled:
      Dict A  — {"type": "Order", "data": {...order_fields...}}
      Dict B  — {"action": "data", "type": "Order", "value": [{...}]}
      Array   — [["Order", {...order_fields...}], ...]
      Raw     — any dict that contains an "id" or "order-id" key directly

    All received messages are logged at DEBUG level so the exact wire format can be
    validated against the sandbox and the parser adjusted if needed.
    """

    INITIAL_RECONNECT_DELAY = 2.0
    MAX_RECONNECT_DELAY     = 60.0
    RECONNECT_BACKOFF       = 2.0
    MAX_RECONNECT_ATTEMPTS  = 10

    def __init__(self, session_token: str, account_number: str, base_url: str):
        self.session_token  = session_token
        self.account_number = account_number
        # Infer WebSocket URL from the REST base URL
        self._url = (
            "wss://streamer.cert.tastyworks.com"
            if "cert" in base_url
            else "wss://streamer.tastyworks.com"
        )
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running            = False
        self._reconnect_attempts = 0
        self._last_heartbeat: float = 0.0
        # order_id (str) → asyncio.Queue that receives raw order-event dicts
        self._order_queues: dict = {}

    async def connect(self) -> None:
        """Establish WebSocket, authenticate, and subscribe to order events."""
        logger.info(f"[ACCT] Connecting to account streamer: {self._url}")
        self.ws = await websockets.connect(
            self._url,
            additional_headers={"Authorization": self.session_token},
        )
        self._running = True

        # Authenticate
        await self._send({"action": "auth", "value": self.session_token})
        try:
            auth_msg = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            logger.debug(f"[ACCT] Auth response: {auth_msg}")
        except asyncio.TimeoutError:
            logger.warning("[ACCT] No auth ack within 10 s — proceeding anyway")

        # Subscribe to order events for this account
        await self._send({
            "action": "subscribe",
            "value": [f"order:{self.account_number}"],
        })
        logger.info("[ACCT] Account streamer connected and subscribed to order events")

    async def close(self) -> None:
        """Stop the run loop and close the WebSocket."""
        self._running = False
        if self.ws:
            await self.ws.close()

    # ── Order watcher interface ────────────────────────────────────────────────

    def watch_order(self, order_id: str) -> asyncio.Queue:
        """
        Register interest in events for order_id.
        Returns a Queue that will receive raw order-event dicts.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._order_queues[str(order_id)] = q
        logger.debug(f"[ACCT] Watching order {order_id}")
        return q

    def unwatch_order(self, order_id: str) -> None:
        """Remove the watcher for order_id."""
        self._order_queues.pop(str(order_id), None)
        logger.debug(f"[ACCT] Stopped watching order {order_id}")

    # ── Receive loop ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main receive loop — parses messages and dispatches order events to queues."""
        while self._running:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=35.0)
                self._last_heartbeat = time.time()
                try:
                    msg = json.loads(raw)
                    logger.debug(f"[ACCT] Received: {msg}")
                    await self._handle_message(msg)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"[ACCT] Parse error: {e} | raw={raw!r}")

            except asyncio.TimeoutError:
                # 35 s of silence — send a keepalive probe
                if self._running and self.ws:
                    try:
                        await self._send({"action": "heartbeat"})
                    except Exception:
                        pass

            except websockets.ConnectionClosed as e:
                logger.warning(f"[ACCT] Connection closed: {e}")
                if not self._running:
                    break
                if not await self._reconnect():
                    logger.error("[ACCT] Max reconnect attempts reached — account streamer offline")
                    self._running = False
                    break

            except Exception as e:
                logger.error(f"[ACCT] Unexpected error: {e}", exc_info=True)
                if not self._running:
                    break
                if not await self._reconnect():
                    self._running = False
                    break

    # ── Message parsing ────────────────────────────────────────────────────────

    async def _handle_message(self, msg) -> None:
        """
        Route an incoming message to the relevant order queue(s).

        Tries four formats in priority order:
          1. Dict with "type" == "order" / "Order"
          2. Dict with "action" == "data" wrapping order items
          3. List of [event_type, order_dict] tuples
          4. Raw dict that directly contains an order "id"
        """
        if isinstance(msg, dict):
            action   = msg.get("action", "").lower()
            msg_type = msg.get("type",   "").lower()

            if "heartbeat" in action or "heartbeat" in msg_type:
                return

            if msg_type == "order":
                # Format A: {"type": "Order", "data": {...}} or {"type": "Order", "value": [...]}
                payload = msg.get("data") or msg.get("value")
                if isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict):
                            await self._dispatch_order(item)
                elif isinstance(payload, dict):
                    await self._dispatch_order(payload)
                else:
                    await self._dispatch_order(msg)  # msg itself is the order
                return

            if action == "data":
                # Format B: {"action": "data", "type": "Order", "value": [{...}]}
                payload = msg.get("value") or msg.get("data")
                if isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict):
                            await self._dispatch_order(item)
                elif isinstance(payload, dict):
                    await self._dispatch_order(payload)
                return

            # Format D fallback: dict that directly carries an order id
            if msg.get("id") or msg.get("order-id"):
                await self._dispatch_order(msg)

        elif isinstance(msg, list):
            # Format C: [["Order", {...}], ...] or similar array envelope
            for item in msg:
                if isinstance(item, list) and len(item) >= 2:
                    label = str(item[0]).lower()
                    if label == "order" and isinstance(item[1], dict):
                        await self._dispatch_order(item[1])
                    elif isinstance(item[1], dict) and (item[1].get("id") or item[1].get("order-id")):
                        await self._dispatch_order(item[1])
                elif isinstance(item, dict) and (item.get("id") or item.get("order-id")):
                    await self._dispatch_order(item)

    async def _dispatch_order(self, order: dict) -> None:
        """Put an order event into its registered queue (if any)."""
        if not order:
            return
        order_id = str(order.get("id") or order.get("order-id") or "")
        if not order_id:
            return
        q = self._order_queues.get(order_id)
        if q is not None:
            logger.info(
                "[ACCT] Order event dispatched → id=%s  status=%s",
                order_id, order.get("status", "?"),
            )
            await q.put(order)

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _send(self, msg: dict) -> None:
        if self.ws:
            await self.ws.send(json.dumps(msg))

    async def _reconnect(self) -> bool:
        """Exponential-backoff reconnect. Returns True on success."""
        delay = self.INITIAL_RECONNECT_DELAY
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            self._reconnect_attempts += 1
            logger.warning(
                "[ACCT] Reconnect attempt %d/%d in %.0f s…",
                attempt, self.MAX_RECONNECT_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)
            try:
                await self.connect()
                self._reconnect_attempts = 0
                logger.info("[ACCT] Account streamer reconnected successfully")
                return True
            except Exception as e:
                logger.error(f"[ACCT] Reconnect failed: {e}")
                delay = min(delay * self.RECONNECT_BACKOFF, self.MAX_RECONNECT_DELAY)
        return False
