"""Options chain fetching and strike selection for 0DTE strategy."""
import logging
import time
import requests
import urllib.parse
from datetime import datetime, date
from typing import Optional, List, Dict
import pytz

from config import BASE_URL, PRICING_URL, STRIKES_OTM, TARGET_DELTA, OAUTH_CLIENT_SECRET, OAUTH_REFRESH_TOKEN

logger = logging.getLogger(__name__)


def _oauth_get_token() -> Optional[str]:
    """Get a production access token via OAuth 2.0. Returns full 'Bearer <token>' string."""
    try:
        response = requests.post(
            f"{PRICING_URL}/oauth/token",
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
            return None

        data = response.json()
        if "data" in data:
            data = data["data"]

        access_token = data.get("access-token") or data.get("access_token")
        token_type = data.get("token-type") or data.get("token_type", "Bearer")
        expires_in = data.get("expires-in") or data.get("expires_in", 900)

        if not access_token:
            logger.error(f"OAuth response missing access token. Keys: {list(data.keys())}")
            return None

        logger.info(f"OAuth token obtained (expires in {expires_in}s)")
        return f"{token_type} {access_token}", time.time() + expires_in
    except Exception as e:
        logger.error(f"OAuth login error: {e}")
        return None


def create_production_options_chain() -> 'OptionsChain':
    """Create an OptionsChain authenticated against production API via OAuth."""
    logger.info("Authenticating against production API via OAuth...")

    result = _oauth_get_token()
    if not result:
        logger.error("Production OAuth auth failed")
        return None

    session_token, token_expiry = result
    logger.info("Production OAuth authentication successful")

    return OptionsChain(
        session_token,
        base_url=PRICING_URL,
        oauth_expiry=token_expiry
    )


def discover_0dte_symbols(options_chain: 'OptionsChain', candidates: list) -> list:
    """Check which candidate symbols have 0DTE options today.

    Calls /option-chains/{symbol}/nested for each candidate and checks if
    today's date appears in the expirations list. Uses 0.5s delay between
    calls to respect API rate limits.

    Returns list of symbols confirmed to have 0DTE options today.
    """
    import time as _time

    today_str = options_chain.get_0dte_expiration()
    confirmed = []

    logger.info(f"[0DTE DISCOVERY] Checking {len(candidates)} candidates for {today_str}: {candidates}")

    for symbol in candidates:
        try:
            chain = options_chain.fetch_chain(symbol)
            if chain is None:
                logger.info(f"[0DTE] {symbol}: no chain data available")
                continue

            expirations = options_chain._extract_expirations(chain)
            exp_dates = [
                exp.get("expiration-date") or exp.get("expirationDate") or ""
                for exp in expirations
            ]

            if today_str in exp_dates:
                logger.info(f"[0DTE] {symbol}: CONFIRMED 0DTE for {today_str}")
                confirmed.append(symbol)
            else:
                nearest = exp_dates[0] if exp_dates else "none"
                logger.info(f"[0DTE] {symbol}: no 0DTE (nearest: {nearest})")

            # Rate limit between calls
            if symbol != candidates[-1]:
                _time.sleep(0.5)

        except Exception as e:
            logger.error(f"[0DTE] {symbol}: error checking chain: {e}")

    logger.info(f"[0DTE DISCOVERY] Complete: {confirmed} ({len(confirmed)}/{len(candidates)} confirmed)")
    return confirmed


class OptionsChain:
    """Fetches and manages SPY options chain data."""

    # Token refresh settings
    MAX_REFRESH_ATTEMPTS = 3
    REFRESH_COOLDOWN_SEC  = 300  # after exhausting retries, wait 5 min before trying again

    def __init__(self, session_token: str, base_url: str = None,
                 oauth_expiry: float = 0.0):
        self.session_token = session_token
        self.base_url = base_url or BASE_URL  # Default to sandbox
        self.headers = {
            "Authorization": session_token,
            "Content-Type": "application/json"
        }
        self.et_tz = pytz.timezone('US/Eastern')
        self.chain_cache: Dict = {}
        self.cache_date: Optional[date] = None
        self._refresh_attempts = 0
        self._refresh_locked_until: float = 0.0  # unix ts; 0 = not locked
        self._oauth_expiry = oauth_expiry
        self._is_oauth = (oauth_expiry > 0)

    def _refresh_token(self) -> bool:
        """Refresh the session token. Uses OAuth for production."""
        now = time.time()

        # Locked after exhausting retries — but only until the cooldown expires.
        if self._refresh_attempts >= self.MAX_REFRESH_ATTEMPTS:
            if now < self._refresh_locked_until:
                remaining = int(self._refresh_locked_until - now)
                logger.error(
                    "Max token refresh attempts (%d) reached — retrying in %ds",
                    self.MAX_REFRESH_ATTEMPTS, remaining,
                )
                return False
            # Cooldown elapsed — reset and try again
            logger.info(
                "Token refresh cooldown elapsed — resetting attempt counter and retrying"
            )
            self._refresh_attempts = 0

        self._refresh_attempts += 1
        logger.info(f"Refreshing session token (attempt {self._refresh_attempts})...")

        result = _oauth_get_token()
        if not result:
            logger.error("OAuth token refresh failed")
            if self._refresh_attempts >= self.MAX_REFRESH_ATTEMPTS:
                self._refresh_locked_until = now + self.REFRESH_COOLDOWN_SEC
                logger.warning(
                    "Token refresh locked for %ds after %d consecutive failures",
                    self.REFRESH_COOLDOWN_SEC, self.MAX_REFRESH_ATTEMPTS,
                )
            return False

        self.session_token, self._oauth_expiry = result
        self.headers["Authorization"] = self.session_token
        self._refresh_attempts = 0
        self._refresh_locked_until = 0.0
        logger.info("Session token refreshed via OAuth")
        return True

    def _check_oauth_refresh(self):
        """Refresh OAuth token if it expires within 2 minutes."""
        if self._is_oauth and time.time() > self._oauth_expiry - 120:
            logger.info("OAuth token expiring soon, refreshing...")
            self._refresh_token()

    def _is_auth_error(self, status_code: int) -> bool:
        """Check if status code indicates an authentication failure."""
        return status_code in (401, 403)

    def get_0dte_expiration(self) -> str:
        """Get today's expiration date in YYYY-MM-DD format."""
        today = datetime.now(self.et_tz).date()
        return today.strftime("%Y-%m-%d")

    def fetch_chain(self, symbol: str = "SPY", _retry: bool = True) -> Optional[Dict]:
        """Fetch the options chain for a symbol."""
        today = datetime.now(self.et_tz).date()

        # Use cache if we already fetched today
        if self.cache_date == today and symbol in self.chain_cache:
            return self.chain_cache[symbol]

        logger.info(f"Fetching options chain for {symbol}...")
        self._check_oauth_refresh()
        try:
            response = requests.get(
                f"{self.base_url}/option-chains/{symbol}/nested",
                headers=self.headers,
                timeout=10
            )

            # Handle auth errors with automatic token refresh
            if self._is_auth_error(response.status_code) and _retry:
                logger.warning(f"Auth error ({response.status_code}) fetching chain, attempting token refresh...")
                if self._refresh_token():
                    return self.fetch_chain(symbol, _retry=False)  # Retry once after refresh
                logger.error("Token refresh failed, cannot fetch chain")
                return None

            if response.status_code != 200:
                logger.error(f"Failed to fetch options chain: {response.status_code} - {response.text}")
                return None

            json_data = response.json()
            if "data" not in json_data:
                logger.error(f"Unexpected API response format: {list(json_data.keys())}")
                return None

            data = json_data["data"]

            # Log chain structure for debugging
            logger.info(f"Chain data keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")

            # Tastytrade nested chain API structure:
            # data['items'][0] = metadata wrapper with 'expirations' key containing actual expirations
            # OR data['items'] directly contains expirations (older format)
            items = data.get("items", [])
            expirations = []

            if items and isinstance(items[0], dict):
                first_item = items[0]
                # Check if this is the metadata wrapper format (has 'expirations' key)
                if "expirations" in first_item:
                    logger.info(f"Detected nested format: items[0] contains 'expirations' key")
                    expirations = first_item.get("expirations", [])
                # Check if this is the direct format (has 'expiration-date' key)
                elif "expiration-date" in first_item or "expirationDate" in first_item:
                    logger.info(f"Detected direct format: items contains expirations")
                    expirations = items
                else:
                    logger.warning(f"Unknown format. First item keys: {list(first_item.keys())}")

            # Debug: log expirations found
            logger.info(f"Expirations found: {len(expirations)}")

            if expirations:
                first_exp = expirations[0]
                if isinstance(first_exp, dict):
                    logger.info(f"First expiration keys: {list(first_exp.keys())}")
                    exp_date = first_exp.get("expiration-date", first_exp.get("expirationDate", "NO_DATE_KEY"))
                    logger.info(f"First expiration date: {exp_date}")
                    strikes = first_exp.get("strikes", [])
                    logger.info(f"Strikes in first expiration: {len(strikes)}")
                    if strikes and isinstance(strikes[0], dict):
                        logger.info(f"First strike keys: {list(strikes[0].keys())}")
            else:
                logger.warning(f"No expirations found in chain data")

            self.chain_cache[symbol] = data
            self.cache_date = today

            return data

        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching options chain for {symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching options chain: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Failed to parse options chain response: {e}")
            return None

    def _extract_expirations(self, chain: Dict) -> List[Dict]:
        """Extract expirations list from chain data, handling nested format."""
        items = chain.get("items", [])
        if not items:
            return []

        # Check if nested format: items[0] contains 'expirations' key
        if isinstance(items[0], dict) and "expirations" in items[0]:
            return items[0].get("expirations", [])

        # Direct format: items contains expirations
        if isinstance(items[0], dict) and ("expiration-date" in items[0] or "expirationDate" in items[0]):
            return items

        return []

    def get_0dte_strikes(self, symbol: str = "SPY") -> Optional[Dict]:
        """Get strikes for today's expiration."""
        chain = self.fetch_chain(symbol)
        if not chain:
            return None

        today_exp = self.get_0dte_expiration()
        expirations = self._extract_expirations(chain)

        # Debug: show available expirations
        available_dates = [exp.get("expiration-date") or exp.get("expirationDate") for exp in expirations[:5]]
        logger.info(f"Looking for 0DTE expiration: {today_exp}")
        logger.info(f"Available expirations (first 5): {available_dates}")

        # Try exact match first (handle both key formats)
        for exp in expirations:
            exp_date = exp.get("expiration-date") or exp.get("expirationDate") or exp.get("expiration_date")
            if exp_date == today_exp:
                logger.info(f"Found exact 0DTE expiration: {today_exp}")
                return exp

        # Fallback: use nearest expiration
        if expirations:
            nearest = expirations[0]
            nearest_date = nearest.get("expiration-date") or nearest.get("expirationDate")
            logger.warning(f"No 0DTE for {today_exp}, using nearest: {nearest_date}")
            return nearest

        logger.warning(f"No expirations found in chain data")
        return None

    def select_strike(self, current_price: float, is_call: bool,
                      strikes_otm: int = STRIKES_OTM, symbol: str = "SPY",
                      target_delta: Optional[float] = TARGET_DELTA) -> Optional[Dict]:
        """
        Select a strike for entry.

        When target_delta is set (default 0.50), selects the strike whose
        abs(delta) is >= target_delta and closest to it from above.
        Falls back to OTM-count logic when target_delta is None.

        Args:
            current_price: Current SPY price
            is_call: True for call, False for put
            strikes_otm: Strikes OTM to use when target_delta is None
            symbol: Underlying symbol
            target_delta: Target abs(delta) threshold (None = use strikes_otm)

        Returns:
            Dict with strike info including symbol, strike price, etc.
        """
        exp_data = self.get_0dte_strikes(symbol)
        if not exp_data:
            return None

        # Handle different key formats for strikes
        strikes = exp_data.get("strikes") or exp_data.get("items") or []
        if not strikes:
            logger.warning("No strikes found in expiration data")
            logger.warning(f"Expiration data keys: {list(exp_data.keys())}")
            return None

        exp_date = exp_data.get('expiration-date') or exp_data.get('expirationDate')
        logger.info(f"Found {len(strikes)} strikes for expiration {exp_date}")

        # Helper to get strike price with fallback keys (convert to float)
        def get_strike_price(s):
            val = s.get("strike-price") or s.get("strikePrice") or s.get("strike_price") or 0
            return float(val) if val else 0.0

        # Sort strikes by strike price
        sorted_strikes = sorted(strikes, key=get_strike_price)

        # Log available strike range
        if sorted_strikes:
            min_strike = get_strike_price(sorted_strikes[0])
            max_strike = get_strike_price(sorted_strikes[-1])
            logger.info(f"Strike range: ${min_strike} - ${max_strike} | SPY price: ${current_price:.2f}")

        # Find ATM strike index
        atm_idx = 0
        min_diff = float('inf')
        for i, strike in enumerate(sorted_strikes):
            diff = abs(get_strike_price(strike) - current_price)
            if diff < min_diff:
                min_diff = diff
                atm_idx = i

        atm_strike = get_strike_price(sorted_strikes[atm_idx])
        logger.info(f"ATM strike: ${atm_strike} (index {atm_idx})")

        # Delta-based selection (primary path)
        if target_delta is not None:
            return self._select_by_delta(
                sorted_strikes, atm_idx, is_call, target_delta, exp_date, get_strike_price
            )

        # --- Fallback: OTM-count logic ---
        if is_call:
            # For calls, go higher (OTM is above current price)
            target_idx = min(atm_idx + strikes_otm, len(sorted_strikes) - 1)
            strike_data = sorted_strikes[target_idx]
            option_symbol = strike_data.get("call") or strike_data.get("call-streamer-symbol")
            streamer_symbol = strike_data.get("call-streamer-symbol") or strike_data.get("call")
        else:
            # For puts, go lower (OTM is below current price)
            target_idx = max(atm_idx - strikes_otm, 0)
            strike_data = sorted_strikes[target_idx]
            option_symbol = strike_data.get("put") or strike_data.get("put-streamer-symbol")
            streamer_symbol = strike_data.get("put-streamer-symbol") or strike_data.get("put")

        selected_strike = get_strike_price(strike_data)
        logger.info(f"Selected {'CALL' if is_call else 'PUT'} strike: ${selected_strike} ({strikes_otm} OTM)")
        logger.info(f"  OCC symbol: {option_symbol}")
        logger.info(f"  Streamer symbol: {streamer_symbol}")

        if not option_symbol:
            logger.error(f"No option symbol found for strike {selected_strike}")
            logger.error(f"Strike data keys: {list(strike_data.keys())}")
            logger.error(f"Strike data sample: {strike_data}")
            return None

        return {
            "symbol": option_symbol,
            "streamer_symbol": streamer_symbol,
            "strike": selected_strike,
            "expiration": exp_date,
            "type": "call" if is_call else "put",
            "entry_delta": 0.0,
            "entry_gamma": 0.0,
        }

    def _get_batch_quotes(self, option_symbols: List[str], _retry: bool = True) -> Dict[str, Dict]:
        """Fetch bid/ask quotes for multiple OCC option symbols via /market-data/by-type.

        Note: This endpoint returns price/quote data only (bid, ask, mark, etc.).
        Delta and gamma are NOT available via any Tastytrade REST endpoint — they
        are only available from the DXLink WebSocket streamer (Greeks event type).
        """
        self._check_oauth_refresh()
        if not option_symbols:
            return {}

        # Correct endpoint: /market-data/by-type with repeated equity-option= params
        # Each OCC symbol is a separate key-value pair (not array notation)
        params = "&".join(
            f"equity-option={urllib.parse.quote(s, safe='')}"
            for s in option_symbols
        )
        url = f"{self.base_url}/market-data/by-type?{params}"

        try:
            response = requests.get(url, headers=self.headers, timeout=10)

            if self._is_auth_error(response.status_code) and _retry:
                logger.warning(f"Auth error ({response.status_code}) in batch quote, refreshing token...")
                if self._refresh_token():
                    return self._get_batch_quotes(option_symbols, _retry=False)
                return {}

            if response.status_code != 200:
                logger.warning(
                    f"Batch quote failed: {response.status_code} - {response.text[:200]}\n"
                    f"  URL: {url[:300]}"
                )
                return {}

            json_data = response.json()
            # Response structure: {"data": {"items": [{symbol, delta, gamma, bid, ask, ...}]}}
            items = json_data.get("data", {}).get("items", [])
            return {item.get("symbol", ""): item for item in items if item.get("symbol")}

        except Exception as e:
            logger.error(f"Batch quote error: {e}")
            return {}

    def _select_by_delta(self, sorted_strikes: list, atm_idx: int, is_call: bool,
                         target_delta: float, exp_date: str, get_strike_price) -> Optional[Dict]:
        """
        Pick the strike whose abs(delta) >= target_delta, minimized (closest to
        target_delta from above).

        For calls:  ITM = lower strike index → higher positive delta
        For puts:   ITM = higher strike index → more negative delta (larger abs)
        We check a window of 12 strikes around ATM weighted toward ITM.
        """
        opt_key = "call" if is_call else "put"
        sym_key = f"{opt_key}-streamer-symbol"

        # Candidate window: 12 strikes ITM + ATM + 2 OTM for safety
        if is_call:
            start_idx = max(0, atm_idx - 12)
            end_idx = min(len(sorted_strikes) - 1, atm_idx + 2)
        else:
            start_idx = max(0, atm_idx - 2)
            end_idx = min(len(sorted_strikes) - 1, atm_idx + 12)

        candidates = sorted_strikes[start_idx:end_idx + 1]

        # Collect OCC symbols (e.g. "SPY   260302C00685000") for the batch quotes REST API.
        # The DXLink streamer format (".SPY260302C685") returns HTTP 400 from that endpoint.
        candidate_symbols = []
        strike_map: Dict[str, dict] = {}
        for s in candidates:
            api_sym = s.get(opt_key) or s.get(sym_key)   # OCC format required by REST quotes API
            if api_sym and api_sym not in strike_map:
                candidate_symbols.append(api_sym)
                strike_map[api_sym] = s

        if not candidate_symbols:
            logger.error("No candidate symbols available for delta selection")
            return None

        logger.info(f"Fetching deltas for {len(candidate_symbols)} candidate strikes (target abs(delta)>={target_delta})...")
        quotes = self._get_batch_quotes(candidate_symbols)

        # Evaluate each candidate; capture delta + gamma per quote
        qualifying = []
        quote_greeks: Dict[str, tuple] = {}  # api_sym -> (abs_delta, gamma)
        for api_sym, strike_data in strike_map.items():
            quote = quotes.get(api_sym)
            if not quote:
                continue
            raw_delta = quote.get("delta")
            if raw_delta is None:
                continue
            abs_delta = abs(float(raw_delta))
            gamma = float(quote.get("gamma") or 0.0)
            quote_greeks[api_sym] = (abs_delta, gamma)
            strike_px = get_strike_price(strike_data)
            logger.info(f"  {api_sym}: strike=${strike_px:.2f}, delta={float(raw_delta):.3f}, gamma={gamma:.4f}")
            if abs_delta >= target_delta:
                qualifying.append((abs_delta, api_sym, strike_data))

        if not qualifying:
            logger.warning(
                f"No strikes with abs(delta) >= {target_delta} found; "
                f"falling back to ATM strike"
            )
            atm_data = sorted_strikes[atm_idx]
            occ_sym = atm_data.get(opt_key) or atm_data.get(sym_key)
            streamer = atm_data.get(sym_key) or atm_data.get(opt_key)
            strike_px = get_strike_price(atm_data)
            logger.info(f"Fallback ATM {'CALL' if is_call else 'PUT'}: ${strike_px:.2f}")
            if not occ_sym:
                return None
            atm_greeks = quote_greeks.get(occ_sym, (0.0, 0.0))
            return {
                "symbol": occ_sym,
                "streamer_symbol": streamer,
                "strike": strike_px,
                "expiration": exp_date,
                "type": "call" if is_call else "put",
                "entry_delta": atm_greeks[0],
                "entry_gamma": atm_greeks[1],
            }

        # Sort ascending by abs_delta → smallest qualifying value first (closest to target)
        qualifying.sort(key=lambda x: x[0])
        abs_delta_chosen, api_sym_chosen, strike_data_chosen = qualifying[0]
        gamma_chosen = quote_greeks.get(api_sym_chosen, (0.0, 0.0))[1]

        # Return OCC symbol for order placement, streamer symbol for DXLink streaming
        occ_sym = strike_data_chosen.get(opt_key)       # e.g. "SPY   260226C00690000"
        streamer_sym = strike_data_chosen.get(sym_key)  # e.g. ".SPY260226C690"
        selected_strike = get_strike_price(strike_data_chosen)

        logger.info(
            f"Delta-selected {'CALL' if is_call else 'PUT'}: "
            f"strike=${selected_strike:.2f}, abs(delta)={abs_delta_chosen:.3f}, gamma={gamma_chosen:.4f} "
            f"(target>={target_delta})"
        )
        logger.info(f"  OCC symbol: {occ_sym}")
        logger.info(f"  Streamer symbol: {streamer_sym}")

        if not occ_sym and not streamer_sym:
            return None

        return {
            "symbol": occ_sym or streamer_sym,
            "streamer_symbol": streamer_sym or occ_sym,
            "strike": selected_strike,
            "expiration": exp_date,
            "type": "call" if is_call else "put",
            "entry_delta": abs_delta_chosen,
            "entry_gamma": gamma_chosen,
        }

    def get_option_quote(self, option_symbol: str, _retry: bool = True) -> Optional[Dict]:
        """Fetch current quote for an option."""
        self._check_oauth_refresh()
        try:
            # Correct endpoint: /market-data/by-type with equity-option= param
            encoded = urllib.parse.quote(option_symbol, safe='')
            url = f"{self.base_url}/market-data/by-type?equity-option={encoded}"
            response = requests.get(
                url,
                headers=self.headers,
                timeout=3
            )

            # Handle auth errors with automatic token refresh
            if self._is_auth_error(response.status_code) and _retry:
                logger.warning(f"Auth error ({response.status_code}) fetching quote, attempting token refresh...")
                if self._refresh_token():
                    return self.get_option_quote(option_symbol, _retry=False)
                logger.warning("Token refresh failed, cannot fetch quote")
                return None

            if response.status_code != 200:
                logger.warning(f"Failed to fetch option quote: {response.status_code}")
                return None

            json_data = response.json()
            data = json_data.get("data", {})
            items = data.get("items", [])

            if items:
                return items[0]
            return None

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching quote for {option_symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Network error fetching option quote: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.warning(f"Failed to parse option quote response: {e}")
            return None

    def get_mid_price(self, option_symbol: str) -> Optional[float]:
        """Get the mid price for an option."""
        quote = self.get_option_quote(option_symbol)
        if not quote:
            logger.warning(f"No quote returned for {option_symbol}")
            return None

        # API returns values as strings — convert defensively
        def _f(val) -> float:
            try:
                return float(val) if val else 0.0
            except (TypeError, ValueError):
                return 0.0

        bid = _f(quote.get("bid"))
        ask = _f(quote.get("ask"))
        last = _f(quote.get("last"))

        logger.info(f"Quote for {option_symbol}: bid=${bid:.2f} ask=${ask:.2f} last=${last:.2f}")

        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            logger.info(f"Mid price: ${mid:.2f}")
            return mid
        elif ask > 0:
            logger.info(f"Using ask price (no bid): ${ask:.2f}")
            return ask

        logger.warning(f"No valid bid/ask for {option_symbol}")
        return None

    def get_equity_quote(self, symbol: str, _retry: bool = True) -> Optional[Dict]:
        """Fetch current quote for an UNDERLYING equity (e.g. SPY itself),
        via /market-data/by-type?equity=<symbol> -- the equity counterpart
        to get_option_quote's equity-option= param. Added for this
        project's fetch_chain_snapshot(), which needs the underlying's own
        current price; get_mid_price/get_option_quote can't provide that,
        they're option-symbol-only (confirmed against spy_0dte_strat: every
        real call site there passes an OPTION symbol, never a bare ticker --
        this project's first version of fetch_chain_snapshot made exactly
        that mistake, caught before ever hitting the real API)."""
        self._check_oauth_refresh()
        try:
            encoded = urllib.parse.quote(symbol, safe='')
            url = f"{self.base_url}/market-data/by-type?equity={encoded}"
            response = requests.get(url, headers=self.headers, timeout=3)

            if self._is_auth_error(response.status_code) and _retry:
                logger.warning(f"Auth error ({response.status_code}) fetching equity quote, attempting token refresh...")
                if self._refresh_token():
                    return self.get_equity_quote(symbol, _retry=False)
                logger.warning("Token refresh failed, cannot fetch equity quote")
                return None

            if response.status_code != 200:
                logger.warning(f"Failed to fetch equity quote: {response.status_code} - {response.text[:200]}")
                return None

            items = response.json().get("data", {}).get("items", [])
            return items[0] if items else None

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching equity quote for {symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Network error fetching equity quote: {e}")
            return None

    def get_equity_mid_price(self, symbol: str) -> Optional[float]:
        """Mid price for an underlying equity -- same bid/ask/last
        fallback logic as get_mid_price, built on get_equity_quote."""
        quote = self.get_equity_quote(symbol)
        if not quote:
            logger.warning(f"No equity quote returned for {symbol}")
            return None

        def _f(val) -> float:
            try:
                return float(val) if val else 0.0
            except (TypeError, ValueError):
                return 0.0

        bid, ask, last = _f(quote.get("bid")), _f(quote.get("ask")), _f(quote.get("last"))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        if last > 0:
            return last
        logger.warning(f"No valid bid/ask/last for {symbol}")
        return None

    def get_vix_day_change_pct(self) -> Optional[float]:
        """VIX's live % change vs its own previous settled close, straight
        off the quote's own last/prev-close fields -- confirmed live
        2026-11-05 that /market-data/by-type?equity=VIX answers VIX (an
        Index instrument-type) the same as ?index=VIX would, so this
        reuses get_equity_quote rather than adding a second code path.
        No separate historical fetch needed: Tastytrade's quote already
        carries both numbers. Built for the VIX-recross-exit check in
        daily_loop.py."""
        quote = self.get_equity_quote("VIX")
        if not quote:
            logger.warning("No VIX quote returned")
            return None
        try:
            last = float(quote.get("last") or 0)
            prev_close = float(quote.get("prev-close") or 0)
        except (TypeError, ValueError):
            return None
        if last <= 0 or prev_close <= 0:
            logger.warning(f"No valid last/prev-close in VIX quote: {quote}")
            return None
        return (last / prev_close - 1) * 100
