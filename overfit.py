"""过拟合诊断：PSR、Deflated Sharpe 与多重试验校正。

这些函数只处理回测指标，不改变撮合逻辑。

用法：
    python -m overfit --sharpe 1.8 --periods 756 --trials 120
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from statistics import NormalDist

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist()


def _norm_cdf(x: float) -> float:
    return _NORM.cdf(x)


def _norm_ppf(x: float) -> float:
    return _NORM.inv_cdf(min(max(x, 1e-12), 1.0 - 1e-12))


def _psr_periodic(observed_sr: float, num_periods: int, benchmark_sr: float = 0.0,
                  skewness: float = 0.0, excess_kurtosis: float = 0.0) -> float:
    if num_periods < 2:
        return 0.0
    sr = float(observed_sr)
    br = float(benchmark_sr)
    denom_sq = 1.0 - float(skewness) * sr + (float(excess_kurtosis) + 2.0) / 4.0 * sr * sr
    if denom_sq <= 1e-12:
        return 0.0
    z = (sr - br) * math.sqrt(num_periods - 1) / math.sqrt(denom_sq)
    return float(_norm_cdf(z))


def probabilistic_sharpe_ratio(observed_sharpe: float, num_periods: int,
                               benchmark_sharpe: float = 0.0,
                               skewness: float = 0.0,
                               excess_kurtosis: float = 0.0,
                               periods_per_year: int = 252) -> float:
    """PSR：观察到 SR 大于 benchmark SR 的概率。输入为年化 Sharpe。"""
    scale = math.sqrt(max(1, int(periods_per_year)))
    return _psr_periodic(float(observed_sharpe) / scale, int(num_periods),
                         float(benchmark_sharpe) / scale, skewness, excess_kurtosis)


def expected_max_sharpe(num_periods: int, num_trials: int) -> float:
    """在 N 次独立试验、真实 SR=0 的零假设下，最大 SR 的期望（周期口径）。"""
    n = max(1, int(num_trials))
    if n <= 1 or num_periods < 2:
        return 0.0
    z1 = _norm_ppf(1.0 - 1.0 / n)
    z2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    sr_std = 1.0 / math.sqrt(int(num_periods) - 1)
    return float(sr_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe_ratio(observed_sharpe: float, num_periods: int,
                          num_trials: int = 1, periods_per_year: int = 252,
                          skewness: float = 0.0,
                          excess_kurtosis: float = 0.0) -> dict:
    """Bailey/López de Prado 的 Deflated Sharpe Ratio。

    输入 `observed_sharpe` 为年化 Sharpe；`num_trials` 必须包含所有参数/策略尝试，
    否则 DSR 会高估显著性。
    """
    scale = math.sqrt(max(1, int(periods_per_year)))
    sr_periodic = float(observed_sharpe) / scale
    sr0 = expected_max_sharpe(num_periods, num_trials)
    dsr = _psr_periodic(sr_periodic, num_periods, sr0, skewness, excess_kurtosis)
    return {
        "observed_sharpe": float(observed_sharpe),
        "expected_max_sharpe": float(sr0 * scale),
        "deflated_sharpe": float(dsr),
        "p_value": float(1.0 - dsr),
        "significant": bool(dsr >= 0.95),
        "num_trials": int(num_trials),
        "num_periods": int(num_periods),
        "method": "Bailey/Lopez de Prado DSR (normal approximation)",
    }


def summarize_trials(results: list[dict], objective: str = "sharpe") -> dict:
    """为一组参数试验生成过拟合摘要。"""
    successful = [r for r in results if r.get("ok") and r.get("metrics")]
    if not successful:
        return {"num_trials": len(results), "error": "没有成功的试验"}
    best = max(successful, key=lambda r: float(r["metrics"].get(objective, 0) or 0))
    m = best["metrics"]
    dsr = deflated_sharpe_ratio(
        observed_sharpe=float(m.get("sharpe", 0.0) or 0.0),
        num_periods=int(m.get("trading_days", 0) or 0),
        num_trials=max(1, len(results)),
    )
    return {"num_trials": len(results), "objective": objective,
            "best_params": best.get("params", {}),
            "best_metrics": {k: m.get(k) for k in (
                "total_return", "annualized_return", "sharpe", "sortino",
                "max_drawdown", "trade_count", "trade_win_rate")},
            **dsr}


def main(argv=None):
    ap = argparse.ArgumentParser(description="PSR/Deflated Sharpe 过拟合诊断")
    ap.add_argument("--sharpe", type=float, required=True, help="年化 Sharpe")
    ap.add_argument("--periods", type=int, required=True, help="收益周期数")
    ap.add_argument("--trials", type=int, default=1, help="总试验次数，默认 1")
    ap.add_argument("--periods-per-year", type=int, default=252)
    ap.add_argument("--skew", type=float, default=0.0)
    ap.add_argument("--excess-kurtosis", type=float, default=0.0)
    a = ap.parse_args(argv)
    psr = probabilistic_sharpe_ratio(a.sharpe, a.periods, periods_per_year=a.periods_per_year,
                                     skewness=a.skew, excess_kurtosis=a.excess_kurtosis)
    dsr = deflated_sharpe_ratio(a.sharpe, a.periods, a.trials, a.periods_per_year,
                                a.skew, a.excess_kurtosis)
    out = {"psr": psr, **dsr}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())