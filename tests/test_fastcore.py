"""Unit tests for the fast-core array/flow/signal helpers.

These tests monkeypatch the store layer so they do not depend on the 1.3GB
market data directory.  They assert the exact per-code last-N semantics that
the legacy pandas implementation used.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TEST_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("JQSHIM_DATA", os.path.join(TEST_DIR, "data_test_fastcore"))

from jqdata import store  # noqa: E402
from jqdata.fastcore import flow_agg, panel, signals  # noqa: E402

CAL = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"]


class FastCoreTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "load_calendar": store.load_calendar,
            "snap_arrays": store.snap_arrays,
            "extra_arrays": store.extra_arrays,
            "flow_arrays": store.flow_arrays,
            "SNAP_FIELDS": store.SNAP_FIELDS,
        }
        store.load_calendar = lambda: list(CAL)
        store.SNAP_FIELDS = ("close", "pctChg", "open", "high", "low")
        # A: all dates; B: missing 01-06, so its last 2 observations are
        # 01-02 and 01-03 (legacy per-code last-N semantics, not last-N days).
        self._arrays = {
            "A": (np.array(CAL, dtype=object),
                  {"close": np.array([10., 11., 12., 13., 14.]),
                   "pctChg": np.array([0., 10., 9.09, 8.33, 7.69]),
                   "open": np.array([9., 10., 11., 12., 13.]),
                   "high": np.array([11., 12., 13., 14., 15.]),
                   "low": np.array([8., 9., 10., 11., 12.])}),
            "B": (np.array(["2020-01-02", "2020-01-03"], dtype=object),
                  {"close": np.array([20., 21.]),
                   "pctChg": np.array([0., 5.]),
                   "open": np.array([19., 20.]),
                   "high": np.array([21., 22.]),
                   "low": np.array([18., 19.])}),
        }
        self._flow = {
            "A": (np.array(CAL, dtype=object),
                  np.array([1., 2., 3., 4., 5.])),
            "B": (np.array(["2020-01-02", "2020-01-03"], dtype=object),
                  np.array([10., 20.])),
        }

        def snap(code):
            return self._arrays.get(code, (None, None))

        def extra(code, fields):
            return snap(code)

        def flow(code):
            return self._flow.get(code, (None, None))

        store.snap_arrays = snap
        store.extra_arrays = extra
        store.flow_arrays = flow
        flow_agg._PANELS.clear()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(store, k, v)
        flow_agg._PANELS.clear()

    def test_price_window_keeps_per_code_last_n(self):
        df = panel.price_window(["A", "B"], "2020-01-08", 2,
                                ["open", "close"])
        self.assertIsNotNone(df)
        # code-major: A last two dates, then B's only two available dates
        self.assertEqual(list(df["code"]), ["A", "A", "B", "B"])
        self.assertEqual(list(df["time"]),
                         ["2020-01-07", "2020-01-08", "2020-01-02", "2020-01-03"])
        self.assertEqual(list(df["close"]), [13., 14., 20., 21.])
        self.assertEqual(list(df["open"]), [12., 13., 19., 20.])

    def test_flow_window_units(self):
        df = panel.flow_window(["A", "B"], "2020-01-08", 2,
                               ["date", "sec_code", "net_amount_main"])
        self.assertEqual(list(df["sec_code"]), ["A", "A", "B", "B"])
        self.assertEqual(list(df["date"]),
                         ["2020-01-07", "2020-01-08", "2020-01-02", "2020-01-03"])
        self.assertEqual(list(df["net_amount_main"]), [4. / 1e4, 5. / 1e4,
                                                       10. / 1e4, 20. / 1e4])

    def test_flow_panel_last_n_sums(self):
        p = flow_agg.FlowPanel(("A", "B"))
        dates, sums, counts = p.sum_window("2020-01-08", 2)
        # last two observations: A 01-07/01-08 and B 01-02/01-03
        self.assertEqual(list(dates), ["2020-01-02", "2020-01-03",
                                       "2020-01-07", "2020-01-08"])
        self.assertEqual(list(sums), [10., 20., 4., 5.])
        self.assertEqual(list(counts), [1, 1, 1, 1])

    def test_signal_recorder_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "signals.csv")
            rec = signals.SignalRecorder(path)
            rec.record("2020-01-02", "09:31:00", "A", 100)
            rec.record("2020-01-02", "14:50:00", "A", -100)
            rec.set_meta(strategy="test", start="2020-01-02", end="2020-01-03",
                         cash=100000.0, fill_mode="next_open",
                         jq_options={"slippage": {"value": 0.02}})
            rec.save()
            loaded = signals.load_signals(path)
            self.assertEqual(len(loaded["2020-01-02"]), 2)
            self.assertEqual(loaded["2020-01-02"][1]["qty"], -100)
            meta = signals.load_meta(path)
            self.assertEqual(meta["cash"], 100000.0)


if __name__ == "__main__":
    unittest.main()
