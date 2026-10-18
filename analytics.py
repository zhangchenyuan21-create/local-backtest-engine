"""回测绩效与风险报告。

只读取 `equity.csv` / `fills.csv`，不改变回测撮合逻辑。

用法：
    python -m analytics --out out/demo --format md,json,html
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ANN = 252


def _fmt_pf(x):
    return "∞" if isinstance(x, float) and math.isinf(x) else f"{float(x):.3f}"


def json_safe(obj):
    """把 inf/nan 转成 None，保证输出是严格 JSON。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def _read_csv(path: Path, name: str) -> pd.DataFrame:
    p = path / name if path.is_dir() else path
    if not p.exists():
        raise FileNotFoundError(f"找不到 {p}")
    df = pd.read_csv(p)
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
    return df


def load_equity(path) -> pd.DataFrame:
    df = _read_csv(Path(path), "equity.csv")
    missing = {"date", "total_value"} - set(df.columns)
    if missing:
        raise ValueError(f"equity.csv 缺少字段: {sorted(missing)}")
    df = df.copy()
    df["total_value"] = pd.to_numeric(df["total_value"], errors="coerce")
    df = df.dropna(subset=["total_value"]).sort_values("date").reset_index(drop=True)
    for col in ("positions_value", "cash", "n_pos"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def load_fills(path) -> pd.DataFrame:
    df = _read_csv(Path(path), "fills.csv")
    if df.empty:
        return df
    df = df.copy()
    for col in ("price", "amount", "value", "fee"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df.sort_values("date", kind="mergesort").reset_index(drop=True)


def _returns(equity: pd.DataFrame) -> pd.Series:
    return equity["total_value"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def drawdown_series(equity: pd.DataFrame) -> pd.Series:
    values = equity["total_value"].astype(float)
    return (values / values.cummax() - 1.0).fillna(0.0)


def max_drawdown(equity: pd.DataFrame) -> dict:
    if equity.empty:
        return {"max_drawdown": 0.0, "peak_date": None, "trough_date": None,
                "recovery_date": None, "underwater_days": 0}
    values = equity["total_value"].astype(float).reset_index(drop=True)
    dates = equity["date"].astype(str).reset_index(drop=True)
    peak = values.cummax()
    dd = values / peak - 1.0
    trough = int(dd.idxmin())
    peak_value = float(peak.iloc[trough])
    peak_idx = int((values.iloc[:trough + 1] == peak_value).to_numpy().argmax())
    recovery_idx = None
    for i in range(trough + 1, len(values)):
        if values.iloc[i] >= peak_value - 1e-12:
            recovery_idx = i
            break
    return {
        "max_drawdown": float(dd.iloc[trough]),
        "peak_date": str(dates.iloc[peak_idx]),
        "trough_date": str(dates.iloc[trough]),
        "recovery_date": str(dates.iloc[recovery_idx]) if recovery_idx is not None else None,
        "underwater_days": int((dd < -1e-12).sum()),
    }


def build_trades(fills: pd.DataFrame) -> pd.DataFrame:
    """按 FIFO 把买卖成交还原成逐笔交易。"""
    cols = ["entry_date", "exit_date", "code", "quantity", "entry_price",
            "exit_price", "pnl", "return_pct", "holding_days", "fee"]
    if fills is None or fills.empty:
        return pd.DataFrame(columns=cols)
    lots = defaultdict(deque)
    trades = []
    for row in fills.itertuples(index=False):
        code = str(getattr(row, "code", ""))
        side = str(getattr(row, "side", "")).lower()
        qty = int(getattr(row, "amount", 0) or 0)
        price = float(getattr(row, "price", 0) or 0)
        fee = float(getattr(row, "fee", 0) or 0)
        date = str(getattr(row, "date", ""))
        if qty <= 0 or price <= 0:
            continue
        if side == "buy":
            lots[code].append({"date": date, "qty": qty, "price": price,
                               "fee_ps": fee / qty if qty else 0.0})
            continue
        if side != "sell":
            continue
        remaining = qty
        sell_fee_ps = fee / qty if qty else 0.0
        while remaining > 0 and lots[code]:
            lot = lots[code][0]
            take = min(remaining, int(lot["qty"]))
            cost = (float(lot["price"]) + float(lot["fee_ps"])) * take
            proceeds = (price - sell_fee_ps) * take
            pnl = proceeds - cost
            entry_date = str(lot["date"])
            try:
                holding = (pd.Timestamp(date) - pd.Timestamp(entry_date)).days
            except Exception:
                holding = None
            trades.append({
                "entry_date": entry_date, "exit_date": date, "code": code,
                "quantity": take, "entry_price": float(lot["price"]),
                "exit_price": price, "pnl": pnl,
                "return_pct": pnl / cost if cost else 0.0,
                "holding_days": holding,
                "fee": (float(lot["fee_ps"]) + sell_fee_ps) * take,
            })
            lot["qty"] = int(lot["qty"]) - take
            if lot["qty"] <= 0:
                lots[code].popleft()
            remaining -= take
    return pd.DataFrame(trades, columns=cols)


def compute_metrics(equity: pd.DataFrame, fills: pd.DataFrame | None = None,
                    risk_free: float = 0.0) -> dict:
    if equity is None or len(equity) < 2:
        raise ValueError("equity 至少需要两行数据")
    equity = equity.sort_values("date").reset_index(drop=True)
    vals = equity["total_value"].astype(float)
    rets = _returns(equity)
    start, end = str(equity["date"].iloc[0]), str(equity["date"].iloc[-1])
    initial, final = float(vals.iloc[0]), float(vals.iloc[-1])
    total_return = final / initial - 1.0 if initial else 0.0
    periods = max(1, len(vals) - 1)
    ann_return = (final / initial) ** (ANN / periods) - 1.0 if initial > 0 and final > 0 else 0.0
    vol = float(rets.std(ddof=1) * math.sqrt(ANN)) if len(rets) > 1 else 0.0
    rf_period = (1.0 + risk_free) ** (1.0 / ANN) - 1.0
    excess = rets - rf_period
    sharpe = float(excess.mean() / excess.std(ddof=1) * math.sqrt(ANN)) \
        if len(excess) > 1 and excess.std(ddof=1) > 0 else 0.0
    downside = np.minimum(rets.to_numpy(dtype=float), 0.0)
    downside_dev = float(math.sqrt(float(np.mean(downside ** 2)))) if len(downside) else 0.0
    sortino = float(rets.mean() / downside_dev * math.sqrt(ANN)) if downside_dev > 0 else 0.0
    dd = max_drawdown(equity)
    calmar = ann_return / abs(dd["max_drawdown"]) if dd["max_drawdown"] < 0 else 0.0
    daily_win = float((rets > 0).sum() / max(1, (rets != 0).sum())) if len(rets) else 0.0

    fills = fills if fills is not None else pd.DataFrame()
    trades = build_trades(fills)
    pnl = trades["pnl"] if not trades.empty else pd.Series(dtype=float)
    wins = float(pnl[pnl > 0].sum())
    losses = float(-pnl[pnl < 0].sum())
    trade_win = float((pnl > 0).sum() / len(pnl)) if len(pnl) else 0.0
    profit_factor = float(wins / losses) if losses > 0 else \
        (float("inf") if wins > 0 else 0.0)
    avg_trade = float(trades["return_pct"].mean()) if not trades.empty else 0.0
    avg_hold = float(trades["holding_days"].dropna().mean()) \
        if not trades.empty and trades["holding_days"].notna().any() else 0.0
    avg_equity = float(vals.mean()) if len(vals) else 0.0
    total_traded = float(fills["value"].sum()) \
        if not fills.empty and "value" in fills.columns else 0.0
    turnover = total_traded / avg_equity * (ANN / max(1, len(vals) - 1)) \
        if avg_equity > 0 else 0.0
    total_fee = float(fills["fee"].sum()) \
        if not fills.empty and "fee" in fills.columns else 0.0
    avg_positions = float(equity["n_pos"].mean()) if "n_pos" in equity.columns else 0.0
    cash_ratio = (equity["cash"] / equity["total_value"]).replace(
        [np.inf, -np.inf], np.nan).mean() if "cash" in equity.columns else 0.0
    return {
        "start": start, "end": end, "trading_days": len(vals),
        "initial_value": initial, "final_value": final,
        "total_return": total_return, "annualized_return": ann_return,
        "annualized_volatility": vol, "sharpe": sharpe,
        "sortino": sortino, "calmar": calmar,
        "max_drawdown": dd["max_drawdown"],
        "max_drawdown_peak": dd["peak_date"],
        "max_drawdown_trough": dd["trough_date"],
        "max_drawdown_recovery": dd["recovery_date"],
        "underwater_days": dd["underwater_days"],
        "daily_win_rate": daily_win, "trade_count": int(len(trades)),
        "trade_win_rate": trade_win, "profit_factor": profit_factor,
        "avg_trade_return": avg_trade, "avg_holding_days": avg_hold,
        "turnover": turnover, "total_fees": total_fee,
        "fee_ratio": total_fee / avg_equity if avg_equity else 0.0,
        "avg_positions": avg_positions,
        "avg_cash_ratio": float(cash_ratio) if pd.notna(cash_ratio) else 0.0,
    }


def format_metrics(m: dict) -> str:
    def pct(x): return f"{x * 100:.2f}%"
    def num(x): return f"{x:,.2f}"
    lines = [
        "=" * 64, "回测绩效报告", "=" * 64,
        f"区间              {m['start']} ~ {m['end']}",
        f"交易日            {m['trading_days']}",
        f"期初/期末资产     {num(m['initial_value'])} / {num(m['final_value'])}",
        f"总收益            {pct(m['total_return'])}",
        f"年化收益          {pct(m['annualized_return'])}",
        f"年化波动          {pct(m['annualized_volatility'])}",
        f"Sharpe            {m['sharpe']:.3f}",
        f"Sortino           {m['sortino']:.3f}",
        f"Calmar            {m['calmar']:.3f}",
        f"最大回撤          {pct(m['max_drawdown'])}",
        f"回撤区间          {m['max_drawdown_peak']} -> {m['max_drawdown_trough']}",
        f"日胜率            {pct(m['daily_win_rate'])}",
        f"交易笔数          {m['trade_count']}",
        f"交易胜率          {pct(m['trade_win_rate'])}",
        f"盈亏比            {_fmt_pf(m['profit_factor'])}",
        f"平均单笔收益      {pct(m['avg_trade_return'])}",
        f"平均持仓天数      {m['avg_holding_days']:.1f}",
        f"年化换手          {m['turnover']:.2f}",
        f"总费用            {num(m['total_fees'])}",
    ]
    return "\n".join(lines)


def report_markdown(m: dict) -> str:
    def pct(x): return f"{x * 100:.2f}%"
    def num(x): return f"{x:,.2f}"
    rows = [
        ("区间", f"{m['start']} ~ {m['end']}"),
        ("交易日", str(m["trading_days"])),
        ("期初资产", num(m["initial_value"])),
        ("期末资产", num(m["final_value"])),
        ("总收益", pct(m["total_return"])),
        ("年化收益", pct(m["annualized_return"])),
        ("年化波动", pct(m["annualized_volatility"])),
        ("Sharpe", f"{m['sharpe']:.3f}"),
        ("Sortino", f"{m['sortino']:.3f}"),
        ("Calmar", f"{m['calmar']:.3f}"),
        ("最大回撤", pct(m["max_drawdown"])),
        ("回撤区间", f"{m['max_drawdown_peak']} -> {m['max_drawdown_trough']}"),
        ("日胜率", pct(m["daily_win_rate"])),
        ("交易笔数", str(m["trade_count"])),
        ("交易胜率", pct(m["trade_win_rate"])),
        ("盈亏比", _fmt_pf(m['profit_factor'])),
        ("平均单笔收益", pct(m["avg_trade_return"])),
        ("平均持仓天数", f"{m['avg_holding_days']:.1f}"),
        ("年化换手", f"{m['turnover']:.2f}"),
        ("总费用", num(m["total_fees"])),
    ]
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"# 回测绩效报告\n\n| 指标 | 数值 |\n|---|---|\n{table}\n"


def _svg_equity(equity: pd.DataFrame, width=900, height=260) -> str:
    vals = equity["total_value"].astype(float).to_numpy()
    if len(vals) < 2:
        return ""
    lo, hi = float(vals.min()), float(vals.max())
    pad = max(1.0, (hi - lo) * 0.05)
    lo, hi = lo - pad, hi + pad
    m = 38
    pts = []
    for i, v in enumerate(vals):
        x = m + i / (len(vals) - 1) * (width - 2 * m)
        y = m + (hi - v) / max(1e-9, hi - lo) * (height - 2 * m)
        pts.append(f"{x:.1f},{y:.1f}")
    line = " ".join(pts)
    return f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" aria-label="equity curve"><rect width="100%" height="100%" fill="#111827"/><polyline points="{line}" fill="none" stroke="#60a5fa" stroke-width="2"/></svg>'


def report_html(m: dict, equity: pd.DataFrame) -> str:
    def pct(x): return f"{x * 100:.2f}%"
    cards = [
        ("总收益", pct(m["total_return"])),
        ("年化收益", pct(m["annualized_return"])),
        ("最大回撤", pct(m["max_drawdown"])),
        ("Sharpe", f"{m['sharpe']:.3f}"),
        ("Sortino", f"{m['sortino']:.3f}"),
        ("交易胜率", pct(m["trade_win_rate"])),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{html.escape(k)}</div>"
        f"<div class='v'>{html.escape(v)}</div></div>" for k, v in cards)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>回测绩效报告</title>
<style>body{{background:#0b1020;color:#e5e7eb;font:14px/1.6 system-ui,Segoe UI,sans-serif;margin:0;padding:28px}}h1{{margin:0 0 4px}}.sub{{color:#94a3b8;margin-bottom:20px}}.cards{{display:grid;grid-template-columns:repeat(3,minmax(140px,1fr));gap:10px;margin-bottom:18px}}.card{{background:#151c31;border:1px solid #26314d;border-radius:10px;padding:12px}}.k{{color:#94a3b8;font-size:12px}}.v{{font-size:22px;font-weight:650;margin-top:2px}}.chart{{background:#111827;border:1px solid #26314d;border-radius:10px;padding:8px}}table{{border-collapse:collapse;width:100%;margin-top:18px}}td,th{{border-bottom:1px solid #26314d;padding:7px 9px;text-align:left}}th{{color:#94a3b8;font-weight:500}}</style></head><body>
<h1>回测绩效报告</h1><div class="sub">{html.escape(m['start'])} ~ {html.escape(m['end'])}</div>
<div class="cards">{card_html}</div><div class="chart">{_svg_equity(equity)}</div>
<table><tbody>
<tr><th>期初 / 期末资产</th><td>{m['initial_value']:,.2f} / {m['final_value']:,.2f}</td></tr>
<tr><th>年化波动</th><td>{pct(m['annualized_volatility'])}</td></tr>
<tr><th>Calmar</th><td>{m['calmar']:.3f}</td></tr>
<tr><th>回撤区间</th><td>{html.escape(str(m['max_drawdown_peak']))} -> {html.escape(str(m['max_drawdown_trough']))}</td></tr>
<tr><th>日胜率</th><td>{pct(m['daily_win_rate'])}</td></tr>
<tr><th>交易笔数 / 盈亏比</th><td>{m['trade_count']} / {_fmt_pf(m['profit_factor'])}</td></tr>
<tr><th>平均单笔 / 持仓天数</th><td>{pct(m['avg_trade_return'])} / {m['avg_holding_days']:.1f}</td></tr>
<tr><th>年化换手 / 总费用</th><td>{m['turnover']:.2f} / {m['total_fees']:,.2f}</td></tr>
</tbody></table></body></html>"""


def write_reports(outdir, formats=("md", "json", "html"), risk_free=0.0) -> dict:
    out = Path(outdir)
    equity = load_equity(out)
    fills = load_fills(out)
    metrics = compute_metrics(equity, fills, risk_free=risk_free)
    written = {}
    out.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        p = out / "metrics.json"
        p.write_text(json.dumps(json_safe(metrics), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written["json"] = str(p)
    if "md" in formats:
        p = out / "report.md"
        p.write_text(report_markdown(metrics), encoding="utf-8")
        written["md"] = str(p)
    if "html" in formats:
        p = out / "report.html"
        p.write_text(report_html(metrics, equity), encoding="utf-8")
        written["html"] = str(p)
    return {"metrics": metrics, "written": written}


def main(argv=None):
    ap = argparse.ArgumentParser(description="回测绩效/风险报告")
    ap.add_argument("--out", required=True, help="回测输出目录（含 equity.csv/fills.csv）")
    ap.add_argument("--format", default="md,json,html", help="md,json,html 逗号分隔")
    ap.add_argument("--risk-free", type=float, default=0.0, help="年化无风险利率，默认 0")
    a = ap.parse_args(argv)
    formats = tuple(x.strip().lower() for x in a.format.split(",") if x.strip())
    result = write_reports(a.out, formats, risk_free=a.risk_free)
    print(format_metrics(result["metrics"]))
    for k, v in result["written"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())