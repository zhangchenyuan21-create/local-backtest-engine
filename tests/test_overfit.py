from __future__ import annotations

import unittest

import overfit


class OverfitTests(unittest.TestCase):
    def test_psr_monotonic(self):
        low = overfit.probabilistic_sharpe_ratio(0.5, 252)
        high = overfit.probabilistic_sharpe_ratio(1.5, 252)
        self.assertGreater(high, low)
        self.assertGreater(overfit.probabilistic_sharpe_ratio(1.0, 1000),
                           overfit.probabilistic_sharpe_ratio(1.0, 100))

    def test_deflated_sharpe_penalizes_trials(self):
        one = overfit.deflated_sharpe_ratio(1.5, 756, 1)
        many = overfit.deflated_sharpe_ratio(1.5, 756, 200)
        self.assertGreater(one["deflated_sharpe"], many["deflated_sharpe"])
        self.assertGreater(many["expected_max_sharpe"], one["expected_max_sharpe"])
        self.assertFalse(many["significant"])

    def test_summarize_trials(self):
        results = [
            {"ok": True, "params": {"x": 1}, "metrics": {"sharpe": 1.0, "trading_days": 500}},
            {"ok": True, "params": {"x": 2}, "metrics": {"sharpe": 2.0, "trading_days": 500}},
            {"ok": False, "error": "bad"},
        ]
        s = overfit.summarize_trials(results, "sharpe")
        self.assertEqual(s["best_params"], {"x": 2})
        self.assertEqual(s["num_trials"], 3)
        self.assertIn("deflated_sharpe", s)


if __name__ == "__main__":
    unittest.main()