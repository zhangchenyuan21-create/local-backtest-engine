"""板块数据兼容层 —— 行业 / 概念 列表与成分。

═══════════════════════════════════════════════════════════════════════════
策略需要的 4 个接口（静态提取）
═══════════════════════════════════════════════════════════════════════════
  get_industries(name="sw_l1", date=)   -> DataFrame(index=板块代码, 列含 "name")
      用法：[(idx, str(r["name"]).strip()) for idx, r in inds.iterrows()]
  get_concepts()                         -> DataFrame(index=概念代码, 列含 "name")
  get_industry_stocks(code, date=)       -> 成分股列表
  get_concept_stocks(code, date=)        -> 成分股列表

  discover_sectors 会把【全部申万一级】纳入候选池（不只 SECTOR_MAP 里那 12 个），
  所以行业清单必须完整。

═══════════════════════════════════════════════════════════════════════════
数据来源的现实约束（已实测）
═══════════════════════════════════════════════════════════════════════════
  ✗ baostock query_stock_industry() —— 是【证监会分类】(84 个，如 "C27医药制造业")，
    与申万一级【名字对不上】，无法映射。
  ✗ akshare —— 本机网络被拒（ConnectionReset）
  ✓ **聚宽 Data API**（get_industries / get_industry_stocks）—— 唯一同源选择，
    语义与回测完全一致。需激活后抓取并落盘。

  ⇒ 本模块负责：
      1) 从本地缓存读板块清单与成分（一次抓取，永久复用）
      2) 缓存缺失时给出【明确的降级警告】，而不是静默返回空
      3) 提供从聚宽 Data API 落盘的写入接口（见 fetch.py）
"""
from __future__ import annotations

import json
import os

import pandas as pd

from . import store
from . import jqlog as log

# ★ 运行时目录覆盖：支持「双数据源对比」。
#   现有数据用 data/sector/sw_l1.json（单日快照 2025-07-04）；
#   Tushare 用 data_ts/sector/sw_l1.json（按月历史快照）。
#   由环境变量 JQSHIM_SECTOR_DIR 切换，见 store.sector_dir()。
#
# 注意：这里用【函数】而不是模块级常量 —— 常量在 import 时求值一次，
# 之后改环境变量就失效了。函数每次调用都重新读，进程内可动态切换。
def _ind_dir() -> str:
    return store.sector_dir()
os.makedirs(_ind_dir(), exist_ok=True)


def _p(name):
    return os.path.join(_ind_dir(), f"{name}.json")


# ═══════════════════════════════════════════════════════════════════════════
# 列表：行业 / 概念
# ═══════════════════════════════════════════════════════════════════════════
def save_sector_list(kind: str, recs: dict) -> None:
    """kind ∈ {"sw_l1", "concept"}；recs = {板块代码: 板块名}"""
    with open(_p(kind), "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)


def load_sector_list(kind: str) -> dict:
    p = _p(kind)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def get_industries(name="sw_l1", date=None, **kw):
    """申万一级行业清单 -> DataFrame(index=板块代码, columns=["name"])"""
    recs = load_sector_list(name or "sw_l1")
    if not recs:
        log.warn("[板块数据] 本地无行业清单（%s）。需从聚宽 Data API 抓取："
                 "python -m jqdata.fetch_cli sectors" % name)
        return pd.DataFrame(columns=["name"])
    df = pd.DataFrame({"name": [recs[k] for k in recs]},
                      index=list(recs.keys()))
    df.index.name = "industry_code"
    return df


def concept_data_span() -> tuple[str | None, str | None, int]:
    """返回本地概念【成分】数据的 (最早日期, 最晚日期, 涉及概念数)。

    ═══════════════════════════════════════════════════════════════════════
    ⚠️ 已知数据局限（2026-10-15 14:50 实测）
    ═══════════════════════════════════════════════════════════════════════
    data/sector/concept/ 与 data_ts/sector/concept/ 各只有 11 个文件，
    快照日期并集只有 {2025-07-04}【一天】。
    ⇒ 回测全程概念板块的【成分】恒为空，只有名称表（concept.json，400 项）可用。

    尝试用 Tushare 重建：`ths_member` 返回列 = ['ts_code','con_code','con_name']
    【无 in_date / out_date】→ 无法重建历史快照。
    且部分概念指数成立很晚（先进封装 list_date=20220808、创新药=20221020），
    用其【当前成分】回溯更早期间属于【前视】。

    结论：本项目回测中概念板块【不参与选股】，候选池 = 31 个申万一级行业。
    这是【已知局限】而非 bug；如需概念板块须先补历史成分数据。
    ═══════════════════════════════════════════════════════════════════════
    """
    import glob as _g
    import json as _j
    d = os.path.join(_ind_dir(), "concept")
    fs = sorted(_g.glob(os.path.join(d, "*.json")))
    if not fs:
        return None, None, 0
    days = set()
    n_with = 0
    for f in fs:
        try:
            s = _j.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if s:
            n_with += 1
            days |= set(s.keys())
    if not days:
        return None, None, 0
    return min(days), max(days), n_with


def get_concepts(date=None, **kw):
    """概念板块清单 -> DataFrame(index=概念代码, columns=["name"])

    ★ 只返回【清单】，不保证有成分数据。成分缺失见 concept_data_span()。
    """
    recs = load_sector_list("concept")
    if not recs:
        log.warn("[板块数据] 本地无概念板块清单。需从聚宽 Data API 抓取："
                 "python -m jqdata.fetch_cli sectors")
        return pd.DataFrame(columns=["name"])
    df = pd.DataFrame({"name": [recs[k] for k in recs]},
                      index=list(recs.keys()))
    df.index.name = "concept_code"
    # 【显式声明局限】只提醒一次，避免刷屏
    global _CONCEPT_SPAN_WARNED
    if not _CONCEPT_SPAN_WARNED:
        _CONCEPT_SPAN_WARNED = True
        lo, hi, n = concept_data_span()
        if lo is None:
            log.warn("[板块发现·已知局限] 概念板块【无任何成分数据】"
                     "（本地 %d 个名称可用，成分全缺）→ 本回测概念板块不参与选股，"
                     "候选池 = 申万一级行业。原因：Tushare ths_member 无 "
                     "in/out 日期，无法重建历史快照。" % len(recs))
        else:
            log.warn("[板块发现·已知局限] 概念板块成分数据仅覆盖 %s ~ %s"
                     "（%d 个概念）→ 该区间外概念板块不参与选股。"
                     % (lo, hi, n))
    return df


_CONCEPT_SPAN_WARNED = False


# ═══════════════════════════════════════════════════════════════════════════
# 成分股
# ═══════════════════════════════════════════════════════════════════════════
def save_sector_members(kind: str, code: str, date: str,
                        members: list[str]) -> None:
    """按【日期快照】存成分，便于按 date 取当时生效的名单（避免幸存者偏差）。"""
    d = os.path.join(_ind_dir(), kind)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{code}.json")
    snaps = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                snaps = json.load(f)
        except Exception:
            snaps = {}
    snaps[str(date)[:10]] = list(members)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False)


def load_sector_members(kind: str, code: str, date: str) -> list[str]:
    """取 date 当日【生效】的板块成分（按日期快照，避免前视 / 幸存者偏差）。

    ═══════════════════════════════════════════════════════════════════════
    🔴 已修正的严重前视 Bug（2026-10-15 10:45，由子代理发现，已独立复现）
    ═══════════════════════════════════════════════════════════════════════
    旧代码：
        keys = sorted(k for k in snaps if k <= str(date)[:10])
        if not keys:
            keys = sorted(snaps)          # 注释写"没有更早的快照就用最早的"
        return list(snaps[keys[-1]])      # ← 实际取【最晚】的快照
    注释与代码相反。后果（实测）：
        data_ts/sector/sw_l1/*.json 的快照最早只到 2019-01-31，
        于是 2019-01-30 及之前的所有日期都取到【2026-10-08 的成分】：
            2015-01-05 → 103 只
            2019-01-30 → 103 只
            2019-01-31 →  95 只   ← 真实快照起点
            2026-10-08 → 103 只
            set(2015-07-08) == set(2026-10-08)  →  True
        → 2015-2018 用今天的行业成分选股，前视最长 7.8 年，
          并叠加幸存者偏差（2026 仍在成分里的才有完整价格/财务记录）。

    修正原则：
        ★ 宁可返回空列表（策略会报错/警告），也【绝不】静默取未来快照。
        ★ 若确实需要早于最早快照的成分，应【补建历史快照】，
          而不是用未来快照兜底。
    ═══════════════════════════════════════════════════════════════════════
    """
    p = os.path.join(_ind_dir(), kind, f"{code}.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            snaps = json.load(f)
    except Exception:
        return []
    if not snaps:
        return []
    d = str(date)[:10]
    keys = sorted(k for k in snaps if k <= d)
    if not keys:
        # 【修正】无更早快照 → 返回空，并留痕。
        # 绝不回退到 keys[-1]（那会取到最晚快照 = 未来数据）。
        _missing_snapshot_warn(kind, code, d, min(snaps))
        return []
    return list(snaps[keys[-1]])


# 前视防护的告警去重（同一 (kind, code) 只告警一次，避免刷屏）
_MISSING_WARNED: set[tuple[str, str]] = set()


def _missing_snapshot_warn(kind: str, code: str, date: str,
                           earliest: str) -> None:
    key = (kind, code)
    if key in _MISSING_WARNED:
        return
    _MISSING_WARNED.add(key)
    log.warn("[板块数据·前视防护] %s/%s 在 %s 无生效快照"
             "（本地最早快照 %s）→ 返回空名单。"
             "请补建历史快照，不要依赖未来快照。"
             % (kind, code, date, earliest))


def get_industry_stocks(industry_code, date=None, **kw):
    d = str(date)[:10] if date else "2100-01-01"
    r = load_sector_members("sw_l1", str(industry_code), d)
    if not r:
        log.warn("[板块数据] 本地无行业成分：%s @ %s" % (industry_code, d))
    return r


def get_concept_stocks(concept_code, date=None, **kw):
    d = str(date)[:10] if date else "2100-01-01"
    r = load_sector_members("concept", str(concept_code), d)
    if not r:
        log.warn("[板块数据] 本地无概念成分：%s @ %s" % (concept_code, d))
    return r


# ═══════════════════════════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════════════════════════
def status() -> dict:
    sw = load_sector_list("sw_l1")
    cp = load_sector_list("concept")
    d_sw = os.path.join(_ind_dir(), "sw_l1")
    d_cp = os.path.join(_ind_dir(), "concept")
    n_sw = len([f for f in os.listdir(d_sw)]) if os.path.isdir(d_sw) else 0
    n_cp = len([f for f in os.listdir(d_cp)]) if os.path.isdir(d_cp) else 0
    return {"申万一级板块数": len(sw), "概念板块数": len(cp),
            "有成分快照的行业数": n_sw, "有成分快照的概念数": n_cp}
