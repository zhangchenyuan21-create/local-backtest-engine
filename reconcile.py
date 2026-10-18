"""PaperBroker 与同花顺模拟盘订单/成交对账。

只读取文件和只读 broker 查询，不会提交任何委托。

用法：
    python -m reconcile --paper out/paper --ths-orders ths_orders.csv ^
      --ths-fills ths_fills.csv --out out/reconcile
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _norm_symbol(value) -> str:
    s = str(value or "").strip().upper()
    if s.endswith((".XSHG", ".XSHE", ".BJSE", ".SH", ".SZ", ".BJ")):
        s = s.split(".", 1)[0]
    elif s.startswith(("SH", "SZ", "BJ")) and len(s) > 6:
        s = s[2:]
    return "".join(ch for ch in s if ch.isdigit()).zfill(6) if s else ""


def _norm_date(value) -> str:
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    s2 = str(value or "")[:10]
    return s2 if len(s2) == 10 else ""


def _norm_side(value) -> str:
    s = str(value or "").strip().lower()
    if s in ("buy", "b", "买", "买入", "cmd_wt_mairu"):
        return "buy"
    if s in ("sell", "s", "卖", "卖出", "cmd_wt_maichu"):
        return "sell"
    return s


def _norm_status(value) -> str:
    s = str(value or "").strip().lower()
    if any(x in s for x in ("filled", "全部成交", "已成", "成交")):
        return "filled"
    if any(x in s for x in ("reject", "废单", "拒绝", "失败")):
        return "rejected"
    if any(x in s for x in ("cancel", "已撤", "撤单")):
        return "cancelled"
    if any(x in s for x in ("pending", "未成", "待成", "部成", "partial")):
        return "pending"
    return s


def _pick(df, names, default=None):
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([default] * len(df), index=df.index)


def normalize_orders(df: pd.DataFrame, source: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "order_id": _pick(df, ["order_id", "htbh", "id", "委托编号", "合同编号"]).astype(str),
        "order_date": _pick(df, ["order_date", "date", "wtrq", "委托日期", "日期"]).map(_norm_date),
        "symbol": _pick(df, ["symbol", "stock_code", "code", "证券代码", "股票代码"]).map(_norm_symbol),
        "side": _pick(df, ["side", "direction", "买卖", "方向"]).map(_norm_side),
        "quantity": pd.to_numeric(_pick(df, ["quantity", "amount", "委托数量", "数量"]), errors="coerce").fillna(0).astype(int),
        "filled_quantity": pd.to_numeric(_pick(df, ["filled_quantity", "filled", "成交数量", "已成交数量"], 0), errors="coerce").fillna(0).astype(int),
        "price": pd.to_numeric(_pick(df, ["price", "fill_price", "委托价格", "价格"]), errors="coerce").fillna(0.0),
        "status": _pick(df, ["status", "state", "状态", "委托状态"]).map(_norm_status),
        "fee": pd.to_numeric(_pick(df, ["fee", "费用", "手续费"], 0), errors="coerce").fillna(0.0),
    })
    out["source"] = source
    return out


def normalize_fills(df: pd.DataFrame, source: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "date": _pick(df, ["date", "trade_date", "fill_date", "成交日期", "日期"]).map(_norm_date),
        "symbol": _pick(df, ["symbol", "stock_code", "code", "证券代码", "股票代码"]).map(_norm_symbol),
        "side": _pick(df, ["side", "direction", "买卖", "方向"]).map(_norm_side),
        "amount": pd.to_numeric(_pick(df, ["amount", "filled_quantity", "quantity", "成交数量", "数量"]), errors="coerce").fillna(0).astype(int),
        "price": pd.to_numeric(_pick(df, ["price", "fill_price", "成交价格", "价格"]), errors="coerce").fillna(0.0),
        "value": pd.to_numeric(_pick(df, ["value", "amount_value", "成交金额", "金额"], 0), errors="coerce").fillna(0.0),
        "fee": pd.to_numeric(_pick(df, ["fee", "费用", "手续费"], 0), errors="coerce").fillna(0.0),
    })
    out["source"] = source
    return out


def _key(row):
    return (row["order_date"], row["symbol"], row["side"], int(row["quantity"]))


def compare_orders(paper: pd.DataFrame, ths: pd.DataFrame) -> dict:
    pgroups = defaultdict(list)
    tgroups = defaultdict(list)
    for _, r in paper.iterrows():
        pgroups[_key(r)].append(r)
    for _, r in ths.iterrows():
        tgroups[_key(r)].append(r)
    details, matched = [], 0
    for key in sorted(set(pgroups) | set(tgroups)):
        ps, ts = pgroups.get(key, []), tgroups.get(key, [])
        for i in range(max(len(ps), len(ts))):
            p = ps[i] if i < len(ps) else None
            t = ts[i] if i < len(ts) else None
            if p is None:
                details.append({"type": "missing_in_paper", "key": key,
                                "ths_order": t["order_id"]})
            elif t is None:
                details.append({"type": "missing_in_ths", "key": key,
                                "paper_order": p["order_id"]})
            else:
                matched += 1
                if p["status"] != t["status"]:
                    details.append({"type": "status_mismatch", "key": key,
                                    "paper": p["status"], "ths": t["status"]})
                if int(p["filled_quantity"]) != int(t["filled_quantity"]):
                    details.append({"type": "fill_quantity_mismatch", "key": key,
                                    "paper": int(p["filled_quantity"]),
                                    "ths": int(t["filled_quantity"])})
                if abs(float(p["price"]) - float(t["price"])) > 0.011:
                    details.append({"type": "price_mismatch", "key": key,
                                    "paper": float(p["price"]), "ths": float(t["price"])})
    return {
        "paper_orders": int(len(paper)), "ths_orders": int(len(ths)),
        "matched": matched,
        "missing_in_ths": int(sum(1 for d in details if d["type"] == "missing_in_ths")),
        "missing_in_paper": int(sum(1 for d in details if d["type"] == "missing_in_paper")),
        "issues": len(details), "details": details,
    }


def compare_fills(paper: pd.DataFrame, ths: pd.DataFrame) -> dict:
    def agg(df):
        if df.empty:
            return {}
        g = df.groupby(["date", "symbol", "side"], dropna=False)
        return {k: {"amount": int(v["amount"].sum()), "value": float(v["value"].sum()),
                    "fee": float(v["fee"].sum()), "count": int(len(v))}
                for k, v in g}
    pa, ta = agg(paper), agg(ths)
    details = []
    for key in sorted(set(pa) | set(ta)):
        p, t = pa.get(key), ta.get(key)
        if p is None:
            details.append({"type": "fill_missing_in_paper", "key": key, "ths": t})
        elif t is None:
            details.append({"type": "fill_missing_in_ths", "key": key, "paper": p})
        else:
            for field in ("amount", "value", "fee", "count"):
                if abs(float(p[field]) - float(t[field])) > (1e-6 if field != "amount" else 0):
                    details.append({"type": f"fill_{field}_mismatch", "key": key,
                                    "paper": p[field], "ths": t[field]})
    return {"paper_fills": int(len(paper)), "ths_fills": int(len(ths)),
            "matched_groups": len(set(pa) & set(ta)), "issues": len(details),
            "details": details}


def build_report(paper_dir, ths_orders_path, ths_fills_path=None, out_dir="out/reconcile") -> dict:
    paper_dir = Path(paper_dir)
    paper_orders = normalize_orders(pd.read_csv(paper_dir / "paper_orders.csv"), "paper")
    ths_orders = normalize_orders(pd.read_csv(ths_orders_path), "ths")
    order_cmp = compare_orders(paper_orders, ths_orders)
    fill_cmp = None
    if ths_fills_path:
        paper_fills = normalize_fills(pd.read_csv(paper_dir / "paper_fills.csv"), "paper")
        ths_fills = normalize_fills(pd.read_csv(ths_fills_path), "ths")
        fill_cmp = compare_fills(paper_fills, ths_fills)
    report = {"orders": order_cmp, "fills": fill_cmp,
              "paper_orders_file": str(paper_dir / "paper_orders.csv"),
              "ths_orders_file": str(ths_orders_path)}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reconciliation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# Paper / THS Reconciliation", "",
             f"- paper orders: {order_cmp['paper_orders']}",
             f"- ths orders: {order_cmp['ths_orders']}",
             f"- matched: {order_cmp['matched']}",
             f"- missing in ths: {order_cmp['missing_in_ths']}",
             f"- missing in paper: {order_cmp['missing_in_paper']}",
             f"- order issues: {order_cmp['issues']}"]
    if fill_cmp:
        lines += ["", f"- paper fills: {fill_cmp['paper_fills']}",
                  f"- ths fills: {fill_cmp['ths_fills']}",
                  f"- fill groups matched: {fill_cmp['matched_groups']}",
                  f"- fill issues: {fill_cmp['issues']}"]
    (out / "reconciliation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper/同花顺订单成交对账（只读）")
    ap.add_argument("--paper", required=True, help="含 paper_orders.csv/paper_fills.csv 的目录")
    ap.add_argument("--ths-orders", required=True, help="同花顺订单 CSV")
    ap.add_argument("--ths-fills", default=None, help="同花顺成交 CSV（可选）")
    ap.add_argument("--out", default="out/reconcile")
    a = ap.parse_args(argv)
    report = build_report(a.paper, a.ths_orders, a.ths_fills, a.out)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"  report: {Path(a.out) / 'reconciliation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())