from unittest.mock import patch, MagicMock

import run_trade_day


class TestMain:
    def test_sandbox_login_failure_aborts_cleanly_and_alerts(self):
        with patch("streamer.TastytradeAuth.login", return_value=False), \
             patch("alerts.notify_alert") as mock_alert:
            rc = run_trade_day.main()
        assert rc == 1
        mock_alert.assert_called_once()
        assert "sandbox login FAILED" in mock_alert.call_args.args[0]

    def test_production_login_failure_aborts_cleanly_and_alerts(self):
        with patch("streamer.TastytradeAuth.login", return_value=True), \
             patch("options.create_production_options_chain", return_value=None), \
             patch("alerts.notify_alert") as mock_alert:
            rc = run_trade_day.main()
        assert rc == 1
        mock_alert.assert_called_once()
        assert "production login FAILED" in mock_alert.call_args.args[0]

    def test_sets_execute_orders_true_and_runs_day(self):
        """Confirms the wiring: both logins succeed, EXECUTE_ORDERS gets
        flipped for this process, and run_day() is actually invoked with
        the real chain object and a real executor pointed at the sandbox
        session -- without touching the network (run_day itself mocked)."""
        with patch("streamer.TastytradeAuth.login", return_value=True), \
             patch("streamer.TastytradeAuth.session_token", "fake-token", create=True), \
             patch("options.create_production_options_chain", return_value=MagicMock()) as mock_chain, \
             patch("daily_loop.run_day") as mock_run_day:
            # patch() auto-detects `async def run_day` and builds an
            # AsyncMock -- its return_value is what AWAITING it yields,
            # not a coroutine to wrap again (that was a real bug caught
            # here: wrapping in _async_none() double-wrapped it, so
            # `position` came back as a coroutine object instead of None).
            import condor_order_executor as coe
            mock_run_day.return_value = None
            rc = run_trade_day.main()

        assert rc == 0
        assert coe.EXECUTE_ORDERS is True
        mock_run_day.assert_called_once()
        called_chain = mock_run_day.call_args.args[0]
        assert called_chain is mock_chain.return_value

    def test_unhandled_exception_alerts_and_reraises(self):
        with patch("streamer.TastytradeAuth.login", side_effect=RuntimeError("boom")), \
             patch("alerts.notify_crash") as mock_crash:
            try:
                run_trade_day.main()
                assert False, "expected the exception to propagate"
            except RuntimeError:
                pass
        mock_crash.assert_called_once()
        assert "boom" in mock_crash.call_args.args[0]
