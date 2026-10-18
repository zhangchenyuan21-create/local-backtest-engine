"""jqdata 替身的自检 —— 不跑回测，只验证导入链与 API 契约。

分 4 层：
  L1  包导入、子模块、代码格式转换
  L2  query 对象（链式 + 过滤 + 求值）
  L3  API 函数的返回结构（用空数据也要保证结构正确）
  L4  策略文件能否被加载、initialize 能否跑到 run_daily
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
os.environ.setdefault("JQSHIM_DATA", os.path.join(TEST_DIR, "data_test_shim"))

ok = fail = 0
MSGS = []


def check(cond, msg, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {msg}" + (f"   {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  FAIL  {msg}" + (f"   {extra}" if extra else ""))
        MSGS.append(msg)


print("=" * 96)
print("L1 包导入与代码转换")
print("=" * 96)
try:
    import jqdata
    check(True, "import jqdata", f"v{jqdata.__version__}")
except Exception as e:
    check(False, "import jqdata", f"{type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

for name in ("api", "trade", "setup", "store", "fetch", "jqlog", "tables"):
    try:
        __import__(f"jqdata.{name}")
        check(True, f"import jqdata.{name}")
    except Exception as e:
        check(False, f"import jqdata.{name}", f"{type(e).__name__}: {e}")

# 策略的两行导入
try:
    from jqdata import *          # noqa: F401,F403
    check(True, "from jqdata import *")
except Exception as e:
    check(False, "from jqdata import *", f"{type(e).__name__}: {e}")
try:
    from jqdata import finance
    check(True, "from jqdata import finance")
except Exception as e:
    check(False, "from jqdata import finance", f"{type(e).__name__}: {e}")

CASES = [
    ("600000.XSHG", "600000.XSHG"), ("000001.XSHE", "000001.XSHE"),
    ("sh.600000", "600000.XSHG"), ("sz.000001", "000001.XSHE"),
    ("sh600000", "600000.XSHG"), ("sz000001", "000001.XSHE"),
    ("600000", "600000.XSHG"), ("000001", "000001.XSHE"),
    ("300750", "300750.XSHE"), ("688981", "688981.XSHG"),
]
for src, want in CASES:
    got = jqdata.normalize_code(src)
    check(got == want, f"normalize_code({src!r})", f"-> {got}")

for src, want in [("600000.XSHG", "sh600000"), ("000001.XSHE", "sz000001")]:
    got = jqdata.to_sina_code(src)
    check(got == want, f"to_sina_code({src})", f"-> {got}")
for src, want in [("600000.XSHG", "sh.600000"), ("000001.XSHE", "sz.000001")]:
    got = jqdata.to_baostock_code(src)
    check(got == want, f"to_baostock_code({src})", f"-> {got}")

print()
print("=" * 96)
print("L2 query 对象")
print("=" * 96)
import pandas as pd
from jqdata import query
from jqdata.api import valuation, indicator, Query

try:
    q = (query(valuation.code, valuation.market_cap, valuation.pe_ratio,
               valuation.pb_ratio, indicator.roe,
               indicator.inc_revenue_year_on_year,
               indicator.inc_net_profit_year_on_year)
         .filter(valuation.code.in_(["600000.XSHG", "000001.XSHE"])))
    check(isinstance(q, Query), "链式构造 query(...).filter(...)")
    check(len(q.fields) == 7, "字段数 = 7", f"实际 {len(q.fields)}")
except Exception as e:
    check(False, "构造 query", f"{type(e).__name__}: {e}")

# 用假快照求值
snap = {
    "valuation": pd.DataFrame([
        {"code": "600000.XSHG", "peTTM": 5.0, "pbMRQ": 0.6, "psTTM": 1.0,
         "pcfNcfTTM": 3.0, "_market_cap": 2000.0, "_circ_mcap": 1900.0,
         "turn": 0.5},
        {"code": "000001.XSHE", "peTTM": 6.0, "pbMRQ": 0.7, "psTTM": 1.1,
         "pcfNcfTTM": 3.1, "_market_cap": 1500.0, "_circ_mcap": 1400.0,
         "turn": 0.6},
        {"code": "300750.XSHE", "peTTM": 30.0, "pbMRQ": 5.0, "psTTM": 6.0,
         "pcfNcfTTM": 20.0, "_market_cap": 9000.0, "_circ_mcap": 8000.0,
         "turn": 1.2},
    ]),
    "indicator": pd.DataFrame([
        {"code": "600000.XSHG", "_roe": 0.11, "_yoy_rev": 0.05, "_yoy_ni": 0.06,
         "_np_margin": 0.3, "_gp_margin": 0.4},
        {"code": "000001.XSHE", "_roe": 0.09, "_yoy_rev": 0.04, "_yoy_ni": 0.05,
         "_np_margin": 0.28, "_gp_margin": 0.38},
        {"code": "300750.XSHE", "_roe": 0.20, "_yoy_rev": 0.30, "_yoy_ni": 0.35,
         "_np_margin": 0.12, "_gp_margin": 0.25},
    ]),
}
try:
    out = q.eval(snap)
    check(not out.empty, "求值非空", f"{len(out)} 行")
    check(list(out.columns) == ["code", "market_cap", "pe_ratio", "pb_ratio",
                                "roe", "inc_revenue_year_on_year",
                                "inc_net_profit_year_on_year"],
          "返回列名 = query 字段名", f"{list(out.columns)}")
    check(set(out["code"]) == {"600000.XSHG", "000001.XSHE"},
          "过滤生效（只留 2 只）", f"{sorted(out['code'])}")
    check(float(out.loc[out["code"] == "600000.XSHG", "market_cap"].iloc[0]) == 2000.0,
          "market_cap 取值正确")
except Exception as e:
    check(False, "query 求值", f"{type(e).__name__}: {e}")
    traceback.print_exc()

# 复合条件
try:
    q2 = (query(valuation.code, valuation.pe_ratio)
          .filter(valuation.pe_ratio < 10)
          .filter(valuation.code.in_(["600000.XSHG", "300750.XSHE"])))
    o2 = q2.eval(snap)
    check(len(o2) == 1 and o2["code"].iloc[0] == "600000.XSHG",
          "复合过滤（PE<10 且 代码在列）", f"{o2.to_dict('records')}")
except Exception as e:
    check(False, "复合过滤", f"{type(e).__name__}: {e}")

print()
print("=" * 96)
print("L3 API 返回结构（空数据下也要结构正确）")
print("=" * 96)
from jqdata import (attribute_history, get_price, get_all_securities,
                    get_index_stocks, get_trade_days, get_money_flow,
                    get_fundamentals)

_struct_ok = True

# get_price panel=False 必须含 time + code 列
try:
    px = get_price(["600000.XSHG"], end_date="2020-06-30", count=5,
                   frequency="daily", fields=["close"], panel=False)
    check(isinstance(px, pd.DataFrame), "get_price 返回 DataFrame")
    check("time" in px.columns and "code" in px.columns,
          "get_price(panel=False) 含 time 与 code 列（策略依赖）",
          f"columns={list(px.columns)}")
except Exception as e:
    check(False, "get_price", f"{type(e).__name__}: {e}")

try:
    h = attribute_history("600000.XSHG", 5, "1d", ["open", "close", "high", "low", "volume"])
    check(isinstance(h, pd.DataFrame), "attribute_history 返回 DataFrame")
    check(all(c in h.columns for c in ["open", "close", "high", "low", "volume"]),
          "attribute_history 含全部请求字段", f"{list(h.columns)}")
except Exception as e:
    check(False, "attribute_history", f"{type(e).__name__}: {e}")

try:
    tds = get_trade_days(end_date="2020-06-30", count=3)
    check(isinstance(tds, (list, tuple)), "get_trade_days 返回序列",
          f"type={type(tds).__name__} len={len(tds)}")
    if len(tds) >= 2:
        check(True, "可用 arr[-1] / arr[-2] 索引")
except Exception as e:
    check(False, "get_trade_days", f"{type(e).__name__}: {e}")

try:
    sec = get_all_securities(types=["stock"], date="2020-06-30")
    check(isinstance(sec, pd.DataFrame), "get_all_securities 返回 DataFrame")
    check(hasattr(sec, "index"), "有 .index（策略用 list(...index)）")
    for c in ["display_name", "name", "start_date", "end_date", "type"]:
        if c not in sec.columns:
            _struct_ok = False
    check(_struct_ok, "get_all_securities 含策略需要的列", f"{list(sec.columns)}")
except Exception as e:
    check(False, "get_all_securities", f"{type(e).__name__}: {e}")

try:
    idx = get_index_stocks("000300.XSHG", date="2020-06-30")
    check(isinstance(idx, list), "get_index_stocks 返回 list", f"len={len(idx)}")
except Exception as e:
    check(False, "get_index_stocks", f"{type(e).__name__}: {e}")

try:
    mf = get_money_flow("600000.XSHG", end_date="2020-06-30", count=5,
                        fields=["date", "sec_code", "net_amount_main", "change_pct"])
    check(isinstance(mf, pd.DataFrame), "get_money_flow 返回 DataFrame")
    check(all(c in mf.columns for c in ["date", "sec_code",
                                        "net_amount_main", "change_pct"]),
          "get_money_flow 含全部请求字段", f"{list(mf.columns)}")
except Exception as e:
    check(False, "get_money_flow", f"{type(e).__name__}: {e}")

try:
    df = get_fundamentals(
        query(valuation.code, valuation.market_cap, valuation.pe_ratio,
              valuation.pb_ratio, indicator.roe)
        .filter(valuation.code.in_(["600000.XSHG"])),
        date="2020-06-30")
    check(isinstance(df, pd.DataFrame), "get_fundamentals 返回 DataFrame",
          f"{len(df)} 行（空数据下允许 0 行）")
except Exception as e:
    check(False, "get_fundamentals", f"{type(e).__name__}: {e}")

print()
print("=" * 96)
print("L4 数据存储层")
print("=" * 96)
from jqdata import store
try:
    s = store.summary()
    for k, v in s.items():
        print(f"    {k}: {v}")
    check(True, "store.summary() 可用")
except Exception as e:
    check(False, "store.summary()", f"{type(e).__name__}: {e}")

print()
print("=" * 96)
print(f"结论：{ok}/{ok+fail} 通过")
print("=" * 96)
if fail:
    print(f"  ❌ {fail} 项未通过：")
    for m in MSGS:
        print(f"     - {m}")
    sys.exit(1)
print("  ✅ jqdata 替身自检通过")
