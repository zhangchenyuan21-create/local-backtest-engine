from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import walkforward


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range("2024-01-02", periods=20).strftime("%Y-%m-%d").tolist()

    def test_make_splits_purge_and_embargo(self):
        splits = walkforward.make_splits(self.dates, train_size=5, test_size=3,
                                         step=3, purge=1, embargo=1)
        self.assertEqual(len(splits), 5)
        self.assertEqual(splits[0]["train_days"], 4)
        self.assertEqual(splits[0]["test_days"], 2)
        self.assertLess(splits[0]["train_end"], splits[0]["test_start"])
        for s in splits:
            self.assertLess(s["train_indices"][1], s["test_indices"][0])

    def test_expanding_window(self):
        splits = walkforward.make_splits(self.dates, train_size=5, test_size=3,
                                         step=3, expanding=True)
        self.assertEqual(splits[0]["train_start"], self.dates[0])
        self.assertEqual(splits[1]["train_start"], self.dates[0])
        self.assertGreater(splits[1]["train_days"], splits[0]["train_days"])

    def test_walk_forward_with_mocked_runner(self):
        def fake_run(strategy, params=None, outdir=None, *args, **kwargs):
            score = float((params or {}).get("fast", 0)) / 10.0
            return {"ok": True, "strategy": str(strategy), "out": str(outdir),
                    "params": params or {}, "returncode": 0,
                    "metrics": {"sharpe": score, "trading_days": 10,
                                "total_return": 0.1, "max_drawdown": -0.05}}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "calendar.json").write_text(json.dumps(self.dates), encoding="utf-8")
            template = root / "template.py"
            template.write_text("FAST = {{fast}}\n", encoding="utf-8")
            with patch.object(walkforward.optimize, "run_one", side_effect=fake_run):
                result = walkforward.walk_forward_optimize(
                    template=template, grid={"fast": [5, 10]}, data_dir=root,
                    train_size=10, test_size=5, step=5, purge=1, embargo=1,
                    out_dir=root / "wf", objective="sharpe", workers=2,
                    max_splits=2)
            self.assertEqual(result["summary"]["splits"], 2)
            self.assertEqual(result["summary"]["completed"], 2)
            self.assertTrue((root / "wf" / "walkforward_splits.csv").exists())
            for row in result["splits"]:
                self.assertIn("fast", row["selected_params"])


if __name__ == "__main__":
    unittest.main()