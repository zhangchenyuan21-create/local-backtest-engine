from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import analytics


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            "total_value": [100.0, 110.0, 105.0, 120.0, 120.0],
            "positions_value": [0.0, 50.0, 50.0, 0.0, 0.0],
            "cash": [100.0, 50.0, 50.0, 120.0, 120.0],
            "n_pos": [0, 1, 1, 0, 0],
        }).to_csv(self.dir / "equity.csv", index=False)
        pd.DataFrame({
            "date": ["2024-01-03", "2024-01-04"],
            "code": ["600000.XSHG", "600000.XSHG"],
            "side": ["buy", "sell"],
            "price": [10.0, 11.0],
            "amount": [100, 100],
            "value": [1000.0, 1100.0],
            "fee": [5.0, 5.0],
            "cash_after": [0.0, 0.0],
        }).to_csv(self.dir / "fills.csv", index=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics(self):
        eq = analytics.load_equity(self.dir)
        fills = analytics.load_fills(self.dir)
        m = analytics.compute_metrics(eq, fills)
        self.assertAlmostEqual(m["total_return"], 0.20)
        self.assertAlmostEqual(m["max_drawdown"], -0.045454545454545456)
        self.assertEqual(m["trade_count"], 1)
        self.assertAlmostEqual(m["trade_win_rate"], 1.0)
        self.assertAlmostEqual(m["avg_trade_return"], 90.0 / 1005.0)
        self.assertGreater(m["sharpe"], 0)

    def test_json_safe(self):
        self.assertIsNone(analytics.json_safe(float("inf")))
        self.assertEqual(analytics.json_safe({"x": float("nan")}), {"x": None})

    def test_fifo_trade_pnl_and_reports(self):
        trades = analytics.build_trades(analytics.load_fills(self.dir))
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(float(trades["pnl"].iloc[0]), 90.0)
        result = analytics.write_reports(self.dir, formats=("md", "json", "html"))
        self.assertEqual(set(result["written"]), {"md", "json", "html"})
        metrics = json.loads((self.dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["trade_count"], 1)
        self.assertTrue((self.dir / "report.html").read_text(encoding="utf-8").startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()