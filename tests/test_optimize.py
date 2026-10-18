from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import optimize


class OptimizeTests(unittest.TestCase):
    def test_expand_grid(self):
        grid = {"fast": [5, 10], "slow": [20, 30]}
        rows = optimize.expand_grid(grid)
        self.assertEqual(len(rows), 4)
        self.assertIn({"fast": 5, "slow": 20}, rows)

    def test_render_template(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td) / "template.py"
            t.write_text("FAST = {{ fast }}\nSLOW = {{slow}}\n", encoding="utf-8")
            out = optimize.render_template(t, {"fast": 5, "slow": 20}, Path(td) / "out.py")
            text = out.read_text(encoding="utf-8")
            self.assertIn("FAST = 5", text)
            self.assertIn("SLOW = 20", text)

    def test_rank_results(self):
        results = [
            {"ok": True, "metrics": {"sharpe": 1.2}, "strategy": "a"},
            {"ok": True, "metrics": {"sharpe": 2.0}, "strategy": "b"},
            {"ok": False, "error": "x", "strategy": "c"},
        ]
        ranked = optimize.rank_results(results, "sharpe")
        self.assertEqual(ranked[0]["strategy"], "b")
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(len(ranked), 2)

    def test_run_optimization_with_mocked_runner(self):
        def fake_run(strategy, params=None, outdir=None, *args, **kwargs):
            score = float((params or {}).get("fast", 0))
            return {"strategy": str(strategy), "out": str(outdir), "params": params,
                    "ok": True, "returncode": 0,
                    "metrics": {"sharpe": score / 10.0, "total_return": 0.1}}

        with tempfile.TemporaryDirectory() as td:
            t = Path(td) / "template.py"
            t.write_text("FAST = {{fast}}\n", encoding="utf-8")
            with patch.object(optimize, "run_one", side_effect=fake_run):
                result = optimize.run_optimization(
                    template=t, grid={"fast": [5, 10]}, out_dir=Path(td) / "opt",
                    objective="sharpe", workers=2)
            self.assertTrue(result["best"])
            self.assertEqual(result["best"]["params"]["fast"], 10)
            self.assertTrue(Path(result["trials"]).exists())

    def test_invalid_objective(self):
        with self.assertRaises(ValueError):
            optimize.expand_grid({})
            optimize.rank_results([], "not_a_metric")


if __name__ == "__main__":
    unittest.main()