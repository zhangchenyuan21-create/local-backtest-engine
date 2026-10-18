"""轻量 ML 信号层：Walk-forward 训练 → 样本外预测 → signal.csv。

默认使用纯 NumPy Ridge，不强制引入 scikit-learn。生成的信号可以交给
`examples/signal_strategy.py`，由现有本地回测引擎执行 T+1/涨跌停/成交量撮合。

用法：
    python -m ml --data data --features momentum:20,volatility:20,ma_gap:20 ^
      --horizon 5 --train-size 252 --test-size 63 --step 63 --purge 5 ^
      --out out/ml --alpha 1.0
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import factors
import walkforward

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_feature_spec(spec: str) -> list[tuple[str, int]]:
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        name, _, window = part.partition(":")
        name = name.strip().lower()
        win = int(window) if window.strip() else 20
        if name not in ("momentum", "reversal", "volatility", "ma_gap", "rsi"):
            raise ValueError(f"未知因子: {name}")
        out.append((name, win))
    if not out:
        raise ValueError("至少提供一个因子，例如 momentum:20")
    return out


def build_features(data_dir, specs) -> dict[str, pd.DataFrame]:
    close = factors.load_close_panel(data_dir)
    out = {}
    for name, window in specs:
        df = factors._factor_from_name(name, close, window)
        df.columns = close.columns
        out[f"{name}_{window}"] = df
    return out


def _feature_matrix(features: dict[str, pd.DataFrame], date, codes):
    kept, rows = [], []
    for code in codes:
        row = []
        ok = True
        for frame in features.values():
            try:
                v = float(frame.at[date, code])
            except (KeyError, TypeError, ValueError):
                ok = False
                break
            if not math.isfinite(v):
                ok = False
                break
            row.append(v)
        if ok:
            kept.append(code)
            rows.append(row)
    if not rows:
        return [], np.empty((0, len(features)))
    return kept, np.asarray(rows, dtype=float)


def _train_matrix(features: dict[str, pd.DataFrame], fwd: pd.DataFrame,
                  dates, codes):
    X_parts, y_parts = [], []
    for date in dates:
        kept, X = _feature_matrix(features, date, codes)
        if not kept:
            continue
        try:
            y = fwd.loc[date].reindex(kept).to_numpy(dtype=float)
        except Exception:
            continue
        mask = np.isfinite(y)
        if mask.any():
            X_parts.append(X[mask])
            y_parts.append(y[mask])
    if not X_parts:
        return np.empty((0, len(features))), np.empty((0,))
    return np.vstack(X_parts), np.concatenate(y_parts)


def fit_ridge(X, y, alpha: float = 1.0) -> dict:
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or y.ndim != 1 or len(X) != len(y):
        raise ValueError("X/y 形状不匹配")
    if len(X) == 0:
        raise ValueError("训练集为空")
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std < 1e-12] = 1.0
    Z = (X - mean) / std
    Z1 = np.column_stack([np.ones(len(Z)), Z])
    p = Z1.shape[1]
    A = Z1.T @ Z1 + float(alpha) * np.eye(p)
    A[0, 0] -= float(alpha)
    b = Z1.T @ y
    try:
        w = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        w = np.linalg.pinv(A) @ b
    return {"mean": mean, "std": std, "coef": w[1:], "intercept": float(w[0]),
            "n_train": int(len(X)), "alpha": float(alpha)}


def predict_ridge(model: dict, X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    if X.ndim != 2 or X.shape[0] == 0:
        return np.empty((0,))
    Z = (X - model["mean"]) / model["std"]
    return Z @ model["coef"] + model["intercept"]


def walk_forward_predict(features: dict[str, pd.DataFrame], fwd: pd.DataFrame,
                         train_size: int, test_size: int, step: int | None = None,
                         purge: int = 0, embargo: int = 0,
                         expanding: bool = False, alpha: float = 1.0) -> pd.DataFrame:
    if not features:
        raise ValueError("features 不能为空")
    index = None
    for frame in features.values():
        index = frame.index if index is None else index.intersection(frame.index)
    dates = sorted(str(x)[:10] for x in (index if index is not None else []))
    dates = [d for d in dates if d in fwd.index]
    codes = sorted(set.intersection(*(set(f.columns) for f in features.values()),
                                    set(fwd.columns)))
    if not dates or not codes:
        raise ValueError("features/fwd 没有共同日期或代码")
    splits = walkforward.make_splits(dates, train_size, test_size, step,
                                     purge, embargo, expanding)
    if not splits:
        raise ValueError("没有足够的日期生成切分；请减小 train/test 或检查数据")
    rows = []
    for split in splits:
        train_dates = [d for d in dates if split["train_start"] <= d <= split["train_end"]]
        test_dates = [d for d in dates if split["test_start"] <= d <= split["test_end"]]
        X, y = _train_matrix(features, fwd, train_dates, codes)
        if len(X) < max(10, len(features) + 2):
            continue
        model = fit_ridge(X, y, alpha)
        for date in test_dates:
            kept, Xt = _feature_matrix(features, date, codes)
            if not kept:
                continue
            pred = predict_ridge(model, Xt)
            for code, score in zip(kept, pred):
                rows.append({"date": date, "code": code,
                             "score": float(score), "split": split["split"]})
    out = pd.DataFrame(rows, columns=["date", "code", "score", "split"])
    if out.empty:
        return out
    return out.drop_duplicates(["date", "code"], keep="last").sort_values(
        ["date", "score"], ascending=[True, False]).reset_index(drop=True)

def run_ml(data_dir, feature_spec="momentum:20,volatility:20,ma_gap:20",
           horizon=5, train_size=252, test_size=63, step=None,
           purge=None, embargo=0, expanding=False, alpha=1.0,
           out_dir="out/ml") -> dict:
    specs = parse_feature_spec(feature_spec)
    features = build_features(data_dir, specs)
    close = factors.load_close_panel(data_dir)
    fwd = factors.forward_returns(close, horizon)
    effective_purge = int(horizon if purge is None else purge)
    signal = walk_forward_predict(features, fwd, train_size, test_size, step,
                                  effective_purge, embargo, expanding, alpha)
    if signal.empty:
        raise ValueError("没有生成任何样本外预测")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    signal.to_csv(out / "signal.csv", index=False)
    try:
        signal.to_parquet(out / "signal.parquet", index=False)
    except Exception:
        pass
    summary = {
        "features": [f"{n}_{w}" for n, w in specs],
        "horizon": int(horizon),
        "train_size": int(train_size),
        "test_size": int(test_size),
        "purge": effective_purge,
        "embargo": int(embargo),
        "expanding": bool(expanding),
        "alpha": float(alpha),
        "predictions": int(len(signal)),
        "dates": int(signal["date"].nunique()),
        "codes": int(signal["code"].nunique()),
        "date_start": str(signal["date"].min()),
        "date_end": str(signal["date"].max()),
        "score_mean": float(signal["score"].mean()),
        "score_std": float(signal["score"].std(ddof=1)) if len(signal) > 1 else 0.0,
    }
    (out / "ml_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"signal": signal, "summary": summary, "out": str(out)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward ML 信号生成")
    ap.add_argument("--data", default="data")
    ap.add_argument("--features", default="momentum:20,volatility:20,ma_gap:20")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--train-size", type=int, default=252)
    ap.add_argument("--test-size", type=int, default=63)
    ap.add_argument("--step", type=int, default=None)
    ap.add_argument("--purge", type=int, default=None, help="默认等于 horizon")
    ap.add_argument("--embargo", type=int, default=0)
    ap.add_argument("--expanding", action="store_true")
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--out", default="out/ml")
    a = ap.parse_args(argv)
    result = run_ml(a.data, a.features, a.horizon, a.train_size, a.test_size,
                    a.step, a.purge, a.embargo, a.expanding, a.alpha, a.out)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"  signal: {Path(result['out']) / 'signal.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())