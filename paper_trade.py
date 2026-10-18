"""基于本地日线的 PaperBroker 回放。

读取 `ml.py` 生成的 signal.csv，通过 PaperBroker 模拟下单、T+1、涨跌停、
成交量约束和费用，输出 paper_equity.csv / paper_orders.csv / paper_fills.csv。

用法：
    python -m paper_trade --signal out/ml/signal.csv --data data ^
      --start 2022-01-01 --end 2024-12-31 --cash 350000 --top-n 5 ^
      --out out/paper
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd

import analytics
import broker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_signal(path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str})
    need = {"date", "code", "score"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"signal 缺少字段: {sorted(missing)}")
    df = df.copy()
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str)
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    return df.dropna(subset=["score"]).sort_values(["date", "score"],
                                                   ascending=[True, False])


def _bar_map(store, limit_prices, date, symbols):
    bars = {}
    for code in symbols:
        try:
            d = store.load_daily(code)
        except Exception:
            continue
        if d is None or d.empty:
            continue
        hit = d[d["date"] == date]
        if hit.empty:
            continue
        row = hit.iloc[0].to_dict()
        try:
            up, down = limit_prices(code, date, row)
        except Exception:
            up, down = None, None
        bars[code] = {
            "open": float(row.get("open") or 0),
            "high": float(row.get("high") or 0),
            "low": float(row.get("low") or 0),
            "close": float(row.get("close") or 0),
            "volume": float(row.get("volume") or 0),
            "limit_up": up,
            "limit_down": down,
        }
    return bars


def _close_on(store, code, date):
    try:
        d = store.load_daily(code)
        if d is None or d.empty:
            return None
        hit = d[d["date"] == date]
        if hit.empty:
            return None
        return float(hit["close"].iloc[0])
    except Exception:
        return None


def run_paper(signal_file, data_dir, start=None, end=None, cash=350000.0,
              top_n=5, out_dir="out/paper", volume_limit=0.25,
              commission_rate=0.0003, min_commission=5.0,
              stamp_duty_rate=0.0005, transfer_fee_rate=0.00001) -> dict:
    os.environ["JQSHIM_DATA"] = str(Path(data_dir).resolve())
    from jqdata import store
    from jqdata.trade import _limit_prices

    signal = load_signal(signal_file)
    calendar = [str(x)[:10] for x in store.load_calendar()]
    if not calendar:
        raise RuntimeError("交易日历为空，请先抓取 calendar")
    if start:
        calendar = [d for d in calendar if d >= str(start)]
    if end:
        calendar = [d for d in calendar if d <= str(end)]
    if not calendar:
        raise ValueError("区间内没有交易日")
    by_date = {str(d): x for d, x in signal.groupby("date")}
    paper = broker.PaperBroker(broker.PaperConfig(
        initial_cash=float(cash), commission_rate=float(commission_rate),
        min_commission=float(min_commission), stamp_duty_rate=float(stamp_duty_rate),
        transfer_fee_rate=float(transfer_fee_rate),
        volume_limit=float(volume_limit)))
    equity_rows = []
    fills_all = []
    for date in calendar:
        symbols = set(p["symbol"] for p in paper.get_positions())
        symbols.update(by_date.get(date, pd.DataFrame()).get("code", []))
        symbols.update(o["symbol"] for o in paper.get_orders() if o["status"] == "pending")
        bars = _bar_map(store, _limit_prices, date, symbols)
        fills_all.extend(paper.process_bar(date, bars))
        acct = paper.get_account()
        equity_rows.append({"date": date, "cash": acct["available_cash"],
                            "market_value": acct["market_value"],
                            "total_value": acct["total_assets"],
                            "n_pos": acct["positions_count"]})
        rows = by_date.get(date)
        if rows is None or rows.empty:
            continue
        top = rows.head(max(1, int(top_n)))
        if top.empty:
            continue
        target_weight = 1.0 / len(top)
        current = {p["symbol"]: int(p["quantity"]) for p in paper.get_positions()}
        targets = set(top["code"])
        total_assets = float(acct["total_assets"])
        # 先卖不在目标中的持仓。
        for code, qty in current.items():
            if code not in targets and qty > 0:
                paper.submit_order(code, "sell", qty, order_date=date)
        for row in top.itertuples():
            code = str(row.code)
            px = _close_on(store, code, date)
            if px is None or px <= 0:
                continue
            target_qty = int((total_assets * target_weight / px) // 100 * 100)
            have = int(current.get(code, 0))
            delta = target_qty - have
            if delta > 0:
                paper.submit_order(code, "buy", delta, order_date=date)
            elif delta < 0:
                paper.submit_order(code, "sell", min(-delta, have), order_date=date)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    eq = pd.DataFrame(equity_rows)
    eq.to_csv(out / "paper_equity.csv", index=False)
    orders = pd.DataFrame(paper.get_orders())
    orders.to_csv(out / "paper_orders.csv", index=False)
    fills = pd.DataFrame(fills_all)
    if not fills.empty:
        fills.to_csv(out / "paper_fills.csv", index=False)
    else:
        pd.DataFrame(columns=["date", "code", "side", "price", "amount", "value", "fee"]).to_csv(
            out / "paper_fills.csv", index=False)
    if not fills.empty:
        for_metrics = pd.DataFrame({
            "date": fills["fill_date"].astype(str),
            "code": fills["symbol"].astype(str),
            "side": fills["side"].astype(str),
            "price": pd.to_numeric(fills["fill_price"], errors="coerce"),
            "amount": pd.to_numeric(fills["filled_quantity"], errors="coerce"),
            "value": pd.to_numeric(fills["value"], errors="coerce"),
            "fee": pd.to_numeric(fills["fee"], errors="coerce"),
        })
    else:
        for_metrics = pd.DataFrame(columns=["date", "code", "side", "price", "amount", "value", "fee"])
    m = analytics.compute_metrics(eq, for_metrics)
    summary = {
        "signal_file": str(signal_file),
        "start": str(calendar[0]), "end": str(calendar[-1]),
        "trading_days": len(calendar),
        "initial_cash": float(cash),
        "final_equity": float(eq["total_value"].iloc[-1]),
        "total_return": float(m["total_return"]),
        "max_drawdown": float(m["max_drawdown"]),
        "sharpe": float(m["sharpe"]),
        "orders": int(len(orders)),
        "filled_orders": int((orders["status"] == "filled").sum()) if not orders.empty else 0,
        "rejected_orders": int((orders["status"] == "rejected").sum()) if not orders.empty else 0,
        "pending_orders": int((orders["status"] == "pending").sum()) if not orders.empty else 0,
        "final_positions": len(paper.get_positions()),
        "fills": int(len(fills)),
    }
    (out / "paper_summary.json").write_text(
        json.dumps(analytics.json_safe(summary), ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"summary": summary, "out": str(out), "equity": eq,
            "orders": orders, "fills": fills}


def main(argv=None):
    ap = argparse.ArgumentParser(description="PaperBroker 信号回放")
    ap.add_argument("--signal", required=True)
    ap.add_argument("--data", default="data")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--cash", type=float, default=350000.0)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--out", default="out/paper")
    ap.add_argument("--volume-limit", type=float, default=0.25)
    ap.add_argument("--commission-rate", type=float, default=0.0003)
    ap.add_argument("--min-commission", type=float, default=5.0)
    ap.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    ap.add_argument("--transfer-fee-rate", type=float, default=0.00001)
    a = ap.parse_args(argv)
    result = run_paper(a.signal, a.data, a.start, a.end, a.cash, a.top_n,
                       a.out, a.volume_limit, a.commission_rate,
                       a.min_commission, a.stamp_duty_rate, a.transfer_fee_rate)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"  output: {result['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())