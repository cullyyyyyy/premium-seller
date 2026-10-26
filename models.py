from dataclasses import dataclass


@dataclass
class Candle:
    """One OHLCV bar, as streamer.py's DXLink candle channel emits it.
    Kept here (not a full indicators.py port from spy_0dte_strat) since
    this project has no RSI/EMA/ADX indicator engine -- entries are
    delta-targeted at a fixed time of day, not signal-driven."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = ""
