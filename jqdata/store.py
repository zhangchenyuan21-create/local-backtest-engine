"""本地数据存储层 —— parquet 缓存 + 内存索引。

设计原则
═══════════════════════════════════════════════════════════════════════════
1. **一次抓取，永久复用**：所有 API 调用先查本地缓存，缺什么才去抓。
   聚宽 Data API 的配额只消耗一次。
2. **不复权**：与聚宽 get_price 默认口径一致（已实测腾讯不复权 = 聚宽）。
3. **按日期截断**：所有读取都提供 `end_date`，绝不返回未来数据。
4. **列存 + 懒加载**：日线按股票分文件，用到的才读。

目录布局
═══════════════════════════════════════════════════════════════════════════
  data/
    daily/<code>.parquet        日线 OHLCV + 估值（不复权）
    financial/<code>.parquet    季频财务（含 pubDate 公告日）
    index/<index>.json          指数成分（按季快照）
    calendar.json               交易日历
    securities.json             全市场 + 上市/退市日 + 行业
    moneyflow/<code>.parquet    主力资金流
    meta/fetch_state.json       抓取进度（断点续传）
"""
from __future__ import annotations

import json
import os
import threading
from typing import Iterable

import numpy as np
import pandas as pd

from . import DATA_ROOT

_LOCK = threading.RLock()
_MEM = {}          # 进程内缓存：{(kind, key): DataFrame}

DAILY = os.path.join(DATA_ROOT, "daily")
FIN = os.path.join(DATA_ROOT, "financial")
IDX = os.path.join(DATA_ROOT, "index")
FLOW = os.path.join(DATA_ROOT, "moneyflow")
META = os.path.join(DATA_ROOT, "meta")
CAL = os.path.join(DATA_ROOT, "calendar.json")
SEC = os.path.join(DATA_ROOT, "securities.json")
STATE = os.path.join(META, "fetch_state.json")


# ═══════════════════════════════════════════════════════════════════════════
# ★ 运行时目录覆盖 —— 支持「双数据源对比」
# ═══════════════════════════════════════════════════════════════════════════
# 为什么要这个：需要用【同一份策略代码】跑两套数据源，量化
# 「前视偏差修复 + 数据源切换」各自贡献了多少。
#
#   现有数据：data/daily + data/daily_qfq（新浪日线 + 东财估值/财务 + 单日成分快照）
#   Tushare：data/ts_daily + data/ts_daily_qfq（Tushare 全套 + 历史成分时间线）
#
# 实现方式：把两套数据组装成各自独立的【数据根】，用环境变量切换，
# 而不是在同一目录里混放 —— 避免误用与难以回退。
#   JQSHIM_DAILY_DIR  覆盖不复权日线目录
#   JQSHIM_QFQ_DIR    覆盖前复权日线目录
#   JQSHIM_SECTOR_DIR 覆盖板块快照目录
#
# 这些函数在【每次调用时】读环境变量，所以同一进程内可动态切换
#（便于在一个脚本里跑完 A/B 两套）。
def daily_dir() -> str:
    return os.environ.get("JQSHIM_DAILY_DIR") or DAILY


def qfq_dir() -> str:
    return os.environ.get("JQSHIM_QFQ_DIR") or QFQ


def sector_dir() -> str:
    """板块目录。

    优先级：JQSHIM_SECTOR_DIR > JQSHIM_NEWDATA_ROOT/sector > DATA_ROOT/sector

    ★ 为什么要吃 JQSHIM_NEWDATA_ROOT
      踩过的坑：迁移时上层只用 JQSHIM_NEWDATA_ROOT 切数据根，
      而本函数当时只认 JQSHIM_SECTOR_DIR —— 于是 fetch_ts_all.py 仍然
      把 Tushare 的申万快照写进了 data/sector/sw_l1.json，
      覆盖了现有（对照）数据，导致 get_industries() 返回错误结构、
      test_routeA_数据接通.py 报「股票池 0」。
      加上第二优先级后，切数据根即可整片隔离。
    """
    d = os.environ.get("JQSHIM_SECTOR_DIR")
    if d:
        return d
    r = os.environ.get("JQSHIM_NEWDATA_ROOT")
    if r:
        return os.path.join(r, "sector")
    return os.path.join(DATA_ROOT, "sector")


def _p(*a):
    os.makedirs(os.path.dirname(os.path.join(*a)), exist_ok=True)
    return os.path.join(*a)


# ═══════════════════════════════════════════════════════════════════════════
# 交易日历
# ═══════════════════════════════════════════════════════════════════════════
def load_calendar() -> list[str]:
    """返回升序排列的交易日 ['2020-01-02', ...]"""
    if "cal" in _MEM:
        return _MEM["cal"]
    if not os.path.exists(CAL):
        _MEM["cal"] = []
        return []
    with open(CAL, encoding="utf-8") as f:
        days = json.load(f)
    days = sorted(set(days))
    _MEM["cal"] = days
    return days


def save_calendar(days: Iterable[str]) -> None:
    with _LOCK:
        days = sorted(set(days))
        with open(_p(CAL), "w", encoding="utf-8") as f:
            json.dump(days, f)
        _MEM.pop("cal", None)


def trade_days_before(d: str, n: int = 1) -> list[str]:
    """d 及之前（含）的最近 n 个交易日，升序返回。"""
    cal = load_calendar()
    if not cal:
        return []
    i = _bisect_right(cal, d)
    return cal[max(0, i - n):i]


def prev_trade_day(d: str) -> str | None:
    r = trade_days_before(d, 2)
    return r[0] if len(r) >= 2 else (r[0] if r and r[0] < d else None)


def _bisect_right(arr, x):
    import bisect
    return bisect.bisect_right(arr, x)


# ═══════════════════════════════════════════════════════════════════════════
# 股票列表
# ═══════════════════════════════════════════════════════════════════════════
def load_securities() -> pd.DataFrame:
    """index=聚宽代码, columns=[display_name, name, start_date, end_date, type, industry]"""
    if "sec" in _MEM:
        return _MEM["sec"]
    if not os.path.exists(SEC):
        _MEM["sec"] = pd.DataFrame()
        return _MEM["sec"]
    with open(SEC, encoding="utf-8") as f:
        raw = json.load(f)
    df = pd.DataFrame.from_dict(raw, orient="index")
    df.index.name = "code"
    _MEM["sec"] = df
    return df


def save_securities(recs: dict) -> None:
    with _LOCK:
        with open(_p(SEC), "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        _MEM.pop("sec", None)


# ═══════════════════════════════════════════════════════════════════════════
# 日线
# ═══════════════════════════════════════════════════════════════════════════
DAILY_COLS = ["date", "open", "high", "low", "close", "volume", "amount",
              "turn", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST"]

# ═══════════════════════════════════════════════════════════════════════════
# ★ 复权口径：默认使用【前复权】价
# ═══════════════════════════════════════════════════════════════════════════
# 为什么（实测确认）：
#   聚宽 get_price / attribute_history 的默认参数是 fq='pre'（前复权）：
#       get_price(code, ...) 默认 = 8.97  ==  fq='pre'
#       get_price(code, ..., fq=None)     = 9.03
#   而策略 12 处调用【无一显式指定 fq】-> 聚宽全程使用前复权价。
#
#   若本地用【不复权】价跑同一策略，除权除息日会出现虚假跳空
#   （10送10 -> 价格腰斩），被误判为暴跌，触发 ATR 止损 / 跌破MA20 等。
#   实测 60 只抽样：不复权 vs 前复权 中位差 0.49%、P90 差 5.41%、最大 43.51%。
#
# 数据来源：data/daily_qfq/（由 adjust_price.py 从 daily + dividend 生成）
# 可用 JQSHIM_NO_QFQ=1 强制使用不复权（仅用于对比诊断）。
# ═══════════════════════════════════════════════════════════════════════════
QFQ = os.path.join(DATA_ROOT, "daily_qfq")


def use_qfq() -> bool:
    return os.environ.get("JQSHIM_NO_QFQ", "") not in ("1", "true", "True")


def daily_path(code: str) -> str:
    """返回日线文件路径。存在前复权文件时优先返回它。"""
    name = f"{code.replace('.', '_')}.parquet"
    if use_qfq():
        q = os.path.join(qfq_dir(), name)
        if os.path.exists(q):
            return q
    return os.path.join(daily_dir(), name)


def raw_daily_path(code: str) -> str:
    """不复权日线路径（诊断用）。"""
    return os.path.join(daily_dir(), f"{code.replace('.', '_')}.parquet")


# ═══════════════════════════════════════════════════════════════════════════
# ★ 缓存上限必须 >= 全部证券数（实测教训）
# ═══════════════════════════════════════════════════════════════════════════
# 我最初设为 1200（想省内存），结果造成【缓存抖动】：
#   策略的 _sector_return_5d_now 要遍历全市场算板块 5 日收益，
#   每次遍历都击穿 1200 条 LRU -> 反复重新解析 parquet。
#   py-spy 抓到的栈：
#       read_parquet <- load_daily <- slice_daily <- get_price
#                    <- _sector_return_5d_now <- prefetch_daily_flow
#   后果：4 组并发时外推要 20+ 小时，日志 15 分钟不推进。
#
# 正解：上限设为 6000（> 5,158 只证券），让 DataFrame 缓存完整驻留。
#   单进程约 1.6GB；2 组并发约 3.2GB，31GB 内存完全够。
# ═══════════════════════════════════════════════════════════════════════════
# ★ 只统计 ("daily", code) 条目 —— _MEM 里还有 ("dates", code) 这类
#   轻量索引（约 1.6KB/只），它们必须【常驻】否则每次都要重建。
#   踩过的坑：早期 while len(_MEM) > MAX 把两类一起计数，
#   2 x 5,158 = 10,316 条 vs 上限 6000 -> 每加一条就淘汰一条，永久抖动，
#   回测退化到 27 秒/交易日（正常应为 2.4 秒）。
_DAILY_CACHE_MAX = int(os.environ.get("JQSHIM_DAILY_CACHE_MAX", "6000"))


def _daily_cache_size() -> int:
    return sum(1 for k in _MEM if k[0] == "daily")


def _evict_daily() -> None:
    """只淘汰 "daily" 条目（最旧优先），绝不碰 "dates"。"""
    while _daily_cache_size() > _DAILY_CACHE_MAX:
        for k in list(_MEM.keys()):
            if k[0] == "daily":
                _MEM.pop(k, None)
                break
        else:
            return


def load_daily(code: str) -> pd.DataFrame | None:
    """读日线。带 LRU 上限的 DataFrame 缓存。

    ★ 为什么需要上限：完整 DataFrame 约 330KB/只（22 列 x 1,637 行），
      5,158 只全驻留就是 1.6GB。而 _build_snapshot 已走独立的 _SNAP
      数组缓存（约 22KB/只），不必再长期持有 DataFrame。
      4 组回测并发时，这个上限直接决定系统会不会换页。
    """
    key = ("daily", code)
    with _LOCK:
        if key in _MEM:
            v = _MEM.pop(key)
            _MEM[key] = v          # 移到末尾 = 最近使用
            return v
    p = daily_path(code)
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
    with _LOCK:
        _MEM[key] = df
        _evict_daily()
    return df


def save_daily(code: str, df: pd.DataFrame) -> None:
    # 写入后必须清掉该票的日期索引缓存，否则 slice_daily 会用旧数组
    _MEM.pop(("dates", code), None)
    _MEM.pop(("daily", code), None)
    with _LOCK:
        df = df.copy()
        if "date" in df.columns:
            df["date"] = df["date"].astype(str)
            df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        df.to_parquet(_p(daily_path(code)), index=False)
        _MEM.pop(("daily", code), None)


# _build_snapshot 真正需要的列
SNAP_FIELDS = ("close", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM",
               "market_cap", "circ_mcap", "turn",
               "_roe", "_yoy_rev", "_yoy_ni")

# ═══════════════════════════════════════════════════════════════════════════
# ★ 两级缓存（性能关键，实测数据见下）
# ═══════════════════════════════════════════════════════════════════════════
# 实测：单只票的完整 DataFrame 含 22 列 × 1,637 行，驻留 ~330KB；
#       5,000 只就是 1.6 GB，且首次读 parquet 需 1.9ms/只。
#       而 _build_snapshot 每天要对全市场每只票取一行 —— 若走 DataFrame，
#       全市场 5.2s/天 × 1,637 天 = 2.4 小时。
#
# 方案：快照只需要的 11 列单独放一个缓存（numpy float32，约 22KB/只），
#       完整 DataFrame 仍走 _MEM 但可被清掉。
# ═══════════════════════════════════════════════════════════════════════════
_SNAP = {}          # {code: (dates_ndarray, {field: ndarray})}


def _build_snap_cache(code: str):
    """从磁盘构建某只票的快照数组缓存（只做一次）。"""
    p = daily_path(code)
    if not os.path.exists(p):
        _SNAP[code] = (None, None)
        return
    try:
        import pyarrow.parquet as pq
        avail = set(pq.ParquetFile(p).schema.names)
        use = [c for c in ("date",) + SNAP_FIELDS if c in avail]
        df = pd.read_parquet(p, columns=use)
    except Exception:
        df = pd.read_parquet(p)
    if df is None or df.empty:
        _SNAP[code] = (None, None)
        return
    df = df.sort_values("date")
    dates = df["date"].astype(str).to_numpy()
    cols = {}
    for c in SNAP_FIELDS:
        if c in df.columns:
            # ★ float32：快照缓存是常驻内存的大头。
            #   12 列 x 1,637 行 x 5,158 只：float64 约 810MB，float32 约 405MB。
            #   4 组回测并发时这一项就决定系统会不会换页。
            #   价格有效位约 7 位十进制，float32 足够（实测与 float64 结果一致）。
            cols[c] = pd.to_numeric(df[c], errors="coerce").to_numpy(
                dtype="float32", na_value=float("nan"))
    _SNAP[code] = (dates, cols)


def snap_arrays(code: str, fields=None):
    """返回 (日期数组, {字段: ndarray})。带独立缓存。"""
    v = _SNAP.get(code)
    if v is None:
        _build_snap_cache(code)
        v = _SNAP.get(code)
    if v is None:
        return None, None
    return v


def row_asof(code: str, end_date: str, fields=SNAP_FIELDS) -> dict | None:
    """取截止 end_date 的最后一根日线，返回只含 fields 的 dict。"""
    arr, cols = snap_arrays(code)
    if arr is None or cols is None:
        return None
    i = int(np.searchsorted(arr, np.str_(end_date), side="right")) - 1
    if i < 0:
        return None
    out = {}
    for c, a in cols.items():
        out[c] = a[i]
    return out


def rows_asof(codes, end_date: str):
    """批量版：返回 {code: {field: value}}。供 _build_snapshot 用。

    比逐只调 row_asof 少一层函数调用与 dict 查找。
    """
    d = np.str_(str(end_date)[:10])
    out = {}
    for c in codes:
        arr, cols = snap_arrays(c)
        if arr is None:
            continue
        i = int(np.searchsorted(arr, d, side="right")) - 1
        if i < 0:
            continue
        out[c] = {k: v[i] for k, v in cols.items()}
    return out


def slice_daily(code: str, end_date: str, count: int) -> pd.DataFrame | None:
    """取 (code, 截至 end_date 的最近 count 根) 日线。按日期截断，无未来数据。

    ★ 性能要点（实测）：
        第一版写成 df[df["date"] <= end_date] —— 每次做一次全列比较 +
        布尔索引，1,637 行约 1.6ms。策略每天对 5,156 只候选调用，
        1,637 天累计 -> 约 5 小时，是回测的主要瓶颈。
        改为 numpy 二分（date 已排序）后是 O(log n)。
        仅需「一行」的场景请用 row_asof()，更快。
    """
    df = load_daily(code)
    if df is None or df.empty:
        return None
    arr = _MEM.get(("dates", code))
    if arr is None:
        arr = df["date"].to_numpy()
        _MEM[("dates", code)] = arr
    i = int(np.searchsorted(arr, np.str_(end_date), side="right"))
    if i <= 0:
        return None
    lo = max(0, i - count)
    return df.iloc[lo:i].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# 财务
# ═══════════════════════════════════════════════════════════════════════════
def fin_path(code: str) -> str:
    return os.path.join(FIN, f"{code.replace('.', '_')}.parquet")


def load_financial(code: str) -> pd.DataFrame | None:
    key = ("fin", code)
    with _LOCK:
        if key in _MEM:
            return _MEM[key]
    p = fin_path(code)
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    with _LOCK:
        _MEM[key] = df
    return df


def save_financial(code: str, df: pd.DataFrame) -> None:
    with _LOCK:
        df.to_parquet(_p(fin_path(code)), index=False)
        _MEM.pop(("fin", code), None)


def financial_asof(code: str, date: str) -> dict | None:
    """取 date 时点【已公告】的最新一期财务。

    ★ 关键：用 pubDate（公告日）过滤，而不是 statDate（报告期）。
      否则会用到"还没公布"的财报 —— 典型前视偏差。
    """
    df = load_financial(code)
    if df is None or df.empty or "pubDate" not in df.columns:
        return None
    ok = df[df["pubDate"].astype(str) <= date]
    if ok.empty:
        return None
    ok = ok.sort_values("pubDate")
    return ok.iloc[-1].to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# 指数成分
# ═══════════════════════════════════════════════════════════════════════════
def idx_path(index_code: str) -> str:
    return os.path.join(IDX, f"{index_code.replace('.', '_')}.json")


def load_index(index_code: str) -> dict:
    """返回 {快照日期: [成分代码, ...]}"""
    p = idx_path(index_code)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_index(index_code: str, snaps: dict) -> None:
    with _LOCK:
        with open(_p(idx_path(index_code)), "w", encoding="utf-8") as f:
            json.dump(snaps, f, ensure_ascii=False)


def index_members_asof(index_code: str, date: str) -> list[str]:
    """取 date 时点生效的成分（用不晚于 date 的最近快照）。"""
    snaps = load_index(index_code)
    if not snaps:
        return []
    keys = sorted(k for k in snaps if k <= date)
    if not keys:
        return []
    return list(snaps[keys[-1]])


# ═══════════════════════════════════════════════════════════════════════════
# 资金流
# ═══════════════════════════════════════════════════════════════════════════
# 【单位换算】Tushare `net_mf_amount` 的原生单位是【万元】。
# d4_rebuild_flow_tushare.py 按其审计结论「不写 ×1e4」，即 parquet 里是原始万元；
# 而引擎与策略两端都按【元】工作：
#     parquet(元) --load_flow /1e4--> net_amount_main(万元) --策略 ×1e4--> 元 --/1e8--> 亿
# 直接读万元会让整条链小 1e4 倍。实测（600000 @ 2020-01-07）：
#     parquet -2294.55(万元) -> net_amount_main -0.229455 -> 日志显示 -0.00 亿
#     全市场日均显示 "0.00 亿"，而本仓库旧注释记录的历史值是 -101.95 亿
# 后果：板块资金层整层全灭（门槛 -0.000 亿，各板块 -0.00 亿），候选全部被挡。
# 处理：读入时乘回 1e4（写回时除回去），parquet 永久保持 Tushare 原始口径。
# 若将来数据改为直接存元，把本常数改为 1.0 即可（save_flow 会自动对称）。
FLOW_RAW_SCALE = 1e4
_FLOW_SCALED_COLS = ("netamount", "r0_net")


def flow_path(code: str) -> str:
    return os.path.join(FLOW, f"{code.replace('.', '_')}.parquet")


def load_flow(code: str) -> pd.DataFrame | None:
    key = ("flow", code)
    with _LOCK:
        if key in _MEM:
            return _MEM[key]
    p = flow_path(code)
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if FLOW_RAW_SCALE != 1.0:
        for _c in _FLOW_SCALED_COLS:
            if _c in df.columns:
                df[_c] = pd.to_numeric(df[_c], errors="coerce") * FLOW_RAW_SCALE
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
    with _LOCK:
        _MEM[key] = df
    return df


def save_flow(code: str, df: pd.DataFrame) -> None:
    with _LOCK:
        df = df.copy()
        # 与 load_flow 对称：落盘前换算回 Tushare 原始口径（万元）
        if FLOW_RAW_SCALE != 1.0:
            for _c in _FLOW_SCALED_COLS:
                if _c in df.columns:
                    df[_c] = pd.to_numeric(df[_c], errors="coerce") / FLOW_RAW_SCALE
        if "date" in df.columns:
            df["date"] = df["date"].astype(str)
            df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        df.to_parquet(_p(flow_path(code)), index=False)
        _MEM.pop(("flow", code), None)


# ═══════════════════════════════════════════════════════════════════════════
# 抓取进度（断点续传）
# ═══════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    if not os.path.exists(STATE):
        return {}
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save_state(st: dict) -> None:
    with _LOCK:
        with open(_p(STATE), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)


def mark_done(kind: str, code: str, st: dict | None = None) -> None:
    st = st if st is not None else load_state()
    st.setdefault(kind, {})
    st[kind][code] = True
    save_state(st)


def is_done(kind: str, code: str, st: dict | None = None) -> bool:
    return bool((st if st is not None else load_state()).get(kind, {}).get(code))


# ═══════════════════════════════════════════════════════════════════════════
# 概览
# ═══════════════════════════════════════════════════════════════════════════
def summary() -> dict:
    def n(d, ext):
        if not os.path.isdir(d):
            return 0
        return sum(1 for f in os.listdir(d) if f.endswith(ext))
    cal = load_calendar()
    return {
        "数据根目录": DATA_ROOT,
        "交易日历": f"{len(cal)} 天  {cal[0] if cal else '-'} ~ {cal[-1] if cal else '-'}",
        "日线文件": n(daily_dir(), ".parquet"),
        "财务文件(本地)": n(FIN, ".parquet"),
        "资金流文件": n(FLOW, ".parquet"),
        "估值文件(东财)": n(os.path.join(DATA_ROOT, "valuation"), ".parquet"),
        "财务文件(东财)": n(os.path.join(DATA_ROOT, "financial_em"), ".parquet"),
        "指数成分": ", ".join(os.path.splitext(f)[0]
                          for f in (os.listdir(IDX) if os.path.isdir(IDX) else [])),
        "股票列表": len(load_securities()),
    }


def clear_memory_cache(keep_snap: bool = True):
    """清空 DataFrame 缓存。默认【保留】快照数组缓存 _SNAP ——
    它体积小（11 列 float32，约 22KB/只）且是全市场每日复用的热数据。"""
    with _LOCK:
        _MEM.clear()
        if not keep_snap:
            _SNAP.clear()


def pct_at_dates(code: str, dates):
    """批量取某只票在给定日期序列上的涨跌幅（%）。

    ★ 性能关键（cProfile 实测）：
        资金流预取要为每只票的每个日期算 change_pct。早期实现逐日调用
        _pct_from_price（内含一次全列比较），又被调用 101 万次 ->
        单函数 172 秒（占回测 42%）。
        这里改为【一次 searchsorted 数组】，把 N 次二分合并成一次 C 层调用。
        另外直接用日线里已算好的 pctChg 列，避免重复算除法。
    """
    arr, cols = snap_arrays(code)
    if arr is None or cols is None or len(dates) == 0:
        return None
    pc = cols.get("pctChg")
    if pc is None:
        return None
    want = np.asarray([np.str_(str(d)[:10]) for d in dates])
    idx = np.searchsorted(arr, want, side="left")
    ok = (idx < len(arr))
    out = np.full(len(want), np.nan, dtype="float64")
    if ok.any():
        ii = idx[ok]
        hit = arr[ii] == want[ok]
        sel = np.where(ok)[0][hit]
        out[sel] = pc[ii[hit]]
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 资金流数组缓存（性能关键）
# ═══════════════════════════════════════════════════════════════════════════
# 实测：load_flow 首次读 parquet 需 1.28ms/只。策略每天要为全市场每只票
#       取资金流，5,000 只 × 1,637 天 -> 数小时。
#       这里把 (date, netamount) 两个 numpy 数组单独缓存，绕开 DataFrame。
_FLOW = {}


def flow_arrays(code: str):
    """返回 (date 数组, netamount 数组)。带独立缓存。"""
    v = _FLOW.get(code)
    if v is not None:
        return v
    f = load_flow(code)
    if f is None or f.empty:
        _FLOW[code] = (None, None)
        return None, None
    d = f["date"].astype(str).to_numpy()
    n = (pd.to_numeric(f["netamount"], errors="coerce").to_numpy(dtype="float64")
         if "netamount" in f.columns else None)
    _FLOW[code] = (d, n)
    return d, n
