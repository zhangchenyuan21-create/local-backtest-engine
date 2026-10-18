"""使用 walk-forward ML 信号的最小策略示例。

信号文件由 `python -m ml ...` 生成，格式为 date,code,score。
本策略每天选择 score 最高的 top_n 只股票，等权持有；如果没有信号则空仓。

示例：
    $env:JQBT_SIGNAL_FILE = "out/ml/signal.csv"
    python runner_main.py run --strategy examples/signal_strategy.py --out out/signal --force
"""
from __future__ import annotations

import os

import pandas as pd

from jqdata import *


def initialize(context):
    set_option("order_volume_ratio", 0.25)
    g.top_n = max(1, int(os.environ.get("JQBT_TOP_N", "5")))
    g.signal_file = os.environ.get("JQBT_SIGNAL_FILE", "out/ml/signal.csv")
    g.by_date = {}
    try:
        df = pd.read_csv(g.signal_file, dtype={"code": str})
        if not df.empty:
            df["date"] = df["date"].astype(str)
            df["code"] = df["code"].astype(str)
            g.by_date = {str(d): x for d, x in df.groupby("date")}
    except Exception:
        # 没有信号文件时保持空仓，便于先验证数据/回测链路。
        g.by_date = {}
    run_daily(rebalance, time="09:31")


def rebalance(context):
    rows = g.by_date.get(str(context.current_date))
    if rows is None or rows.empty:
        return
    top = rows.sort_values("score", ascending=False).head(g.top_n)
    if top.empty:
        return
    targets = {str(r.code): 1.0 / len(top) for r in top.itertuples()}
    for code in list(context.portfolio.positions):
        if code not in targets:
            order_target_percent(code, 0.0)
    for code, weight in targets.items():
        order_target_percent(code, weight)