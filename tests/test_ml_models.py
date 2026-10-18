from __future__ import annotations

import unittest

import numpy as np

import ml_models


class MlModelTests(unittest.TestCase):
    def test_ridge_fit_predict(self):
        rng = np.random.default_rng(5)
        X = rng.normal(size=(200, 2))
        y = 1.5 * X[:, 0] - 0.7 * X[:, 1] + 0.2
        fitted = ml_models.fit_model("ridge", X, y, {"alpha": 0.01})
        pred = ml_models.predict_model(fitted, X)
        self.assertLess(float(np.mean(np.abs(pred - y))), 0.05)

    def test_availability_and_unknown_model(self):
        self.assertTrue(ml_models.available("ridge"))
        self.assertFalse(ml_models.available("not-a-model"))
        with self.assertRaises(ValueError):
            ml_models.fit_model("not-a-model", [[1.0]], [1.0])

    def test_optional_sklearn_if_installed(self):
        if not ml_models.available("sklearn-ridge"):
            self.skipTest("scikit-learn not installed")
        X = np.arange(20, dtype=float).reshape(-1, 1)
        y = 2.0 * X[:, 0] + 1.0
        fitted = ml_models.fit_model("sklearn-ridge", X, y, {"alpha": 0.1})
        pred = ml_models.predict_model(fitted, X)
        self.assertLess(float(np.mean(np.abs(pred - y))), 0.5)


if __name__ == "__main__":
    unittest.main()