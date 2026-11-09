from unittest.mock import patch, MagicMock

import alerts


class TestPost:
    def test_posts_json_payload_to_configured_webhook(self):
        with patch("alerts.urllib.request.urlopen") as mock_open:
            alerts.notify_alert("hello")
        assert mock_open.called
        req = mock_open.call_args.args[0]
        assert req.data == b'{"text": "hello"}'

    def test_never_raises_on_network_failure(self):
        """Fire-and-forget -- a Slack outage must never crash the caller,
        especially an unattended daily run with nobody watching."""
        with patch("alerts.urllib.request.urlopen", side_effect=Exception("network down")):
            alerts.notify_alert("hello")  # must not raise


class TestNotifyHelpers:
    def test_notify_entry_includes_strike_contracts_and_dollars(self):
        with patch("alerts._post") as mock_post:
            alerts.notify_entry(short_strike=765.0, contracts=8, net_credit=0.91, capital_at_risk=872.0)
        text = mock_post.call_args.args[0]
        assert "765.00" in text
        assert "8ct" in text
        assert "872.00" in text

    def test_notify_exit_sign_reflects_pnl(self):
        with patch("alerts._post") as mock_post:
            alerts.notify_exit(reason="profit_target", exit_debit=0.68, realized_pnl=143.88)
        assert "+" in mock_post.call_args.args[0]

        with patch("alerts._post") as mock_post:
            alerts.notify_exit(reason="backstop", exit_debit=1.00, realized_pnl=-924.46)
        assert "-924.46" in mock_post.call_args.args[0]

    def test_notify_exit_omits_pct_without_net_credit(self):
        with patch("alerts._post") as mock_post:
            alerts.notify_exit(reason="profit_target", exit_debit=0.68, realized_pnl=143.88)
        assert "%" not in mock_post.call_args.args[0]

    def test_notify_exit_includes_pct_of_net_credit(self):
        """137.04 realized P&L against 548.00 net credit collected =
        25.0%, not the capital_at_risk-basis figure this used to report
        (user correction 2026-11-06)."""
        with patch("alerts._post") as mock_post:
            alerts.notify_exit(reason="profit_target", exit_debit=4.06, realized_pnl=137.04, net_credit=548.0)
        text = mock_post.call_args.args[0]
        assert "(+25.0%)" in text

        with patch("alerts._post") as mock_post:
            alerts.notify_exit(reason="backstop", exit_debit=5.68, realized_pnl=-845.89, net_credit=927.0)
        text = mock_post.call_args.args[0]
        assert "(-91.3%)" in text

    def test_notify_backstop_failure_marks_critical(self):
        with patch("alerts._post") as mock_post:
            alerts.notify_backstop_failure(attempts=10, last_price_override=1.95)
        text = mock_post.call_args.args[0]
        assert "CRITICAL" in text
        assert "STILL OPEN" in text

    def test_notify_crash_marks_critical(self):
        with patch("alerts._post") as mock_post:
            alerts.notify_crash("ValueError: boom")
        text = mock_post.call_args.args[0]
        assert "CRITICAL" in text
        assert "boom" in text
