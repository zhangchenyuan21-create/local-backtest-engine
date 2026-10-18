"""回测完成后的综合分析报告。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import analytics

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_json(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find(run_dir, name, filename):
    candidates = [run_dir.parent / name / filename,
                  Path.cwd() / "out" / name / filename,
                  Path.cwd() / name / filename]
    existing = [p for p in candidates if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def _fmt(x, pct=False, digits=2):
    if x is None:
        return "—"
    try:
        v = float(x)
    except Exception:
        return str(x)
    return f"{v * 100:.{digits}f}%" if pct else f"{v:,.{digits}f}"

def build_report(run_dir, factor=None, optimize=None, walkforward=None,
                 ml=None, paper=None, reconcile=None) -> dict:
    run = Path(run_dir)
    equity = analytics.load_equity(run)
    fills = analytics.load_fills(run)
    metrics = analytics.compute_metrics(equity, fills)
    strategy = str(fills["run"].iloc[0]) if (not fills.empty and "run" in fills.columns) else None
    paths = {
        "factor": factor or _find(run, "factor", "factor_summary.json"),
        "optimize": optimize or _find(run, "optimize", "best.json"),
        "walkforward": walkforward or _find(run, "walkforward", "walkforward_summary.json"),
        "ml": ml or _find(run, "ml", "ml_summary.json"),
        "paper": paper or _find(run, "paper", "paper_summary.json"),
        "reconcile": reconcile or _find(run, "reconcile", "reconciliation.json"),
    }
    evidence = {k: _load_json(v) for k, v in paths.items()}
    missing = [k for k, v in evidence.items() if v is None]
    flags = []
    if metrics["total_return"] <= 0:
        flags.append("总收益不为正")
    if metrics["max_drawdown"] <= -0.30:
        flags.append("最大回撤超过 30%")
    if metrics["trade_count"] < 10:
        flags.append("交易样本少于 10 笔")
    dsr = ((evidence.get("optimize") or {}).get("overfit") or {}).get("deflated_sharpe")
    if dsr is not None and float(dsr) < 0.95:
        flags.append("参数搜索的 Deflated Sharpe < 0.95")
    rec = evidence.get("reconcile") or {}
    if (rec.get("orders", {}).get("issues", 0) or (rec.get("fills") or {}).get("issues", 0)):
        flags.append("模拟盘对账存在差异")
    wf = evidence.get("walkforward") or {}
    if wf and wf.get("completed", 0) == 0:
        flags.append("Walk-forward 没有完成窗口")
    if wf and evidence.get("paper"):
        readiness = "已有 Walk-forward 和 Paper 证据；仍需模拟盘/小资金验证，尚不可直接实盘"
    elif wf:
        readiness = "已有 Walk-forward 样本外证据；尚无 Paper 回放，尚不可实盘"
    elif evidence.get("paper"):
        readiness = "已有 Paper 回放；尚无 Walk-forward 样本外证据"
    else:
        readiness = "研究观察阶段：尚无 Walk-forward 和 Paper 证据，不可用于实盘"
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_dir": str(run.resolve()),
        "strategy": strategy,
        "metrics": metrics,
        "evidence": evidence,
        "evidence_status": {k: ("present" if v is not None else "missing")
                            for k, v in evidence.items()},
        "missing_evidence": missing,
        "flags": flags,
        "readiness": readiness,
        "next_commands": {
            "factor": "python -m factors --data data --factor momentum --window 20 --horizon 5 --out out/factor",
            "optimize": "python -m optimize --template variants/template.py --grid grid.json --out out/optimize",
            "walkforward": "python -m walkforward --template variants/template.py --grid grid.json --data data --train-size 252 --test-size 63 --out out/walkforward",
            "ml": "python -m ml --data data --features momentum:20,volatility:20,ma_gap:20 --out out/ml",
            "paper": "python -m paper_trade --signal out/ml/signal.csv --data data --out out/paper",
            "reconcile": "python -m reconcile --paper out/paper --ths-orders ths_orders.csv --out out/reconcile",
        },
    }

def report_markdown(r: dict) -> str:
    m = r["metrics"]
    lines = [
        "# 回测综合分析报告", "",
        f"- 生成时间：{r['generated_at']}",
        f"- 回测目录：`{r['run_dir']}`",
        f"- 策略：`{r.get('strategy') or '—'}`",
        f"- **当前结论：{r['readiness']}**", "",
        "## 一、绩效与风险", "",
        "| 指标 | 数值 |", "|---|---:|",
        f"| 区间 | {m['start']} ~ {m['end']} |",
        f"| 总收益 | {_fmt(m['total_return'], pct=True)} |",
        f"| 年化收益 | {_fmt(m['annualized_return'], pct=True)} |",
        f"| 年化波动 | {_fmt(m['annualized_volatility'], pct=True)} |",
        f"| Sharpe | {_fmt(m['sharpe'], digits=3)} |",
        f"| Sortino | {_fmt(m['sortino'], digits=3)} |",
        f"| Calmar | {_fmt(m['calmar'], digits=3)} |",
        f"| 最大回撤 | {_fmt(m['max_drawdown'], pct=True)} |",
        f"| 日胜率 | {_fmt(m['daily_win_rate'], pct=True)} |",
        f"| 交易笔数 | {m['trade_count']} |",
        f"| 交易胜率 | {_fmt(m['trade_win_rate'], pct=True)} |",
        f"| 盈亏比 | {_fmt(m['profit_factor'], digits=3)} |",
        f"| 年化换手 | {_fmt(m['turnover'])} |",
        f"| 总费用 | {_fmt(m['total_fees'])} |", "",
        "## 二、因子 / IC 证据", "",
    ]
    f = r["evidence"].get("factor")
    if f:
        lines += [f"- IC 均值：{_fmt(f.get('ic_mean'), digits=4)}",
                  f"- RankIC 均值：{_fmt(f.get('rank_ic_mean'), digits=4)}",
                  f"- ICIR：{_fmt(f.get('icir'), digits=3)}",
                  f"- 多空均值：{_fmt(f.get('long_short_mean'), pct=True)}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["factor"], "```"]
    lines += ["", "## 三、参数优化与过拟合", ""]
    o = r["evidence"].get("optimize")
    if o:
        ov = o.get("overfit") or {}
        lines += [f"- 最优参数：`{json.dumps(o.get('params', {}), ensure_ascii=False)}`",
                  f"- Deflated Sharpe：{_fmt(ov.get('deflated_sharpe'), digits=3)}",
                  f"- 显著：{ov.get('significant', '—')}",
                  f"- 试验次数：{ov.get('num_trials', '—')}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["optimize"], "```"]
    lines += ["", "## 四、Walk-forward 样本外证据", ""]
    w = r["evidence"].get("walkforward")
    if w:
        lines += [f"- 窗口数：{w.get('splits', '—')}",
                  f"- 完成窗口：{w.get('completed', '—')}",
                  f"- 平均测试 Sharpe：{_fmt(w.get('mean_test_sharpe'), digits=3)}",
                  f"- 平均测试收益：{_fmt(w.get('mean_test_return'), pct=True)}",
                  f"- 平均 DSR：{_fmt(w.get('mean_dsr'), digits=3)}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["walkforward"], "```"]
    lines += ["", "## 五、ML 信号证据", ""]
    ml = r["evidence"].get("ml")
    if ml:
        lines += [f"- 模型：`{ml.get('model', '—')}`",
                  f"- 特征：`{', '.join(ml.get('features', []))}`",
                  f"- 预测条数：{ml.get('predictions', '—')}",
                  f"- 日期范围：{ml.get('date_start', '—')} ~ {ml.get('date_end', '—')}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["ml"], "```"]
    lines += ["", "## 六、Paper / 模拟盘证据", ""]
    p = r["evidence"].get("paper")
    if p:
        lines += [f"- 模拟区间：{p.get('start', '—')} ~ {p.get('end', '—')}",
                  f"- 模拟收益：{_fmt(p.get('total_return'), pct=True)}",
                  f"- 订单/成交：{p.get('orders', '—')} / {p.get('fills', '—')}",
                  f"- 未完成订单：{p.get('pending_orders', '—')}",
                  f"- 拒单：{p.get('rejected_orders', '—')}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["paper"], "```"]
    lines += ["", "## 七、模拟盘对账", ""]
    rec = r["evidence"].get("reconcile")
    if rec:
        lines += [f"- 订单匹配：{rec.get('orders', {}).get('matched', '—')}",
                  f"- 订单差异：{rec.get('orders', {}).get('issues', '—')}",
                  f"- 成交差异：{(rec.get('fills') or {}).get('issues', '—')}"]
    else:
        lines += ["未提供。可运行：", "", "```powershell", r["next_commands"]["reconcile"], "```"]
    lines += ["", "## 八、风险提示与就绪度", "", f"**就绪度：{r['readiness']}**", ""]
    lines += [f"- {x}" for x in r["flags"]] if r["flags"] else ["当前没有触发硬性风险标记。"]
    lines += ["", "## 九、证据清单", ""]
    lines += [f"- {k}: {v}" for k, v in r["evidence_status"].items()]
    lines += ["", "> 本报告是研究工具输出，不构成投资建议。实盘前必须完成模拟盘验证、对账和人工风控。"]
    return "\n".join(lines) + "\n"

def write_report(run_dir, report_dir=None, factor=None, optimize=None,
                 walkforward=None, ml=None, paper=None, reconcile=None,
                 formats=("md", "json")) -> dict:
    report = build_report(run_dir, factor, optimize, walkforward, ml, paper, reconcile)
    out = Path(report_dir) if report_dir else Path(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    if "json" in formats:
        p = out / "analysis_report.json"
        p.write_text(json.dumps(analytics.json_safe(report), ensure_ascii=False,
                                indent=2, default=str), encoding="utf-8")
        written["json"] = str(p)
    if "md" in formats:
        p = out / "analysis_report.md"
        p.write_text(report_markdown(report), encoding="utf-8")
        written["md"] = str(p)
    return {"report": report, "written": written}


def main(argv=None):
    ap = argparse.ArgumentParser(description="回测完成后的综合分析报告")
    ap.add_argument("--run", required=True, help="回测输出目录")
    ap.add_argument("--factor", default=None)
    ap.add_argument("--optimize", default=None)
    ap.add_argument("--walkforward", default=None)
    ap.add_argument("--ml", default=None)
    ap.add_argument("--paper", default=None)
    ap.add_argument("--reconcile", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--format", default="md,json")
    a = ap.parse_args(argv)
    formats = tuple(x.strip().lower() for x in a.format.split(",") if x.strip())
    result = write_report(a.run, a.out, a.factor, a.optimize, a.walkforward,
                          a.ml, a.paper, a.reconcile, formats)
    print(f"  status: {result['report']['readiness']}")
    for k, v in result["written"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())