"""聚宽配置函数（set_*）与定时任务（run_daily 等）。

这些函数在本地不真正"调度"，只记录配置与注册回调；
真正的驱动由 runner 按交易日循环完成。
"""
from __future__ import annotations

from . import CONTEXT

# 默认配置（聚宽默认值）
DEFAULTS = {
    "benchmark": "000300.XSHG",
    "use_real_price": True,
    "order_volume_ratio": 0.25,
    "match_with_volume_limit": True,
    "limit_mode": "open",
    "avoid_future_data": True,
    "slippage": None,
    "order_cost": {"stock": {"close_today_commission": 0.0}},
}


def _ctx():
    return CONTEXT


def _cfgstore():
    c = _ctx()
    if c is None:
        return {}
    if not hasattr(c, "jq_options"):
        c.jq_options = dict(DEFAULTS)
    return c.jq_options


# ═══════════════════════════════════════════════════════════════════════════
# 聚宽的配置【类】—— 策略里以构造器形式使用
# ═══════════════════════════════════════════════════════════════════════════
class OrderCost:
    """聚宽费率对象。

    策略用法：
        set_order_cost(
            OrderCost(close_tax=0.0005, open_commission=0.0003,
                      close_commission=0.0003, min_commission=5),
            type="stock")
    字段含义：
        close_tax         印花税（仅卖出）
        open_commission   买入佣金
        close_commission  卖出佣金
        min_commission    最低佣金（元）
    """

    def __init__(self, close_tax=0.001, open_commission=0.0003,
                 close_commission=0.0003, min_commission=5.0, **kw):
        self.close_tax = float(close_tax)
        self.open_commission = float(open_commission)
        self.close_commission = float(close_commission)
        self.min_commission = float(min_commission)
        for k, v in kw.items():
            setattr(self, k, v)

    def as_dict(self):
        return {"close_tax": self.close_tax,
                "open_commission": self.open_commission,
                "close_commission": self.close_commission,
                "min_commission": self.min_commission}

    def __repr__(self):
        return (f"<OrderCost tax={self.close_tax} open={self.open_commission} "
                f"close={self.close_commission} min={self.min_commission}>")


class FixedSlippage:
    """固定比例滑点。聚宽用法：FixedSlippage(0.02) —— 注意是【比例】。

    ⚠️ 策略里写的是 FixedSlippage(0.02)，即 2% —— 非常大的滑点。
       我们的撮合引擎按 value 逐笔加减，必须读准它。
    """

    def __init__(self, value=0.0, **kw):
        self.value = float(value)

    def __repr__(self):
        return f"<FixedSlippage {self.value}>"


class PriceRelatedSlippage:
    def __init__(self, value=0.0, **kw):
        self.value = float(value)


class StepRelatedSlippage:
    def __init__(self, value=0.0, **kw):
        self.value = float(value)


def set_benchmark(code):
    _cfgstore()["benchmark"] = code


def set_option(name, value):
    _cfgstore()[name] = value


def set_order_cost(cost, type="stock", ref=None):
    """cost 可以是 OrderCost 对象或 dict。"""
    st = _cfgstore()
    oc = st.setdefault("order_cost", {})
    if isinstance(cost, OrderCost):
        oc[type] = cost.as_dict()
    elif isinstance(cost, dict):
        oc[type] = dict(cost)
    else:
        oc[type] = cost


def set_slippage(slip, type=None, ref=None):
    st = _cfgstore()
    st["slippage"] = {"obj": slip, "type": type,
                      "value": float(getattr(slip, "value", 0.0) or 0.0)}


def set_universe(securities):
    _cfgstore()["universe"] = list(securities)


def set_commission(commission=None, **kw):
    _cfgstore()["commission"] = commission or kw


def get_option(name, default=None):
    return _cfgstore().get(name, default)


# ═══════════════════════════════════════════════════════════════════════════
# 定时任务：注册到 runner
# ═══════════════════════════════════════════════════════════════════════════
def _register(freq, func, time_rule, reference_security, extra):
    c = _ctx()
    if c is None:
        raise RuntimeError("run_daily: CONTEXT 未注入（应由 runner 设置 jqdata.CONTEXT）")
    if not hasattr(c, "jobs"):
        c.jobs = []
    c.jobs.append({"freq": freq, "func": func, "time": str(time_rule),
                   "ref": reference_security, "extra": extra})
    return {"freq": freq, "func": func}


# ⚠️ 策略调用的是 `run_daily(func, time="09:00")` —— 参数名必须是 `time`，
#    不是聚宽文档里的 `time_rule`。为兼容两种写法，两个都接受。
def run_daily(func, time=None, time_rule=None, reference_security="000300.XSHG",
              **kw):
    return _register("daily", func, time or time_rule or "09:30",
                     reference_security, None)


def run_weekly(func, date_rule=None, time=None, time_rule=None,
               reference_security="000300.XSHG", **kw):
    return _register("weekly", func, time or time_rule or "09:30",
                     reference_security, date_rule)


def run_monthly(func, monthday=None, time=None, time_rule=None,
                reference_security="000300.XSHG", **kw):
    return _register("monthly", func, time or time_rule or "09:30",
                     reference_security, monthday)


# ═══════════════════════════════════════════════════════════════════════════
# record：聚宽用于画曲线，本地只收集
# ═══════════════════════════════════════════════════════════════════════════
def record(**kw):
    c = _ctx()
    if c is None:
        return
    if not hasattr(c, "records"):
        c.records = {}
    for k, v in kw.items():
        c.records.setdefault(k, []).append(v)
