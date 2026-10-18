from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import factors


class FactorTests(unittest.TestCase):
    def setUp(self):
        dates = pd.bdate_range("2024-01-02", periods=40)
        rng = np.random.default_rng(7)
        data = {}
        for i, code in enumerate(["600000.XSHG", "000001.XSHE", "300001.XSHE", "688001.XSHG"]):
            drift = 0.001 * (i + 1)
            noise = rng.normal(0, 0.002, len(dates))
            data[code] = 10.0 * np.exp(np.cumsum(drift + noise))
        self.close = pd.DataFrame(data, index=dates)
        self.dates = dates

    def test_momentum_and_forward_alignment(self):
        m = factors.momentum(self.close, 5)
        self.assertTrue(m.iloc[:5].isna().all().all())
        self.assertAlmostEqual(float(m.iloc[5, 0]),
                               float(self.close.iloc[5, 0] / self.close.iloc[0, 0] - 1))
        f = factors.forward_returns(self.close, 3)
        self.assertAlmostEqual(float(f.iloc[0, 0]),
                               float(self.close.iloc[3, 0] / self.close.iloc[0, 0] - 1))
        self.assertTrue(f.iloc[-3:].isna().all().all())

    def test_ic_and_quantiles_on_perfect_factor(self):
        fwd = factors.forward_returns(self.close, 5)
        result = factors.evaluate_factor(fwd, fwd, quantiles=4, horizon=5, min_names=4)
        self.assertAlmostEqual(result["summary"]["ic_mean"], 1.0, places=6)
        self.assertGreater(result["summary"]["long_short_mean"], 0.0)
        self.assertEqual(len(result["ic"]), len(self.close) - 5)

    def test_cross_sectional_rank(self):
        r = factors.cross_sectional_rank(self.close)
        self.assertTrue(((r >= 0) & (r <= 1)).all().all())


if __name__ == "__main__":
    unittest.main()