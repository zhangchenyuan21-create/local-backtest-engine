from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import reconcile


class ReconcileTests(unittest.TestCase):
    def test_normalizers(self):
        self.assertEqual(reconcile._norm_symbol("SH600000"), "600000")
        self.assertEqual(reconcile._norm_symbol("000001.XSHE"), "000001")
        self.assertEqual(reconcile._norm_date("20240102"), "2024-01-02")
        self.assertEqual(reconcile._norm_side("买入"), "buy")

    def test_reconciliation_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pd.DataFrame([
                {"order_id": "P1", "symbol": "000001.XSHE", "side": "buy",
                 "quantity": 1000, "order_date": "2024-01-02", "status": "filled",
                 "filled_quantity": 1000, "fill_price": 10.0, "fee": 5.0,
                 "fill_date": "2024-01-03"},
                {"order_id": "P2", "symbol": "600000.XSHG", "side": "sell",
                 "quantity": 500, "order_date": "2024-01-02", "status": "filled",
                 "filled_quantity": 500, "fill_price": 11.0, "fee": 8.0,
                 "fill_date": "2024-01-03"},
            ]).to_csv(root / "paper_orders.csv", index=False)
            pd.DataFrame([
                {"order_id": "P1", "symbol": "000001.XSHE", "side": "buy",
                 "quantity": 1000, "order_date": "2024-01-02", "status": "filled",
                 "filled_quantity": 1000, "fill_price": 10.0, "fee": 5.0,
                 "fill_date": "2024-01-03", "value": 10000.0},
                {"order_id": "P2", "symbol": "600000.XSHG", "side": "sell",
                 "quantity": 500, "order_date": "2024-01-02", "status": "filled",
                 "filled_quantity": 500, "fill_price": 11.0, "fee": 8.0,
                 "fill_date": "2024-01-03", "value": 5500.0},
            ]).to_csv(root / "paper_fills.csv", index=False)
            pd.DataFrame([
                {"htbh": "T1", "wtrq": "20240102", "证券代码": "000001",
                 "买卖": "buy", "委托数量": 1000, "状态": "已成",
                 "成交数量": 1000, "委托价格": 10.0, "手续费": 5.0},
                {"htbh": "T2", "wtrq": "20240102", "证券代码": "600000",
                 "买卖": "sell", "委托数量": 500, "状态": "已撤",
                 "成交数量": 0, "委托价格": 11.0, "手续费": 0.0},
                {"htbh": "T3", "wtrq": "20240102", "证券代码": "300001",
                 "买卖": "buy", "委托数量": 100, "状态": "已成",
                 "成交数量": 100, "委托价格": 10.0, "手续费": 5.0},
            ]).to_csv(root / "ths_orders.csv", index=False)
            pd.DataFrame([
                {"成交日期": "2024-01-03", "证券代码": "000001", "买卖": "buy",
                 "成交数量": 1000, "成交价格": 10.0, "成交金额": 10000.0,
                 "手续费": 5.0},
            ]).to_csv(root / "ths_fills.csv", index=False)
            r = reconcile.build_report(root, root / "ths_orders.csv",
                                       root / "ths_fills.csv", root / "out")
            self.assertEqual(r["orders"]["matched"], 2)
            self.assertEqual(r["orders"]["missing_in_ths"], 0)
            self.assertEqual(r["orders"]["missing_in_paper"], 1)
            self.assertEqual(r["orders"]["issues"], 3)  # 1 missing + 2 status/fill mismatches
            self.assertEqual(r["fills"]["matched_groups"], 1)
            self.assertTrue((root / "out" / "reconciliation.json").exists())


if __name__ == "__main__":
    unittest.main()