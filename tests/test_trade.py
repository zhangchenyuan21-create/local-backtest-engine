"""撮合引擎验证 —— 用【自造数据】跑完整交易链路。

═══════════════════════════════════════════════════════════════════════════
为什么不用 baostock 了
═══════════════════════════════════════════════════════════════════════════
第一版用 baostock 抓真实日线来测撮合。后来并行抓取测试触发其风控，
本机 IP 被【永久拉黑】（code=10001011 黑名单用户），测试从此无法运行。

撮合逻辑本身与数据来源无关（它只消费价格），所以改为【自造确定数据】：
  · 完全不依赖网络，测试始终可跑
  · 价格手工设定，断言可以精确到分，比真实数据更严格
  · 不再有"外部源挂了导致测试红"的假故障

═══════════════════════════════════════════════════════════════════════════
验证项
═══════════════════════════════════════════════════════════════════════════
  ① 买入：整手取整、滑点、佣金(最低5元)、过户费
  ② 卖出：印花税(仅卖出)、佣金、过户费
  ③ T+1：当日买入当日不可卖（★ 曾经的真实 bug）
  ④ 资金约束：现金不足时自动缩量
  ⑤ order_target 的目标股数语义
  ⑥ 组合估值：total_value / positions_value
  ⑦ 高价股买不起 1 手时明确拒绝
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import traceback

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TEST_DIR)
HERE = ROOT
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ★ 测试隔离：本测试写自己的日线/日历，必须用独立数据目录
os.environ["JQSHIM_DATA"] = os.path.join(TEST_DIR, "data_test_trade")
# ★ 撮合测试用不复权价（价格是我手工设的，不需要复权）
os.environ["JQSHIM_NO_QFQ"] = "1"

import pandas as pd

import jqdata
from jqdata import store, jqlog, trade as jqt
import jqdata.setup as _s

ok = fail = 0
FAILS = []


def check(cond, msg, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {msg}" + (f"   {extra}" if extra else ""))
    else:
        fail += 1
        FAILS.append(msg)
        print(f"  FAIL  {msg}" + (f"   {extra}" if extra else ""))
        traceback.print_stack(limit=2)


# ═══════════════════════════════════════════════════════════════════════════
print("=" * 92)
print("① 自造确定数据")
print("=" * 92)
# 生成 20 个工作日
days = []
d = pd.Timestamp("2023-01-02")
while len(days) < 20:
    if d.weekday() < 5:
        days.append(d.strftime("%Y-%m-%d"))
    d += pd.Timedelta(days=1)
store.save_calendar(days)
print(f"    ✓ 交易日历 {len(days)} 天  {days[0]} ~ {days[-1]}")

# 两只票，价格手工设定（便于精确断言）
# A: 低价股，开盘价固定 10.00
# B: 高价股（茅台级），开盘价 1737.00
PRICES = {}
rows_a, rows_b = [], []
for i, day in enumerate(days):
    oa, ca = 10.00, 10.20
    ob, cb = 1737.00, 1740.00
    rows_a.append({"date": day, "open": oa, "high": max(oa, ca) * 1.01,
                   "low": min(oa, ca) * 0.99, "close": ca, "volume": 1_000_000,
                   "amount": 10_200_000.0, "turn": 1.0, "pctChg": 0.0,
                   "peTTM": 10.0, "pbMRQ": 1.0, "psTTM": 1.0,
                   "pcfNcfTTM": 1.0, "isST": 0})
    rows_b.append({"date": day, "open": ob, "high": max(ob, cb) * 1.01,
                   "low": min(ob, cb) * 0.99, "close": cb, "volume": 10_000,
                   "amount": 17_400_000.0, "turn": 0.5, "pctChg": 0.0,
                   "peTTM": 30.0, "pbMRQ": 6.0, "psTTM": 8.0,
                   "pcfNcfTTM": 20.0, "isST": 0})
    PRICES[day] = (oa, ob)

C1, C2 = "600000.XSHG", "600519.XSHG"
store.save_daily(C1, pd.DataFrame(rows_a))
store.save_daily(C2, pd.DataFrame(rows_b))
store.save_securities({
    C1: {"display_name": "测试低价", "name": "CSDJ", "start_date": "1999-01-01",
         "end_date": None, "type": "stock"},
    C2: {"display_name": "测试高价", "name": "CSGJ", "start_date": "1999-01-01",
         "end_date": None, "type": "stock"},
})
print(f"    ✓ {C1} 开盘 10.00   {C2} 开盘 1737.00")
jqlog.configure(handlers=[lambda r: None])


class Ctx:
    def __init__(self, cash):
        self.portfolio = jqt.Portfolio(cash)
        self.current_date = days[1]
        self.next_date = days[2]
        self.previous_date = days[0]      # ★ order_value/order_value 参考价
        self.current_dt = None
        self.jobs = []
        self.records = {}
        self.jq_options = {}
        self.pending_orders = []


ctx = Ctx(200000.0)
jqdata.CONTEXT = ctx
import jqdata.api as _a
_a.CONTEXT = _s.CONTEXT = jqt.CONTEXT = ctx

d1, d2, d3 = days[1], days[2], days[3]
o1 = jqt._open_on(C1, d2)      # 10.00
o2 = jqt._open_on(C2, d2)      # 1737.00
print(f"    测试日 {d1} -> {d2}(成交) -> {d3}")
check(abs(o1 - 10.00) < 1e-9, "C1 开盘价 = 10.00", f"{o1}")
check(abs(o2 - 1737.00) < 1e-9, "C2 开盘价 = 1737.00", f"{o2}")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("② 买入：整手 / 滑点 / 佣金 / 过户费")
print("=" * 92)
jqt.order_target(C1, 1234)          # 非整手，应截到 1200
fills = jqt.flush_orders(d2)
check(len(fills) == 1, "买入成交 1 笔", f"{len(fills)}")
if fills:
    f = fills[0]
    print(f"      成交价 {f['price']:.4f}  数量 {f['amount']}  "
          f"金额 {f['value']:,.2f}  费用 {f['fee']:,.2f}")
    check(f["amount"] == 1200, "整手：1234 -> 1200", f"实际 {f['amount']}")
    check(abs(f["price"] - 10.00) < 1e-9, "无滑点时成交价 = 开盘价",
          f"{f['price']}")
    check(abs(f["value"] - 12000.0) < 1e-6, "成交额 = 1200 × 10.00 = 12000",
          f"{f['value']}")
    exp_comm = max(5.0, 12000.0 * 0.0003)     # = 5.00（最低佣金）
    exp_trans = 12000.0 * 0.00001             # = 0.12
    check(abs(f["fee"] - (exp_comm + exp_trans)) < 0.01,
          "买入费用 = 佣金(最低5元) + 过户费", f"{f['fee']:.4f} vs "
          f"{exp_comm + exp_trans:.4f}")
    check(exp_comm == 5.0, "★ 最低佣金 5 元生效（0.03%×12000=3.6 < 5）",
          f"佣金={exp_comm}")
    pos = ctx.portfolio.positions.get(C1)
    check(pos is not None and pos.total_amount == 1200, "持仓数量 1200")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("③ T+1：当日买入不可卖（★ 曾经的真实 bug）")
print("=" * 92)
pos = ctx.portfolio.positions.get(C1)
check(pos.closeable_amount == 0, "买入当日 closeable_amount = 0",
      f"实际 {pos.closeable_amount}")
amt_before = pos.total_amount
_r = jqt.execute_sell(C1, 600, d2)
check(_r is None, "★ 当日卖出被拒绝（返回 None）", f"{_r}")
check(ctx.portfolio.positions.get(C1).total_amount == amt_before,
      "持仓未变", f"{ctx.portfolio.positions.get(C1).total_amount}")
# 模拟 runner 在开盘前做的 roll_closeable
pos.last_buy_date = d1
pos.closeable_amount = pos.total_amount
check(pos.closeable_amount == 1200, "次日 closeable_amount 提升为 1200",
      f"实际 {pos.closeable_amount}")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("④ 卖出：印花税（仅卖出）")
print("=" * 92)
before_cash = ctx.portfolio.available_cash
f2 = jqt.execute_sell(C1, 600, d3)
check(f2 is not None, "卖出成交")
if f2:
    print(f"      成交价 {f2['price']:.4f}  数量 {f2['amount']}  "
          f"金额 {f2['value']:,.2f}  费用 {f2['fee']:,.2f}")
    exp_comm = max(5.0, f2["value"] * 0.0003)
    exp_stamp = f2["value"] * jqt._stamp_duty(d3)
    exp_trans = f2["value"] * 0.00001
    check(abs(f2["fee"] - (exp_comm + exp_stamp + exp_trans)) < 0.01,
          "卖出费用 = 佣金 + 印花税 + 过户费",
          f"{f2['fee']:.4f} vs {exp_comm + exp_stamp + exp_trans:.4f}")
    check(exp_stamp > 0, "★ 印花税 > 0（2023-08-28 前为 0.1%）",
          f"{exp_stamp:.4f}")
    check(ctx.portfolio.available_cash > before_cash, "现金流入")
    check(ctx.portfolio.positions[C1].total_amount == 600, "剩余持仓 600")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("⑤ 滑点（★ 语义回归：FixedSlippage 是【元】不是【比例】）")
print("=" * 92)
ctx.jq_options = {"slippage": {"value": 0.02, "obj": _s.FixedSlippage(0.02)}}
check(abs(jqt._slippage_pct() - 0.02) < 1e-9, "滑点读取 = 0.02",
      f"{jqt._slippage_pct()}")
ctx.portfolio.available_cash = 200000.0
jqt.order_target(C1, 0)
jqt.flush_orders(d3)
jqt.order_value(C1, 10000)          # 1000 股 @10
fb = jqt.flush_orders(d3)
check(len(fb) == 1, "滑点买入成交 1 笔", f"{len(fb)}")
if fb:
    f = fb[0]
    o3 = jqt._open_on(C1, d3)
    # ★ 绝对值语义：fill = open + 0.02（不是 open × 1.02）
    print(f"      成交价 {f['price']:.4f}  开盘 {o3:.2f}  "
          f"期望(绝对值) {o3 + 0.02:.4f}  期望(比例) {o3 * 1.02:.4f}")
    check(abs(f["price"] - (o3 + 0.02)) < 1e-6,
          "★ 买入成交价 = 开盘 + 0.02 元（绝对值语义）",
          f"{f['price']:.4f} vs {o3 + 0.02:.4f}")
    check(abs(f["price"] - o3 * 1.02) > 1e-6,
          "★ 且不等于 开盘 × 1.02（证明不是比例语义）",
          f"{f['price']:.4f} != {o3 * 1.02:.4f}")
p1 = ctx.portfolio.positions.get(C1)
if p1:
    p1.last_buy_date = d1
    p1.closeable_amount = p1.total_amount
fs = jqt.execute_sell(C1, 100, d3)
if fs:
    o3 = jqt._open_on(C1, d3)
    check(abs(fs["price"] - (o3 - 0.02)) < 1e-6,
          "★ 卖出成交价 = 开盘 - 0.02 元", f"{fs['price']:.4f} vs {o3 - 0.02:.4f}")

# 高价股：绝对值语义下滑点占比应远小于 2%
ctx.jq_options = {"slippage": {"value": 0.02, "obj": _s.FixedSlippage(0.02)}}
ctx.portfolio.available_cash = 2000000.0
jqt.order_value(C2, 174000)         # 100 股 @1737
f2b = jqt.flush_orders(d3)
if f2b:
    f = f2b[0]
    o3b = jqt._open_on(C2, d3)
    rel = (f["price"] / o3b - 1) * 100
    print(f"      高价股 开盘 {o3b:.2f}  成交 {f['price']:.2f}  相对滑点 {rel:.4f}%")
    check(rel < 0.01, "★ 高价股相对滑点远小于 2%（绝对值语义的直接后果）",
          f"{rel:.4f}%")
    # ★ 用后即清仓，避免污染后续 order_target 测试的前提
    p2 = ctx.portfolio.positions.get(C2)
    if p2:
        p2.last_buy_date = d1
        p2.closeable_amount = p2.total_amount
    jqt.order_target(C2, 0)
    jqt.flush_orders(d3)
else:
    check(False, "高价股滑点测试未成交")
ctx.jq_options = {}

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("⑥ 资金约束：现金不足自动缩量")
print("=" * 92)
ctx.portfolio.available_cash = 5000.0
jqt.order_target(C1, 100000)        # 想要 10 万股（需 100 万）
fc = jqt.flush_orders(d3)
if fc:
    f = fc[0]
    print(f"      想要 100000 股，实际成交 {f['amount']} 股，花掉 {f['value']:,.2f}")
    check(f["value"] <= 5000.0 + 1e-6, "成交金额不超过可用现金",
          f"{f['value']:.2f}")
    check(f["amount"] % 100 == 0, "缩量后仍是整手", f"{f['amount']}")
else:
    check(False, "资金不足时应有缩量成交")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("⑦ 高价股买不起 1 手 -> 明确拒绝")
print("=" * 92)
ctx.portfolio.available_cash = 50000.0
jqt.order_value(C2, 50000)          # C2 @1737，1 手需 17.37 万
check(jqt.flush_orders(d3) == [], "买不起 1 手时拒绝成交（非成交 0 股）")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("⑧ order_target 的目标语义")
print("=" * 92)
ctx.portfolio.available_cash = 200000.0
jqt.order_target(C2, 0)
jqt.flush_orders(d3)
check(C2 not in ctx.portfolio.positions or
      ctx.portfolio.positions[C2].total_amount == 0, "order_target(code, 0) 清仓")
jqt.order_target(C1, 1000)
p = ctx.portfolio.positions.get(C1)
if p:
    p.last_buy_date = d1
    p.closeable_amount = p.total_amount
before = ctx.portfolio.positions.get(C1, jqt.Position(C1)).total_amount
jqt.flush_orders(d3)
after = ctx.portfolio.positions.get(C1)
check(after is not None and after.total_amount == 1000,
      "order_target(code, 1000) 调到 1000 股",
      f"{before} -> {after.total_amount if after else 'None'}")

# ═══════════════════════════════════════════════════════════════════════════
print()
print("=" * 92)
print("⑨ 组合估值")
print("=" * 92)
ctx.portfolio.mark_price(d3)
p = ctx.portfolio
print(f"      现金 {p.available_cash:,.2f}  持仓市值 {p.positions_value:,.2f}"
      f"  总资产 {p.total_value:,.2f}")
check(abs(p.total_value - (p.available_cash + p.positions_value)) < 1e-6,
      "total_value = cash + positions_value")
check(p.total_value > 0, "总资产为正")

print()
print("=" * 92)
print("⑩ 涨跌停与成交量约束")
print("=" * 92)

# 使用独立的股票，避免和前文持仓混在一起。
# C3: 次日一字涨停；C4: 次日一字跌停；C5: 低成交量；C6: ST 5% 涨停。
C3, C4, C5, C6 = "600003.XSHG", "600004.XSHG", "600005.XSHG", "600006.XSHG"


def make_constraint_rows(kind, volume=1_000_000, is_st=0):
    out = []
    for day in days:
        if day == d2:
            if kind == "limit_up":
                o = c = hi = lo = 11.00
            elif kind == "limit_down":
                o = c = hi = lo = 9.00
            elif kind == "st_limit_up":
                o = c = hi = lo = 10.50
            else:
                o = c = 10.00
                hi, lo = 10.05, 9.95
        else:
            o, c = 10.00, 10.00
            hi, lo = 10.05, 9.95
        out.append({"date": day, "open": o, "high": hi, "low": lo,
                    "close": c, "volume": volume, "amount": volume * c,
                    "turn": 1.0, "pctChg": 0.0, "peTTM": 10.0,
                    "pbMRQ": 1.0, "psTTM": 1.0, "pcfNcfTTM": 1.0,
                    "isST": is_st})
    return out


store.save_daily(C3, pd.DataFrame(make_constraint_rows("limit_up")))
store.save_daily(C4, pd.DataFrame(make_constraint_rows("limit_down")))
store.save_daily(C5, pd.DataFrame(make_constraint_rows("normal", volume=4000)))
store.save_daily(C6, pd.DataFrame(make_constraint_rows("st_limit_up", is_st=1)))

ctx.portfolio.available_cash = 200000.0
ctx.jq_options = {"match_with_volume_limit": True,
                  "order_volume_ratio": 0.25,
                  "limit_mode": "open"}

up3, down3 = jqt._limit_prices(C3, d2, jqt._bar_on(C3, d2))
check(abs(up3 - 11.00) < 1e-9 and abs(down3 - 9.00) < 1e-9,
      "主板 10% 涨跌停价", f"up={up3} down={down3}")

r = jqt.execute_buy(C3, 1000, d2)
check(r is None, "开盘涨停不买入", f"成交={r}")

b = jqt.execute_buy(C4, 1000, d1)
if b:
    p4 = ctx.portfolio.positions.get(C4)
    if p4:
        p4.closeable_amount = p4.total_amount
s = jqt.execute_sell(C4, 1000, d2)
check(s is None, "开盘跌停不卖出", f"成交={s}")
check(C4 in ctx.portfolio.positions and
      ctx.portfolio.positions[C4].total_amount == 1000,
      "跌停拒单后持仓不变")

ctx.portfolio.available_cash = 200000.0
r = jqt.execute_buy(C5, 2000, d2)
check(r is not None and r["amount"] == 1000,
      "成交量 25% 上限缩量：2000 -> 1000",
      f"成交={r['amount'] if r else None}")

up6, _ = jqt._limit_prices(C6, d2, jqt._bar_on(C6, d2))
check(abs(up6 - 10.50) < 1e-9, "ST 主板 5% 涨停价", f"up={up6}")
r = jqt.execute_buy(C6, 1000, d2)
check(r is None, "ST 涨停不买入", f"成交={r}")

# 板块涨跌幅：创业板/科创板 20%，北交所 30%
for code, want in (("300001.XSHE", 12.00), ("688001.XSHG", 12.00),
                   ("430001.BJSE", 13.00)):
    store.save_daily(code, pd.DataFrame(make_constraint_rows("normal")))
    up, _ = jqt._limit_prices(code, d2, jqt._bar_on(code, d2))
    check(abs(up - want) < 1e-9, f"{code} 涨停价 {want:.2f}", f"up={up}")

# open 模式：开盘涨停即拒绝；one_price 模式：仅一字板拒绝。
C7 = "600007.XSHG"
rows_c7 = make_constraint_rows("normal")
for row in rows_c7:
    if row["date"] == d2:
        row.update({"open": 11.00, "high": 11.00, "low": 10.50, "close": 10.90})
store.save_daily(C7, pd.DataFrame(rows_c7))
ctx.jq_options["limit_mode"] = "open"
check(jqt.execute_buy(C7, 1000, d2) is None,
      "open 模式下开盘涨停拒绝买入")
ctx.jq_options["limit_mode"] = "one_price"
r = jqt.execute_buy(C7, 1000, d2)
check(r is not None and r["amount"] == 1000,
      "one_price 模式下非一字涨停允许买入",
      f"成交={r['amount'] if r else None}")
ctx.jq_options["limit_mode"] = "open"

print()
print("=" * 92)
print(f"结论：{ok}/{ok+fail} 通过")
print("=" * 92)
if fail:
    print(f"  ❌ {fail} 项未通过：")
    for m in FAILS:
        print(f"     - {m}")
    sys.exit(1)
print("  ✅ 撮合引擎验证通过（整手/滑点/费用/T+1/资金/涨跌停/成交量）")
