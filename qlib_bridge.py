"""Qlib 数据导出与预测信号导入。

导出的是 Qlib 友好的 CSV bundle（calendars/instruments/features）；
Qlib 实际训练前可按其 `dump_bin.py` 转成二进制格式。本模块不 import qlib，
因此默认安装和当前回测不受影响。

用法：
    python -m qlib_bridge export --data data --out out/qlib
    python -m qlib_bridge import-signal --input qlib_pred.csv --out out/ml/signal.csv
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def to_qlib_instrument(code: str) -> str:
    s = str(code).strip().upper()
    if s.endswith(".XSHG"):
        return "SH" + s.split(".")[0]
    if s.endswith(".XSHE"):
        return "SZ" + s.split(".")[0]
    if s.endswith(".BJSE"):
        return "BJ" + s.split(".")[0]
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", s):
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 6:
        raise ValueError(f"无法识别的证券代码: {code!r}")
    if digits.startswith("6"):
        return "SH" + digits
    if digits.startswith(("0", "3")):
        return "SZ" + digits
    if digits.startswith(("4", "8")):
        return "BJ" + digits
    return "SZ" + digits


def from_qlib_instrument(value: str) -> str:
    s = str(value).strip().upper()
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)
    if m:
        prefix, digits = m.groups()
        return digits + {"SH": ".XSHG", "SZ": ".XSHE", "BJ": ".BJSE"}[prefix]
    if re.fullmatch(r"\d{6}\.(XSHG|XSHE|BJSE)", s):
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 6:
        if digits.startswith("6"):
            return digits + ".XSHG"
        if digits.startswith(("0", "3")):
            return digits + ".XSHE"
        if digits.startswith(("4", "8")):
            return digits + ".BJSE"
    raise ValueError(f"无法识别的 Qlib 证券代码: {value!r}")


def export_qlib(data_dir, out_dir, start=None, end=None) -> dict:
    root = Path(data_dir)
    src = root / "daily_qfq" if (root / "daily_qfq").is_dir() else root / "daily"
    files = sorted(src.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"{src} 下没有 parquet 日线文件")
    out = Path(out_dir)
    (out / "calendars").mkdir(parents=True, exist_ok=True)
    (out / "instruments").mkdir(parents=True, exist_ok=True)
    (out / "features").mkdir(parents=True, exist_ok=True)
    all_dates = set()
    instruments = []
    for p in files:
        df = pd.read_parquet(p)
        need = {"date", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            continue
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        if start:
            df = df[df["date"] >= str(start)]
        if end:
            df = df[df["date"] <= str(end)]
        if df.empty:
            continue
        inst = to_qlib_instrument(p.stem.replace("_", ".", 1))
        df = df.sort_values("date")
        df["factor"] = 1.0
        df[["date", "open", "high", "low", "close", "volume", "factor"]].to_csv(
            out / "features" / f"{inst}.csv", index=False)
        instruments.append((inst, df["date"].iloc[0], df["date"].iloc[-1]))
        all_dates.update(df["date"].tolist())
    if not instruments:
        raise ValueError("没有可导出的日线数据")
    dates = sorted(all_dates)
    (out / "calendars" / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    (out / "instruments" / "all.txt").write_text(
        "\n".join(f"{i}\t{s}\t{e}" for i, s, e in instruments) + "\n", encoding="utf-8")
    summary = {"source": str(src), "out": str(out), "instruments": len(instruments),
               "dates": len(dates), "start": dates[0], "end": dates[-1]}
    (out / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "README.md").write_text(
        "# Qlib CSV export\n\n"
        "This is a Qlib-friendly CSV bundle: `calendars/day.txt`, "
        "`instruments/all.txt` and `features/<INSTRUMENT>.csv`.\n\n"
        "Qlib may still require its `dump_bin.py` step to produce binary data. "
        "No Qlib dependency is imported by the exporter.\n", encoding="utf-8")
    return summary


def import_signal(input_csv, out_csv) -> pd.DataFrame:
    df = pd.read_csv(input_csv)
    date_col = next((c for c in ("date", "trade_date", "datetime") if c in df.columns), None)
    code_col = next((c for c in ("instrument", "code", "symbol", "asset") if c in df.columns), None)
    score_col = next((c for c in ("score", "pred", "prediction", "return", "value") if c in df.columns), None)
    if not date_col or not code_col or not score_col:
        raise ValueError("signals 需要 date/instrument/score 或等价字段")
    out = pd.DataFrame({
        "date": df[date_col].astype(str).str[:10],
        "code": df[code_col].map(from_qlib_instrument),
        "score": pd.to_numeric(df[score_col], errors="coerce"),
    }).dropna(subset=["score"]).sort_values(["date", "score"], ascending=[True, False])
    out_path = Path(out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Qlib CSV 导出 / 预测信号导入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--data", default="data")
    ex.add_argument("--out", default="out/qlib")
    ex.add_argument("--start", default=None)
    ex.add_argument("--end", default=None)
    im = sub.add_parser("import-signal")
    im.add_argument("--input", required=True)
    im.add_argument("--out", default="out/ml/signal.csv")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(json.dumps(export_qlib(a.data, a.out, a.start, a.end),
                         ensure_ascii=False, indent=2))
    else:
        df = import_signal(a.input, a.out)
        print(f"  rows: {len(df)}  output: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())