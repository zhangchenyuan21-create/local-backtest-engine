"""数据抓取 CLI。

用法：
    python -m jqdata.fetch_cli status                    # 查看本地数据现状
    python -m jqdata.fetch_cli calendar                  # 抓交易日历
    python -m jqdata.fetch_cli securities                # 抓股票列表
    python -m jqdata.fetch_cli sectors                   # 抓行业/概念列表 + 成分
    python -m jqdata.fetch_cli index 000300.XSHG         # 抓指数成分（按季快照）
    python -m jqdata.fetch_cli daily                     # 抓日线（按需/全量）
    python -m jqdata.fetch_cli flow                      # 抓新浪资金流
    python -m jqdata.fetch_cli all                       # 全流程

环境变量：
    JQDATA_USERNAME / JQDATA_PASSWORD   聚宽账号（抓数据必需）
    JQSHIM_DATA                         数据目录（默认 ./data）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import DATA_ROOT, normalize_code, store
from . import fetch as F


def _client():
    c = F.JQClient()
    if not (c.user and c.pwd):
        print("  ✗ 缺少聚宽凭证。请设置：")
        print("      setx JQDATA_USERNAME 你的手机号")
        print("      setx JQDATA_PASSWORD 你的密码")
        print("    （或本次运行前 export/set 环境变量）")
        return None
    try:
        c.login()
        print(f"  ✓ 聚宽登录成功（token 长度 {len(c._token or '')}）")
    except Exception as e:
        print(f"  ✗ 聚宽登录失败：{str(e)[:200]}")
        return None
    return c


def cmd_status(_a):
    print("=" * 78)
    print("本地数据现状")
    print("=" * 78)
    for k, v in store.summary().items():
        print(f"  {k}: {v}")
    print()
    from . import sector as S
    print("  板块数据：")
    for k, v in S.status().items():
        print(f"    {k}: {v}")
    return 0


def cmd_calendar(a):
    c = _client()
    if not c:
        return 1
    n = F.fetch_calendar(c, a.start, a.end)
    print(f"  ✓ 交易日历 {n} 天 -> {store.load_calendar()[:1]} ~ {store.load_calendar()[-1:]}")
    return 0


def cmd_securities(a):
    c = _client()
    if not c:
        return 1
    n = F.fetch_securities(c, a.date)
    print(f"  ✓ 股票列表 {n} 只")
    return 0


def cmd_sectors(a):
    """抓行业/概念清单 + 成分。这是策略选股的地基，必须用聚宽 Data API。"""
    c = _client()
    if not c:
        return 1
    from . import sector as S
    import pandas as pd

    # 行业清单
    inds = {}
    for name in ("sw_l1",):
        try:
            df = c.industry_list(name=name, date=a.date) if hasattr(c, "industry_list") \
                else _industry_list(c, name, a.date)
            for idx, r in df.iterrows():
                inds[str(idx)] = str(r.get("name") or r.iloc[0])
        except Exception as e:
            print(f"  ✗ 行业清单 {name} 失败：{str(e)[:120]}")
    if inds:
        S.save_sector_list("sw_l1", inds)
        print(f"  ✓ 申万一级 {len(inds)} 个")

    # 概念清单
    cons = {}
    try:
        df = _concept_list(c, a.date)
        for idx, r in df.iterrows():
            cons[str(idx)] = str(r.get("name") or r.iloc[0])
    except Exception as e:
        print(f"  ✗ 概念清单失败：{str(e)[:120]}")
    if cons:
        S.save_sector_list("concept", cons)
        print(f"  ✓ 概念板块 {len(cons)} 个")

    # 成分快照（按日期）
    for kind, recs in (("sw_l1", inds), ("concept", cons)):
        if not recs:
            continue
        # 只抓策略实际会用到的（行业全抓，概念按关键词）
        keys = list(recs) if kind == "sw_l1" else _filter_concepts(recs)
        print(f"  抓 {kind} 成分 {len(keys)} 个（日期 {a.date}）...")
        ok = 0
        for i, code in enumerate(keys, 1):
            try:
                mem = _sector_members(c, kind, code, a.date)
                if mem:
                    S.save_sector_members(kind, code, a.date, mem)
                    ok += 1
            except Exception as e:
                print(f"    ✗ {code}: {str(e)[:70]}")
            if i % 10 == 0:
                print(f"      {i}/{len(keys)}  成功 {ok}")
            time.sleep(0.1)
        print(f"  ✓ {kind} 成分 {ok}/{len(keys)}")
    return 0


def _industry_list(c, name, date):
    """get_industries(name, date) -> DataFrame(index=code, 列含 name)"""
    import pandas as pd
    txt = c.call("get_industries", name=name, date=date)
    df = F._csv(txt)
    if df.empty:
        return df
    codecol = "industry_code" if "industry_code" in df.columns else df.columns[0]
    namecol = "name" if "name" in df.columns else df.columns[-1]
    out = df[[namecol]].copy()
    out.index = df[codecol].astype(str)
    out.columns = ["name"]
    return out


def _concept_list(c, date):
    import pandas as pd
    txt = c.call("get_concepts", date=date)
    df = F._csv(txt)
    if df.empty:
        return df
    codecol = "concept_code" if "concept_code" in df.columns else df.columns[0]
    namecol = "name" if "name" in df.columns else df.columns[-1]
    out = df[[namecol]].copy()
    out.index = df[codecol].astype(str)
    out.columns = ["name"]
    return out


def _sector_members(c, kind, code, date):
    method = "get_industry_stocks" if kind == "sw_l1" else "get_concept_stocks"
    txt = c.call(method, code=code, date=date)
    df = F._csv(txt)
    if df.empty:
        return []
    col = "code" if "code" in df.columns else df.columns[0]
    return [normalize_code(x) for x in df[col].tolist()]


KEYWORD_CONCEPTS = ("北斗", "卫星导航", "卫星互联网", "导航", "先进封装",
                    "封装测试", "Chiplet", "半导体封装", "晶圆", "创新药",
                    "生物医药", "医药创新", "锂电池", "动力电池", "固态电池",
                    "储能", "电池")


def _filter_concepts(recs):
    return [k for k, v in recs.items()
            if any(t in str(v) for t in KEYWORD_CONCEPTS)]


def cmd_index(a):
    c = _client()
    if not c:
        return 1
    snaps = a.snapshots.split(",") if a.snapshots else [
        f"{y}-06-30" for y in range(int(a.start[:4]), int(a.end[:4]) + 1)]
    n = F.fetch_index(c, normalize_code(a.index), snaps)
    print(f"  ✓ 指数 {a.index} 新增 {n} 个快照")
    return 0


def cmd_daily(a):
    c = _client()
    if not c:
        return 1
    codes = _target_codes(a)
    print(f"  目标 {len(codes)} 只，区间 {a.start} ~ {a.end}")
    st = store.load_state()
    ok = skip = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        if not a.force and store.is_done("daily", code, st):
            skip += 1
            continue
        try:
            n = F.fetch_daily(c, code, a.start, a.end)
            if n:
                store.mark_done("daily", code, st)
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"    ✗ {code}: {str(e)[:90]}")
        if i % 25 == 0:
            print(f"    {i}/{len(codes)}  成功 {ok} 跳过 {skip} 失败 {fail}  "
                  f"{time.time()-t0:.0f}s")
        time.sleep(a.pause)
    print(f"  ✓ 日线：成功 {ok} 跳过 {skip} 失败 {fail}  {time.time()-t0:.0f}s")
    return 0


def cmd_flow(a):
    codes = _target_codes(a)
    print(f"  抓新浪资金流 {len(codes)} 只")
    r = F.fetch_flow(codes, pause=a.pause, force=a.force)
    print(f"  ✓ {r}")
    return 0


def _target_codes(a):
    """确定要抓哪些股票：显式指定 > 本地股票列表 > 指数成分 > 报错"""
    if a.codes:
        return [normalize_code(x) for x in a.codes.split(",")]
    sec = store.load_securities()
    if not sec.empty:
        return list(sec.index)
    for idx in ("000300.XSHG", "000905.XSHG"):
        m = store.index_members_asof(idx, a.end)
        if m:
            print(f"    （股票列表为空，改用指数 {idx} 的 {len(m)} 只成分）")
            return m
    print("  ✗ 无法确定股票范围。请先抓 securities 或 index，或用 --codes 指定。")
    raise SystemExit(2)


def cmd_all(a):
    rc = 0
    for name, fn in (("calendar", cmd_calendar), ("securities", cmd_securities),
                     ("sectors", cmd_sectors), ("index", cmd_index),
                     ("daily", cmd_daily), ("flow", cmd_flow)):
        print()
        print("=" * 78)
        print(f"▶ {name}")
        print("=" * 78)
        try:
            rc |= (fn(a) or 0)
        except SystemExit as e:
            rc |= int(e.code or 1)
        except Exception as e:
            print(f"  ✗ {name} 失败：{type(e).__name__}: {e}")
            rc |= 1
    print()
    cmd_status(a)
    return rc


def main(argv=None):
    p = argparse.ArgumentParser(prog="jqdata.fetch_cli",
                                description="本地回测数据抓取")
    p.add_argument("cmd", choices=["status", "calendar", "securities", "sectors",
                                   "index", "daily", "flow", "all"])
    p.add_argument("--start", default="2019-12-01")
    p.add_argument("--end", default="2026-10-08")
    p.add_argument("--date", default="2026-10-01", help="列表类接口的基准日")
    p.add_argument("--index", default="000300.XSHG")
    p.add_argument("--snapshots", default="", help="逗号分隔的指数成分快照日")
    p.add_argument("--codes", default="", help="逗号分隔的股票代码")
    p.add_argument("--pause", type=float, default=0.15, help="每只之间的间隔秒")
    p.add_argument("--force", action="store_true", help="忽略断点续传，重抓")
    a = p.parse_args(argv)

    print(f"  数据目录 {DATA_ROOT}")
    fn = {"status": cmd_status, "calendar": cmd_calendar,
          "securities": cmd_securities, "sectors": cmd_sectors,
          "index": cmd_index, "daily": cmd_daily, "flow": cmd_flow,
          "all": cmd_all}[a.cmd]
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
