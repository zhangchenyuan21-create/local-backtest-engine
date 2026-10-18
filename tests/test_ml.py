from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import ml
import runner


class MlTests(unittest.TestCase):
    def test_ridge_recovers_linear_signal(self):
        rng = np.random.default_rng(11)
        X = rng.normal(size=(300, 3))
        y = 2.0 * X[:, 0] - 1.3 * X[:, 1] + 0.4
        model = ml.fit_ridge(X, y, alpha=0.01)
        pred = ml.predict_ridge(model, X)
        self.assertLess(float(np.mean(np.abs(pred - y))), 0.05)

    def test_walk_forward_predictions_are_test_only(self):
        dates = pd.bdate_range("2023-01-02", periods=120).strftime("%Y-%m-%d")
        codes = ["000001.XSHE", "600000.XSHG", "300001.XSHE"]
        rng = np.random.default_rng(3)
        f = pd.DataFrame(rng.normal(size=(len(dates), len(codes))),
                         index=dates, columns=codes)
        fwd = 0.01 * f + pd.DataFrame(rng.normal(0, 0.001, size=f.shape),
                                      index=dates, columns=codes)
        signal = ml.walk_forward_predict({"momentum_5": f}, fwd,
                                         train_size=40, test_size=20,
                                         step=20, purge=5, alpha=0.1)
        self.assertFalse(signal.empty)
        self.assertEqual(set(signal.columns), {"date", "code", "score", "split"})
        # 第一个测试窗口从第 40 根开始
        self.assertGreaterEqual(signal["date"].min(), dates[40])
        self.assertTrue(signal["score"].notna().all())

    def test_signal_strategy_loads(self):
        mod = runner.load_strategy("examples/signal_strategy.py")
        self.assertTrue(callable(mod.initialize))
        self.assertTrue(callable(mod.rebalance))


if __name__ == "__main__":
    unittest.main()