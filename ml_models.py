"""可选 ML 模型适配层。

默认模型是 `ml.py` 里的纯 NumPy Ridge；本模块只在显式选择时才导入
scikit-learn / LightGBM，因此默认安装和默认回测不受影响。
"""
from __future__ import annotations

from typing import Any

import numpy as np

MODELS = ("ridge", "sklearn-ridge", "sklearn-hgb", "lightgbm")


def available(name: str) -> bool:
    name = str(name).lower()
    if name == "ridge":
        return True
    if name in ("sklearn-ridge", "sklearn-hgb"):
        try:
            import sklearn  # noqa: F401
            return True
        except Exception:
            return False
    if name == "lightgbm":
        try:
            import lightgbm  # noqa: F401
            return True
        except Exception:
            return False
    return False


def fit_model(name: str, X, y, params: dict | None = None) -> dict:
    """返回统一的模型字典，供 ml.py 的预测循环调用。"""
    name = str(name).lower()
    params = dict(params or {})
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(X) == 0:
        raise ValueError("训练集为空")
    if name == "ridge":
        # 与 ml.fit_ridge 相同的闭式解，但保持本模块可独立使用。
        mean = X.mean(axis=0)
        std = X.std(axis=0)
        std[std < 1e-12] = 1.0
        Z = (X - mean) / std
        Z1 = np.column_stack([np.ones(len(Z)), Z])
        alpha = float(params.get("alpha", 1.0))
        A = Z1.T @ Z1 + alpha * np.eye(Z1.shape[1])
        A[0, 0] -= alpha
        try:
            w = np.linalg.solve(A, Z1.T @ y)
        except np.linalg.LinAlgError:
            w = np.linalg.pinv(A) @ (Z1.T @ y)
        return {"kind": "ridge", "mean": mean, "std": std,
                "coef": w[1:], "intercept": float(w[0])}
    if name == "sklearn-ridge":
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        alpha = float(params.get("alpha", 1.0))
        model = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
        model.fit(X, y)
        return {"kind": "sklearn", "model": model}
    if name == "sklearn-hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor
        kwargs = {"max_iter": 200, "learning_rate": 0.05, "max_leaf_nodes": 31}
        kwargs.update({k: v for k, v in params.items() if k not in ("alpha",)})
        model = HistGradientBoostingRegressor(**kwargs)
        model.fit(X, y)
        return {"kind": "sklearn", "model": model}
    if name == "lightgbm":
        import lightgbm as lgb
        kwargs = {"n_estimators": 300, "learning_rate": 0.03, "num_leaves": 31,
                  "subsample": 0.8, "colsample_bytree": 0.8, "verbose": -1}
        kwargs.update(params)
        model = lgb.LGBMRegressor(**kwargs)
        model.fit(X, y)
        return {"kind": "lightgbm", "model": model}
    raise ValueError(f"未知模型: {name}; 可选 {MODELS}")


def predict_model(fitted: dict, X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] == 0:
        return np.empty((0,))
    kind = fitted.get("kind")
    if kind == "ridge":
        return (X - fitted["mean"]) / fitted["std"] @ fitted["coef"] + fitted["intercept"]
    model = fitted.get("model")
    if model is None:
        raise ValueError("模型没有可预测对象")
    return np.asarray(model.predict(X), dtype=float)