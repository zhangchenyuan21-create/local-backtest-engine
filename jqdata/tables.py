"""finance / valuation / indicator 等表对象容器。

策略里有：
    from jqdata import finance
    q = query(finance.STK_MT_TOTAL).filter(...)
    mt = finance.run_query(q)

finance.STK_MT_TOTAL 是【融资融券全市场汇总】，本地数据源暂缺。
本模块提供对象占位 + 明确的降级行为（返回空表，策略自己会走 except 分支）。
"""
from __future__ import annotations

from .api import Query, _Field, _Order, _Table, query  # noqa: F401

# ---------------------------------------------------------------------------
# 已实现的表（valuation / indicator）由 api 提供
# ---------------------------------------------------------------------------
from .api import valuation, indicator, balance, income, cash_flow  # noqa: F401


# ---------------------------------------------------------------------------
# 融资融券（finance.*）
# ---------------------------------------------------------------------------
class _FinTable(_Table):
    """finance 命名空间下的表。"""

    def __init__(self, name, cols):
        super().__init__(name, cols)


STK_MT_TOTAL = _FinTable("STK_MT_TOTAL", {
    "date": "date", "exchange_code": "exchange_code",
    "fin_value": "fin_value", "fin_buy_value": "fin_buy_value",
    # ⚠️ 策略读的是 sec_value（融券余量金额）—— 见 margin_trading_series。
    #    第一版写成 fin_sec_value，导致 '_FinTable' object has no attribute
    #    'sec_value'，两融信号整体失效 -> 市场状态恒为「震荡」-> 熊市防御不触发。
    "sec_value": "sec_value",
    "fin_sec_value": "sec_value",
})
STK_MT_SSE = _FinTable("STK_MT_SSE", {
    "date": "date", "fin_value": "fin_value", "fin_buy_value": "fin_buy_value",
    "sec_value": "sec_value",
})
STK_MT_SZSE = _FinTable("STK_MT_SZSE", {
    "date": "date", "fin_value": "fin_value", "fin_buy_value": "fin_buy_value",
    "sec_value": "sec_value",
})


_META = {"缺失原因": "两融全市场汇总由 fetch_margin_em.py 从东财逐股聚合生成；"
                   "若 data/margin/market_margin.parquet 不存在则本表返回空。"}


# 两融市场汇总落盘位置（由 fetch_margin_em.py 生成）
_MT_CACHE = {"df": None, "mtime": None}


def _load_mt():
    """读两融市场汇总序列（data/margin/market_margin.parquet）。

    列：date, fin_value, fin_buy_value, sec_value（单位：元）
    """
    import os
    import pandas as pd
    from . import DATA_ROOT
    p = os.path.join(DATA_ROOT, "margin", "market_margin.parquet")
    if not os.path.exists(p):
        return None
    mt = os.path.getmtime(p)
    if _MT_CACHE["df"] is not None and _MT_CACHE["mtime"] == mt:
        return _MT_CACHE["df"]
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["date"] = df["date"].astype(str)
    # 策略按 date 分组聚合（因为它以为有沪深两行）——
    # 本地已是全市场汇总，这里补一个 exchange_code 以便 groupby 语义一致
    if "exchange_code" not in df.columns:
        df["exchange_code"] = "TOTAL"
    df = df.sort_values("date").reset_index(drop=True)
    _MT_CACHE["df"] = df
    _MT_CACHE["mtime"] = mt
    return df


def run_query(query_object):
    """执行 finance 表的查询。

    支持 STK_MT_TOTAL / STK_MT_SSE / STK_MT_SZSE 的
        date / fin_value / fin_buy_value / sec_value
    过滤条件（date <= X）与排序（date desc）与 limit。

    数据来源：data/margin/market_margin.parquet（东财逐股聚合，全市场按日）
    """
    import pandas as pd
    df = _load_mt()
    if df is None:
        return pd.DataFrame()
    try:
        tab = getattr(query_object, "_tables", None)
        names = [t.name for t in tab()] if callable(tab) else []
    except Exception:
        names = []
    if names and not any(n.startswith("STK_MT") for n in names):
        return pd.DataFrame()
    out = df
    # 应用过滤条件（策略只用到 date <= end_date）
    try:
        conds = getattr(query_object, "conds", []) or []
        for c in conds:
            f = getattr(c, "lhs", None)
            op = getattr(c, "op", None)
            rhs = getattr(c, "rhs", None)
            fname = getattr(f, "name", None)
            if fname in out.columns and op in ("<=", "<", ">=", ">", "=="):
                col = out[fname].astype(str) if fname == "date" else out[fname]
                r = str(rhs)[:10] if fname == "date" else rhs
                out = out[{"<=": col <= r, "<": col < r, ">=": col >= r,
                           ">": col > r, "==": col == r}[op]]
    except Exception:
        pass
    # 排序
    try:
        orders = getattr(query_object, "orders", []) or []
        for o in orders:
            fn = getattr(getattr(o, "field", None), "name", None)
            if fn in out.columns:
                out = out.sort_values(fn, ascending=not getattr(o, "desc", False))
    except Exception:
        out = out.sort_values("date")
    lim = getattr(query_object, "_limit", None)
    if lim:
        out = out.head(int(lim))
    return out.reset_index(drop=True)


def get_mt_data(*a, **kw):
    df = _load_mt()
    import pandas as pd
    return df if df is not None else pd.DataFrame()


# ---------------------------------------------------------------------------
# finance 命名空间
# ---------------------------------------------------------------------------
# 策略里写的是：
#     from jqdata import finance
#     q = query(finance.STK_MT_TOTAL).filter(...)
#     mt = finance.run_query(q)
# 所以 `finance` 必须是一个【独立命名空间对象】，且 `from jqdata import finance`
# 要能解析到它。用 types.SimpleNamespace 而不是子模块，保证 `import jqdata`
# 之后属性就在（不依赖惰性 __getattr__）。
# ---------------------------------------------------------------------------
import types as _types

finance = _types.SimpleNamespace(
    STK_MT_TOTAL=STK_MT_TOTAL,
    STK_MT_SSE=STK_MT_SSE,
    STK_MT_SZSE=STK_MT_SZSE,
    run_query=run_query,
    get_mt_data=get_mt_data,
    **{"_META": _META},
)

__all__ = ["STK_MT_TOTAL", "STK_MT_SSE", "STK_MT_SZSE", "run_query",
           "get_mt_data", "finance", "query", "Query", "valuation",
           "indicator", "balance", "income", "cash_flow", "_META"]
