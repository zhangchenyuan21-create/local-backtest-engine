"""轻量因子计算与因子评价。

不参与回测撮合，只消费 `date × code` 的宽表。

用法：
    python -m factors --data data --factor momentum --window 20 --horizon 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_close_panel(data_dir, adjusted=True) -> pd.DataFrame:
    """从本地 parquet 目录读取收盘价宽表。

    优先读取 `daily_qfq/`，不存在时回退 `daily/`。
    """
    root = Path(data_dir)
    sub = root / "daily_qfq" if adjusted and (root / "daily_qfq").is_dir() else root / "daily"
    files = sorted(sub.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"{sub} 下没有 parquet 日线文件")
    series = []
    for p in files:
        df = pd.read_parquet(p)
        if "date" not in df.columns or "close" not in df.columns:
            continue
        code = p.stem.replace("_", ".", 1)
        s = df.set_index("date")["close"].astype(float)
        s.name = code
        series.append(s)
    if not series:
        raise ValueError("日线文件缺少 date/close 字段")
    panel = pd.concat(series, axis=1).sort_index()
    panel.index = pd.Index([str(x)[:10] for x in panel.index], name="date")
    return panel


def momentum(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    return close / close.shift(window) - 1.0


def reversal(close: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    return -momentum(close, window)


def volatility(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    return np.log(close).diff().rolling(window).std() * math.sqrt(252)


def ma_gap(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    return close / close.rolling(window).mean() - 1.0


def rsi(close: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def forward_returns(close: pd.DataFrame, periods: int = 5) -> pd.DataFrame:
    return close.shift(-periods) / close - 1.0


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True)


def cross_sectional_zscore(frame: pd.DataFrame) -> pd.DataFrame:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1).replace(0, np.nan)
    return frame.sub(mean, axis=0).div(std, axis=0)


def evaluate_factor(factor: pd.DataFrame, forward: pd.DataFrame,
                    quantiles: int = 5, horizon: int = 5,
                    min_names: int = 5) -> dict:
    """计算 IC / RankIC / ICIR / 分层收益 / 多空 / 换手。"""
    if factor is None or forward is None or factor.empty:
        raise ValueError("factor/forward 不能为空")
    common_dates = sorted(set(factor.index) & set(forward.index))
    common_codes = sorted(set(factor.columns) & set(forward.columns))
    factor = factor.reindex(index=common_dates, columns=common_codes)
    forward = forward.reindex(index=common_dates, columns=common_codes)
    rows = []
    qrows = []
    prev_bucket = None
    turnovers = []
    for date in common_dates:
        f = factor.loc[date].dropna()
        r = forward.loc[date].reindex(f.index).dropna()
        f = f.reindex(r.index)
        if len(f) < min_names:
            continue
        ic = f.corr(r)
        rank_ic = f.rank().corr(r.rank())
        buckets = pd.qcut(f.rank(method="first"), quantiles, labels=False)
        qret = r.groupby(buckets).mean().reindex(range(quantiles))
        long_short = float(qret.iloc[-1] - qret.iloc[0])
        rows.append({"date": str(date), "ic": float(ic), "rank_ic": float(rank_ic),
                     "long_short": long_short, "n": len(f)})
        qrows.append({"date": str(date), **{f"q{i + 1}": float(qret.iloc[i])
                                             for i in range(quantiles)}})
        if prev_bucket is not None:
            common = prev_bucket.index.intersection(buckets.index)
            if len(common):
                turnovers.append(float((prev_bucket.loc[common] != buckets.loc[common]).mean()))
        prev_bucket = buckets
    if not rows:
        raise ValueError("没有足够的有效截面")
    ic_df = pd.DataFrame(rows)
    q_df = pd.DataFrame(qrows)
    ic_mean = float(ic_df["ic"].mean())
    ic_std = float(ic_df["ic"].std(ddof=1)) if len(ic_df) > 1 else 0.0
    ric_mean = float(ic_df["rank_ic"].mean())
    ric_std = float(ic_df["rank_ic"].std(ddof=1)) if len(ic_df) > 1 else 0.0
    scale = math.sqrt(252.0 / max(1, horizon))
    summary = {
        "dates": int(len(ic_df)),
        "avg_names": float(ic_df["n"].mean()),
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": ic_mean / ic_std * scale if ic_std > 0 else 0.0,
        "ic_t_stat": ic_mean / ic_std * math.sqrt(len(ic_df)) if ic_std > 0 else 0.0,
        "ic_positive_rate": float((ic_df["ic"] > 0).mean()),
        "rank_ic_mean": ric_mean,
        "rank_icir": ric_mean / ric_std * scale if ric_std > 0 else 0.0,
        "long_short_mean": float(ic_df["long_short"].mean()),
        "turnover_mean": float(np.mean(turnovers)) if turnovers else 0.0,
        "quantile_mean": {f"q{i + 1}": float(q_df[f"q{i + 1}"].mean())
                          for i in range(quantiles)},
    }
    return {"summary": summary, "ic": ic_df, "quantiles": q_df}


def _factor_from_name(name: str, close: pd.DataFrame, window: int) -> pd.DataFrame:
    name = name.lower()
    if name == "momentum":
        return momentum(close, window)
    if name == "reversal":
        return reversal(close, window)
    if name == "volatility":
        return volatility(close, window)
    if name == "ma_gap":
        return ma_gap(close, window)
    if name == "rsi":
        return rsi(close, window)
    raise ValueError(f"未知因子: {name}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="因子计算与 IC/分层评价")
    ap.add_argument("--data", default="data", help="本地数据根目录")
    ap.add_argument("--factor", default="momentum",
                    choices=["momentum", "reversal", "volatility", "ma_gap", "rsi"])
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--out", default=None, help="输出目录，默认不写文件")
    a = ap.parse_args(argv)
    close = load_close_panel(a.data)
    factor = _factor_from_name(a.factor, close, a.window)
    fwd = forward_returns(close, a.horizon)
    result = evaluate_factor(factor, fwd, quantiles=a.quantiles, horizon=a.horizon)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "factor_summary.json").write_text(
            json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
        result["ic"].to_csv(out / "factor_ic.csv", index=False)
        result["quantiles"].to_csv(out / "factor_quantiles.csv", index=False)
        print(f"  written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())