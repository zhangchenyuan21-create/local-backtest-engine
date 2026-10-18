from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import qlib_bridge


class QlibBridgeTests(unittest.TestCase):
    def test_code_normalization(self):
        self.assertEqual(qlib_bridge.to_qlib_instrument("600000.XSHG"), "SH600000")
        self.assertEqual(qlib_bridge.to_qlib_instrument("000001.XSHE"), "SZ000001")
        self.assertEqual(qlib_bridge.from_qlib_instrument("SH600000"), "600000.XSHG")
        self.assertEqual(qlib_bridge.from_qlib_instrument("SZ000001"), "000001.XSHE")

    def test_import_signal(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "pred.csv"
            pd.DataFrame({"date": ["2024-01-02"], "instrument": ["SH600000"],
                          "score": [0.2]}).to_csv(src, index=False)
            out = Path(td) / "signal.csv"
            df = qlib_bridge.import_signal(src, out)
            self.assertEqual(df.iloc[0]["code"], "600000.XSHG")
            self.assertTrue(out.exists())

    def test_export_csv_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data" / "daily"
            root.mkdir(parents=True)
            dates = ["2024-01-02", "2024-01-03"]
            for code, close in (("600000_XSHG", 10.0), ("000001_XSHE", 12.0)):
                pd.DataFrame({
                    "date": dates, "open": [close, close], "high": [close, close],
                    "low": [close, close], "close": [close, close],
                    "volume": [1000, 1200],
                }).to_parquet(root / f"{code}.parquet", index=False)
            summary = qlib_bridge.export_qlib(Path(td) / "data", Path(td) / "qlib")
            self.assertEqual(summary["instruments"], 2)
            self.assertTrue((Path(td) / "qlib" / "calendars" / "day.txt").exists())
            self.assertTrue((Path(td) / "qlib" / "features" / "SH600000.csv").exists())
            self.assertTrue((Path(td) / "qlib" / "features" / "SZ000001.csv").exists())


if __name__ == "__main__":
    unittest.main()