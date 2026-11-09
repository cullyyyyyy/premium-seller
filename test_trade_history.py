import trade_history as th


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


class TestParseTradeDayLog:
    def test_closed_day(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-27.log",
                   "2026-10-27 11:30:51,110 INFO daily_loop: [CONDOR] Entered 6x short $769.00 straddle "
                   "| net credit $1.59/share | max loss $1.39/share\n"
                   "2026-10-27 13:30:38,114 INFO daily_loop: [CONDOR] Exited (profit_target): "
                   "debit=$1.17/share, realized P&L=$222.22\n")
        record = th.parse_trade_day_log(p)
        assert record["date"] == "2026-10-27"
        assert record["status"] == "closed"
        assert record["contracts"] == 6
        assert record["short_strike"] == 769.00
        assert record["net_credit"] == 1.59
        assert record["exit_reason"] == "profit_target"
        assert record["exit_debit"] == 1.17
        assert record["realized_pnl"] == 222.22
        # max_loss_per_share=1.39, contracts=6 -> capital_at_risk=834.00
        # (structural/sizing figure, kept for reference, not the
        # reported percent gain/loss)
        assert record["max_loss_per_share"] == 1.39
        assert record["capital_at_risk"] == 834.00
        # net_credit=1.59, contracts=6 -> net_credit_total=954.00 -- the
        # ACTUAL percent gain/loss basis (user correction 2026-11-06:
        # realized P&L as a fraction of premium collected, not of
        # capital_at_risk).
        assert record["net_credit_total"] == 954.00
        assert round(record["pnl_pct_of_credit"], 2) == round(222.22 / 954.00 * 100, 2)

    def test_closed_day_without_logged_max_loss_omits_capital_at_risk_but_not_credit_pct(self, tmp_path):
        # Older/short-form entry lines that never logged "| max loss
        # $X/share" shouldn't make up a denominator for capital_at_risk/
        # pnl_pct_of_risk -- those must simply be absent, not 0 or wrong.
        # net credit IS always logged on every entry line, though, so
        # pnl_pct_of_credit (the percent gain/loss actually reported)
        # is still computed.
        p = _write(tmp_path, "trade_day_2026-10-27.log",
                   "Entered 6x short $769.00 straddle | net credit $1.59/share\n"
                   "Exited (profit_target): debit=$1.17/share, realized P&L=$222.22\n")
        record = th.parse_trade_day_log(p)
        assert record["status"] == "closed"
        assert "max_loss_per_share" not in record
        assert "capital_at_risk" not in record
        assert round(record["pnl_pct_of_credit"], 2) == round(222.22 / 954.00 * 100, 2)

    def test_closed_day_via_resting_order_exit(self, tmp_path):
        # daily_loop.py's resting-order exit path logs a comma-suffixed
        # reason ("profit_target, resting order") rather than a bare
        # \w+ token -- regression guard for the day this showed up as
        # "open" in /prem history despite having already closed.
        p = _write(tmp_path, "trade_day_2026-10-29.log",
                   "INFO daily_loop: [CONDOR] Entered 2x short $769.00 straddle | net credit $3.04/share | max loss $3.92/share\n"
                   "INFO daily_loop: [CONDOR] Exited (profit_target, resting order): debit=$2.27/share, realized P&L=$144.07\n")
        record = th.parse_trade_day_log(p)
        assert record["status"] == "closed"
        assert record["exit_reason"] == "profit_target"
        assert record["realized_pnl"] == 144.07
        # max_loss_per_share=3.92, contracts=2 -> capital_at_risk=784.00
        # (structural figure, kept for reference)
        assert record["capital_at_risk"] == 784.00
        # net_credit=3.04, contracts=2 -> net_credit_total=608.00 -- the
        # reported percent gain/loss basis: matches the dashboard's
        # "+23.70%" tile for this day.
        assert record["net_credit_total"] == 608.00
        assert round(record["pnl_pct_of_credit"], 2) == 23.70

    def test_wing_strikes_and_breakeven_parsed_when_logged(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-31.log",
                   "INFO daily_loop: [CONDOR] Entered 3x short $761.00 straddle | net credit $2.91/share "
                   "| max loss $3.09/share | put wing $754.50 (side max loss $3.09/share) "
                   "| call wing $768.20 (side max loss $2.71/share)\n")
        record = th.parse_trade_day_log(p)
        assert record["put_wing_strike"] == 754.50
        assert record["put_side_max_loss"] == 3.09
        assert record["call_wing_strike"] == 768.20
        assert record["call_side_max_loss"] == 2.71
        assert record["breakeven_lower"] == 758.09
        assert round(record["breakeven_upper"], 2) == 763.91

    def test_wing_strikes_absent_on_older_log_without_them(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-27.log",
                   "Entered 6x short $769.00 straddle | net credit $1.59/share | max loss $1.39/share\n")
        record = th.parse_trade_day_log(p)
        assert "put_wing_strike" not in record
        assert "call_wing_strike" not in record
        assert record["breakeven_lower"] == 767.41
        assert record["breakeven_upper"] == 770.59

    def test_closed_day_negative_pnl(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-29.log",
                   "INFO daily_loop: [CONDOR] Entered 3x short $770.00 straddle | net credit $3.96/share | max loss $3.03/share\n"
                   "INFO daily_loop: [CONDOR] Exited (backstop): debit=$5.50/share, realized P&L=$-477.78\n")
        record = th.parse_trade_day_log(p)
        assert record["realized_pnl"] == -477.78
        assert record["exit_reason"] == "backstop"

    def test_open_day_no_exit_yet(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-28.log",
                   "INFO daily_loop: [CONDOR] Entered 3x short $770.00 straddle | net credit $3.96/share | max loss $3.03/share\n")
        record = th.parse_trade_day_log(p)
        assert record["status"] == "open"
        assert record["contracts"] == 3
        assert "exit_debit" not in record

    def test_no_entry_day(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-30.log",
                   "ERROR daily_loop: [CONDOR] Sized to 0 contracts (max_loss=$5.00/share vs capital budget) -- skipping today.\n")
        record = th.parse_trade_day_log(p)
        assert record == {"date": "2026-10-30", "status": "no_entry"}

    def test_no_entry_day_from_vix_entry_skip(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-30.log",
                   "INFO daily_loop: [CONDOR] Skipping entry -- VIX already up 9.24% at entry (threshold 5.00%)\n")
        record = th.parse_trade_day_log(p)
        assert record == {"date": "2026-10-30", "status": "no_entry"}

    def test_login_failure_day(self, tmp_path):
        p = _write(tmp_path, "trade_day_2026-10-26.log",
                   "ERROR run_trade_day: Sandbox login FAILED -- aborting.\n")
        record = th.parse_trade_day_log(p)
        assert record == {"date": "2026-10-26", "status": "no_run", "detail": "login failed"}

    def test_unreadable_file(self, tmp_path):
        missing = tmp_path / "trade_day_2026-11-01.log"
        record = th.parse_trade_day_log(missing)
        assert record["date"] == "2026-11-01"
        assert record["status"] == "no_run"


class TestBuildHistory:
    def test_sorted_oldest_first_and_limited(self, tmp_path):
        _write(tmp_path, "trade_day_2026-10-26.log", "Sandbox login FAILED\n")
        _write(tmp_path, "trade_day_2026-10-27.log",
               "Entered 6x short $769.00 straddle | net credit $1.59/share\n"
               "Exited (profit_target): debit=$1.17/share, realized P&L=$222.22\n")
        _write(tmp_path, "trade_day_2026-10-28.log",
               "Entered 3x short $770.00 straddle | net credit $3.96/share\n"
               "Exited (backstop): debit=$3.65/share, realized P&L=$78.11\n")

        history = th.build_history(log_dir=tmp_path, limit=10)
        assert [r["date"] for r in history] == ["2026-10-26", "2026-10-27", "2026-10-28"]

    def test_limit_keeps_the_most_recent(self, tmp_path):
        for day in ("26", "27", "28"):
            _write(tmp_path, f"trade_day_2026-10-{day}.log", "Sandbox login FAILED\n")
        history = th.build_history(log_dir=tmp_path, limit=2)
        assert [r["date"] for r in history] == ["2026-10-27", "2026-10-28"]
