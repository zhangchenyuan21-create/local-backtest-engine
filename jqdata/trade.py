"""组合与撮合引擎 —— 自实现，因为聚宽不提供回测撮合。

═══════════════════════════════════════════════════════════════════════════
策略实际读取的属性（静态提取）
═══════════════════════════════════════════════════════════════════════════
  context.portfolio.total_value        4 处
  context.portfolio.positions_value    3 处
  context.portfolio.positions         10 处
  pos.total_amount                     4 处
  pos.closeable_amount                 1 处（T+1 判定）
  order_target / order_target_value    9 处

═══════════════════════════════════════════════════════════════════════════
撮合规则（对齐 A 股 + 聚宽默认）
═══════════════════════════════════════════════════════════════════════════
  · 成交价：**下一交易日开盘价**（策略在 09:31 决策，聚宽默认次bar成交）
    ⚠️ 若下一日停牌/涨停无法买入，本次下单失败（不成交）
  · 滑点：  买入 +slippage，卖出 -slippage（默认按配置）
  · 手续费：佣金（双向，最低 5 元）+ 印花税（卖出 0.05%，2023-08-28 起）
            过户费（沪深双向 0.001%）
  · 整手：  买入数量向下取整到 100 股；卖出可不足 100（清仓允许零股）
  · T+1：   当日买入的股票当日不可卖（closeable_amount）
  · 资金：  买入需现金充足，不足则按可用现金缩量
  · 涨跌停：开盘触及涨停不买 / 跌停不卖；可用 JQSHIM_LIMIT_MODE=one_price 放宽
  · 成交量：默认按当日 volume × order_volume_ratio(25%) 缩量，可关闭
"""
from __future__ import annotations

import os
import json
import datetime as dt
import math
from decimal import Decimal, ROUND_HALF_UP

from . import CONTEXT, normalize_code, store
from . import jqlog as log

# ═══════════════════════════════════════════════════════════════════════════
# 费率
# ═══════════════════════════════════════════════════════════════════════════
COMMISSION_RATE = 0.0003       # 佣金 万三
COMMISSION_MIN = 5.0           # 最低 5 元
TRANSFER_FEE = 0.00001         # 过户费 0.001%（双向）
STAMP_DUTY = 0.0005            # 印花税 0.05%（仅卖出）
STAMP_DUTY_CUT_DATE = "2023-08-28"   # 该日起 0.1% -> 0.05%
STAMP_DUTY_OLD = 0.001


def _stamp_duty(date: str) -> float:
    return STAMP_DUTY if str(date) >= STAMP_DUTY_CUT_DATE else STAMP_DUTY_OLD


def _transfer_fee_rate(date: str, security: str = "", amount_value=None,
                       qty=None) -> float:
    """过户费 —— 直接返回【应缴金额】（按日期与市场分段）。

    ★ 外部审计第二轮指出 v1 的历史基数有误（2026-10-16 22:45），现按中国结算通知口径：
      · 2015-08-01 之前：沪市按成交【面值】0.3‰（面值 1 元 ⇒ ≈ 股数 × 0.0003）；
                        深市按成交【金额】0.0255‰
        （v1 写成"沪市按成交金额 0.06‰、深市为 0" —— 基数与费率都不对）
      · 2015-08-01 ~ 2022-04-28：双向按成交金额 0.02‰（0.2 bps）
      · 2022-04-29 起：双向按成交金额 0.01‰（0.1 bps）
    影响量级：仅涉 2015-01-05 ~ 2015-07-31 约 7 个月，且差异 ≤ 0.1bps/边 ⇒ 可忽略；
              但口径正确性须修正。
    """
    d = str(date)[:10]
    av = float(amount_value or 0.0)
    if d >= "2022-04-29":
        return av * 0.00001
    if d >= "2015-08-01":
        return av * 0.00002
    if str(security).endswith("XSHG"):
        return float(qty or 0) * 0.0003        # 面值 ≈ 股数 × 1 元
    return av * 0.0000255                      # 深市按成交金额


# ═══════════════════════════════════════════════════════════════════════════
# 持仓 / 组合
# ═══════════════════════════════════════════════════════════════════════════
class Position:
    __slots__ = ("security", "total_amount", "closeable_amount", "price",
                 "cost_basis", "init_time", "last_buy_date", "lock_until", "lots")

    def __init__(self, security):
        self.security = security
        self.total_amount = 0
        self.closeable_amount = 0
        self.price = 0.0            # 最新价（用于市值）
        self.cost_basis = 0.0       # 摊薄成本
        self.init_time = None
        self.last_buy_date = None
        self.lock_until = None       # 兼容字段（实际以 lots 的锁定期为准）
        self.lots = []               # 批次: [[买入日, 股数, 锁定期至], ...]（P1-f）

    @property
    def value(self):
        return self.total_amount * self.price

    # ★★ 审计修正（2026-10-16 02:00，关卡2 复核 D1）★★
    #   JoinQuant 语义里 `position.avg_cost` 是【摊薄成本（含费用）/ 股】，
    #   策略此前拿不到它，只能用"建仓日前最近收盘价"作近似 ⇒ 成本价滞后 2 个交易日，
    #   使"浮盈≥0"退出门槛偏差中位 +0.757pt（与全区间净结果同量级，12.5% 门槛决策翻转）。
    #   引擎内部本来就维护了正确的摊薄成本（execute_buy 里 cost_basis，含佣金+过户费），
    #   这里按 JQ 字段名暴露出去，供策略在【成交后】取真实成本。
    @property
    def avg_cost(self):
        """摊薄成本（含费用）/ 股 —— JQ 兼容字段名。"""
        if self.total_amount:
            return self.cost_basis
        return 0.0

    # 便于策略判断"成本是否已是真实成交口径"（成交前 total_amount=0）
    @property
    def has_fill(self):
        return self.total_amount > 0

    def __repr__(self):
        return (f"<Position {self.security} amt={self.total_amount} "
                f"closeable={self.closeable_amount} px={self.price:.2f}>")


class Portfolio:
    def __init__(self, cash):
        self.starting_cash = float(cash)
        self.available_cash = float(cash)
        self.positions = {}
        self._price_cache = {}

    # ---- 估值 ----
    def mark_price(self, date: str | None = None):
        """按当收盘价给全部持仓估值。"""
        for code, pos in self.positions.items():
            px = _close_on(code, date) if date else None
            if px is None or px != px:
                px = pos.price or pos.cost_basis
            pos.price = float(px)

    @property
    def positions_value(self):
        return sum(p.value for p in self.positions.values())

    @property
    def total_value(self):
        return self.available_cash + self.positions_value

    def snapshot(self):
        return {"cash": self.available_cash,
                "positions_value": self.positions_value,
                "total_value": self.total_value,
                "n_pos": len(self.positions)}


def _close_on(code: str, date: str):
    d = store.load_daily(code)
    if d is None or d.empty:
        return None
    sub = d[d["date"] <= date]
    if sub.empty:
        return None
    return float(sub["close"].iloc[-1])


def _bar_on(code: str, date: str):
    """取指定交易日的整根日线；停牌/无数据返回 None。"""
    d = store.load_daily(code)
    if d is None or d.empty:
        return None
    hit = d[d["date"] == date]
    if hit.empty:
        return None
    return hit.iloc[0].to_dict()


def _open_on(code: str, date: str):
    """取指定交易日的开盘价。找不到（停牌）返回 None。"""
    bar = _bar_on(code, date)
    if bar is None:
        return None
    try:
        return float(bar.get("open"))
    except (TypeError, ValueError):
        return None


def _pre_close_on(code: str, date: str):
    """取 date 之前最近一根日线的收盘价，用于计算涨跌停价。"""
    d = store.load_daily(code)
    if d is None or d.empty:
        return None
    sub = d[d["date"] < date]
    if sub.empty:
        return None
    try:
        return float(sub["close"].iloc[-1])
    except (TypeError, ValueError):
        return None


def _limit_pct(code: str, date: str, bar: dict) -> float:
    """返回该股票当日的涨跌停比例。

    当前近似覆盖：
      - 主板 10%，ST 5%
      - 创业板 2020-08-24 起 20%，之前 10%
      - 科创板 20%
      - 北交所 30%

    未覆盖：新股上市前几日的无涨跌幅限制、退市整理期等特殊阶段。
    """
    num = str(code).split(".")[0]
    is_st = False
    raw_st = (bar or {}).get("isST", 0)
    if isinstance(raw_st, str):
        is_st = raw_st.strip().lower() in ("1", "true", "yes", "y")
    else:
        try:
            is_st = bool(int(raw_st))
        except (TypeError, ValueError):
            is_st = bool(raw_st)

    if str(code).endswith(".BJSE") or num.startswith(("4", "8")):
        return 0.30
    if num.startswith(("688", "689")):
        return 0.20
    if num.startswith(("300", "301")):
        if str(date) >= "2020-08-24":
            return 0.20 if not is_st else 0.20
        return 0.10 if not is_st else 0.05
    return 0.05 if is_st else 0.10


def _limit_prices(code: str, date: str, bar: dict):
    """返回 (涨停价, 跌停价)，按 A 股分位四舍五入到 0.01。"""
    pre = _pre_close_on(code, date)
    if pre is None or pre <= 0:
        return None, None
    pct = Decimal(str(_limit_pct(code, date, bar)))
    base = Decimal(str(pre))
    q = Decimal("0.01")
    up = (base * (Decimal("1") + pct)).quantize(q, rounding=ROUND_HALF_UP)
    down = (base * (Decimal("1") - pct)).quantize(q, rounding=ROUND_HALF_UP)
    return float(up), float(down)


def _limit_mode() -> str:
    env = os.environ.get("JQSHIM_LIMIT_MODE", "").strip().lower()
    if env:
        return env
    c = CONTEXT
    opts = getattr(c, "jq_options", {}) if c is not None else {}
    return str((opts or {}).get("limit_mode", "open")).strip().lower()


def _limit_blocked(code: str, date: str, side: str, bar: dict):
    """返回阻止成交的原因；未触发返回 None。

    模式：
      open       开盘价触及涨停/跌停即拒绝（默认，保守）
      one_price  只有全天封死在一字板才拒绝
      off        关闭涨跌停约束
    """
    mode = _limit_mode()
    if mode == "off":
        return None
    if mode not in ("open", "one_price"):
        mode = "open"
    up, down = _limit_prices(code, date, bar)
    if up is None or down is None:
        return None
    try:
        o = float(bar.get("open"))
        hi = float(bar.get("high", o))
        lo = float(bar.get("low", o))
    except (TypeError, ValueError):
        return None
    eps = 0.005
    if side == "buy":
        hit = (o >= up - eps) if mode == "open" else (hi <= up + eps and lo >= up - eps)
        if hit:
            return f"开盘涨停 {o:.2f}，涨停价 {up:.2f}"
    else:
        hit = (o <= down + eps) if mode == "open" else (hi <= down + eps and lo >= down - eps)
        if hit:
            return f"开盘跌停 {o:.2f}，跌停价 {down:.2f}"
    return None


def _volume_cap(bar: dict):
    """按当日成交量 × order_volume_ratio 返回最大可成交股数。

    返回 None 表示不限制；返回 0 表示当日无成交量。
    """
    c = CONTEXT
    opts = getattr(c, "jq_options", {}) if c is not None else {}
    opts = opts or {}
    if os.environ.get("JQSHIM_VOLUME_LIMIT", "").strip().lower() in ("0", "false", "off", "no"):
        return None
    if not bool(opts.get("match_with_volume_limit", True)):
        return None
    try:
        ratio = float(opts.get("order_volume_ratio",
                               os.environ.get("JQSHIM_VOLUME_RATIO", "0.25")))
    except (TypeError, ValueError):
        ratio = 0.25
    ratio = max(0.0, min(ratio, 1.0))
    try:
        volume = float(bar.get("volume") or 0)
    except (TypeError, ValueError):
        volume = 0.0
    if volume <= 0:
        return 0
    return int(volume * ratio)


# ═══════════════════════════════════════════════════════════════════════════
# 撮合
# ═══════════════════════════════════════════════════════════════════════════
def _fee(amount_value, price, qty, date, side, security=""):
    """返回 (佣金, 印花税, 过户费)。★ 过户费改为按日期分段（见 _transfer_fee_rate）。"""
    comm = max(COMMISSION_MIN, amount_value * COMMISSION_RATE)
    stamp = amount_value * _stamp_duty(date) if side == "sell" else 0.0
    transfer = _transfer_fee_rate(date, security, amount_value, qty)
    return comm, stamp, transfer


def _slippage_pct():
    """读取滑点比例。

    ⚠️ 健壮性要点：context.jq_options 可能被测试/调用方整体替换掉，
       所以这里对缺失、None、非 dict 都要兜底为 0.0，
       否则滑点会静默变成"读不到就用 0"，与配置不符却无任何提示。
    """
    c = CONTEXT
    if c is None:
        return 0.0
    opt = getattr(c, "jq_options", None)
    if not isinstance(opt, dict):
        return 0.0
    sp = opt.get("slippage")
    if sp is None:
        return 0.0
    if isinstance(sp, dict):
        v = sp.get("value")
        if v is None:
            v = getattr(sp.get("obj"), "value", None)
    else:
        v = getattr(sp, "value", None)
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _tick_size(px: float) -> float:
    """A 股最小报价单位（tick）。"""
    return 0.01


# ═══════════════════════════════════════════════════════════════════════════
# 数据驱动滑点模型（Corwin-Schultz 估计 + tick 下限）
# ═══════════════════════════════════════════════════════════════════════════
# 参数由 build_slip_model.py 从 out/cs_spread 的估计结果生成 out/slip_model.json。
# 模型：单边滑点 = max( CS相对价差/2 × 股价 , 1 个 tick )
#
# 为什么比"恒定 0.02 元"更可信（实测 5,757 只股票）：
#   恒定 0.02 元等价于"所有股票都吃 2 个 tick"，而 A 股 tick=0.01 是硬下限。
#     0~3  元股  CS单边 0.0014 元  -> 恒定值高估 14 倍
#     20~35 元股 CS单边 0.0192 元  -> 恰好约等于 0.02
#     60 元以上  CS单边 0.0646 元  -> 恒定值低估 3.2 倍
#   即恒定值不是偏高或偏低，而是【结构性错配】。
#
# 为什么取 max(CS, tick)：
#   Corwin-Schultz 在 65% 的窗口"失声"（单日波动淹没价差，α<0 被 clamp 到 0），
#   故其有效估计是有偏样本，应视为【下限】；1 个 tick 是物理下限。
#   两者取大：低价股由 tick 托住，高价股用 CS 估计。
_MODEL_CACHE: dict = {}


def _load_cs_model():
    """懒加载 out/slip_model.json 的阶梯表。失败返回 (None, None) 并回退。"""
    if "upper" in _MODEL_CACHE:
        return _MODEL_CACHE["upper"], _MODEL_CACHE["rel"]
    p = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "out", "slip_model.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        st = cfg.get("阶梯") or []
        _MODEL_CACHE["upper"] = [float(s["上限价"]) for s in st]
        _MODEL_CACHE["rel"] = [float(s["CS相对价差"]) for s in st]
        _MODEL_CACHE["tick"] = float(cfg.get("tick", 0.01))
    except Exception:
        _MODEL_CACHE["upper"] = None
        _MODEL_CACHE["rel"] = None
        _MODEL_CACHE["tick"] = 0.01
    return _MODEL_CACHE["upper"], _MODEL_CACHE["rel"]


def _slip_per_share(px: float) -> float:
    """按模型返回【每股滑点金额（元）】。

    ★ 为什么需要模型选择
      FixedSlippage(0.02) 是【每股 0.02 元】的恒定值。但真实滑点由买卖
      价差决定，而 A 股 tick = 0.01 元，价差不可能小于 1 个 tick。
      于是恒定 0.02 元实际等价于「所有股票都吃 2 个 tick」：
          2.6 元股   0.02/2.6 = 0.769%/边   1 tick 只需 0.385%
          95  元股   0.02/95  = 0.021%/边   1 tick 只需 0.011%
      这【高估了低价股、低估了高价股】。

    模型（环境变量 JQSHIM_SLIP_MODEL）：
      fix     每股 0.02 元（引擎原语义，默认，保持历史结果可比）
      tick    每股 1 个 tick = 0.01 元
      tick2   每股 2 个 tick = 0.02 元（等价 fix，对照确认）
      tiered  低价股加宽：<3 元 3 tick、3~8 元 2 tick、>=8 元 1 tick
      cs      ★ 数据驱动：max( CS相对价差/2 × 股价 , 1 tick )
    """
    m = (os.environ.get("JQSHIM_SLIP_MODEL") or "fix").strip().lower()
    t = _tick_size(px)
    if m == "tick":
        return max(t, 0.01)
    if m == "tick2":
        return max(t * 2.0, 0.01)
    if m == "cs":
        upper, rel = _load_cs_model()
        if upper and rel and px > 0:
            i = 0
            for i, u in enumerate(upper):
                if px < u:
                    break
            return max(rel[i] / 2.0 * px, _MODEL_CACHE.get("tick", 0.01))
        return max(t, 0.01)          # 模型文件缺失时回退到 1 tick
    if m == "tiered":
        mult = 3.0 if px < 3 else (2.0 if px < 8 else 1.0)
        return max(t * mult, 0.01)
    # fix：沿用策略里的 FixedSlippage 数值
    return _slippage_pct()


def _fill_price(px: float, direction: int) -> float:
    """按聚宽滑点语义算成交价。direction=+1 买入 / -1 卖出。

    ★ 默认按【绝对值（元）】语义 —— 对应策略里的 FixedSlippage(0.02)。

    聚宽有两个不同的类（这是关键，最初搞错了）：
        FixedSlippage(x)          x 是【价格单位】（元）
        PriceRelatedSlippage(x)   x 是【比例】

    策略写的是 set_slippage(FixedSlippage(0.02)) -> 0.02 元，不是 2%。
    实测同区间同策略、同一份数据（2020-01-01 ~ 2020-06-30）：
        2% 比例（错误）   总收益 -31.37%   单笔期望 -3.715%
        0.02 元（正确）   总收益  -1.26%   单笔期望 -0.657%
        0%  无滑点        总收益  -0.67%   单笔期望 -0.464%

    把 0.02 当 2% 会让 10 元股的滑点高估 10 倍，单笔期望被压低约 4pt，
    整条对账链（L1/L2/L5/L6）因此全部不达标。

    可用环境变量切换语义与模型（便于做敏感性对照）：
        JQSHIM_REL_SLIPPAGE=1        改为比例语义（历史对照实验用）
        JQSHIM_SLIP_MODEL=fix|tick|tick2|tiered
                                     每股滑点模型，见 _slip_per_share
    """
    if os.environ.get("JQSHIM_REL_SLIPPAGE", "") in ("1", "true", "True"):
        return px * (1.0 + _slippage_pct() * direction)
    return px + _slip_per_share(px) * direction


def execute_buy(code, qty, date, ref_px=None):
    """按 date 的开盘价买入 qty 股（已整手）。返回成交字典或 None。

    执行约束：
      - 停牌（无当日 bar）不成交
      - 开盘涨停不成交（可用 JQSHIM_LIMIT_MODE=one_price 放宽）
      - 成交量参与率上限（默认 25%）会缩量
      - 现金不足继续缩量
    """
    pf = CONTEXT.portfolio
    bar = _bar_on(code, date)
    if bar is None:
        log.warn("[撮合] %s %s 无当日行情（停牌/缺失），买入失败" % (date, code))
        return None
    try:
        px = float(bar.get("open"))
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        log.warn("[撮合] %s %s 开盘价无效，买入失败" % (date, code))
        return None
    why = _limit_blocked(code, date, "buy", bar)
    if why:
        log.warn("[撮合] %s %s %s，买入失败" % (date, code, why))
        return None
    fill = _fill_price(px, +1)
    qty = int(qty // 100 * 100)
    if qty <= 0:
        return None
    cap = _volume_cap(bar)
    if cap is not None and qty > cap:
        capped = int(cap // 100 * 100)
        if capped <= 0:
            log.warn("[撮合] %s %s 成交量约束，可买不足 1 手（cap=%s）"
                     % (date, code, cap))
            return None
        log.warn("[撮合] %s %s 成交量约束：%d -> %d 股"
                 % (date, code, qty, capped))
        qty = capped
    # 资金约束
    while qty > 0:
        val = fill * qty
        comm, _st, transfer = _fee(val, fill, qty, date, "buy", code)
        need = val + comm + transfer
        if need <= pf.available_cash + 1e-6:
            break
        qty -= 100
    if qty <= 0:
        log.warn("[撮合] %s %s 现金不足，买入失败（可用 %.2f）"
                 % (date, code, pf.available_cash))
        return None
    val = fill * qty
    comm, _st, transfer = _fee(val, fill, qty, date, "buy", code)
    cost = val + comm + transfer
    pf.available_cash -= cost

    pos = pf.positions.get(code)
    if pos is None:
        pos = Position(code)
        pf.positions[code] = pos
    old_amt = pos.total_amount
    pos.total_amount = old_amt + qty
    pos.cost_basis = ((pos.cost_basis * old_amt) + cost) / pos.total_amount
    pos.price = fill
    pos.init_time = pos.init_time or date
    pos.last_buy_date = date
    pos.lots.append([date, int(qty), None, next_lot_id()])
    pos.closeable_amount = closeable_of(pos, date)
    return {"date": date, "code": code, "side": "buy", "price": fill,
            "amount": qty, "value": val, "fee": comm + transfer,
            "cash_after": pf.available_cash}


_LOT_SEQ = [0]


def next_lot_id() -> int:
    """稳定批次标识（进程内单调自增）——同一证券同日可有多个独立批次。"""
    _LOT_SEQ[0] += 1
    return _LOT_SEQ[0]


def closeable_of(pos, date: str) -> int:
    """可卖量 = Σ 批次股数，条件为【买入日 < date】且【锁定期已过】。

    与聚宽 T+1 语义一致；锁定期用于公司行动（换股新券上市前不可交易）。
    批次缺失时（旧引擎创建的持仓）退化为 total_amount，保持兼容。
    """
    lots = getattr(pos, "lots", None)
    if not lots:
        return int(getattr(pos, "total_amount", 0) or 0)
    n = 0
    for lot in lots:
        try:
            d, q, lk = lot[0], int(lot[1]), (lot[2] if len(lot) > 2 else None)
        except Exception:
            continue
        if q <= 0:
            continue
        if str(d) >= str(date):
            continue                     # 当日买入 ⇒ T+1 不可卖
        if lk and str(date) < str(lk):
            continue                     # 锁定期内不可交易
        n += q
    return int(n)


def execute_sell(code, qty, date):
    """按 date 的开盘价卖出 qty 股。返回成交字典或 None。

    执行约束：
      - 停牌不成交
      - 开盘跌停不成交（可用 JQSHIM_LIMIT_MODE=one_price 放宽）
      - T+1 可卖量
      - 成交量参与率上限（默认 25%）会缩量；部分卖出保持整手
    """
    pf = CONTEXT.portfolio
    pos = pf.positions.get(code)
    if pos is None or pos.total_amount <= 0:
        return None
    bar = _bar_on(code, date)
    if bar is None:
        log.warn("[撮合] %s %s 无当日行情（停牌/缺失），卖出失败" % (date, code))
        return None
    why = _limit_blocked(code, date, "sell", bar)
    if why:
        log.warn("[撮合] %s %s %s，卖出失败" % (date, code, why))
        return None
    # T+1：先按可卖量封顶
    closeable = int(getattr(pos, "closeable_amount", pos.total_amount) or 0)
    qty = int(min(qty, closeable, pos.total_amount))
    if qty <= 0:
        log.warn("[撮合] %s %s 卖出被拒：可平仓量为 0（T+1，持仓 %d）"
                 % (date, code, pos.total_amount))
        return None
    cap = _volume_cap(bar)
    if cap is not None and qty > cap:
        capped = int(cap)
        if capped < pos.total_amount:
            capped = capped // 100 * 100
        if capped <= 0:
            log.warn("[撮合] %s %s 成交量约束，可卖不足 1 手（cap=%s）"
                     % (date, code, cap))
            return None
        log.warn("[撮合] %s %s 成交量约束：%d -> %d 股"
                 % (date, code, qty, capped))
        qty = min(qty, capped)
    try:
        px = float(bar.get("open"))
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        log.warn("[撮合] %s %s 开盘价无效，卖出失败" % (date, code))
        return None
    fill = _fill_price(px, -1)
    val = fill * qty
    comm, stamp, transfer = _fee(val, fill, qty, date, "sell", code)
    proceeds = val - comm - stamp - transfer
    pf.available_cash += proceeds
    pos.total_amount -= qty
    # 按 FIFO 消耗批次，再据剩余批次重算可卖量
    _left = qty
    _lots = getattr(pos, "lots", None) or []
    for _lt in _lots:
        if _left <= 0:
            break
        if str(_lt[0]) >= str(date):
            continue                      # 当日买入批次不可卖，跳过
        if len(_lt) > 2 and _lt[2] and str(date) < str(_lt[2]):
            continue                      # 锁定期内跳过
        _take = min(_left, int(_lt[1]))
        _lt[1] = int(_lt[1]) - _take
        _left -= _take
    pos.lots = [x for x in _lots if int(x[1]) > 0]
    pos.closeable_amount = closeable_of(pos, date)
    if pos.total_amount <= 0:
        pf.positions.pop(code, None)
    return {"date": date, "code": code, "side": "sell", "price": fill,
            "amount": qty, "value": val, "fee": comm + stamp + transfer,
            "cash_after": pf.available_cash}


# ═══════════════════════════════════════════════════════════════════════════
# 聚宽交易 API
# ═══════════════════════════════════════════════════════════════════════════
def _pending():
    c = CONTEXT
    if not hasattr(c, "pending_orders"):
        c.pending_orders = []
    return c.pending_orders


def order(security, amount, style=None, side=False, **kw):
    """正数买入，负数卖出。"""
    code = normalize_code(security)
    _pending().append({"code": code, "qty": int(amount)})
    return _mk_order(code, amount)


def _order_ref_price(code: str):
    """下单时用于【估算股数】的参考价 —— 只用 D 日已知信息。

    ★ 修正（2026-10-16 11:20，外部审计第二轮发现）：
      原实现 `order_value` / `order_target_value` 用
          px = _open_on(code, CONTEXT.next_date or CONTEXT.current_date)
      即**D 日下单时就读了 D+1 日的开盘价**来算股数 ⇒ 未来信息依赖
      （这与"策略指标只看昨天收盘"是两件事，不能互相覆盖）。
      且它使"目标金额"被精确实现：现实中按 D 日已知价估股数、按 D+1 开盘成交，
      实际金额会与目标有偏离 —— 那才是真实行为。
      现改为：优先取 **D−1 收盘**（与策略决策的信息集一致：策略只用截至前收的数据），
      缺失时退回 **D 日开盘**（09:31 时已发生、可视为已知）。

    ★ 边界修正（2026-10-16 22:45，外部审计指出 v1 仍有首日漏洞）：
      v1 写的是 `_close_on(code, CONTEXT.previous_date or CONTEXT.current_date)`，
      当首个交易日 `previous_date = None` 时会去取 **当天收盘价** —— 而信号在当天 09:31 产生
      ⇒ 首日仍构成未来信息依赖（隔离用例：当天开 10 / 收 11，函数返回 11）。
      v2：只有【确实存在前一交易日】时才读前收，否则直接用当天已知的开盘价。

    ★ 公司行动护栏（2026-10-17，P0 修复）：
      已终止（退市/换股合并/数据终止）或当日无行情的标的【不允许下单】。
      原先 `_close_on(code, prev)` 对终止标的会返回陈旧收盘价 ⇒ 仍能下单并建仓。
    """
    try:
        from . import corpaction as _ca
        _d = CONTEXT.current_date
        if _d and not _ca.is_buyable(code, _d):
            return None
    except Exception:
        pass
    if CONTEXT.previous_date:
        px = _close_on(code, CONTEXT.previous_date)
        if px:
            return px
    return _open_on(code, CONTEXT.current_date)


def order_value(security, value, style=None, side=False, **kw):
    code = normalize_code(security)
    px = _order_ref_price(code)
    if not px:
        return None
    qty = int(value / px // 100 * 100)
    _pending().append({"code": code, "qty": qty})
    return _mk_order(code, qty)


def order_target(security, amount, style=None, side=False, **kw):
    """调仓到目标【股数】。

    ★ 修正（2026-10-17，第三轮审计 P1-e）：必须计入【未成交订单】的净变动。
      原实现 `cur = pos.total_amount`（只算已成交持仓），而订单要到次日开盘才成交 ⇒
        持有 1000 股时连续两次 `order_target(code, 500)` 会提交【两张】卖出 500 股的单，
        次日合计卖出 1000 股 ⇒ 意外清仓（过度交易）。
      现改为 `d = target − (已成交持仓 + 在途订单净额)`，重复目标单第二次返回 None。
    """
    code = normalize_code(security)
    pf = CONTEXT.portfolio
    pos = pf.positions.get(code)
    cur = pos.total_amount if pos else 0
    pend = sum(int(o.get("qty", 0)) for o in _pending() if o.get("code") == code)
    d = int(amount) - (cur + pend)
    if d == 0:
        return None
    _pending().append({"code": code, "qty": d})
    return _mk_order(code, d)


def order_target_value(security, value, style=None, side=False, **kw):
    """调仓到目标【市值】。

    ★ 修正：股数用 D 日已知参考价估算（见 _order_ref_price），
      不再读 D+1 开盘价。实际成交仍发生在 D+1 开盘，成交金额会与目标有小幅偏离。
    """
    code = normalize_code(security)
    px = _order_ref_price(code)
    if not px:
        return None
    target_qty = int(value / px // 100 * 100)
    return order_target(code, target_qty, style=style, side=side, **kw)


def order_target_percent(security, percent, style=None, side=False, **kw):
    val = CONTEXT.portfolio.total_value * float(percent)
    return order_target_value(security, val)


def cancel_order(order_obj):
    return None


def get_position(security):
    return CONTEXT.portfolio.positions.get(normalize_code(security))


def get_orders(*a, **kw):
    return []


def get_open_orders(*a, **kw):
    return []


def _mk_order(code, qty):
    return {"security": code, "amount": qty, "action":
            "open" if qty > 0 else "close"}


# ═══════════════════════════════════════════════════════════════════════════
# 由 runner 在每日开盘时执行挂单
# ═══════════════════════════════════════════════════════════════════════════
def flush_orders(date: str):
    """把 pending_orders 用【当日开盘价】撮合掉。返回成交列表。"""
    fills = []
    orders = _pending()
    _pending_clear()
    for o in orders:
        code, qty = o["code"], o["qty"]
        if qty > 0:
            f = execute_buy(code, qty, date)
        elif qty < 0:
            f = execute_sell(code, -qty, date)
        else:
            f = None
        if f:
            fills.append(f)
    return fills


def _pending_clear():
    c = CONTEXT
    if hasattr(c, "pending_orders"):
        c.pending_orders = []
