"""聚宽 API 兼容层 —— 严格对齐策略的实际调用方式。

═══════════════════════════════════════════════════════════════════════════
规格（静态提取自 joinquant_strategy_AB_F.py，共 17 处调用）
═══════════════════════════════════════════════════════════════════════════
① attribute_history(code, 1, "1d", ["close"])
       -> DataFrame，列 = 字段，行 = 时间升序
       用法：bars["close"], bars["high"], bars["low"], bars["open"], bars["volume"]
            close.rolling(20).mean(), close.iloc[-1], close.tail(k)
            bars["high"].iloc[-1]
② get_price(stocks, end_date=, count=2, frequency="daily",
             fields=[...], panel=False)
       -> ★ 长表 DataFrame，必须含 "code" 列与 "time" 列
       用法：for code, grp in px.groupby("code"):
                 grp = grp.sort_values("time") if "time" in grp.columns else grp
                 grp["close"].iloc[-2] / .iloc[-1] / .tail(n).mean()
             pd.isna(px).all().all()
③ get_all_securities(types=["stock"], date=)   -> DataFrame，index = 代码
       用法：list(...index)
④ get_index_stocks(idx, date=)                 -> 代码列表
       用法：list(get_index_stocks(...))
⑤ get_trade_days(end_date=d, count=1)          -> 日期序列
       用法：arr[-1], arr[-2]
⑥ get_money_flow(part, end_date=, count=, fields=[...])
       -> DataFrame，列 = fields
       用法：df["date"], df["net_amount_main"], df.empty
⑦ get_fundamentals(query(...), date=)          -> DataFrame
       用法：r["code"], r.get("roe"), r.get("inc_revenue_year_on_year"),
            r.get("inc_net_profit_year_on_year"), r.get("pe_ratio"),
            r.get("pb_ratio"), r.get("market_cap")   # market_cap 单位=亿元
"""
from __future__ import annotations

import bisect
import datetime as dt
import os
from typing import Iterable

import numpy as np
import pandas as pd

from . import DATA_ROOT, CONTEXT, normalize_code, guard_date, store

try:
    from .fastcore import config as _fc_config
except Exception:  # pragma: no cover
    _fc_config = None

# ═══════════════════════════════════════════════════════════════════════════
# 估值/财务表定义（供 query 对象使用）
# ═══════════════════════════════════════════════════════════════════════════
VALUATION_COLS = {
    "code": "code",
    "pe_ratio": "peTTM",
    "pb_ratio": "pbMRQ",
    "ps_ratio": "psTTM",
    "pcf_ratio": "pcfNcfTTM",
    "market_cap": "_market_cap",
    "circulating_market_cap": "_circ_mcap",
    "turnover_ratio": "turn",
}
INDICATOR_COLS = {
    "code": "code",
    "roe": "_roe",
    "inc_revenue_year_on_year": "_yoy_rev",
    "inc_net_profit_year_on_year": "_yoy_ni",
    "net_profit_margin": "_np_margin",
    "gross_profit_margin": "_gp_margin",
    "inc_total_revenue_year_on_year": "_yoy_rev_total",
}
# ⚠️ 踩过的坑：曾把 inc_total_revenue_year_on_year 也映射到 "_yoy_rev"，
#    导致 rename 表里 {"_yoy_rev": "inc_revenue_year_on_year"} 被
#    {"_yoy_rev": "inc_total_revenue_year_on_year"} 覆盖，
#    策略读 inc_revenue_year_on_year 恒为 NaN -> 业绩层与聚宽完全不同。
#    两个公开字段必须各自有独立的内部列。


class _Field:
    """表字段引用，如 valuation.code。"""

    __slots__ = ("table", "name")

    def __init__(self, table, name):
        self.table = table
        self.name = name

    def __repr__(self):
        return f"{self.table}.{self.name}"

    def __eq__(self, other):
        return _Cmp("==", self, other)

    def __ne__(self, other):
        return _Cmp("!=", self, other)

    def __lt__(self, other):
        return _Cmp("<", self, other)

    def __le__(self, other):
        return _Cmp("<=", self, other)

    def __gt__(self, other):
        return _Cmp(">", self, other)

    def __ge__(self, other):
        return _Cmp(">=", self, other)

    def in_(self, values):
        return _Cmp("in", self, list(values))

    def notin_(self, values):
        return _Cmp("notin", self, list(values))

    def between(self, a, b):
        return _Cmp("between", self, (a, b))

    def desc(self):
        return _Order(self, True)

    def asc(self):
        return _Order(self, False)


class _Cmp:
    __slots__ = ("op", "lhs", "rhs")

    def __init__(self, op, lhs, rhs):
        self.op, self.lhs, self.rhs = op, lhs, rhs

    def __and__(self, other):
        return _Bool("and", [self, other])

    def __or__(self, other):
        return _Bool("or", [self, other])

    def __invert__(self):
        return _Bool("not", [self])


class _Bool:
    __slots__ = ("op", "items")

    def __init__(self, op, items):
        self.op, self.items = op, items

    def __and__(self, other):
        return _Bool("and", [self, other])

    def __or__(self, other):
        return _Bool("or", [self, other])

    def __invert__(self):
        return _Bool("not", [self])


class _Order:
    __slots__ = ("field", "desc")

    def __init__(self, field, desc):
        self.field, self.desc = field, desc


class _Table:
    """valuation / indicator 等表对象。"""

    def __init__(self, name, cols):
        self._name = name
        self._cols = cols
        for cname in cols:
            setattr(self, cname, _Field(name, cname))

    def __repr__(self):
        return f"<table {self._name}>"


valuation = _Table("valuation", VALUATION_COLS)
indicator = _Table("indicator", INDICATOR_COLS)
balance = _Table("balance", {"code": "code"})
income = _Table("income", {"code": "code"})
cash_flow = _Table("cash_flow", {"code": "code"})


class Query:
    """`query(...).filter(...).order_by(...).limit(n)` 链式对象。"""

    def __init__(self, *fields):
        self.fields = [f for f in fields if isinstance(f, _Field)]
        self.conds = []
        self.orders = []
        self._limit = None

    def filter(self, *conds):
        for c in conds:
            if isinstance(c, (_Cmp, _Bool)):
                self.conds.append(c)
        return self

    def order_by(self, *orders):
        self.orders.extend(o for o in orders if isinstance(o, _Order))
        return self

    def limit(self, n):
        self._limit = int(n)
        return self

    # ---- 求值 ----
    def _tables(self):
        return {f.table for f in self.fields} | {
            c.lhs.table for c in self.conds if isinstance(c, _Cmp)}

    def eval(self, df_map: dict) -> pd.DataFrame:
        """df_map: {表名: DataFrame}。返回合并后的结果。

        ★ 返回的列名必须是【query 里的字段名】（如 market_cap），
          而不是内部存储列名（如 _market_cap）—— 策略里写的是
              r.get("market_cap") / r.get("pe_ratio") / r.get("roe")
        """
        base = None
        for t in sorted(self._tables()):
            df = df_map.get(t)
            if df is None or df.empty:
                continue
            base = df if base is None else base.merge(df, on="code", how="inner")
        if base is None or base.empty:
            return pd.DataFrame()

        out = base
        for c in self.conds:
            out = _apply(out, c)
            if out.empty:
                return out

        # 存储列名 -> 字段名 的重命名表
        rename = {}
        for f in self.fields:
            rename[f.name] = f.name          # 多数同名字段：code/pe_ratio...
        # 内部列（下划线前缀）按表定义映射回公开名
        for tname, cols in (("valuation", VALUATION_COLS),
                            ("indicator", INDICATOR_COLS)):
            for public, internal in cols.items():
                if internal != public and internal in out.columns:
                    rename[internal] = public

        # 选列：只要 query 里出现的字段（保留 code）
        want = ["code"] + [f.name for f in self.fields if f.name != "code"]
        keep = []
        for w in dict.fromkeys(want):
            if w in out.columns:
                keep.append(w)
            else:
                # 字段对应内部列
                for tname, cols in (("valuation", VALUATION_COLS),
                                    ("indicator", INDICATOR_COLS)):
                    internal = cols.get(w)
                    if internal and internal in out.columns:
                        keep.append(internal)
                        break
        if keep:
            out = out[keep].copy()
        out = out.rename(columns=rename)
        # 补齐缺失字段（避免策略 r.get(...) 取到 KeyError）
        for w in dict.fromkeys(want):
            if w not in out.columns:
                out[w] = float("nan")
        out = out[list(dict.fromkeys(want))]
        if self.orders:
            by = [o.field.name for o in self.orders if o.field.name in out.columns]
            asc = [not o.desc for o in self.orders if o.field.name in out.columns]
            if by:
                out = out.sort_values(by, ascending=asc)
        if self._limit:
            out = out.head(self._limit)
        return out.reset_index(drop=True)


def query(*fields):
    return Query(*fields)


# 表名 -> {公开字段名: 内部存储列名}
_TABLE_COLS = {"valuation": VALUATION_COLS, "indicator": INDICATOR_COLS}


def _resolve_col(df: pd.DataFrame, field: _Field):
    """把 query 里的【公开字段名】解析到 DataFrame 里的【实际列名】。

    例：valuation.pe_ratio -> 快照列 "peTTM"
        indicator.roe      -> 快照列 "_roe"
    返回列名，找不到返回 None。
    """
    name = field.name
    if name in df.columns:
        return name
    internal = _TABLE_COLS.get(field.table, {}).get(name)
    if internal and internal in df.columns:
        return internal
    return None


def _apply(df: pd.DataFrame, c) -> pd.DataFrame:
    if isinstance(c, _Bool):
        if c.op == "not":
            sub = _apply(df, c.items[0])
            return df[~df.index.isin(sub.index)]
        parts = [_apply(df, x) for x in c.items]
        if c.op == "and":
            keep = set(df.index)
            for p in parts:
                keep &= set(p.index)
            return df.loc[sorted(keep)]
        keep = set()
        for p in parts:
            keep |= set(p.index)
        return df.loc[sorted(keep)]

    col = _resolve_col(df, c.lhs)
    if col is None:
        return df.iloc[0:0]
    s, v = df[col], c.rhs
    if c.op in ("in", "notin"):
        vals = [normalize_code(x) if col == "code" else x for x in v]
        return df[s.isin(vals)] if c.op == "in" else df[~s.isin(vals)]
    if c.op == "between":
        return df[(s >= v[0]) & (s <= v[1])]
    import operator
    ops = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
           "<=": operator.le, ">": operator.gt, ">=": operator.ge}
    try:
        return df[ops[c.op](s, v)]
    except Exception:
        return df.iloc[0:0]


# ═══════════════════════════════════════════════════════════════════════════
# 市场快照构造（供 query 求值）—— 从本地缓存拼出「某日全市场截面」
# ═══════════════════════════════════════════════════════════════════════════
_MEM_FLOW = {}
_EM_VAL_CACHE = {}
_EM_FIN_CACHE = {}
_SNAP_CACHE = {}


def _em_valuation(code: str) -> "pd.DataFrame | None":
    """东财历史估值（data/valuation/<code>.parquet）。

    优先级最高 —— 因为新浪 K 线【不含估值】，而日线里的 peTTM/pbMRQ 是 NaN。
    列：date, pe_ratio, pb_ratio, ps_ratio, pcf_ratio,
        market_cap(亿元), circulating_market_cap(亿元), close_price

    ★ 预建 `_d` 列（date 的 10 字符切片）供 bisect 二分查找 —— 见
      _em_valuation_asof 的性能注释。
    """
    p = os.path.join(DATA_ROOT, "valuation",
                     f"{code.replace('.', '_')}.parquet")
    if not os.path.exists(p):
        return None
    df = _EM_VAL_CACHE.get(p)
    if df is None:
        try:
            df = pd.read_parquet(p)
            df["date"] = df["date"].astype(str)
            df = df.sort_values("date")
            df["_d"] = df["date"].str.slice(0, 10)
        except Exception:
            return None
        _EM_VAL_CACHE[p] = df
    return df


def _em_valuation_asof(code: str, date: str) -> dict:
    """取 date 当日或之前最近一日的估值。

    ⚠️ 性能要点（实测踩坑）：
        第一版写成 df[df["date"] <= d].iloc[-1] —— 每次 O(n) 线性扫描。
        而策略每天对 5,156 只候选各调一次，回测 1,637 天：
            5156 × 1637 次扫描 × ~1637 行 ≈ 天文数字
        实测后果：回测 25 分钟只跑完 98 天（6%），外推需 26 小时。
        改用 bisect 二分查找后 O(log n)，该环节开销基本消失。
    """
    df = _em_valuation(code)
    if df is None or df.empty:
        return {}
    d = str(date)[:10]
    i = bisect.bisect_right(df["_d"], d) - 1
    if i < 0:
        return {}
    return df.iloc[i].to_dict()


def _em_financial(code: str) -> "pd.DataFrame | None":
    """东财财务指标（data/financial_em/<code>.parquet）。

    列：statDate, pubDate, roe, inc_net_profit_year_on_year,
        inc_revenue_year_on_year, eps, bps, total_revenue, net_profit
    单位与聚宽一致（百分数）。
    """
    p = os.path.join(DATA_ROOT, "financial_em",
                     f"{code.replace('.', '_')}.parquet")
    if not os.path.exists(p):
        return None
    df = _EM_FIN_CACHE.get(p)
    if df is None:
        try:
            df = pd.read_parquet(p)
            df["pubDate"] = df["pubDate"].astype(str)
            df = df.sort_values("pubDate")
            # 预建排序列，供 bisect 使用（见 _em_valuation_asof 的性能注释）
            df["_d"] = df["pubDate"].str.slice(0, 10)
        except Exception:
            return None
        _EM_FIN_CACHE[p] = df
    return df


def _em_financial_asof(code: str, date: str) -> dict:
    """★ 防前视：只取 pubDate <= date 的最近一期。用二分查找（性能关键）。"""
    df = _em_financial(code)
    if df is None or df.empty:
        return {}
    d = str(date)[:10]
    i = bisect.bisect_right(df["_d"], d) - 1
    if i < 0:
        return {}
    return df.iloc[i].to_dict()


def _build_snapshot(date: str, codes: Iterable[str] | None = None) -> dict:
    """返回 {'valuation': df, 'indicator': df}，列名用聚宽字段名。

    market_cap 单位：亿元（聚宽口径）。

    ═══════════════════════════════════════════════════════════════════════
    ★ 性能设计（这是本模块最关键的一处工程决策）
    ═══════════════════════════════════════════════════════════════════════
    估值与财务已由 merge_into_daily.py 【预先合并进 daily_qfq parquet】，
    所以这里只需读一只票的日线行即可，**不再访问 valuation / financial 文件**。

    为什么必须这样（实测数据）：
      早期实现每天对 5,156 只候选各调 2 次 asof（估值 + 财务），
      每次都做 pandas 索引 + to_dict()。bisect 本身只要 8.8µs，
      但 pandas 逐行访问的固定开销乘以 5156×1637 这个量级后：
          60 个交易日跑了 >10 分钟仍未完成
          进程内存 3.8 GB（5,140 只 × 1,637 行全驻留）
      改为一文件读取后：内存回到「单只票」级别，无逐日 join。

    映射（日线列 -> 聚宽字段）：
        peTTM -> pe_ratio        pbMRQ -> pb_ratio
        psTTM -> ps_ratio        pcfNcfTTM -> pcf_ratio
        market_cap -> market_cap（已是亿元）   circ_mcap -> circulating_market_cap
        _roe -> roe              _yoy_rev -> inc_revenue_year_on_year
        _yoy_ni -> inc_net_profit_year_on_year
    """
    key = (date, tuple(codes) if codes else None)
    if key in _SNAP_CACHE:
        return _SNAP_CACHE[key]

    sec = store.load_securities()
    if codes is None:
        codes = list(sec.index) if not sec.empty else []
    codes = [normalize_code(c) for c in codes]

    d = str(date)[:10]
    vrows, irows = [], []
    n_merged = 0
    # ★ 批量走快路径：一次取全部候选的快照行（热缓存下 5µs/只）
    snap_rows = store.rows_asof(codes, d)
    for c in codes:
        r = snap_rows.get(c)
        if r is None:
            continue
        mc = _fv(r.get("market_cap"))
        if mc == mc:
            n_merged += 1
        else:                             # NaN -> 回退 收盘价×总股本
            shares = _total_shares(c)
            if shares:
                mc = _fv(r.get("close")) * shares / 1e8
        vrows.append({
            "code": c,
            "peTTM": _fv(r.get("peTTM")),
            "pbMRQ": _fv(r.get("pbMRQ")),
            "psTTM": _fv(r.get("psTTM")),
            "pcfNcfTTM": _fv(r.get("pcfNcfTTM")),
            "_market_cap": mc,
            "_circ_mcap": _fv(r.get("circ_mcap")),
            "turn": _fv(r.get("turn")),
        })
        irows.append({
            "code": c,
            "_roe": _fv(r.get("_roe")),
            "_yoy_rev": _fv(r.get("_yoy_rev")),
            "_yoy_ni": _fv(r.get("_yoy_ni")),
            "_np_margin": float("nan"),
            "_gp_margin": float("nan"),
            "_yoy_rev_total": _fv(r.get("_yoy_rev")),
        })

    out = {"valuation": pd.DataFrame(vrows), "indicator": pd.DataFrame(irows)}
    out["_src"] = {"已合并日线": n_merged, "请求股票数": len(codes)}
    _SNAP_CACHE[key] = out
    return out


def _fv(v):
    try:
        f = float(v)
        return f if f == f else float("nan")
    except Exception:
        return float("nan")


def _total_shares(code: str):
    """总股本（股）。从 meta 读取；没有则返回 None（市值将为 NaN）。"""
    p = f"{DATA_ROOT}/meta/shares.json"
    import json
    import os
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get(code)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# ① attribute_history
# ═══════════════════════════════════════════════════════════════════════════
def attribute_history(security, count, unit="1d", fields=None,
                      skip_paused=True, df=True, fq="pre", **kw):
    """个股历史行情 -> DataFrame（行=时间升序，列=字段）。

    ⚠️ 注意：这里是【局部函数】，返回的 DataFrame 以 RangeIndex 排列，
       但策略只用 .iloc / .rolling / .tail，所以不需要 DatetimeIndex。
    """
    fields = list(fields or ["close"])
    code = normalize_code(security)
    end = _ctx_date()
    guard_date(end, f"attribute_history({code})")
    # ★★★ 1 bar 前视修复（2026-10-15 11:40，由子代理发现，已独立确证）★★★
    #
    # 问题：
    #   原写法 ref = end（= current_date）配合 store.slice_daily 的
    #   searchsorted(side="right")，会【包含当日 K 线】。
    #   而策略的 market_open_trade 在 09:31 运行并调用 check_exit_signals
    #   → close.iloc[-1] = 【当日收盘价】
    #   → 09:31 用当日收盘价决策，次日开盘成交 ⇒ 前视 1 根 bar
    #
    # 为什么原来没被发现：
    #   注释写"策略在盘中调用时，语义是'截至最近已收盘交易日'"，
    #   但代码把 ref 设成当日、slice 用 side="right"，与注释相反。
    #
    # 修正：
    #   把 ref 回退【一个自然日】(end - 1 day)，
    #   这样 slice_daily(code, ref, ...) 的最后一根 = 【上一个已收盘交易日】。
    #   close.iloc[-1] 即为"昨收"，与 09:31 的真实可得信息一致。
    #
    # ⚠️ 影响：所有日线信号（新低/均线/ATR/非硬性 4 信号/放量）都会变化，
    #   历史回测结果需重跑。这是修正必需付出的代价。
    #   如需回退到旧行为（仅用于对照），设 JQSHIM_NO_LOOKAHEAD_FIX=1。
    if end and not os.environ.get("JQSHIM_NO_LOOKAHEAD_FIX"):
        try:
            import datetime as _dt
            _d = _dt.date.fromisoformat(str(end)[:10]) - _dt.timedelta(days=1)
            ref = _d.isoformat()
        except Exception:
            ref = end
    else:
        ref = end or "2100-01-01"
    d = store.slice_daily(code, ref, int(count) + 5)
    if d is None or d.empty:
        return pd.DataFrame(columns=fields)
    keep = [f for f in fields if f in d.columns]
    out = d[keep].copy() if keep else pd.DataFrame(columns=fields)
    for f in fields:
        if f not in out.columns:
            out[f] = float("nan")
    return out[fields].tail(int(count)).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# ② get_price（panel=False -> 长表；panel=True -> 宽表）
# ═══════════════════════════════════════════════════════════════════════════
def get_price(security, start_date=None, end_date=None, frequency="daily",
              fields=None, skip_paused=True, fq="pre", count=None,
              panel=False, **kw):
    """批量日线。

    ★ panel=False（策略用的）必须返回【长表】：
        columns = [time, code, <fields...>]
      因为策略里写：
        for code, grp in px.groupby("code"):
            grp = grp.sort_values("time") if "time" in grp.columns else grp
    """
    if isinstance(security, str):
        codes = [security]
    else:
        codes = [normalize_code(s) for s in security]
    fields = list(fields or ["close"])
    fq_map = {"pre": "", "post": "", "none": ""}   # 不复权（与回测默认一致）
    _ = fq_map.get(fq, "")
    end = _as_str(end_date) or _ctx_date() or "2100-01-01"
    guard_date(end, "get_price")

    # Fast path: cross-section NumPy arrays instead of per-stock pandas slices.
    if (_fc_config is not None and _fc_config.fast_enabled()
            and count and not start_date
            and len(codes) >= _fc_config.panel_min_codes()):
        try:
            from .fastcore import panel as _fc_panel
            if panel:
                fast = _fc_panel.price_wide(codes, end, int(count), fields[0])
            else:
                fast = _fc_panel.price_window(codes, end, int(count), fields)
            if fast is not None:
                return fast
        except Exception:
            if _fc_config.strict_fast():
                raise

    frames = []
    for c in codes:
        if count:
            d = store.slice_daily(c, end, int(count) + 5)
        else:
            d = store.load_daily(c)
            if d is not None and not d.empty:
                s0 = _as_str(start_date)
                if s0:
                    d = d[d["date"] >= s0]
                d = d[d["date"] <= end]
        if d is None or d.empty:
            continue
        sub = d.tail(int(count)) if count else d
        keep = [f for f in fields if f in sub.columns]
        if not keep:
            continue
        t = sub[["date"] + keep].copy()
        t = t.rename(columns={"date": "time"})
        t.insert(1, "code", c)
        frames.append(t)

    if not frames:
        return pd.DataFrame(columns=["time", "code"] + fields)
    out = pd.concat(frames, ignore_index=True)
    if panel:
        # 宽表：index=time, columns=code（单字段时）
        f0 = fields[0]
        return out.pivot_table(index="time", columns="code", values=f0,
                               aggfunc="last")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ③ get_all_securities
# ═══════════════════════════════════════════════════════════════════════════
# ── 北交所排除开关 ────────────────────────────────────────────────────────
# 为什么排除：数据质量不可用
#   · Tushare 对北交所（2021-11 开市）的 adj_factor 不可靠：
#     实测 920627 的 adj_factor 连跳 2.3 倍，而真实价格仅涨 25%
#     → 复权后日收益出现 +150%~+190% 的假跳变
#   · 全量扫描：北交所贡献了 4,073 / 4,161 个超限跳变（98%）
#   · 实测 920971 修复后收益偏差仍达 20.86%（无法通过重算修复）
#   · 策略本身也不应选北交所（流动性低、涨跌停 ±30%、参与门槛高）
# 排除方式：按 code 前缀 + 市场后缀双重判定
#   · 前缀：43/83/87/88/92（北交所现行与历史代码段）
#   · 后缀：.BJSE（本地格式）或 .BJ（Tushare 格式）
# 关闭：设环境变量 JQSHIM_KEEP_BJSE=1
_EXCLUDE_BJSE = os.environ.get("JQSHIM_KEEP_BJSE", "") not in ("1", "true")
_BJSE_PREFIX = ("43", "83", "87", "88", "92")


def is_bjse(code: str) -> bool:
    """是否为北交所股票。"""
    s = str(code).upper()
    if s.endswith(".BJSE") or s.endswith(".BJ"):
        return True
    num = s.split(".")[0]
    return len(num) == 6 and num[:2] in _BJSE_PREFIX


def get_all_securities(types=None, date=None, **kw):
    """全市场证券列表。index = 聚宽代码，columns = display_name/name/
    start_date/end_date/type。

    ★ 默认排除北交所（见上方 _EXCLUDE_BJSE 说明）。
    """
    sec = store.load_securities()
    if sec.empty:
        return pd.DataFrame(columns=["display_name", "name", "start_date",
                                     "end_date", "type"])
    out = sec.copy()
    if date:
        d = _as_str(date)
        if "start_date" in out.columns:
            out = out[out["start_date"].astype(str) <= d]
        if "end_date" in out.columns:
            out = out[(out["end_date"].isna())
                      | (out["end_date"].astype(str) >= d)]
    if types:
        if "type" in out.columns:
            out = out[out["type"].isin(list(types))]
    if _EXCLUDE_BJSE and len(out):
        try:
            mask = [not is_bjse(c) for c in out.index]
            out = out[mask]
        except Exception:
            pass
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ④ get_index_stocks
# ═══════════════════════════════════════════════════════════════════════════
def get_index_stocks(index_symbol, date=None, **kw):
    """指数成分股 -> 代码列表。用不晚于 date 的最近快照。"""
    d = _as_str(date) or _ctx_date() or "2100-01-01"
    return store.index_members_asof(normalize_code(index_symbol), d)


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ get_trade_days
# ═══════════════════════════════════════════════════════════════════════════
def get_trade_days(start_date=None, end_date=None, count=None, **kw):
    """交易日。返回升序 list[str]。策略只用 arr[-1] / arr[-2]。"""
    cal = store.load_calendar()
    if not cal:
        return []
    if count:
        end = _as_str(end_date) or _ctx_date() or cal[-1]
        r = store.trade_days_before(end, int(count))
        return r
    s0 = _as_str(start_date) or cal[0]
    s1 = _as_str(end_date) or cal[-1]
    return [d for d in cal if s0 <= d <= s1]


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ get_money_flow（新浪源；只用方向）
# ═══════════════════════════════════════════════════════════════════════════
FLOW_FIELD_MAP = {
    "net_amount_main": "netamount",     # 默认；备选 "r0_net"
    "change_pct": "__from_price__",     # 由价格自算，与聚宽一致
}


def _pct_from_price(code: str, date: str):
    """由【本地价格】自算涨跌幅（%）。

    ★ 性能关键（cProfile 实测）：
        第一版写成  i = d.index[d["date"] == date]  —— 每次对 1,637 行做
        一次全列字符串比较，然后还要 `.iloc` 两次。被调用 101 万次，
        仅此一个函数就吃掉 172 秒（占回测总耗时 42%）。
        改用 numpy 二分（日期已排序）后是 O(log n)，且只取需要的两个值。
    """
    arr, cols = store.snap_arrays(code)
    if arr is None or cols is None:
        return float("nan")
    ds = np.str_(str(date)[:10])
    i = int(np.searchsorted(arr, ds, side="left"))
    if i >= len(arr) or arr[i] != ds or i == 0:
        return float("nan")
    cl = cols.get("close")
    if cl is None:
        return float("nan")
    c1 = float(cl[i])
    c0 = float(cl[i - 1])
    return (c1 / c0 - 1.0) * 100.0 if c0 else float("nan")


def get_money_flow(security, start_date=None, end_date=None, fields=None,
                   count=None, **kw):
    """主力资金流 -> DataFrame，列 = fields。

    ★ 单位：聚宽 net_amount_main 是【万元】。
      新浪 netamount 是【元】。这里统一换算成【万元】以贴近聚宽口径。
      ⚠️ 但策略对资金流【只判方向】(v.tail(3) > 0).all()，
         所以绝对值口径差异不影响决策；换算只为可读性。

    ★ change_pct 由本地价格自算 —— 价格是权威源，自算的涨跌幅更可靠。

    ★ 性能关键（cProfile 实测）：
        第一版用  f[f["date"] <= end]  +  sub.iterrows()  +  逐行 _pct_from_price。
        策略每天要为全市场每只票调用一次，累计 1,106 次、30 万行，
        光这个函数就 305 秒（占回测总耗时 75%）。
        改为 numpy 二分取切片 + 列表推导直接构造 DataFrame 后，
        同等工作量降到毫秒级。
    """
    codes = ([normalize_code(security)] if isinstance(security, str)
             else [normalize_code(s) for s in security])
    fields = list(fields or ["date", "sec_code", "net_amount_main", "change_pct"])
    end = _as_str(end_date) or _ctx_date() or "2100-01-01"
    guard_date(end, "get_money_flow")

    # Fast path: build the (date x code) flow matrix once and emit the long
    # table with NumPy repeat/tile/ravel instead of per-row dicts.
    if (_fc_config is not None and _fc_config.fast_enabled()
            and count and len(codes) >= _fc_config.panel_min_codes()):
        try:
            from .fastcore import panel as _fc_panel
            fast = _fc_panel.flow_window(codes, end, int(count), fields)
            if fast is not None:
                return fast
        except Exception:
            if _fc_config.strict_fast():
                raise
    want_net = "net_amount_main" in fields
    want_pct = "change_pct" in fields

    rows = []
    for c in codes:
        f = store.load_flow(c)
        if f is None or f.empty:
            continue
        # ★ 用 store.flow_arrays 的独立缓存（勿在此重复解析 DataFrame）
        darr, narr = store.flow_arrays(c)
        if darr is None:
            continue
        if darr is None or len(darr) == 0:
            continue
        hi = int(np.searchsorted(darr, np.str_(end), side="right"))
        if hi <= 0:
            continue
        lo = max(0, hi - int(count)) if count else 0
        if lo >= hi:
            continue
        dslice = darr[lo:hi]
        # ★ 一次批量取涨跌幅
        #   （勿逐日调用 _pct_from_price —— 见 store.pct_at_dates 的性能注释）
        pcts = store.pct_at_dates(c, dslice) if want_pct else None
        for j, k in enumerate(range(lo, hi)):
            rec = {"date": str(darr[k]), "sec_code": c}
            if want_net:
                v = narr[k] if narr is not None else float("nan")
                try:
                    v = float(v)
                except Exception:
                    v = float("nan")
                rec["net_amount_main"] = v / 1e4 if v == v else float("nan")
            if want_pct:
                rec["change_pct"] = (float(pcts[j])
                                     if pcts is not None else float("nan"))
            rows.append(rec)

    if not rows:
        return pd.DataFrame(columns=fields)
    out = pd.DataFrame(rows)
    for f in fields:
        if f not in out.columns:
            out[f] = float("nan")
    return out[fields]


def get_money_flow_summary(security, end_date=None, count=5, **kw):
    """Aggregate money flow by date, without materialising per-stock rows.

    Returns ``DataFrame[date, net_amount_main]`` in the same unit as
    ``get_money_flow``'s ``net_amount_main`` (i.e. the sum of the raw values
    divided by 1e4).  Returns ``None`` when the fast path is disabled or the
    batch is small, so callers can fall back to the original API.

    This is intentionally an *aggregate* API: the existing strategy helper
    only ever does ``groupby('date').sum()`` on the batch result.
    """
    if _fc_config is None or not _fc_config.fast_enabled() or not count:
        return None
    codes = ([normalize_code(security)] if isinstance(security, str)
             else [normalize_code(s) for s in security])
    if len(codes) < _fc_config.flow_summary_min_codes():
        return None
    end = _as_str(end_date) or _ctx_date() or "2100-01-01"
    guard_date(end, "get_money_flow_summary")
    try:
        from .fastcore import flow_agg
        df = flow_agg.summary_frame(codes, end, int(count))
        if df is None or df.empty:
            return df
        return df[["date", "net_amount_main"]]
    except Exception:
        if _fc_config.strict_fast():
            raise
        return None


# ═══════════════════════════════════════════════════════════════════════════
# ⑦ get_fundamentals
# ═══════════════════════════════════════════════════════════════════════════
def get_fundamentals(query_object, date=None, stat_date=None, **kw):
    """按 query 对象取截面。返回列 = query 里的字段名（code 在首位）。"""
    if not isinstance(query_object, Query):
        return pd.DataFrame()
    d = _as_str(date) or _as_str(stat_date) or _ctx_date() or "2100-01-01"
    guard_date(d, "get_fundamentals")
    snap = _build_snapshot(d)
    return query_object.eval(snap)


# ═══════════════════════════════════════════════════════════════════════════
# 其他常用接口
# ═══════════════════════════════════════════════════════════════════════════
def get_industry(security, date=None):
    sec = store.load_securities()
    codes = ([normalize_code(security)] if isinstance(security, str) else
             [normalize_code(s) for s in security])
    out = {}
    for c in codes:
        ind = None
        if not sec.empty and c in sec.index and "industry" in sec.columns:
            ind = sec.loc[c, "industry"]
        out[c] = {"sw_l1": {"industry_name": ind}} if ind else {}
    return out


def get_security_info(code):
    sec = store.load_securities()
    c = normalize_code(code)
    if sec.empty or c not in sec.index:
        return None
    r = sec.loc[c]
    return {"display_name": r.get("display_name"), "name": r.get("name"),
            "start_date": r.get("start_date"), "end_date": r.get("end_date"),
            "type": r.get("type")}


def get_extras(field, security_list, start_date=None, end_date=None,
               count=None, **kw):
    return pd.DataFrame()


def get_valuation(security, start_date=None, end_date=None, fields=None, count=None):
    return get_price(security, start_date=start_date, end_date=end_date,
                     count=count, frequency="daily",
                     fields=fields or ["pe_ratio", "pb_ratio"], panel=False)


def get_current_data():
    return {}


def get_last_price(security, **kw):
    c = normalize_code(security)
    d = store.slice_daily(c, _ctx_date() or "2100-01-01", 1)
    if d is None or d.empty:
        return float("nan")
    return float(d["close"].iloc[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════════════
def _ctx_date():
    if CONTEXT is None:
        return None
    d = getattr(CONTEXT, "current_date", None)
    if d is None:
        return None
    if isinstance(d, dt.datetime):
        return d.date().isoformat()
    if isinstance(d, dt.date):
        return d.isoformat()
    return str(d)[:10]


def _as_str(d):
    if d is None:
        return None
    if isinstance(d, dt.datetime):
        return d.date().isoformat()
    if isinstance(d, dt.date):
        return d.isoformat()
    return str(d)[:10]


def clear_snapshot_cache():
    _SNAP_CACHE.clear()
