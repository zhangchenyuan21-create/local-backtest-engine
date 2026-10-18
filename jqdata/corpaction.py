"""公司行动与缺失行情处理 —— P0 重做（v2，2026-10-17 06:10）

★ 为什么 v1 被否决（第三轮审计）
  v1 在【数据终点后第一个交易日】按最后收盘价强制卖出、扣费、释放现金。
  审计指出：公告规定的是 2023-03-17 终止上市、按 1:0.6647 换股；
  **公告并未赋予持有人"02-08 按旧价卖出变现"的交易机会**。
  ⇒ v1 把"永久冻结旧价"换成了"过早获得可再投资现金"，同样改变后续选股/仓位/收益，
    而且它【不是真实成交】，不能记为 sell。
  另：v1 的 classify 以"文件里此后是否还有行情"判定终止 ⇒ **使用了决策日尚不可得的未来数据**；
  边界反例：600387 最后行情 2025-07-04、元数据 end_date 2025-07-10 ⇒
  v1 会在 07-07 就强制了结，早于真实事件。
  ⇒ "行情末日后首个交易日 = 权益可变现日"这条通用规则不成立。

★ v2 的两步策略（采纳审计建议）
  第一步（本模块默认行为）：**缺行情绝不被静默当作正常可交易价格**
     · `has_quote(code,date)` / `tradable(code,date)` —— 只依赖【当日及以前】的信息
     · 持仓在无行情日进入 `restricted`（受限）状态：不可交易、按最后已知收盘价挂账，
       但**记录报价日期与状态**，且【不了结、不释放现金】
     · 估值记录写入 `VALUATION`，供验收核查
     · 期末仍存在受限持仓 ⇒ 该次回测【不能用于最终净值裁决】（验收必须失败）
  第二步（需外部数据，尚未实现）：依据【带生效日期的公告事件】处理换股/现金选择权/清算
     · 002013：应模拟"停牌期间不可交易 → 换股后按 1:0.6647 转为存续方 → 按新证券可交易时点"
     · 数据缺失时，把该笔标为"无法准确结算 ⇒ 阻断最终净值裁决"

★ 敏感性情景（非主回测事实）
  `JQSHIM_CA_MODE=settle_last_price` 时启用 v1 式的"按最后价变现"，
  仅用于回答"若当时可变现会怎样"，**不得作为主回测结果**。
"""
from __future__ import annotations

import json
import os

from . import store as _store
from . import jqlog as log

_END_CACHE: dict[str, str | None] = {}
_LASTBAR_CACHE: dict[str, str | None] = {}
VALUATION: list[dict] = []        # 每日受限持仓的估值记录（供验收）
DAYS_RESTRICTED: dict[str, int] = {}
UNRESOLVED: dict[str, dict] = {}  # 截止当前【仍未解决】的受限标的（阻断裁决只看这个）
RESOLVED: list[dict] = []         # 运行中已被解决（换股/复牌/卖出）的受限记录
SETTLE_LOG: list[dict] = []       # 仅敏感性情景使用
LOCKED_AT_END: dict = {}          # 期末仍未解锁的批次 {(code, 解锁日): 股数}
LOCKED_LOGGED: set = set()        # 锁定日志去重（按日期）
SELLABLE_AT_END: dict = {}        # 期末各持仓的 可卖/锁定 股数明细
ODD_LOT_UNRESOLVED: list[dict] = []  # 零股（结果为 0 股）未决权益（阻断裁决）
FRACTIONAL_UNRESOLVED: list[dict] = []  # 小数余数未决权益（余股分配未接入）
PERF_INVALID_FROM: dict = {}          # 有效绩效自该日起终止（裁定 2 方案 c）
FIRST_IMPACT: dict = {}               # {code: 首次影响日} —— 首次登记后不覆盖
VALUATION_PENDING: list[dict] = []  # 已取得证券权利但估值待解决（缺存续方行情）
CA_MODE = (os.environ.get("JQSHIM_CA_MODE") or "restrict").strip().lower()


# ═══════════════════════════════════════════════════════════════════════════
# 统一事件记录契约（第 13 轮复核后定稿）
#   · 历史行 vs 期末有效行分开：status=open/closed
#   · 自然键索引：RESTR→受限期间起始日；LOCK→计划解锁日；其余→事件日期
#   · summary 的期末计数【全部从 EVENTS 的 open 记录派生】
# ═══════════════════════════════════════════════════════════════════════════
EVENTS: list[dict] = []
EVENT_SEQ: dict = {}
RUN_ID: str = ""
_EVENT_KEY: dict = {}          # {(类, 自然键): EVENTS 下标}
RESTR_OPEN: dict = {}          # {code: 当前受限期间起始日}（按期间建索引）

EVENT_COLUMNS = ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "planned_resolved_date", "resolved_date",
                 "holding_qty", "missing_data", "diagnostic_assumption",
                 "resolution_basis", "lot_or_entitlement", "parent_event_id",
                 "source"]

# 每类必填列（不适用者写明确空值；status=closed 的行 required 见函数注释）
EVENT_REQUIRED = {
    "RESTR":    ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "holding_qty", "missing_data", "source"],
    "LOCK":     ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "planned_resolved_date", "holding_qty",
                 "lot_or_entitlement", "source"],
    "FRAC":     ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "holding_qty", "missing_data",
                 "lot_or_entitlement", "source"],
    "ODDLOT":   ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "holding_qty", "missing_data",
                 "lot_or_entitlement", "source"],
    "VAL":      ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "holding_qty", "missing_data",
                 "diagnostic_assumption", "source"],
    "CONVFAIL": ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "missing_data", "source"],
    "RESOLVED": ["event_id", "event_type", "code", "status", "event_date",
                 "first_impact_date", "resolved_date", "resolution_basis",
                 "parent_event_id", "source"],
}

EVENT_TYPE_CN = {"RESTR": "期末受限持仓（缺行情）", "LOCK": "锁定批次",
                 "FRAC": "换股小数余数", "ODDLOT": "换股零股余数",
                 "VAL": "换股缺存续方行情", "CONVFAIL": "公司行动转换失败",
                 "RESOLVED": "已解决（留档）"}

# 期末计数取【open】记录的类别（RESOLVED 单独计 closed 的历史解除数）
OPEN_CLASSES = ("RESTR", "LOCK", "FRAC", "ODDLOT", "VAL", "CONVFAIL")


def _nat_key(cls: str, code: str, *, event_date: str,
             planned: str = "", batch: str = "") -> tuple:
    """自然键：RESTR 用受限期间起始日；LOCK 用计划解锁日；其余用事件日期。"""
    if cls == "RESTR":
        # 按【独立受限期间】建索引：event_date 即本期起始日
        return (cls, code, event_date)
    if cls == "LOCK":
        # ★ 批次标识：同券同解锁日的不同批次必须区分
        return (cls, code, planned or event_date, batch or "")
    return (cls, code, event_date)


def event_put(cls: str, code: str, *, event_date: str,
              first_impact: str = "", planned_resolved: str = "",
              resolved_date: str = "", qty="", missing="", assume="",
              basis="", lot="", source="corpaction", status="open",
              parent_event_id="", update=False, batch: str = "") -> dict:
    """写入或就地更新一条事件；update=True 时只更新数量/状态，不覆盖日期与 ID。"""
    key = _nat_key(cls, code, event_date=event_date,
                   planned=planned_resolved, batch=batch)
    if update and key in _EVENT_KEY:
        rec = EVENTS[_EVENT_KEY[key]]
        if qty != "":
            rec["holding_qty"] = qty
        if lot:
            rec["lot_or_entitlement"] = lot
        if status:
            rec["status"] = status
        if resolved_date:
            rec["resolved_date"] = resolved_date
        return rec
    sk = (cls, code, event_date, planned_resolved, batch)
    EVENT_SEQ[sk] = EVENT_SEQ.get(sk, 0) + 1
    rec = {
        "event_id": (f"{RUN_ID or 'run'}-{cls}-{code}-{event_date}"
                     f"-{EVENT_SEQ[sk]:02d}" + (f"-b{batch}" if batch else "")),
        "event_type": EVENT_TYPE_CN.get(cls, cls), "code": code, "status": status,
        "event_date": event_date,
        "first_impact_date": first_impact or event_date,
        "planned_resolved_date": planned_resolved, "resolved_date": resolved_date,
        "holding_qty": qty, "missing_data": missing,
        "diagnostic_assumption": assume, "resolution_basis": basis,
        "lot_or_entitlement": lot, "parent_event_id": parent_event_id,
        "source": source,
    }
    EVENTS.append(rec)
    _EVENT_KEY[key] = len(EVENTS) - 1
    return rec


def close_event(cls: str, code: str, date: str, reason: str,
                *, key_date: str = "", planned: str = "", batch: str = "") -> bool:
    """关闭一条 open 记录（置 closed + 记实际解除日）。返回是否命中。"""
    key = _nat_key(cls, code, event_date=(key_date or date), planned=planned,
                   batch=batch)
    idx = _EVENT_KEY.get(key)
    if idx is None:
        return False
    rec = EVENTS[idx]
    if rec.get("status") != "open":
        return False
    rec["status"] = "closed"
    rec["resolved_date"] = date
    rec["resolution_basis"] = reason
    # 写一条 RESOLVED 留档，带 parent_event_id 指向原记录
    event_put("RESOLVED", code, event_date=rec["event_date"],
              first_impact=rec.get("first_impact_date", ""),
              resolved_date=date, qty=rec.get("holding_qty", ""),
              basis=reason, parent_event_id=rec["event_id"], status="closed",
              source="corpaction.RESOLVED")
    return True


def events_by_class(status: str | None = None) -> dict:
    """按类返回记录；status="open" 只返回期末仍有效的。"""
    out = {k: [] for k in EVENT_TYPE_CN}
    for r in EVENTS:
        if status and r.get("status") != status:
            continue
        for k, cn in EVENT_TYPE_CN.items():
            if r["event_type"] == cn:
                out[k].append(r)
                break
    return out


def event_counterpart(cls: str, status: str | None = None) -> int:
    return len(events_by_class(status).get(cls, []))

def _t_closeable(pos, date: str) -> int:
    """与交易执行同一口径的可卖量（含 T+1 与公司行动锁定期）。"""
    try:
        from . import trade as _t
        return int(_t.closeable_of(pos, date))
    except Exception:
        return int(getattr(pos, "closeable_amount", 0) or 0)


def scan_locked(ctx, date: str):
    """逐日扫描仍处于锁定期的持仓批次；到期/不再持有 ⇒ 关闭对应 LOCK 记录。

    与"受限(缺行情)"分开：受限=缺行情，锁定=有行情但不可卖。
    第 13 轮：区分【计划解锁日】planned_resolved_date 与【实际解除日】resolved_date；
    到期并核实可卖后置 status=closed，再按 open 记录对账。
    """
    pf = ctx.portfolio
    cur = {}
    for code, pos in pf.positions.items():
        if pos is None or pos.total_amount <= 0:
            continue
        for lot in (getattr(pos, "lots", None) or []):
            try:
                q = int(lot[1])
                lk = lot[2] if len(lot) > 2 else None
            except Exception:
                continue
            if q <= 0 or not lk or date >= str(lk):
                continue
            # ★ 批次身份：优先用稳定 lot_id；缺失时退化为买入日
            _batch = str(lot[3]) if len(lot) > 3 else str(lot[0])
            cur[(code, str(lk), _batch)] = cur.get((code, str(lk), _batch), 0) + q
    for (code, unlock, batch), qty in cur.items():
        # LOCK 登记：first_impact_date = 锁定开始日；planned = 计划解锁日；batch = 批次买入日
        event_put("LOCK", code, event_date=date, first_impact=date,
                  planned_resolved=unlock, qty=qty,
                  missing="解锁日之前不可交易", assume="按上市流通日锁定",
                  basis="解锁后自动解除", lot=f"unlock={unlock};batch={batch}",
                  batch=batch, update=True)
    for r in list(EVENTS):
        if r.get("status") == "open" and r["event_type"] == EVENT_TYPE_CN["LOCK"]:
            _b = ""
            _lt = r.get("lot_or_entitlement") or ""
            if "batch=" in _lt:
                _b = _lt.split("batch=")[-1]
            if (r["code"], r.get("planned_resolved_date"), _b) not in cur:
                close_event("LOCK", r["code"], date, "锁定到期且已核实可卖",
                            key_date=r["event_date"],
                            planned=r.get("planned_resolved_date", ""), batch=_b)
    LOCKED_AT_END.clear()
    LOCKED_AT_END.update(cur)
    SELLABLE_AT_END.clear()
    for code, pos in pf.positions.items():
        if pos is None or pos.total_amount <= 0:
            continue
        _locked = sum(q for (c, _k, _b), q in cur.items() if c == code)
        _cl = _t_closeable(pos, date)
        SELLABLE_AT_END[code] = {"total": int(pos.total_amount),
                                 "closeable": int(_cl), "locked": int(_locked),
                                 "t1_unavailable": int(pos.total_amount)
                                                   - int(_cl) - int(_locked)}
    if cur and date not in LOCKED_LOGGED:
        for (code, lk, _b), q in cur.items():
            log.warn("[锁定批次] %s %s 有 %d 股锁定至 %s（有行情但不可卖）"
                     "⇒ 期末仍未解锁则不得用于最终净值裁决" % (date, code, q, lk))
        LOCKED_LOGGED.add(date)
    return cur


def mark_perf_invalid(date: str, reason: str):
    """记录首次会影响决策的未决事项 —— 有效绩效自该日起终止（裁定 2 方案 c）。"""
    if not PERF_INVALID_FROM:
        PERF_INVALID_FROM.update({"date": str(date), "reason": reason})
        log.error("[有效绩效终止] 自 %s 起存在会影响决策的未决事项：%s"
                  "⇒ 该日之后不得用于任何收益/风险比较（补齐可核实数据后重跑）"
                  % (date, reason))


ODD_LOT_POLICY = (os.environ.get("JQSHIM_CA_ODD_LOT") or "floor").strip().lower()


def _securities():
    root = os.environ.get("JQSHIM_DATA") or "data_ts"
    p = os.path.join(root, "securities.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def security_end(code: str):
    """证券元数据的 end_date（**元数据字段，仅用于报告与事后核对，不用于定价决策**）。"""
    if code not in _END_CACHE:
        _END_CACHE[code] = (_securities().get(code) or {}).get("end_date")
    return _END_CACHE[code]


def _bars_dates(code: str):
    if code not in _LASTBAR_CACHE:
        try:
            d = _store.load_daily(code)
            _LASTBAR_CACHE[code] = (d["date"].astype(str).str.slice(0, 10).tolist()
                                    if d is not None and len(d) else [])
        except Exception:
            _LASTBAR_CACHE[code] = []
    return _LASTBAR_CACHE[code]


def last_bar_date(code: str):
    ds = _bars_dates(code)
    return ds[-1] if ds else None


def has_quote(code: str, date: str) -> bool:
    """该标的在【date 当日】是否有行情。只依赖当日信息，无未来依赖。"""
    return date in set(_bars_dates(code))


def tradable(code: str, date: str) -> bool:
    """当日是否可交易 —— v2 只要求"当日有行情"，不看未来。"""
    return has_quote(code, date)


def is_buyable(code: str, date: str) -> bool:
    """兼容旧调用点：等价于 tradable。"""
    return tradable(code, date)


def quote_info(code: str, date: str):
    """返回 (估值价, 报价日期, 状态)。报价日期【上限为估值日】。

    状态：'ok'（当日有行情）｜ 'restricted'（当日无行情 ⇒ 按最后已知价挂账，不可交易）
    注意：v2 【不】判定"terminated"。是否为终止上市/换股，需要公告事件数据才能认定；
          在缺少该数据时，正确做法是标记为"无法准确结算"并阻断净值裁决，
          而不是替持有人选择"按旧价卖出"。
    """
    ds = _bars_dates(code)
    upto = [d for d in ds if d <= date]
    px = None
    if upto:
        try:
            d = _store.load_daily(code)
            m = d[d["date"].astype(str).str.slice(0, 10) <= date]
            if len(m):
                px = float(m["close"].iloc[-1])
        except Exception:
            px = None
    if has_quote(code, date):
        return px, date, "ok"
    return px, (upto[-1] if upto else None), "restricted"


def scan_restricted(ctx, date: str):
    """每日结算后调用：记录处于受限状态的持仓（无行情 ⇒ 不可交易但未了结）。

    ★ 默认【不了结、不释放现金】。截止期末仍未解决的受限持仓
      使该次回测无法用于最终净值裁决。

    ★ 修正（2026-10-17，自查发现的假阳性）：
      原先 `summary()` 直接汇总 VALUATION 的【全部历史记录】⇒
      标的即使已在运行中被正确解决（换股/复牌/卖出），仍会被计入
      `n_restricted_codes` ⇒ 验收把**处理正确**的回测误判为不合格
      （实测 `out/_ca_test5`：002013 受限 27 天后于 03-17 成功换股，
      但 summary 仍报 n_restricted_codes=1）。
      现改为维护【未解决集合 UNRESOLVED】：当日不再持有、或当日已有行情 ⇒ 移除。
      历史记录仍完整保留在 VALUATION / RESTRICTED_HISTORY 供审计。
    """
    pf = ctx.portfolio
    held = {c for c, p in pf.positions.items() if p is not None
            and p.total_amount > 0}
    # ① 先移除【已解决】的：不再持有（换股/卖出/了结）或当日已有行情（复牌）
    for c in list(UNRESOLVED):
        if c not in held or has_quote(c, date):
            _orig = UNRESOLVED.pop(c)
            _ep = RESTR_OPEN.pop(c, None)
            close_event("RESTR", c, date,
                        ("已不再持有" if c not in held else "已恢复行情"),
                        key_date=(_ep or _orig.get("date", date)))
            RESOLVED.append({**(_orig or {}), "resolved_on": date,
                             "reason": ("已不再持有" if c not in held else "已恢复行情")})
            # ★ 契约：RESOLVED 保留原首次影响日，另记解除日与依据

    # ② 再登记当日仍受限的
    out = []
    for code, pos in list(pf.positions.items()):
        if pos is None or pos.total_amount <= 0:
            continue
        px, qdate, st = quote_info(code, date)
        if st == "ok":
            continue
        # ★ 复核修正：接入事件驱动 status()（原先该函数在引擎里无调用方，
        #   terminated 态从未生效）。记录 suspended / terminated 以利诊断与审计。
        st = status(code, date)
        DAYS_RESTRICTED[code] = DAYS_RESTRICTED.get(code, 0) + 1
        FIRST_IMPACT.setdefault(code, date)   # ★ 首次登记后保持不变
        rec = {"date": date, "code": code, "qty": int(pos.total_amount),
               "px_used": px, "quote_date": qdate, "status": st,
               "sec_end": security_end(code),
               "days_restricted": DAYS_RESTRICTED[code]}
        VALUATION.append(rec)
        UNRESOLVED[code] = rec
        # ★ 契约：RESTR 首次缺行情日只写一次，之后只更新数量/状态
        _ep = RESTR_OPEN.setdefault(code, date)      # 本期起始日（按期间建索引）
        event_put("RESTR", code, event_date=_ep, first_impact=_ep,
                  qty=int(pos.total_amount), missing="当日行情",
                  assume="按最后已知价挂账、不可交易",
                  basis="恢复行情或取得可核实估值后重跑",
                  lot=f"quote_date={qdate}", update=True)
        out.append(rec)
        if DAYS_RESTRICTED[code] == 1:
            # ★ 复核第 9 轮：缺行情持仓【首次出现当天】就可能影响其它标的的仓位计算
            #   ⇒ 最早失效日不一定是换股日，此处即登记（取最早者）
            mark_perf_invalid(date, "缺行情持仓（%s）可能影响仓位计算" % code)
            log.warn("[受限持仓] %s %s 当日无行情 ⇒ 标记 restricted："
                     "按最后已知收盘价 %s（报价日 %s）挂账，**不可交易、不了结**；"
                     "证券元数据 end_date=%s。若期末仍未解决，本次回测不能用于最终净值裁决。"
                     % (date, code, px, qdate, security_end(code)))
    return out


def write_ledger(path: str) -> str:
    """写出未决登记簿 —— **完全从 EVENTS 派生**（与 corpaction.json 同源）。

    这样两者不可能口径不一致；13 列字段齐备；event_id 单次运行内唯一。
    """
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(EVENT_COLUMNS)
        for r in EVENTS:
            w.writerow([r.get(c, "") for c in EVENT_COLUMNS])
    return path


def summary():
    """给验收用的汇总。

    `restricted` / `n_restricted_codes` = 【期末仍未解决】的受限持仓
      ⇒ 只有这些阻断净值裁决（换股/复牌/卖出后解决的属正确处理，不计入）。
    `resolved_during_run` 与 `n_valuation_records` 完整保留供审计追溯。
    """
    return {"mode": CA_MODE,
            "restricted": events_by_class("open")["RESTR"],
            "locked_lots": events_by_class("open")["LOCK"],
            "fractional_unresolved": events_by_class("open")["FRAC"],
            "odd_lot_unresolved": events_by_class("open")["ODDLOT"],
            "valuation_pending": events_by_class("open")["VAL"],
            "conv_failures": events_by_class("open")["CONVFAIL"],
            "resolved_during_run": events_by_class()["RESOLVED"],
            "n_restricted_codes": event_counterpart("RESTR", "open"),
            "n_locked_lots": event_counterpart("LOCK", "open"),
            "n_fractional_unresolved": event_counterpart("FRAC", "open"),
            "n_odd_lot_unresolved": event_counterpart("ODDLOT", "open"),
            "n_valuation_pending": event_counterpart("VAL", "open"),
            "n_conv_failures": event_counterpart("CONVFAIL", "open"),
            "n_resolved": len(EVENTS) and event_counterpart("RESOLVED") or 0,
            "ledger_rows": len(EVENTS),
            "ledger_open_rows": len([r for r in EVENTS if r.get("status") == "open"]),
            # ★ 两个独立状态：期末是否仍未决 / 本次运行历史是否已受影响
            "currently_unresolved": len([r for r in EVENTS
                                        if r.get("status") == "open"]) > 0,
            "history_affected": bool(PERF_INVALID_FROM),
            "invalid_from": (PERF_INVALID_FROM.get("date") or None),
            "invalid_reason": (PERF_INVALID_FROM.get("reason") or None),
            "ledger_rows_by_class": {k: len(v) for k, v in events_by_class().items()},
            "ledger_open_by_class": {k: len(v)
                                     for k, v in events_by_class("open").items()},
            "perf_invalid_from": (PERF_INVALID_FROM.get("date") or None),
            "perf_invalid_reason": (PERF_INVALID_FROM.get("reason") or None),
            "sizing_caveat": ("存在待解决估值时，代理价/成本价可能影响"
                              "其它标的的仓位计算 ⇒ 本运行仅作受估值影响的"
                              "诊断运行，不得用于优劣裁决"),
            "n_odd_lot_unresolved": len(ODD_LOT_UNRESOLVED),
            "odd_lot_unresolved": list(ODD_LOT_UNRESOLVED),
            "valuation_pending": list(VALUATION_PENDING),
            "conversions": list(CONV_LOG),
            "conv_failures": list(CONV_FAILURES),
            "n_conv_failures": len(CONV_FAILURES),
            "settled": list(SETTLE_LOG)}


# ── 敏感性情景：按最后价变现（仅当 JQSHIM_CA_MODE=settle_last_price）─────────
def settle_terminated(ctx, date: str):
    if CA_MODE != "settle_last_price":
        return []
    from . import trade as _t
    pf = ctx.portfolio
    out = []
    for code in list(pf.positions.keys()):
        pos = pf.positions.get(code)
        if pos is None or pos.total_amount <= 0:
            continue
        if has_quote(code, date):
            continue
        px, qdate, _ = quote_info(code, date)
        if not px or px <= 0:
            continue
        qty = int(pos.total_amount)
        val = px * qty
        comm, stamp, transfer = _t._fee(val, px, qty, date, "sell", code)
        proceeds = val - comm - stamp - transfer
        pf.available_cash += proceeds
        pos.total_amount = 0
        pos.closeable_amount = 0
        pf.positions.pop(code, None)
        SETTLE_LOG.append({"date": date, "code": code, "quote_date": qdate,
                           "px": px, "qty": qty, "proceeds": proceeds,
                           "warning": "敏感性情景：按最后价变现，非真实成交"})
        log.warn("[公司行动·敏感性] %s %s 按最后价 %.4f 变现 %d 股（报价日 %s）"
                 " —— 这是敏感性情景，不得作为主回测事实"
                 % (date, code, px, qty, qdate))
    return out


def reset():
    VALUATION.clear()
    DAYS_RESTRICTED.clear()
    SETTLE_LOG.clear()
    UNRESOLVED.clear()
    RESOLVED.clear()
    CONV_LOG.clear()
    CONV_FAILURES.clear()
    ODD_LOT_UNRESOLVED.clear()
    FRACTIONAL_UNRESOLVED.clear()
    PERF_INVALID_FROM.clear()
    EVENTS.clear()
    EVENT_SEQ.clear()
    _EVENT_KEY.clear()
    RESTR_OPEN.clear()
    FIRST_IMPACT.clear()
    _APPLIED.clear()
    SELLABLE_AT_END.clear()
    LOCKED_AT_END.clear()
    VALUATION_PENDING.clear()

    global _CONV
    _CONV = None
    _END_CACHE.clear()
    _LASTBAR_CACHE.clear()
    _DELIST_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 事件驱动层（P0 第二步）：用【Tushare 权威日期】替换"看未来有没有行情"
# ═══════════════════════════════════════════════════════════════════════════
_DELIST_CACHE: dict[str, str | None] = {}
_CONV: dict | None = None
_APPLIED: set[str] = set()
CONV_LOG: list[dict] = []
CONV_FAILURES: list[dict] = []   # 转换失败（阻断净值裁决）


def _actions_path():
    root = os.environ.get("JQSHIM_DATA") or "data_ts"
    return os.path.join(root, "corpaction", "actions.csv")


def delist_date(code: str):
    """Tushare 的终止上市日（权威事件日，**不是**"数据文件到此为止"）。

    ★ 代码映射修正（2026-10-17，复核指出 .BJ 恒错）：
      原先写作 `code.split('.')[0] + ('.SH' if XSHG else '.SZ')`
      ⇒ 北交所 `XXXXXX.XBSE` 会被错映射为 `.SZ`（恒查不到）。
      现优先复用 `ts_client.ts_code()`（项目内已有的权威映射），失败时再退化。
    """
    if code not in _DELIST_CACHE:
        d = None
        try:
            ts_code = None
            try:
                import sys as _sys
                if os.path.dirname(os.path.abspath(__file__)) not in _sys.path:
                    _sys.path.insert(0, os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__))))
                from ts_client import ts_code as _tsc   # 项目内权威映射
                ts_code = _tsc(code)
            except Exception:
                _suf = {"XSHG": ".SH", "XSHE": ".SZ", "XBSE": ".BJ"}
                ts_code = code.split(".")[0] + _suf.get(code.split(".")[-1], "")
            p = _actions_path()
            if os.path.isfile(p):
                import csv as _csv
                with open(p, encoding="utf-8") as fh:
                    for r in _csv.DictReader(fh):
                        if r.get("ts_code") == ts_code and \
                                r.get("event") == "终止上市":
                            d = str(r.get("effective_date"))[:10]
                            break
        except Exception:
            d = None
        _DELIST_CACHE[code] = (d[:4] + "-" + d[4:6] + "-" + d[6:8]
                               if d and len(d) == 8 and "-" not in d else d)
    return _DELIST_CACHE[code]


def conversions():
    global _CONV
    if _CONV is None:
        try:
            import conversions_table as _ct
            _CONV = _ct.load()
        except Exception:
            _CONV = {}
    return _CONV


def status(code: str, date: str) -> str:
    """事件驱动的三态判定（**不使用决策日之后的价格数据**）。

    ok          当日有行情
    suspended   当日无行情，且【尚未到终止上市日】（停牌/数据缺漏）
    terminated  已过【Tushare 权威的终止上市日】（事件日已知，属事件驱动而非未来函数）
    """
    if has_quote(code, date):
        return "ok"
    dl = delist_date(code)
    if dl and date > dl:
        return "terminated"
    return "suspended"


def apply_conversions(ctx, date: str):
    """在换股生效日把持仓按比例转为存续方；在新券上市日前保持不可交易。

    ★ 与 v1 的关键差异：**不产生现金**，只做证券转换（股数 × 比例）。

    ★ 复核修正（2026-10-17，三处严重问题）
      ① 【静默清零既有存续方持仓】：原先 `pf.positions[tgt] = np_` 无条件覆盖。
         若账户在换股日已持有存续方（本例 600372），这些股数会凭空消失
         （实测：原持 600372 1000 股 + 002013 4700 股 → 只剩 3124 股，权益 −17,378.68 元，无告警）。
         现改为【合并】：股数相加、成本按加权平均，并记日志。
      ② 【存续方缺行情时静默按 0 元估值】：原先 `px_tgt=0` 仍照转换
         （实测总资产 85,238 → 34,572，无异常无告警）。
         现在若取不到存续方价格 ⇒ **不转换**，保持原持仓为受限状态，
         记 log.error 并计入 CONV_FAILURES ⇒ 阻断净值裁决。
      ③ 【异常处理方向反了】：无关标的的坏行会让 KeyError 冒泡 ⇒ runner re-raise ⇒
         整次回测崩掉。现改为【逐标的 try/except】：坏行只影响该标的，
         记 log.error + CONV_FAILURES，并阻断裁决。
    """
    from . import trade as _t
    pf = ctx.portfolio
    out = []
    for code, cv in list(conversions().items()):
        try:
            if code in _APPLIED:
                continue
            if date != cv["swap_date"]:
                continue
            pos = pf.positions.get(code)
            if pos is None or pos.total_amount <= 0:
                _APPLIED.add(code)
                continue
            tgt = cv["target"]
            qty_old = int(pos.total_amount)
            # ★ 复核第 8 轮修正：改用【十进制精确运算】
            #   浮点会少记 1 股：90,000 × 0.6647 = 59822.99999999999
            #   ⇒ floor 记 59,822 股、余数报成 1.0 股未决（正确应为 59,823、余数 0）。
            from decimal import Decimal as _D, ROUND_FLOOR as _RF
            _ratio_d = _D(str(cv["ratio"]))
            _exact_d = _D(int(qty_old)) * _ratio_d
            qty_new = int(_exact_d.to_integral_value(rounding=_RF))
            _frac_d = _exact_d - _D(qty_new)
            _exact = float(_exact_d)
            _frac = float(_frac_d)
            # ★ 复核第 7 轮修正：**凡小数余数 > 0 的换股一律未决**
            #   公告按各股东小数尾数排序分配余股 ⇒ 单靠"持仓 × 比例"无法确定
            #   该账户最终是否多获 1 股（本例 4700 × 0.6647 = 3124.09）。
            #   故不能把 floor 结果当作完成；需接入实际登记结果后结算。
            if _frac_d > 0 and ODD_LOT_POLICY == "ceil":
                # 情景 B：余股向上取整（该账户多获 1 股）——仅用于敏感性对照
                qty_new += 1
                log.warn("[公司行动·换股·情景B] %s %s 小数余数 %.4f 按 ceil 取整 ⇒ %d 股"
                         "（仅用于余股分配敏感性对照，须与情景 A 分别报告）"
                         % (date, code, float(_frac_d), qty_new))
            if _frac_d > 0:          # 精确判定：余数 > 0 才未决
                mark_perf_invalid(date, "换股小数余数未决（余股分配未接入）")
                FRACTIONAL_UNRESOLVED.append(
                    {"date": date, "code": code, "target": tgt, "qty_old": qty_old,
                     "ratio": cv["ratio"], "exact": round(_exact, 6),
                     "qty_floor": qty_new, "fraction": round(_frac, 6),
                     "reason": "换股存在小数余数，余股分配办法（按尾数排序）未接入 ⇒ "
                               "该账户最终股数未定，登记未决权益"})
                event_put("FRAC", code, event_date=date,
                          first_impact=FIRST_IMPACT.get(code, date),
                          qty=qty_old, missing="账户级余股分配登记结果",
                          assume="floor=情景A / ceil=情景B（均仅诊断）",
                          basis="取得账户级登记结果后解除",
                          lot=f"{qty_old}x{cv['ratio']}={round(_exact,6)}")
                log.error("[公司行动·换股] %s %s %d 股 × %.4f = %.4f 股（小数余数 %.4f）"
                          "⇒ 余股分配未接入 ⇒ 登记【未决权益】并阻断净值裁决"
                          % (date, code, qty_old, float(cv["ratio"]), _exact, _frac))
            if qty_new <= 0:
                # ★ 复核第 6 轮修正：零股余数有专门分配办法，不能把向下取整
                #   当作完整换股结果、更不能静默标记事件已处理（原股仍挂在账上）。
                #   登记为【未决权益】并阻断裁决，待核定分配规则后再结算。
                mark_perf_invalid(date, "换股零股未决（分配办法未建模）")
                ODD_LOT_UNRESOLVED.append(
                    {"date": date, "code": code, "target": tgt, "qty_old": qty_old,
                     "ratio": cv["ratio"], "qty_floor": 0,
                     "reason": "换股后不足 1 股（零股余数），分配办法未建模 ⇒ 未决权益"})
                event_put("ODDLOT", code, event_date=date,
                          first_impact=FIRST_IMPACT.get(code, date),
                          qty=qty_old, missing="零股分配办法与账户级结果",
                          assume="保留原股不转换",
                          basis="取得分配办法与登记结果后解除",
                          lot=f"{qty_old}x{cv['ratio']}")
                log.error("[公司行动·换股] %s %s 持 %d 股 × %.4f ⇒ 不足 1 股"
                          "（零股余数）；已登记【未决权益】并阻断净值裁决，"
                          "**不标记事件完成**"
                          % (date, code, qty_old, float(cv["ratio"])))
                continue
            px_tgt = _t._close_on(tgt, date) or 0.0
            # ★ 复核第 4 轮修正：**证券权利转换应与估值取价分开**
            #   即使本地缺存续方行情，也应【记录已取得的证券权益】（股数照转），
            #   同时把估值列为待解决问题（阻断裁决）；而不是拒绝转换、
            #   更不是静默按 0 估值（后者曾使总资产 85,238→34,572）。
            _val_pending = px_tgt <= 0
            if _val_pending:
                # ★★ 复核第 5 轮修正：**绝不读取换股日之后的价格** ★★
                #   （虽被 VALUATION_PENDING 阻断最终裁决，但已影响后续仓位与下单判断）。
                #   现改用【截至当日可得】的代理值：
                #       代理每股价 = 源证券最后已知收盘价 / 换股比例
                #   经济含义：换股前的持仓价值折算到新券每股；源价 <= 换股日 ⇒ 无未来数据。
                px_src = _t._close_on(code, date) or 0.0     # 源证券截至换股日的最后收盘
                px_proxy = (px_src / float(cv["ratio"])) if px_src > 0 else 0.0
                px_tgt = px_proxy
                mark_perf_invalid(date, "换股缺存续方行情（估值待解决）")
                event_put("VAL", code, event_date=date,
                          first_impact=FIRST_IMPACT.get(code, date),
                          qty=qty_new, missing="存续方当日行情或可核实估值",
                          assume="代理价=源股最后收盘/比例",
                          basis="取得可核实行情后重跑",
                          lot=f"target={tgt}")
                VALUATION_PENDING.append(
                    {"date": date, "code": code, "target": tgt, "qty_new": qty_new,
                     "px_proxy": px_proxy, "px_source_last": px_src,
                     "proxy_basis": "源证券最后已知收盘价 / 换股比例（截至换股日可得，无未来数据）"
                     if px_proxy > 0 else "无可用代理值（连源价亦缺失）",
                     "reason": "已取得存续方权益，但换股日缺存续方行情 ⇒ 估值用代理值，仍需复核"})
                log.error("[公司行动·换股] %s %s → %s：已转入 %d 股（权利已取得）；"
                          "换股日缺存续方行情 ⇒ 估值用【截至当日可得的代理值】"
                          " 源价 %.4f / 比例 %.4f = %.4f（**未使用任何未来价格**）"
                          "⇒ 本次回测不得用于最终净值裁决"
                          % (date, code, tgt, qty_new, px_src, float(cv["ratio"]),
                             px_proxy))
            cost_old = float(getattr(pos, "cost_basis", 0.0) or 0.0) * qty_old
            pf.positions.pop(code, None)
            # 缺价时用【存续方最近可用收盘价】兜底（估值口径仍需 pending 复核）
            exist = pf.positions.get(tgt)
            if exist is not None and exist.total_amount > 0:
                # ① 合并而非覆盖
                q0 = int(exist.total_amount)
                c0 = float(getattr(exist, "cost_basis", 0.0) or 0.0) * q0
                exist.total_amount = q0 + qty_new
                exist.cost_basis = (c0 + cost_old) / exist.total_amount
                exist.lots = list(getattr(exist, "lots", []) or []) + \
                    [[date, qty_new, cv["list_date"]]]
                exist.price = px_tgt
                exist.closeable_amount = _t.closeable_of(exist, date)
                log.warn("[公司行动·换股] %s 存续方 %s 已有持仓 %d 股 ⇒ 合并"
                         "（转入 %d 股，合计 %d 股，成本加权）"
                         % (date, tgt, q0, qty_new, exist.total_amount))
            else:
                np_ = _t.Position(tgt)
                np_.total_amount = qty_new
                try:
                    np_.lots = [[date, qty_new, cv["list_date"],
                                 _t.next_lot_id()]]
                except Exception:
                    np_.lots = [[date, qty_new, cv["list_date"]]]
                np_.lock_until = cv["list_date"]
                np_.closeable_amount = 0 if date < cv["list_date"] else qty_new
                np_.price = px_tgt
                np_.cost_basis = cost_old / qty_new
                np_.init_time = getattr(pos, "init_time", None)
                pf.positions[tgt] = np_
            _APPLIED.add(code)
            rec = {"date": date, "code": code, "target": tgt, "qty_old": qty_old,
                   "qty_new": qty_new, "ratio": cv["ratio"], "px_target": px_tgt,
                   "list_date": cv["list_date"], "tradable_from": cv["list_date"],
                   "source": cv["source"]}
            CONV_LOG.append(rec)
            out.append(rec)
            log.warn("[公司行动·换股] %s %s %d 股 × %.4f → %s %d 股"
                     "（存续方价格 %.4f）；新券 %s 起可交易；比例来源：%s"
                     % (date, code, qty_old, cv["ratio"], tgt, qty_new, px_tgt,
                        cv["list_date"], cv["source"]))
        except Exception as _e:
            # ③ 逐标的隔离：坏行只影响该标的，并阻断裁决（不再让整次回测崩掉）
            mark_perf_invalid(date, "公司行动转换失败（%s）"
                              % type(_e).__name__)
            CONV_FAILURES.append({"date": date, "code": code,
                                  "reason": "%s: %s"
                                            % (type(_e).__name__, str(_e)[:80])})
            event_put("CONVFAIL", code, event_date=date,
                      first_impact=FIRST_IMPACT.get(code, date),
                      missing="失败原因与正确条款", assume="未转换（保持原持仓）",
                      basis="修复后重跑",
                      lot=f"{type(_e).__name__}: {str(_e)[:60]}")
            log.error("[公司行动·换股] %s %s 转换失败（%s: %s）⇒ 阻断净值裁决"
                      % (date, code, type(_e).__name__, str(_e)[:80]))
    return out
