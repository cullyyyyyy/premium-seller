from unittest.mock import MagicMock, patch

from options import OptionsChain


def _chain():
    return OptionsChain(session_token="tok", base_url="https://api.cert.tastyworks.com")


class TestGetEquityQuote:
    def test_uses_equity_param_not_equity_option(self):
        """The bug this guards against: fetch_chain_snapshot's first
        version called get_mid_price("SPY"), which hits
        /market-data/by-type?equity-option=SPY -- wrong param for a
        bare underlying ticker. get_equity_quote must use equity=."""
        chain = _chain()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"items": [{"symbol": "SPY", "bid": "670.10", "ask": "670.14"}]}}
        with patch("options.requests.get", return_value=resp) as mock_get:
            quote = chain.get_equity_quote("SPY")
        assert quote["symbol"] == "SPY"
        called_url = mock_get.call_args.args[0]
        assert "equity=SPY" in called_url
        assert "equity-option=" not in called_url

    def test_returns_none_on_non_200(self):
        chain = _chain()
        resp = MagicMock(status_code=500, text="server error")
        with patch("options.requests.get", return_value=resp):
            assert chain.get_equity_quote("SPY") is None

    def test_returns_none_when_no_items(self):
        chain = _chain()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"items": []}}
        with patch("options.requests.get", return_value=resp):
            assert chain.get_equity_quote("SPY") is None


class TestGetEquityMidPrice:
    def test_mid_of_bid_ask(self):
        chain = _chain()
        with patch.object(chain, "get_equity_quote", return_value={"bid": "670.10", "ask": "670.14", "last": "670.12"}):
            assert chain.get_equity_mid_price("SPY") == 670.12

    def test_falls_back_to_last_when_no_bid_ask(self):
        chain = _chain()
        with patch.object(chain, "get_equity_quote", return_value={"bid": "0", "ask": "0", "last": "670.12"}):
            assert chain.get_equity_mid_price("SPY") == 670.12

    def test_none_when_no_quote_at_all(self):
        chain = _chain()
        with patch.object(chain, "get_equity_quote", return_value=None):
            assert chain.get_equity_mid_price("SPY") is None
