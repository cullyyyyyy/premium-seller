from pathlib import Path
from unittest.mock import MagicMock, patch

import slack_commands as sc
import check_position
import trade_history


class TestIsAuthorized:
    def test_denies_when_allowlist_empty(self):
        assert sc.is_authorized("U123", []) is False
        assert sc.is_authorized("U123", None) is False

    def test_allows_exact_match(self):
        assert sc.is_authorized("U123", ["U123", "U456"]) is True

    def test_denies_non_member(self):
        assert sc.is_authorized("U999", ["U123"]) is False


class TestSafetyBoundary:
    """Regression guard, not just a docstring promise -- mirrors
    covered_call_weekly's test_slack_commands.py::TestSafetyBoundary.
    This module must never be able to reach the autonomous scheduled
    trading loop (daily_loop.py / run_trade_day.py) or any of the
    lower-level order-submission entry points that loop owns -- "close"
    goes through manual_close.py exclusively."""

    def test_never_imports_the_autonomous_trading_loop(self):
        source = Path(__file__).parent.joinpath("slack_commands.py").read_text(encoding="utf-8")
        forbidden = [
            "import daily_loop", "from daily_loop", "import run_trade_day", "from run_trade_day",
            "run_day(", "wait_until(", "submit_entry(", "submit_resting_exit(", "_chase_entry(",
        ]
        for name in forbidden:
            assert name not in source, f"slack_commands.py must never reference {name!r}"


def _mock_auth(ok=True):
    auth = MagicMock()
    auth.login.return_value = ok
    auth.session_token = "tok"
    return auth


class TestBuildStatusResponse:
    def test_login_failure(self):
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth(ok=False)):
            reply = sc.build_status_response()
        assert "login failed" in reply.lower()

    def test_no_open_position(self):
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_open_option_positions", return_value=[]):
            reply = sc.build_status_response()
        assert "No open" in reply

    def test_open_position_reports_pnl(self):
        positions = [{"symbol": "SC"}]
        fake_result = {"legs": [], "gross_pnl": 10.0, "round_trip_fees": 2.0,
                        "net_pnl": 8.0, "any_missing_quote": False}
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_open_option_positions", return_value=positions), \
             patch.object(check_position, "create_production_options_chain", return_value=MagicMock()), \
             patch.object(check_position, "compute_position_pnl", return_value=fake_result), \
             patch.object(sc, "_today_net_credit", return_value=None):
            reply = sc.build_status_response()
        assert "Net P&L:* $+8.00" in reply
        assert "%" not in reply
        assert "OPEN" in reply

    def test_open_position_includes_pct_of_net_credit(self):
        positions = [{"symbol": "SC"}]
        fake_result = {"legs": [], "gross_pnl": 10.0, "round_trip_fees": 2.0,
                        "net_pnl": 8.0, "any_missing_quote": False}
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_open_option_positions", return_value=positions), \
             patch.object(check_position, "create_production_options_chain", return_value=MagicMock()), \
             patch.object(check_position, "compute_position_pnl", return_value=fake_result), \
             patch.object(sc, "_today_net_credit", return_value=400.0):
            reply = sc.build_status_response()
        assert "Net P&L:* $+8.00 (+2.0% of net credit)" in reply


class TestTodayNetCredit:
    def test_no_log_for_today(self):
        with patch.object(trade_history, "parse_trade_day_log", return_value={"date": "2026-10-30", "status": "no_run"}):
            assert sc._today_net_credit() is None

    def test_computes_from_todays_log(self):
        record = {"date": "2026-10-30", "status": "open", "contracts": 2, "net_credit": 3.92}
        with patch.object(trade_history, "parse_trade_day_log", return_value=record):
            assert sc._today_net_credit() == 784.0


class TestBuildPositionsResponse:
    def test_lists_each_leg(self):
        positions = [{"symbol": "SC"}, {"symbol": "LP"}]
        fake_result = {
            "legs": [
                {"symbol": "SC", "direction": "Short", "quantity": 3, "open_price": 2.54, "mid": 2.80, "pnl": -78.0},
                {"symbol": "LP", "direction": "Long", "quantity": 3, "open_price": 0.39, "mid": None, "pnl": None},
            ],
            "gross_pnl": -78.0, "round_trip_fees": 14.89, "net_pnl": -92.89, "any_missing_quote": True,
        }
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_open_option_positions", return_value=positions), \
             patch.object(check_position, "create_production_options_chain", return_value=MagicMock()), \
             patch.object(check_position, "compute_position_pnl", return_value=fake_result):
            reply = sc.build_positions_response()
        assert "Short 3x SC" in reply
        assert "LP: no live quote available" in reply


class TestBuildHistoryResponse:
    def test_formats_closed_open_and_no_trade_days(self):
        records = [
            {"date": "2026-10-26", "status": "no_run", "detail": "login failed"},
            {"date": "2026-10-27", "status": "closed", "contracts": 6, "short_strike": 769.0,
             "exit_reason": "profit_target", "realized_pnl": 222.22},
            {"date": "2026-10-28", "status": "open", "contracts": 3, "short_strike": 770.0},
        ]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_history_response()
        assert "222.22" in reply
        assert "still open" in reply
        assert "login failed" in reply

    def test_closed_day_shows_just_date_and_pnl(self):
        records = [{"date": "2026-10-29", "status": "closed", "contracts": 2,
                    "short_strike": 769.0, "exit_reason": "profit_target", "realized_pnl": 144.07}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_history_response()
        assert "2026-10-29: $+144.07" in reply

    def test_closed_day_includes_pct_of_net_credit(self):
        records = [{"date": "2026-10-29", "status": "closed", "contracts": 2,
                    "short_strike": 769.0, "exit_reason": "profit_target", "realized_pnl": 144.07,
                    "net_credit_total": 784.00, "pnl_pct_of_credit": 18.38}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_history_response()
        assert "2026-10-29: $+144.07 (+18.4%)" in reply

    def test_closed_day_omits_pct_when_unavailable(self):
        records = [{"date": "2026-10-27", "status": "closed", "contracts": 6,
                    "short_strike": 769.0, "exit_reason": "profit_target", "realized_pnl": 222.22}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_history_response()
        assert "2026-10-27: $+222.22" in reply
        assert "%" not in reply

    def test_no_logs_at_all(self):
        with patch("trade_history.build_history", return_value=[]):
            reply = sc.build_history_response()
        assert "No trade_day" in reply


class TestBuildTotalResponse:
    def test_no_closed_days(self):
        with patch("trade_history.build_history", return_value=[{"date": "2026-10-29", "status": "open"}]):
            reply = sc.build_total_response()
        assert "No closed trading days" in reply

    def test_sums_realized_pnl_across_closed_days(self):
        records = [
            {"date": "2026-10-27", "status": "closed", "realized_pnl": 222.22},
            {"date": "2026-10-28", "status": "closed", "realized_pnl": -50.00},
            {"date": "2026-10-29", "status": "open"},
        ]
        with patch("trade_history.build_history", return_value=records) as mock_build:
            reply = sc.build_total_response()
        mock_build.assert_called_once_with(limit=None)
        assert "$+172.22" in reply
        assert "1W / 1L" in reply


class TestBuildBalanceResponse:
    def test_login_failure(self):
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth(ok=False)):
            reply = sc.build_balance_response()
        assert "login failed" in reply.lower()

    def test_reports_balance_fields(self):
        fake_balance = {"net_liq": 10500.25, "cash_balance": 9000.00, "buying_power": 4200.50}
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_account_balance", return_value=fake_balance):
            reply = sc.build_balance_response()
        assert "$10,500.25" in reply
        assert "$4,200.50" in reply


class TestBuildTrendResponse:
    def test_no_log_yet(self):
        with patch("intraday_history.load_intraday", return_value=[]):
            reply = sc.build_trend_response()
        assert "No intraday P&L log" in reply

    def test_reports_entry_low_high_last(self):
        rows = [
            {"timestamp": "2026-10-29T10:00:14-04:00", "net_pnl_mark": -17.93, "spy_price": 768.74},
            {"timestamp": "2026-10-29T10:15:00-04:00", "net_pnl_mark": -43.93, "spy_price": 767.50},
            {"timestamp": "2026-10-29T12:58:37-04:00", "net_pnl_mark": 144.07, "spy_price": 769.30},
        ]
        with patch("intraday_history.load_intraday", return_value=rows):
            reply = sc.build_trend_response()
        assert "$-17.93" in reply
        assert "$-43.93" in reply
        assert "$+144.07" in reply
        assert "10:15:00 ET" in reply


class TestBuildStructureResponse:
    def test_no_logs_at_all(self):
        with patch("trade_history.build_history", return_value=[]):
            reply = sc.build_structure_response()
        assert "No trade_day" in reply

    def test_full_structure_for_closed_day(self):
        records = [{
            "date": "2026-10-29", "status": "closed", "contracts": 2, "short_strike": 769.0,
            "net_credit": 3.04, "max_loss_per_share": 3.92, "exit_reason": "profit_target",
            "realized_pnl": 144.07,
        }]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_structure_response()
        assert "2x short $769.00" in reply
        assert "$608.00 gross max gain" in reply
        assert "$784.00 gross" in reply
        assert "$765.96 - $772.04" in reply
        assert "realized P&L $+144.07" in reply

    def test_omits_max_loss_when_not_logged(self):
        records = [{"date": "2026-10-27", "status": "open", "contracts": 6,
                    "short_strike": 769.0, "net_credit": 1.59}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_structure_response()
        assert "older log format" in reply
        assert "still open" in reply


class TestBuildSizingResponse:
    def test_no_max_loss_logged(self):
        records = [{"date": "2026-10-27", "status": "open", "contracts": 6, "short_strike": 769.0}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_sizing_response()
        assert "not available" in reply

    def test_explains_budget_math(self):
        records = [{"date": "2026-10-29", "status": "closed", "contracts": 2, "max_loss_per_share": 3.92}]
        with patch("trade_history.build_history", return_value=records):
            reply = sc.build_sizing_response()
        assert "2 contract(s)" in reply
        assert "$980.00" in reply  # 1000 * 0.98 budget
        assert "over budget" in reply


class TestTriggerClose:
    def test_login_failure(self):
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth(ok=False)):
            reply = sc.trigger_close()
        assert "login failed" in reply.lower()

    def test_delegates_to_manual_close_and_returns_its_message(self):
        positions = [{"symbol": "SC"}]
        with patch.object(check_position, "TastytradeAuth", return_value=_mock_auth()), \
             patch.object(check_position, "fetch_open_option_positions", return_value=positions), \
             patch.object(check_position, "create_production_options_chain", return_value=MagicMock()), \
             patch("manual_close.close_open_position_now",
                   return_value={"ok": True, "message": "Closed 3x short $770.00 straddle"}) as mock_close:
            reply = sc.trigger_close()
        assert reply == "Closed 3x short $770.00 straddle"
        mock_close.assert_called_once()
        assert mock_close.call_args.kwargs["reason"] == "manual"


class TestDispatch:
    def test_unknown_subcommand_returns_help(self):
        reply = sc.dispatch("nonsense")
        assert "Unknown command" in reply
        assert "Available commands" in reply

    def test_help_command(self):
        reply = sc.dispatch("help")
        assert "close" in reply
        assert "Available commands" in reply
