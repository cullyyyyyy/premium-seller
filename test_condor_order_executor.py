import math
from unittest.mock import MagicMock, patch

import pytest

import condor_order_executor as coe
from iron_butterfly import build_iron_butterfly
from test_iron_butterfly import _base_chain


def _sample_result(target_pct=0.25, contracts=1):
    chain = _base_chain()
    return build_iron_butterfly(chain, target_pct=target_pct, contracts=contracts)


class TestPayloadShapes:
    def test_entry_payload_has_four_legs_correct_actions(self):
        result = _sample_result()
        payload = coe.build_entry_payload(result, price=result.net_credit)
        assert payload["order-type"] == "Limit"
        assert payload["price-effect"] == "Credit"
        assert payload["price"] == round(result.net_credit, 2)
        legs = payload["legs"]
        assert len(legs) == 4
        actions = {l["symbol"]: l["action"] for l in legs}
        assert actions[result.short_call.symbol] == "Sell to Open"
        assert actions[result.short_put.symbol] == "Sell to Open"
        assert actions[result.long_call.symbol] == "Buy to Open"
        assert actions[result.long_put.symbol] == "Buy to Open"
        assert all(l["instrument-type"] == "Equity Option" for l in legs)
        assert all(l["quantity"] == result.contracts for l in legs)

    def test_exit_payload_reverses_every_action(self):
        result = _sample_result()
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        payload = coe.build_exit_payload(position, price=1.0)
        assert payload["price-effect"] == "Debit"
        legs = payload["legs"]
        actions = {l["symbol"]: l["action"] for l in legs}
        assert actions[result.short_call.symbol] == "Buy to Close"
        assert actions[result.short_put.symbol] == "Buy to Close"
        assert actions[result.long_call.symbol] == "Sell to Close"
        assert actions[result.long_put.symbol] == "Sell to Close"

    def test_external_id_is_unique_per_call_unless_given(self):
        result = _sample_result()
        p1 = coe.build_entry_payload(result, price=1.0)
        p2 = coe.build_entry_payload(result, price=1.0)
        assert p1["external-identifier"] != p2["external-identifier"]
        p3 = coe.build_entry_payload(result, price=1.0, external_id="fixed-id")
        assert p3["external-identifier"] == "fixed-id"


class TestPaperModeEntry:
    def test_paper_entry_fills_at_bid_ask_not_mid(self):
        """result.net_credit (5.60) is MID-based; the realistic paper
        fill should use bid for shorts, ask for longs:
        (3.00 + 2.90) - (0.25 + 0.20) = 5.90 + ... let's just recompute
        against the fixture's exact bid/ask."""
        result = _sample_result()
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            order_result, position = executor.submit_entry(result)

        expected = ((result.short_call.bid + result.short_put.bid)
                    - (result.long_call.ask + result.long_put.ask))
        assert order_result.status == coe.OrderStatus.FILLED
        assert math.isclose(order_result.net_price, expected, abs_tol=1e-9)
        assert position.entry_net_credit == order_result.net_price
        assert position.status == "open"
        assert position.entry_order_id is None  # no real order in paper mode
        # Planning (mid-based) and realistic (bid/ask-based) fills must differ
        # here since the fixture's bid != ask.
        assert order_result.net_price != result.net_credit

    def test_paper_entry_never_calls_the_http_session(self):
        result = _sample_result()
        executor = coe.CondorOrderExecutor(session_token="tok")
        executor._session = MagicMock()
        with patch.object(coe, "EXECUTE_ORDERS", False):
            executor.submit_entry(result)
        executor._session.post.assert_not_called()


class TestPaperModeExit:
    def _open_position(self, result):
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        return executor, position

    def test_paper_exit_computes_realized_pnl(self):
        result = _sample_result(contracts=2)
        executor, position = self._open_position(result)

        quotes = {
            position.short_call.symbol: (0.10, 0.15),
            position.short_put.symbol: (0.05, 0.10),
            position.long_call.symbol: (0.01, 0.02),
            position.long_put.symbol: (0.01, 0.02),
        }
        with patch.object(coe, "EXECUTE_ORDERS", False):
            order_result = executor.submit_exit(position, quotes, reason="profit_target")

        # buy back shorts at ASK, sell wings at BID:
        # debit = (0.15 + 0.10) - (0.01 + 0.01) = 0.23
        expected_debit = (0.15 + 0.10) - (0.01 + 0.01)
        assert math.isclose(order_result.net_price, expected_debit, abs_tol=1e-9)
        assert position.status == "closed"
        assert position.exit_reason == "profit_target"
        expected_pnl = (position.entry_net_credit - expected_debit) * position.multiplier * position.contracts
        assert math.isclose(position.realized_pnl, expected_pnl, abs_tol=1e-9)

    def test_paper_exit_never_calls_the_http_session(self):
        result = _sample_result()
        executor, position = self._open_position(result)
        quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}
        executor._session = MagicMock()
        with patch.object(coe, "EXECUTE_ORDERS", False):
            executor.submit_exit(position, quotes, reason="backstop")
        executor._session.post.assert_not_called()


class TestExtractNetFill:
    def test_computes_signed_net_price_and_min_quantity(self):
        order_data = {
            "legs": [
                {"action": "Sell to Open", "fills": [{"fill-price": 3.00, "filled-quantity": 2}]},
                {"action": "Sell to Open", "fills": [{"fill-price": 2.90, "filled-quantity": 2}]},
                {"action": "Buy to Open", "fills": [{"fill-price": 0.25, "filled-quantity": 2}]},
                {"action": "Buy to Open", "fills": [{"fill-price": 0.20, "filled-quantity": 1}]},  # partial
            ]
        }
        net_price, qty = coe._extract_net_fill(order_data)
        assert math.isclose(net_price, (3.00 + 2.90) - (0.25 + 0.20), abs_tol=1e-9)
        assert qty == 1  # bounded by the partially-filled leg

    def test_returns_none_when_any_leg_unfilled(self):
        order_data = {"legs": [
            {"action": "Sell to Open", "fills": [{"fill-price": 3.00, "filled-quantity": 1}]},
            {"action": "Sell to Open", "fills": []},
        ]}
        net_price, qty = coe._extract_net_fill(order_data)
        assert net_price is None
        assert qty == 0


class TestLiveModeHttpPlumbing:
    """Live mode is mocked entirely at the requests.Session level --
    never touches a real socket."""

    def _executor_with_mock_session(self):
        executor = coe.CondorOrderExecutor(session_token="tok")
        executor._session = MagicMock()
        return executor

    def test_post_order_dry_run_hits_dry_run_url(self):
        executor = self._executor_with_mock_session()
        resp = MagicMock(status_code=201)
        resp.json.return_value = {"data": {"order": {"id": "123"}}}
        executor._session.post.return_value = resp

        order_id = executor._post_order({"external-identifier": "x"}, dry_run=True)
        assert order_id == "123"
        called_url = executor._session.post.call_args.args[0]
        assert called_url.endswith("/orders/dry-run")

    def test_post_order_live_hits_orders_url(self):
        executor = self._executor_with_mock_session()
        resp = MagicMock(status_code=201)
        resp.json.return_value = {"data": {"order": {"id": "456"}}}
        executor._session.post.return_value = resp

        order_id = executor._post_order({"external-identifier": "x"}, dry_run=False)
        assert order_id == "456"
        called_url = executor._session.post.call_args.args[0]
        assert called_url.endswith("/orders")
        assert not called_url.endswith("/orders/dry-run")

    def test_post_order_retries_on_500_then_succeeds(self):
        executor = self._executor_with_mock_session()
        fail_resp = MagicMock(status_code=500, text="server error")
        ok_resp = MagicMock(status_code=201)
        ok_resp.json.return_value = {"data": {"order": {"id": "789"}}}
        executor._session.post.side_effect = [fail_resp, ok_resp]

        with patch("condor_order_executor.time.sleep"):
            order_id = executor._post_order({"external-identifier": "x"})
        assert order_id == "789"
        assert executor._session.post.call_count == 2

    def test_post_order_gives_up_after_max_retries(self):
        executor = self._executor_with_mock_session()
        fail_resp = MagicMock(status_code=500, text="server error")
        executor._session.post.return_value = fail_resp

        with patch("condor_order_executor.time.sleep"):
            order_id = executor._post_order({"external-identifier": "x"})
        assert order_id is None
        # initial attempt + ORDER_POST_MAX_RETRIES retries
        from config import ORDER_POST_MAX_RETRIES
        assert executor._session.post.call_count == ORDER_POST_MAX_RETRIES + 1

    def test_post_order_401_refreshes_token_and_retries(self):
        executor = self._executor_with_mock_session()
        auth = MagicMock()
        auth.login.return_value = True
        auth.session_token = "new-token"
        executor.auth = auth

        unauth_resp = MagicMock(status_code=401)
        ok_resp = MagicMock(status_code=201)
        ok_resp.json.return_value = {"data": {"order": {"id": "999"}}}
        executor._session.post.side_effect = [unauth_resp, ok_resp]

        order_id = executor._post_order({"external-identifier": "x"})
        assert order_id == "999"
        assert executor.headers["Authorization"] == "new-token"

    def test_post_order_rejection_does_not_retry(self):
        executor = self._executor_with_mock_session()
        rejected_resp = MagicMock(status_code=400, text="bad request")
        executor._session.post.return_value = rejected_resp

        order_id = executor._post_order({"external-identifier": "x"})
        assert order_id is None
        assert executor._session.post.call_count == 1

    def test_check_order_once_extracts_status_and_net_fill(self):
        executor = self._executor_with_mock_session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {
            "status": "Filled",
            "legs": [
                {"action": "Sell to Open", "fills": [{"fill-price": 3.00, "filled-quantity": 1}]},
                {"action": "Sell to Open", "fills": [{"fill-price": 2.90, "filled-quantity": 1}]},
                {"action": "Buy to Open", "fills": [{"fill-price": 0.25, "filled-quantity": 1}]},
                {"action": "Buy to Open", "fills": [{"fill-price": 0.20, "filled-quantity": 1}]},
            ],
        }}
        executor._session.get.return_value = resp

        status, net_price, qty = executor._check_order_once("order-1")
        assert status == "filled"
        assert math.isclose(net_price, (3.00 + 2.90) - (0.25 + 0.20), abs_tol=1e-9)
        assert qty == 1

    def test_poll_fill_times_out_cleanly(self):
        """Real (tiny) timeout/poll interval instead of mocking the time
        module -- mocking time.time() here is fragile because it's the
        SAME module object the stdlib logging module calls internally
        (this function logs a warning on timeout), so a short side_effect
        list gets exhausted by logging's own time.time() calls too."""
        executor = self._executor_with_mock_session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"status": "live", "legs": []}}
        executor._session.get.return_value = resp
        executor._session.delete.return_value = MagicMock(status_code=201)  # the cancel _poll_fill issues

        status, net_price, qty = executor._poll_fill(
            "order-1", expected_contracts=1, timeout_sec=0.05, poll_interval=0.01,
        )
        assert status == coe.OrderStatus.TIMEOUT

    def test_poll_fill_cancels_the_resting_order_on_timeout(self):
        """A timed-out multi-leg order must never be left resting on the
        book unattended -- _poll_fill has to actually cancel it, not just
        report TIMEOUT and leave the caller to remember to."""
        executor = self._executor_with_mock_session()
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"data": {"status": "live", "legs": []}}
        executor._session.get.return_value = get_resp
        delete_resp = MagicMock(status_code=201)
        executor._session.delete.return_value = delete_resp

        executor._poll_fill("order-1", expected_contracts=1, timeout_sec=0.05, poll_interval=0.01)

        executor._session.delete.assert_called_once()
        called_url = executor._session.delete.call_args.args[0]
        assert called_url.endswith("/orders/order-1")

    def test_cancel_order_401_refreshes_token_and_retries(self):
        executor = self._executor_with_mock_session()
        auth = MagicMock()
        auth.login.return_value = True
        auth.session_token = "fresh-token"
        executor.auth = auth
        unauth_resp = MagicMock(status_code=401)
        ok_resp = MagicMock(status_code=200)
        executor._session.delete.side_effect = [unauth_resp, ok_resp]

        assert executor._cancel_order("order-1") is True
        assert executor.headers["Authorization"] == "fresh-token"

    def test_cancel_order_already_terminal_counts_as_success(self):
        executor = self._executor_with_mock_session()
        executor._session.delete.return_value = MagicMock(status_code=422)
        assert executor._cancel_order("order-1") is True
