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
        expected_gross_pnl = (position.entry_net_credit - expected_debit) * position.multiplier * position.contracts
        assert math.isclose(position.gross_pnl, expected_gross_pnl, abs_tol=1e-9)
        # realized_pnl is NET of the real round-trip fees (2026-10-26: "these
        # will need to be net") -- not just the options-market price move.
        assert math.isclose(position.realized_pnl, expected_gross_pnl - position.round_trip_fees, abs_tol=1e-9)
        assert position.round_trip_fees > 0

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
        """fill-price and quantity as JSON STRINGS, key "quantity" (not
        "filled-quantity") -- the real shape, confirmed against two
        actually-filled sandbox orders 2026-10-26 (see
        test_matches_a_real_captured_filled_order_response below)."""
        order_data = {
            "legs": [
                {"action": "Sell to Open", "fills": [{"fill-price": "3.00", "quantity": 2}]},
                {"action": "Sell to Open", "fills": [{"fill-price": "2.90", "quantity": 2}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.25", "quantity": 2}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.20", "quantity": 1}]},  # partial
            ]
        }
        net_price, qty = coe._extract_net_fill(order_data)
        assert math.isclose(net_price, (3.00 + 2.90) - (0.25 + 0.20), abs_tol=1e-9)
        assert qty == 1  # bounded by the partially-filled leg

    def test_returns_none_when_any_leg_unfilled(self):
        order_data = {"legs": [
            {"action": "Sell to Open", "fills": [{"fill-price": "3.00", "quantity": 1}]},
            {"action": "Sell to Open", "fills": []},
        ]}
        net_price, qty = coe._extract_net_fill(order_data)
        assert net_price is None
        assert qty == 0

    def test_matches_a_real_captured_filled_order_response(self):
        """Pinned from an actual filled sandbox order (id 2018359,
        2026-10-26): sell call $0.55 + sell put $0.76 - (buy call $0.07
        + buy put $0.08) = 1.16 net credit. This is the exact shape
        GET /accounts/{acct}/orders/{id} returned for a real filled
        4-leg order -- catches any future regression on the string/int
        casting or the "quantity" key name."""
        order_data = {
            "id": 2018359, "status": "Filled",
            "legs": [
                {"action": "Sell to Open", "instrument-type": "Equity Option", "quantity": 1,
                 "remaining-quantity": 0, "symbol": "SPY   261026C00766000",
                 "fills": [{"destination-venue": "MARKET_SIMULATOR_A", "fill-id": "33116",
                            "fill-price": "0.55", "filled-at": "2026-10-26T18:00:42.990+00:00", "quantity": 1}]},
                {"action": "Sell to Open", "instrument-type": "Equity Option", "quantity": 1,
                 "remaining-quantity": 0, "symbol": "SPY   261026P00766000",
                 "fills": [{"destination-venue": "MARKET_SIMULATOR_A", "fill-id": "33117",
                            "fill-price": "0.76", "filled-at": "2026-10-26T18:00:42.990+00:00", "quantity": 1}]},
                {"action": "Buy to Open", "instrument-type": "Equity Option", "quantity": 1,
                 "remaining-quantity": 0, "symbol": "SPY   261026C00769000",
                 "fills": [{"destination-venue": "MARKET_SIMULATOR_A", "fill-id": "33118",
                            "fill-price": "0.07", "filled-at": "2026-10-26T18:00:42.991+00:00", "quantity": 1}]},
                {"action": "Buy to Open", "instrument-type": "Equity Option", "quantity": 1,
                 "remaining-quantity": 0, "symbol": "SPY   261026P00763000",
                 "fills": [{"destination-venue": "MARKET_SIMULATOR_A", "fill-id": "33119",
                            "fill-price": "0.08", "filled-at": "2026-10-26T18:00:42.992+00:00", "quantity": 1}]},
            ],
        }
        net_price, qty = coe._extract_net_fill(order_data)
        assert math.isclose(net_price, 1.16, abs_tol=1e-9)
        assert qty == 1


class TestSubmitExitPriceOverride:
    """price_override lets daily_loop.py's backstop-retry path escalate
    the submitted price past the mid-based planning figure (see
    daily_loop.run_day's docstring for the real gap this supports)."""

    def test_price_override_is_used_instead_of_planning_debit(self):
        result = _sample_result(contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)

        executor._session = MagicMock()
        post_resp = MagicMock(status_code=201)
        post_resp.json.return_value = {"data": {"order": {"id": "1"}}}
        executor._session.post.return_value = post_resp

        quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}
        with patch.object(coe, "EXECUTE_ORDERS", True), \
             patch.object(executor, "_poll_fill", return_value=(coe.OrderStatus.FILLED, 1.0, 1)):
            executor.submit_exit(position, quotes, reason="backstop", price_override=9.99)

        sent_payload = executor._session.post.call_args.kwargs["json"]
        assert sent_payload["price"] == 9.99

    def test_no_override_falls_back_to_planning_debit(self):
        result = _sample_result(contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)

        executor._session = MagicMock()
        post_resp = MagicMock(status_code=201)
        post_resp.json.return_value = {"data": {"order": {"id": "1"}}}
        executor._session.post.return_value = post_resp

        quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}
        expected_planning_debit = coe.mid_debit_to_close(position, quotes)
        with patch.object(coe, "EXECUTE_ORDERS", True), \
             patch.object(executor, "_poll_fill", return_value=(coe.OrderStatus.FILLED, 1.0, 1)):
            executor.submit_exit(position, quotes, reason="backstop")

        sent_payload = executor._session.post.call_args.kwargs["json"]
        assert math.isclose(sent_payload["price"], round(expected_planning_debit, 2), abs_tol=1e-9)


class TestLiveModeExitSignConvention:
    """Regression test for a real bug found live 2026-10-26: a genuinely
    filled closing order's raw _extract_net_fill value comes back
    NEGATIVE (Buy-to-Close/Sell-to-Close is the reverse of Sell-to-Open/
    Buy-to-Open), but every other debit figure in this module
    (_simulate_exit_fill, mid_debit_to_close) is a POSITIVE cost-to-
    close. submit_exit's live path must flip the sign before using it."""

    def test_live_exit_fill_produces_a_positive_debit_and_correct_pnl(self):
        result = _sample_result(contracts=1)
        executor = coe.CondorOrderExecutor(session_token="tok")
        with patch.object(coe, "EXECUTE_ORDERS", False):
            _, position = executor.submit_entry(result)
        position.entry_net_credit = 1.16  # pin to the real captured-order figure

        executor._session = MagicMock()
        post_resp = MagicMock(status_code=201)
        post_resp.json.return_value = {"data": {"order": {"id": "2018418"}}}
        executor._session.post.return_value = post_resp

        # Shape matches the real filled CLOSE order captured 2026-10-26:
        # buy back the shorts (debit), sell the wings (credit) -- raw
        # extraction is negative (a net debit on this order).
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"data": {
            "status": "Filled",
            "legs": [
                {"action": "Buy to Close", "fills": [{"fill-price": "0.49", "quantity": 1}]},
                {"action": "Buy to Close", "fills": [{"fill-price": "0.85", "quantity": 1}]},
                {"action": "Sell to Close", "fills": [{"fill-price": "0.05", "quantity": 1}]},
                {"action": "Sell to Close", "fills": [{"fill-price": "0.08", "quantity": 1}]},
            ],
        }}
        executor._session.get.return_value = get_resp

        quotes = {leg.symbol: (0.05, 0.10) for leg in position.legs}  # only used for the planning price
        with patch.object(coe, "EXECUTE_ORDERS", True):
            order_result = executor.submit_exit(position, quotes, reason="profit_target")

        expected_debit = (0.49 + 0.85) - (0.05 + 0.08)  # = 1.21, positive
        assert math.isclose(order_result.net_price, expected_debit, abs_tol=1e-9)
        assert position.status == "closed"
        assert math.isclose(position.exit_debit, expected_debit, abs_tol=1e-9)
        expected_gross_pnl = (1.16 - expected_debit) * position.multiplier * position.contracts
        assert math.isclose(position.gross_pnl, expected_gross_pnl, abs_tol=1e-9)
        assert math.isclose(position.realized_pnl, expected_gross_pnl - position.round_trip_fees, abs_tol=1e-9)


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
                {"action": "Sell to Open", "fills": [{"fill-price": "3.00", "quantity": 1}]},
                {"action": "Sell to Open", "fills": [{"fill-price": "2.90", "quantity": 1}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.25", "quantity": 1}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.20", "quantity": 1}]},
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

    def test_poll_fill_catches_a_fill_that_races_the_cancel(self):
        """Real race found live 2026-10-26: an order can genuinely fill in
        the gap between the last poll and the cancel request landing --
        the cancel then correctly reports "already terminal" (422), which
        is ambiguous (filled vs. actually expired) from that return value
        alone. _poll_fill must re-check status after cancelling and return
        FILLED, not TIMEOUT, when that's what actually happened. Caught
        live: an 8-contract order was reported TIMEOUT and treated as
        "nothing opened" while it was genuinely sitting filled in the
        account the whole time."""
        executor = self._executor_with_mock_session()
        live_resp = MagicMock(status_code=200)
        live_resp.json.return_value = {"data": {"status": "live", "legs": []}}
        filled_resp = MagicMock(status_code=200)
        filled_resp.json.return_value = {"data": {
            "status": "Filled",
            "legs": [
                {"action": "Sell to Open", "fills": [{"fill-price": "0.55", "quantity": 8}]},
                {"action": "Sell to Open", "fills": [{"fill-price": "0.46", "quantity": 8}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.08", "quantity": 8}]},
                {"action": "Buy to Open", "fills": [{"fill-price": "0.08", "quantity": 8}]},
            ],
        }}
        # "live" for every poll during the loop (however many that ends up
        # being, real-time-dependent), "filled" only once the cancel has
        # actually been issued -- a flag, not a fixed-length side_effect
        # list, so this isn't sensitive to exactly how many polls happen
        # before the real-time deadline hits.
        cancel_issued = {"done": False}

        def fake_delete(*a, **k):
            cancel_issued["done"] = True
            return MagicMock(status_code=422)  # "already terminal"

        def fake_get(*a, **k):
            return filled_resp if cancel_issued["done"] else live_resp

        executor._session.delete.side_effect = fake_delete
        executor._session.get.side_effect = fake_get

        status, net_price, filled_qty = executor._poll_fill(
            "order-1", expected_contracts=8, timeout_sec=0.02, poll_interval=0.01,
        )

        assert status == coe.OrderStatus.FILLED
        assert math.isclose(net_price, (0.55 + 0.46) - (0.08 + 0.08), abs_tol=1e-9)
        assert filled_qty == 8

    def test_poll_fill_genuinely_times_out_when_cancel_truly_cancels(self):
        """The counterpart to the race test above: when the order really
        is still live and the cancel genuinely cancels it (not a race),
        the post-cancel check must still correctly report TIMEOUT."""
        executor = self._executor_with_mock_session()
        live_resp = MagicMock(status_code=200)
        live_resp.json.return_value = {"data": {"status": "live", "legs": []}}
        cancelled_resp = MagicMock(status_code=200)
        cancelled_resp.json.return_value = {"data": {"status": "Cancelled", "legs": []}}
        cancel_issued = {"done": False}

        def fake_delete(*a, **k):
            cancel_issued["done"] = True
            return MagicMock(status_code=201)

        def fake_get(*a, **k):
            return cancelled_resp if cancel_issued["done"] else live_resp

        executor._session.delete.side_effect = fake_delete
        executor._session.get.side_effect = fake_get

        status, net_price, filled_qty = executor._poll_fill(
            "order-1", expected_contracts=1, timeout_sec=0.02, poll_interval=0.01,
        )

        assert status == coe.OrderStatus.TIMEOUT
        assert net_price is None

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
