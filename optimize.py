"""参数搜索与策略变体批量回测。

默认通过子进程调用 `runner_main.py run`，每个变体独立进程、独立输出目录，
不会干扰当前正在运行的回测。

示例：
    python -m optimize --template variants/template.py --grid grid.json ^
      --cash 350000 --start 2020-01-01 --end 2024-12-31 ^
      --out out/optimize --objective sharpe --workers 4
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

import analytics
import overfit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
OBJECTIVES = {"sharpe", "sortino", "calmar", "total_return",
              "annualized_return", "profit_factor", "trade_win_rate"}


def expand_grid(grid: dict) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    values = [v if isinstance(v, list) else [v] for v in grid.values()]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def render_template(template_path, params: dict, out_path) -> Path:
    text = Path(template_path).read_text(encoding="utf-8")
    for key, value in params.items():
        text = re.sub(r"{{\s*" + re.escape(key) + r"\s*}}", str(value), text)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def _run_command(strategy: Path, outdir: Path, cash: float, start, end,
                 fill_mode: str, runner_main: Path) -> dict:
    cmd = [sys.executable, str(runner_main), "run", "--strategy", str(strategy),
           "--cash", str(cash), "--out", str(outdir), "--force",
           "--fill-mode", fill_mode]
    if start:
        cmd.extend(["--start", str(start)])
    if end:
        cmd.extend(["--end", str(end)])
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr}


def run_one(strategy, params=None, outdir=None, cash=350000.0, start=None,
            end=None, fill_mode="next_open", runner_main=None) -> dict:
    strategy = Path(strategy)
    outdir = Path(outdir)
    runner_main = Path(runner_main) if runner_main else ROOT / "runner_main.py"
    run = _run_command(strategy, outdir, cash, start, end, fill_mode, runner_main)
    result = {"strategy": str(strategy), "out": str(outdir),
              "params": params or {}, "ok": run["ok"],
              "returncode": run["returncode"]}
    if run["ok"]:
        try:
            equity = analytics.load_equity(outdir)
            fills = analytics.load_fills(outdir)
            result["metrics"] = analytics.compute_metrics(equity, fills)
        except Exception as e:
            result["ok"] = False
            result["error"] = f"指标读取失败: {e}"
    else:
        tail = (run["stderr"] or run["stdout"] or "").strip().splitlines()[-12:]
        result["error"] = "\n".join(tail) or f"exit {run['returncode']}"
    return result


def rank_results(results: list[dict], objective: str = "sharpe") -> list[dict]:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective 必须是: {sorted(OBJECTIVES)}")
    rows = [r for r in results if r.get("ok") and r.get("metrics")]
    rows.sort(key=lambda r: float(r["metrics"].get(objective, float("-inf")) or 0),
              reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def run_optimization(strategies=None, template=None, grid=None,
                     out_dir="out/optimize", objective="sharpe",
                     workers=1, cash=350000.0, start=None, end=None,
                     fill_mode="next_open", runner_main=None) -> dict:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective 必须是: {sorted(OBJECTIVES)}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tasks = []
    if template:
        params_list = expand_grid(grid or {})
        variants = out / "variants"
        variants.mkdir(parents=True, exist_ok=True)
        for i, params in enumerate(params_list):
            suffix = "_".join(f"{k}{v}" for k, v in params.items()) or "base"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", suffix)[:80]
            path = render_template(template, params, variants / f"variant_{i:03d}_{safe}.py")
            tasks.append((path, params))
    else:
        for s in strategies or []:
            tasks.append((Path(s), {}))
    if not tasks:
        raise ValueError("没有策略任务：请提供 template+grid 或 strategies")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {}
        for i, (strategy, params) in enumerate(tasks):
            od = out / "runs" / f"run_{i:03d}"
            futures[pool.submit(run_one, strategy, params, od, cash, start,
                                end, fill_mode, runner_main)] = (strategy, params)
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            flag = "OK" if r.get("ok") else "FAIL"
            print(f"  [{flag}] {Path(r['strategy']).name} -> {r['out']}")

    ranked = rank_results(results, objective)
    rows = []
    for r in results:
        m = r.get("metrics") or {}
        rows.append({
            "strategy": r["strategy"], "out": r["out"],
            "params": json.dumps(r.get("params") or {}, ensure_ascii=False),
            "ok": bool(r.get("ok")),
            "error": r.get("error", ""),
            **{k: m.get(k) for k in ("total_return", "annualized_return",
                                     "annualized_volatility", "sharpe", "sortino",
                                     "calmar", "max_drawdown", "trade_count",
                                     "trade_win_rate", "profit_factor", "turnover")},
            "rank": r.get("rank", ""),
        })
    trials = pd.DataFrame(rows).sort_values(["ok", "rank"], ascending=[False, True])
    trials_path = out / "trials.csv"
    trials.to_csv(trials_path, index=False)
    best = ranked[0] if ranked else None
    if best is not None:
        best["overfit"] = overfit.summarize_trials(results, objective)
    (out / "best.json").write_text(
        json.dumps(analytics.json_safe(best), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"objective": objective, "results": results, "ranked": ranked,
            "best": best, "trials": str(trials_path)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="参数搜索/策略变体批量回测")
    ap.add_argument("--template", help="含 {{param}} 占位符的策略模板")
    ap.add_argument("--grid", help="参数网格 JSON，例如 {\"fast\":[5,10],\"slow\":[20,30]}")
    ap.add_argument("--strategies", nargs="+", help="直接运行一组策略文件或 glob")
    ap.add_argument("--out", default="out/optimize")
    ap.add_argument("--objective", default="sharpe", choices=sorted(OBJECTIVES))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cash", type=float, default=350000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--fill-mode", default="next_open", choices=["next_open", "same_close"])
    ap.add_argument("--runner-main", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    grid = None
    if a.grid:
        grid = json.loads(Path(a.grid).read_text(encoding="utf-8"))
    strategies = []
    for item in a.strategies or []:
        strategies.extend(sorted(Path().glob(item)) if any(ch in item for ch in "*?[") else [Path(item)])
    result = run_optimization(
        strategies=strategies, template=a.template, grid=grid, out_dir=a.out,
        objective=a.objective, workers=a.workers, cash=a.cash, start=a.start,
        end=a.end, fill_mode=a.fill_mode, runner_main=a.runner_main)
    if result["best"]:
        print(json.dumps(analytics.json_safe({"objective": result["objective"],
                                              "best": result["best"]}),
                         ensure_ascii=False, indent=2, default=str))
    else:
        print("没有成功完成的任务")
        return 1
    print(f"  trials: {result['trials']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())