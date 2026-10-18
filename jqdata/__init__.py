"""jqdata 替身模块 —— 让聚宽策略代码一字不改地跑在本地。

═══════════════════════════════════════════════════════════════════════════════
设计目标
═══════════════════════════════════════════════════════════════════════════════
策略里只有两行聚宽依赖：
    L49: from jqdata import *
    L50: from jqdata import finance

本包用同名 `jqdata` 出现在 sys.path 前面，即可无缝替换。

═══════════════════════════════════════════════════════════════════════════════
数据源（改法 C：混合）
═══════════════════════════════════════════════════════════════════════════════
  · 聚宽 Data API（HTTPS 443，绕开被封的 7000 端口）
      get_price / get_fundamentals / get_all_securities
      get_index_stocks / get_trade_days / get_industry
  · 新浪资金流（聚宽 Data API 无资金流接口）
      MoneyFlow.ssl_qsfx_zjlrqs
  · 自实现：撮合引擎 / set_* / avoid_future_data 截断

═══════════════════════════════════════════════════════════════════════════════
规格来源（静态提取自 joinquant_strategy_AB_F.py，逐处核对）
═══════════════════════════════════════════════════════════════════════════════
  attribute_history(code, 1, "1d", ["close"])                    -> DataFrame
  attribute_history(code, n, "1d", ["open","close","high","low","volume"])
  get_price(sample, end_date=, count=6, frequency="daily",
            fields=["close"], panel=False)                       -> 长表 DataFrame
      ★ 必须含 "code" 列（代码里 px.groupby("code")）
      ★ 必须含 "time" 列（代码里 grp.sort_values("time") if "time" in grp.columns）
  get_all_securities(types=["stock"], date=).index               -> 代码列表
  get_index_stocks(idx, date=)                                   -> 代码列表
  get_trade_days(end_date=d, count=1)                            -> 日期序列
  get_money_flow(part, end_date=, count=, fields=[...])          -> DataFrame
  get_fundamentals(query(...), date=)                            -> DataFrame

  返回的代码格式必须是 000001.XSHE / 600000.XSHG（策略里按 .XSHE/.XSHG 判断）
      —— 见 check_exit_signals 里 str(code).startswith("300") 与 txc() 映射
"""
from __future__ import annotations

import datetime as dt
import os
import sys

__version__ = "0.5.0"

# 本地数据根目录
DATA_ROOT = os.environ.get(
    "JQSHIM_DATA",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
)

# 回测运行期上下文（由 runner 注入）
CONTEXT = None


def _ensure_dirs():
    for sub in ("daily", "financial", "index", "moneyflow", "meta"):
        os.makedirs(os.path.join(DATA_ROOT, sub), exist_ok=True)


_ensure_dirs()


# ═══════════════════════════════════════════════════════════════════════════
# 代码格式转换
# ═══════════════════════════════════════════════════════════════════════════
def normalize_code(code: str) -> str:
    """任意格式 -> 聚宽格式 000001.XSHE / 600000.XSHG。

    接受：000001.XSHE / sz000001 / sz.000001 / 000001
    """
    if code is None:
        return code
    c = str(code).strip().upper()
    if c.endswith(".XSHE") or c.endswith(".XSHG"):
        return c
    # sz.000001 / sh.600000
    if "." in c:
        pre, num = c.split(".", 1)
        pre = pre.lower()
        if pre in ("sz", "sh", "bj"):
            return num + (".XSHE" if pre == "sz" else
                          ".XSHG" if pre == "sh" else ".BJSE")
    # sz000001
    if len(c) > 6 and c[:2].lower() in ("sz", "sh", "bj"):
        pre, num = c[:2].lower(), c[2:]
        return num + (".XSHE" if pre == "sz" else
                      ".XSHG" if pre == "sh" else ".BJSE")
    # 纯数字：按 A 股规则推断交易所
    if c.isdigit() and len(c) == 6:
        if c[0] == "6":
            return c + ".XSHG"
        if c[0] in ("0", "3"):
            return c + ".XSHE"
        if c[0] in ("4", "8"):
            return c + ".BJSE"
    return code


def to_sina_code(code: str) -> str:
    """聚宽格式 -> 新浪格式 sh600000"""
    c = normalize_code(code)
    num, _, ex = c.partition(".")
    return ("sh" if ex == "XSHG" else "sz" if ex == "XSHE" else "bj") + num


def to_baostock_code(code: str) -> str:
    """聚宽格式 -> baostock 格式 sh.600000"""
    c = normalize_code(code)
    num, _, ex = c.partition(".")
    return ("sh." if ex == "XSHG" else "sz." if ex == "XSHE" else "bj.") + num


# ═══════════════════════════════════════════════════════════════════════════
# 前视偏差防护（对应聚宽 avoid_future_data=True）
# ═══════════════════════════════════════════════════════════════════════════
class LookAheadError(RuntimeError):
    pass


def guard_date(d, what: str = "data"):
    """确保请求的数据不晚于"当前回测日"。

    聚宽 avoid_future_data=True 的语义：盘中（09:31）只能看到 T-1 及更早。
    本地若不模拟，会系统性高估收益。

    ⚠️ 但注意：策略内部自己会传 prev_trade_date(...) 做 T-1 处理，
       所以这里【只在 runner 显式开启严格模式时】才拦截，
       否则会与策略自身的 T-1 逻辑重复。
    """
    if CONTEXT is None or not getattr(CONTEXT, "strict_no_future", False):
        return
    cur = getattr(CONTEXT, "current_date", None)
    if cur is None or d is None:
        return
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    elif isinstance(d, dt.datetime):
        d = d.date()
    if d > cur:
        raise LookAheadError(
            f"{what}: 请求日期 {d} 晚于当前回测日 {cur}（avoid_future_data）")


# ═══════════════════════════════════════════════════════════════════════════
# 子模块（延迟导入，避免一次性拉起全部依赖）
# ═══════════════════════════════════════════════════════════════════════════
def __getattr__(name):
    """惰性暴露子模块与 finance 命名空间。"""
    if name == "finance":
        from . import tables
        return tables.finance
    if name in ("valuation", "indicator", "balance", "income", "cash_flow"):
        from . import tables
        return getattr(tables, name)
    raise AttributeError(f"module 'jqdata' has no attribute {name!r}")


# 导出策略会用的全部符号
from .api import (  # noqa: E402
    attribute_history, get_price, get_all_securities, get_index_stocks,
    get_trade_days, get_money_flow, get_fundamentals, get_industry,
    get_security_info, get_extras, get_valuation, get_current_data,
    get_last_price, query,
)
from .sector import (  # noqa: E402
    get_industries, get_concepts, get_industry_stocks, get_concept_stocks,
)
from .trade import (  # noqa: E402
    order, order_value, order_target, order_target_value, order_target_percent,
    cancel_order, get_position, get_orders, get_open_orders,
)
from .setup import (  # noqa: E402
    set_benchmark, set_option, set_order_cost, set_slippage, set_universe,
    set_commission, run_daily, run_weekly, run_monthly, record,
    OrderCost, FixedSlippage, PriceRelatedSlippage, StepRelatedSlippage,
    get_option,
)
from . import jqlog as log  # noqa: E402
# ⚠️ finance 必须在模块加载时就绑定为属性 —— 策略写的是
#    `from jqdata import finance`，这是【属性查找】而非惰性 __getattr__ 路径。
from . import tables as _tables  # noqa: E402
finance = _tables.finance  # noqa: E402
valuation = _tables.valuation  # noqa: E402
indicator = _tables.indicator  # noqa: E402

__all__ = [
    "attribute_history", "get_price", "get_all_securities", "get_index_stocks",
    "get_trade_days", "get_money_flow", "get_fundamentals", "get_industry",
    "get_security_info", "get_extras", "get_valuation", "get_current_data",
    "get_last_price", "query",
    "get_industries", "get_concepts", "get_industry_stocks",
    "get_concept_stocks",
    "order", "order_value", "order_target", "order_target_value",
    "order_target_percent", "cancel_order", "get_position", "get_orders",
    "get_open_orders",
    "set_benchmark", "set_option", "set_order_cost", "set_slippage",
    "set_universe", "set_commission", "run_daily", "run_weekly", "run_monthly",
    "record", "OrderCost", "FixedSlippage", "PriceRelatedSlippage",
    "StepRelatedSlippage", "get_option",
    "log", "finance", "valuation", "indicator",
    "normalize_code", "to_sina_code", "to_baostock_code", "guard_date",
    "LookAheadError", "DATA_ROOT",
]
