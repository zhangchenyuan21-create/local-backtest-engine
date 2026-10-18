"""Walk-forward 参数选择与样本外评估。

时间切分采用：
    train | purge（从训练尾部删除） | embargo（跳过测试头部） | test
训练期选参数，测试期只评估；测试期不参与参数选择。

用法：
    python -m walkforward --template variants/t.py --grid grid.json ^
      --data data --train-size 252 --test-size 63 --step 63 ^
      --out out/walkforward --objective sharpe --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

import analytics
import optimize
import overfit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_calendar(data_dir) -> list[str]:
    p = Path(data_dir) / "calendar.json"
    if not p.exists():
        raise FileNotFoundError(f"找不到交易日历: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("days") or raw.get("calendar") or []
    return sorted({str(x)[:10] for x in raw})


def make_splits(dates, train_size: int, test_size: int, step: int | None = None,
                purge: int = 0, embargo: int = 0,
                expanding: bool = False) -> list[dict]:
    dates = sorted({str(x)[:10] for x in dates})
    train_size, test_size = int(train_size), int(test_size)
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size/test_size 必须为正")
    step = int(step or test_size)
    if step <= 0:
        raise ValueError("step 必须为正")
    purge = max(0, int(purge))
    embargo = max(0, int(embargo))
    max_offset = len(dates) - train_size - test_size
    if max_offset < 0:
        return []
    out = []
    split_no = 0
    for offset in range(0, max_offset + 1, step):
        train_start_idx = 0 if expanding else offset
        train_end_idx = offset + train_size
        test_start_idx = train_end_idx + embargo
        test_end_idx = train_end_idx + test_size
        if test_end_idx > len(dates):
            break
        train_eff_end = train_end_idx - purge
        if train_eff_end <= train_start_idx or test_start_idx >= test_end_idx:
            continue
        train = dates[train_start_idx:train_eff_end]
        test = dates[test_start_idx:test_end_idx]
        if not train or not test:
            continue
        out.append({
            "split": split_no,
            "train_start": train[0], "train_end": train[-1],
            "test_start": test[0], "test_end": test[-1],
            "train_days": len(train), "test_days": len(test),
            "train_indices": [train_start_idx, train_eff_end - 1],
            "test_indices": [test_start_idx, test_end_idx - 1],
        })
        split_no += 1
    return out


def _run_variants(variants, split, root, cash, workers, runner_main):
    def one(item):
        return optimize.run_one(
            item["strategy"], item["params"],
            Path(root) / f"split_{split['split']:03d}" / "train" / item["name"],
            cash=cash, start=split["train_start"], end=split["train_end"],
            runner_main=runner_main)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        return list(pool.map(one, variants))


def _stitch_oos(parts) -> pd.DataFrame:
    frames = []
    base = 1.0
    for eq in parts:
        if eq is None or eq.empty or len(eq) == 0:
            continue
        e = eq[["date", "total_value"]].copy()
        first = float(e["total_value"].iloc[0])
        if first <= 0:
            continue
        e["total_value"] = e["total_value"].astype(float) / first * base
        base = float(e["total_value"].iloc[-1])
        frames.append(e)
    if not frames:
        return pd.DataFrame(columns=["date", "total_value"])
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def walk_forward_optimize(template, grid, data_dir, train_size: int, test_size: int,
                          step: int | None = None, purge: int = 0, embargo: int = 0,
                          expanding: bool = False, start=None, end=None,
                          out_dir="out/walkforward", objective="sharpe",
                          workers=1, cash=350000.0, runner_main=None,
                          max_splits=None) -> dict:
    if objective not in optimize.OBJECTIVES:
        raise ValueError(f"objective 必须是: {sorted(optimize.OBJECTIVES)}")
    calendar = load_calendar(data_dir)
    if start:
        calendar = [d for d in calendar if d >= str(start)]
    if end:
        calendar = [d for d in calendar if d <= str(end)]
    splits = make_splits(calendar, train_size, test_size, step, purge, embargo, expanding)
    if max_splits:
        splits = splits[: int(max_splits)]
    if not splits:
        raise ValueError("没有足够的交易日生成 walk-forward 切分")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    variants_dir = out / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    variants = []
    for i, params in enumerate(optimize.expand_grid(grid or {})):
        suffix = "_".join(f"{k}{v}" for k, v in params.items()) or "base"
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in suffix)[:70]
        path = optimize.render_template(template, params, variants_dir / f"variant_{i:03d}_{safe}.py")
        variants.append({"name": f"variant_{i:03d}", "strategy": path, "params": params})
    if not variants:
        raise ValueError("参数网格为空")

    split_results = []
    oos_parts = []
    for split in splits:
        train_results = _run_variants(variants, split, out / "runs", cash, workers, runner_main)
        ranked = optimize.rank_results(train_results, objective)
        if not ranked:
            split_results.append({**split, "ok": False, "error": "训练期没有成功变体"})
            continue
        best = ranked[0]
        test = optimize.run_one(
            best["strategy"], best["params"],
            out / "runs" / f"split_{split['split']:03d}" / "test",
            cash=cash, start=split["test_start"], end=split["test_end"],
            runner_main=runner_main)
        test_metrics = test.get("metrics") or {}
        overfit_info = overfit.deflated_sharpe_ratio(
            observed_sharpe=float(test_metrics.get("sharpe", 0.0) or 0.0),
            num_periods=int(test_metrics.get("trading_days", 0) or 0),
            num_trials=max(1, len(train_results)))
        row = {
            **{k: split[k] for k in ("split", "train_start", "train_end",
                                      "test_start", "test_end", "train_days", "test_days")},
            "ok": bool(test.get("ok")),
            "selected_params": json.dumps(best.get("params") or {}, ensure_ascii=False),
            "train_objective": float((best.get("metrics") or {}).get(objective, 0) or 0),
            "test_sharpe": float(test_metrics.get("sharpe", 0.0) or 0.0),
            "test_return": float(test_metrics.get("total_return", 0.0) or 0.0),
            "test_max_drawdown": float(test_metrics.get("max_drawdown", 0.0) or 0.0),
            "dsr": float(overfit_info["deflated_sharpe"]),
            "dsr_significant": bool(overfit_info["significant"]),
            "error": "" if test.get("ok") else test.get("error", "test run failed"),
        }
        split_results.append(row)
        if test.get("ok"):
            try:
                oos_parts.append(analytics.load_equity(Path(test["out"])))
            except Exception:
                pass

    splits_df = pd.DataFrame(split_results)
    splits_path = out / "walkforward_splits.csv"
    splits_df.to_csv(splits_path, index=False)
    oos = _stitch_oos(oos_parts)
    oos_metrics = None
    if len(oos) >= 2:
        oos.to_csv(out / "oos_equity.csv", index=False)
        oos_metrics = analytics.compute_metrics(oos, None)
    completed = [x for x in split_results if x.get("ok")]
    best = max(completed, key=lambda x: x.get("test_sharpe", 0.0)) if completed else None
    summary = {
        "objective": objective,
        "splits": len(splits),
        "completed": len(completed),
        "splits_file": str(splits_path),
        "oos_metrics": oos_metrics,
        "best_oos_split": best,
        "mean_test_sharpe": float(splits_df["test_sharpe"].mean()) if completed else None,
        "mean_test_return": float(splits_df["test_return"].mean()) if completed else None,
        "mean_dsr": float(splits_df["dsr"].mean()) if completed else None,
    }
    (out / "walkforward_summary.json").write_text(
        json.dumps(analytics.json_safe(summary), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    return {"summary": summary, "splits": split_results, "oos_metrics": oos_metrics,
            "splits_file": str(splits_path), "out": str(out)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward 参数选择/样本外评估")
    ap.add_argument("--template", required=True)
    ap.add_argument("--grid", required=True, help="参数网格 JSON")
    ap.add_argument("--data", default="data", help="包含 calendar.json 的数据根目录")
    ap.add_argument("--train-size", type=int, required=True, help="训练交易日数")
    ap.add_argument("--test-size", type=int, required=True, help="测试交易日数")
    ap.add_argument("--step", type=int, default=None, help="窗口步长，默认=test-size")
    ap.add_argument("--purge", type=int, default=0, help="从训练尾部删除的交易日数")
    ap.add_argument("--embargo", type=int, default=0, help="训练/测试之间的间隔交易日数")
    ap.add_argument("--expanding", action="store_true", help="使用扩展训练窗口")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default="out/walkforward")
    ap.add_argument("--objective", default="sharpe", choices=sorted(optimize.OBJECTIVES))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cash", type=float, default=350000.0)
    ap.add_argument("--max-splits", type=int, default=None)
    ap.add_argument("--runner-main", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    grid = json.loads(Path(a.grid).read_text(encoding="utf-8"))
    result = walk_forward_optimize(
        template=a.template, grid=grid, data_dir=a.data,
        train_size=a.train_size, test_size=a.test_size, step=a.step,
        purge=a.purge, embargo=a.embargo, expanding=a.expanding,
        start=a.start, end=a.end, out_dir=a.out, objective=a.objective,
        workers=a.workers, cash=a.cash, runner_main=a.runner_main,
        max_splits=a.max_splits)
    print(json.dumps(analytics.json_safe(result["summary"]), ensure_ascii=False,
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())