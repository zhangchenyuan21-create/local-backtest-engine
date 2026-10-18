from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import analysis_report


class AnalysisReportTests(unittest.TestCase):
    def _run_dir(self, root: Path):
        run = root / "run"
        run.mkdir()
        pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "total_value": [100.0, 101.0, 99.0, 102.0],
            "positions_value": [0.0, 50.0, 50.0, 0.0],
            "cash": [100.0, 51.0, 49.0, 102.0],
            "n_pos": [0, 1, 1, 0],
        }).to_csv(run / "equity.csv", index=False)
        pd.DataFrame({
            "date": ["2024-01-03", "2024-01-05"],
            "code": ["600000.XSHG", "600000.XSHG"],
            "side": ["buy", "sell"],
            "price": [10.0, 10.2],
            "amount": [100, 100],
            "value": [1000.0, 1020.0],
            "fee": [5.0, 5.0],
            "cash_after": [0.0, 0.0],
        }).to_csv(run / "fills.csv", index=False)
        return run

    def test_missing_evidence_and_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(Path(td))
            r = analysis_report.build_report(run)
            self.assertIn("不可用于实盘", r["readiness"])
            self.assertIn("factor", r["missing_evidence"])
            self.assertTrue(r["flags"])

    def test_full_evidence_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = self._run_dir(root)
            factor = root / "factor.json"
            factor.write_text(json.dumps({"ic_mean": 0.03, "rank_ic_mean": 0.04,
                                          "icir": 1.2, "long_short_mean": 0.01}),
                              encoding="utf-8")
            opt = root / "best.json"
            opt.write_text(json.dumps({"params": {"fast": 5}, "overfit": {
                "deflated_sharpe": 0.97, "significant": True, "num_trials": 10}}),
                encoding="utf-8")
            wf = root / "wf.json"
            wf.write_text(json.dumps({"splits": 4, "completed": 4,
                                      "mean_test_sharpe": 1.1,
                                      "mean_test_return": 0.05,
                                      "mean_dsr": 0.96}), encoding="utf-8")
            paper = root / "paper.json"
            paper.write_text(json.dumps({"start": "2024-01-02", "end": "2024-12-31",
                                         "total_return": 0.1, "orders": 2,
                                         "fills": 2, "pending_orders": 0,
                                         "rejected_orders": 0}), encoding="utf-8")
            r = analysis_report.build_report(run, factor=factor, optimize=opt,
                                             walkforward=wf, paper=paper)
            self.assertIn("Walk-forward 和 Paper", r["readiness"])
            self.assertEqual(r["evidence_status"]["factor"], "present")
            self.assertNotIn("walkforward", r["missing_evidence"])
            result = analysis_report.write_report(run, factor=factor, optimize=opt,
                                                  walkforward=wf, paper=paper,
                                                  formats=("md", "json"))
            self.assertTrue((run / "analysis_report.md").exists())
            self.assertTrue((run / "analysis_report.json").exists())


if __name__ == "__main__":
    unittest.main()