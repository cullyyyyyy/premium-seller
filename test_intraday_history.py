import json

import intraday_history as ih


def _write_jsonl(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


class TestLoadIntraday:
    def test_missing_file_returns_empty(self, tmp_path):
        assert ih.load_intraday("2026-10-29", log_dir=tmp_path) == []

    def test_loads_rows_in_order(self, tmp_path):
        rows = [
            {"timestamp": "2026-10-29T10:00:14-04:00", "spy_price": 768.74, "net_pnl_mark": -17.93},
            {"timestamp": "2026-10-29T12:58:37-04:00", "spy_price": 769.30, "net_pnl_mark": 144.07},
        ]
        _write_jsonl(tmp_path, "intraday_pnl_2026-10-29.jsonl", rows)
        loaded = ih.load_intraday("2026-10-29", log_dir=tmp_path)
        assert loaded == rows


class TestSummarize:
    def test_empty_returns_none(self):
        assert ih.summarize([]) is None

    def test_picks_first_last_low_high(self):
        rows = [
            {"timestamp": "t0", "net_pnl_mark": -17.93, "spy_price": 768.74},
            {"timestamp": "t1", "net_pnl_mark": -43.93, "spy_price": 767.50},
            {"timestamp": "t2", "net_pnl_mark": 144.07, "spy_price": 769.30},
        ]
        summary = ih.summarize(rows)
        assert summary["first"]["timestamp"] == "t0"
        assert summary["last"]["timestamp"] == "t2"
        assert summary["low"]["timestamp"] == "t1"
        assert summary["high"]["timestamp"] == "t2"
