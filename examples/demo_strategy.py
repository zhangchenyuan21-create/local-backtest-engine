"""最小聚宽风格策略示例。

仅为演示本地引擎的 API 兼容性，不是投资建议。
"""

from jqdata import *


def initialize(context):
    g.security = "000001.XSHE"
    g.short = 5
    g.long = 20
    run_daily(rebalance, time="09:31")


def rebalance(context):
    bars = attribute_history(g.security, g.long + 1, "1d", ["close"], df=True)
    if bars is None or len(bars) < g.long + 1:
        return

    short_ma = bars["close"].iloc[-g.short:].mean()
    long_ma = bars["close"].iloc[-(g.long + 1):-1].mean()
    target = 1.0 if short_ma > long_ma else 0.0
    order_target_percent(g.security, target)
