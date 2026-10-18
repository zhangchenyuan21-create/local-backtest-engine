"""本地回测命令行入口。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
  # 跑回测
  python runner_main.py run --strategy ../joinquant_strategy_AB_F.py --cash 350000
  python runner_main.py run --strategy ../joinquant_strategy_AB_A.py --start 2020-01-01

  # 与聚宽结果对账（关键：决定本地引擎能否被信任）
  python runner_main.py compare --out out/F \
      --jq-log  "logs/joinquant-log.txt" \
      --jq-csv  "logs/transaction.csv"

  # 查看数据现状
  python runner_main.py status

═══════════════════════════════════════════════════════════════════════════
对账指标（compare）
═══════════════════════════════════════════════════════════════════════════
  L1 买入笔数一致率
  L2 买入标的一致率
  L3 退出日完全吻合率
  L4 退出日 ±1 日吻合率
  L5 单笔期望差异   <- 达标线 < 2pt，只有它达标本地结论才可用
  L6 总收益差异
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pandas as pd


# ═══════════════════════════════════════════════════════════════════════════
# run
# ═══════════════════════════════════════════════════════════════════════════
def cmd_run(a):
    import runner as R
    from jqdata import store, sector as S

    print("=" * 88)
    print("本地回测")
    print("=" * 88)
    # 数据体检
    s = store.summary()
    for k, v in s.items():
        print(f"  {k}: {v}")
    st = S.status()
    print(f"  申万一级 {st['申万一级板块数']} 个，"
          f"有成分快照 {st['有成分快照的行业数']} 个")
    if st["申万一级板块数"] == 0:
        print()
        print("  ⚠️ 本地无板块数据 —— 策略将选不出任何标的。")
        print("     请先抓取：python -m jqdata.fetch_cli sectors")
    if a.out and os.path.isdir(a.out) and not a.force:
        print(f"\n  ✗ 输出目录已存在：{a.out}（加 --force 覆盖）")
        return 1

    logf = os.path.join(a.out, "run.log") if a.out else None
    bt = R.Backtest(
        a.strategy, cash=a.cash, start=a.start, end=a.end,
        fill_mode=a.fill_mode, strict_no_future=a.strict_no_future,
        log_file=logf, quiet=a.quiet)
    print(f"\n  策略 {bt.strategy_name}")
    print(f"  区间 {bt.cal[0]} ~ {bt.cal[-1]}（{len(bt.cal)} 个交易日）")
    print(f"  资金 {a.cash:,.0f}   成交模式 {a.fill_mode}"
          f"   严格防未来 {a.strict_no_future}")
    print()
    bt.run()

    eq, fl = bt.to_frames()
    if not eq.empty:
        print()
        print("=" * 88)
        print("结果")
        print("=" * 88)
        print(f"  {'年份':<8}{'末值':>14}{'当年':>10}{'累计':>10}{'持仓':>6}")
        print("  " + "-" * 48)
        prev = a.cash
        for yr in sorted({d[:4] for d in eq["date"]}):
            sub = eq[eq["date"].str.startswith(yr)]
            v = float(sub["total_value"].iloc[-1])
            print(f"  {yr:<8}{v:>14,.0f}{(v/prev-1)*100:>9.2f}%"
                  f"{(v/a.cash-1)*100:>9.2f}%{int(sub['n_pos'].iloc[-1]):>6}")
            prev = v
        peak = eq["total_value"].cummax()
        mdd = float((eq["total_value"] / peak - 1).min())
        print(f"\n  期末总资产 {eq['total_value'].iloc[-1]:,.0f}   "
              f"总收益 {(eq['total_value'].iloc[-1]/a.cash-1)*100:+.2f}%   "
              f"最大回撤 {mdd*100:.2f}%")
        print(f"  平均持仓 {eq['n_pos'].mean():.2f} 只   "
              f"空仓 {int((eq['n_pos']==0).sum())}/{len(eq)} 天")
    if not fl.empty:
        print(f"  成交 {len(fl)} 笔（买 {int((fl['side']=='buy').sum())} / "
              f"卖 {int((fl['side']=='sell').sum())}）   "
              f"总费用 {fl['fee'].sum():,.2f}")

    if a.out:
        bt.save(a.out)
        print(f"\n  产出目录 {a.out}")
        for f in sorted(os.listdir(a.out)):
            print(f"    {f:<14}{os.path.getsize(os.path.join(a.out, f)):>12,} 字节")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# 解析聚宽侧产物
# ═══════════════════════════════════════════════════════════════════════════
RX_TA = re.compile(r"\[(\d{4}-\d{2}-\d{2})\] 总资产 (\d+)\s+持仓 (\d+)")


def parse_jq_equity(log_path):
    """从聚宽日志提取日终总资产。"""
    raw = open(log_path, "rb").read()
    for enc in ("gbk", "utf-8", "utf-8-sig"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        txt = raw.decode("gbk", errors="replace")
    rows = []
    for l in txt.splitlines():
        m = RX_TA.search(l)
        if m:
            rows.append((m.group(1), int(m.group(2)), int(m.group(3))))
    return pd.DataFrame(rows, columns=["date", "total_value", "n_pos"])


RX_CODE = re.compile(r"\((\d{6}\.[A-Z]+)\)")


def parse_jq_trades(csv_path):
    """从聚宽 transaction.csv 提取成交。只保留"全部成交"。"""
    df = pd.read_csv(csv_path, encoding="gbk", dtype=str)
    df = df[df["状态"] == "全部成交"].copy()
    df["code"] = df["标的"].str.extract(RX_CODE, expand=False)
    df["side"] = df["交易类型"].map({"买": "buy", "卖": "sell"})
    # ⚠️ 聚宽 transaction.csv 里卖出数量是【负数】（如 "-300股"），但方向
    #    已由「交易类型」列表达。这里统一取绝对值 —— 否则任何对 qty 的比较
    #    或求和（如 min(qty, 剩余持仓)）都会因符号产生隐蔽错误。
    df["qty"] = pd.to_numeric(
        df["成交数量"].str.replace(r"[^\d.\-]", "", regex=True),
        errors="coerce").abs()
    df["price"] = pd.to_numeric(
        df["成交价"].str.replace(r"[^\d.\-]", "", regex=True),
        errors="coerce").abs()
    df = df.dropna(subset=["code", "qty", "price"])
    return df[["日期", "code", "side", "qty", "price"]].rename(
        columns={"日期": "date"}).reset_index(drop=True)


def pair_trades(trades):
    """把成交流水配对成 (买入日, 卖出日, 收益%) —— FIFO。"""
    from collections import defaultdict
    opens = defaultdict(list)
    out = []
    for _, r in trades.sort_values("date").iterrows():
        c = r["code"]
        if r["side"] == "buy":
            opens[c].append({"d": r["date"], "px": r["price"], "left": r["qty"]})
        else:
            q = opens.get(c)
            if not q:
                continue
            L = q[0]
            take = min(r["qty"], L["left"])
            out.append({"code": c, "bd": L["d"], "sd": r["date"],
                        "ret": (r["price"] / L["px"] - 1) * 100})
            L["left"] -= take
            if L["left"] <= 1e-6:
                q.pop(0)
    return pd.DataFrame(out)


# ═══════════════════════════════════════════════════════════════════════════
# compare
# ═══════════════════════════════════════════════════════════════════════════
def _local_trades(loc_fl):
    """把本地 fills.csv 规范化成 pair_trades 需要的 4 列。

    ⚠️ 本地 fills.csv 同时有 `date`（成交日）与 `run_date`（下单所属回测日）。
       配对要用【成交日】，所以只取 date，多余的列必须去掉 ——
       否则 rename 后会出现重复列名，pandas 报
       "The column label 'date' is not unique"。
    """
    df = loc_fl.copy()
    if "date" not in df.columns:
        df = df.rename(columns={"run_date": "date"})
    return df[["date", "code", "side", "amount", "price"]].rename(
        columns={"amount": "qty"}).reset_index(drop=True)


def cmd_compare(a):
    import statistics as st

    loc_eq = pd.read_csv(os.path.join(a.out, "equity.csv"))
    loc_fl = pd.read_csv(os.path.join(a.out, "fills.csv"))
    loc_tr = _local_trades(loc_fl) if not loc_fl.empty else pd.DataFrame()
    print("=" * 88)
    print("本地 vs 聚宽 对账")
    print("=" * 88)
    print(f"  本地 {a.out}")
    print(f"    权益 {len(loc_eq)} 天   成交 {len(loc_fl)} 笔")

    jq_eq = parse_jq_equity(a.jq_log) if a.jq_log else pd.DataFrame()
    jq_tr = parse_jq_trades(a.jq_csv) if a.jq_csv else pd.DataFrame()
    if not jq_eq.empty:
        print(f"  聚宽日志 {a.jq_log}")
        print(f"    权益 {len(jq_eq)} 天  {jq_eq['date'].iloc[0]} ~ {jq_eq['date'].iloc[-1]}")
    if not jq_tr.empty:
        print(f"  聚宽成交 {a.jq_csv}")
        print(f"    成交 {len(jq_tr)} 笔"
              f"（买 {int((jq_tr['side']=='buy').sum())} / "
              f"卖 {int((jq_tr['side']=='sell').sum())}）")

    fails = []
    # ---------- L1/L2 买入笔数与标的一致率 ----------
    print()
    print("=" * 88)
    print("L1/L2 买入一致率")
    print("=" * 88)
    if not jq_tr.empty and not loc_tr.empty:
        lb = loc_tr[loc_tr["side"] == "buy"]
        jb = jq_tr[jq_tr["side"] == "buy"]
        n_l, n_j = len(lb), len(jb)
        d = (n_l / n_j - 1) * 100 if n_j else float("nan")
        print(f"  买入笔数：本地 {n_l}  聚宽 {n_j}   差异 {d:+.1f}%")
        if abs(d) <= 10:
            print("    ✅ 笔数接近（±10% 内）")
        else:
            print("    ⚠️ 笔数差异 >10%")
            fails.append("L1 买入笔数")
        sl = set(lb["code"])
        sj = set(jb["code"])
        inter = sl & sj
        cov = len(inter) / max(1, len(sj)) * 100
        print(f"  买入标的：本地 {len(sl)} 只  聚宽 {len(sj)} 只  "
              f"交集 {len(inter)} 只   覆盖率 {cov:.1f}%")
        if cov >= 80:
            print("    ✅ 标的覆盖 >= 80%")
        else:
            print("    ⚠️ 标的覆盖 < 80%")
            fails.append("L2 买入标的")
    else:
        print("  （缺一方数据，跳过）")

    # ---------- L3/L4 退出日吻合 ----------
    print()
    print("=" * 88)
    print("L3/L4 退出日吻合")
    print("=" * 88)
    if not jq_tr.empty and not loc_tr.empty:
        lp = pair_trades(loc_tr)
        jp = pair_trades(jq_tr)
        if not lp.empty and not jp.empty:
            # 按 (code, 买入日) 配对比较退出日
            lm = {(r["code"], r["bd"]): r["sd"] for _, r in lp.iterrows()}
            jm = {(r["code"], r["bd"]): r["sd"] for _, r in jp.iterrows()}
            common = set(lm) & set(jm)
            same = near = 0
            diffs = []
            for k in common:
                if lm[k] == jm[k]:
                    same += 1
                else:
                    try:
                        d0 = dt.date.fromisoformat(str(lm[k]))
                        d1 = dt.date.fromisoformat(str(jm[k]))
                        nd = abs((d0 - d1).days)
                    except Exception:
                        nd = 99
                    diffs.append(nd)
                    if nd <= 1:
                        near += 1
            n = len(common)
            print(f"  可比配对 {n} 笔（本地 {len(lm)} / 聚宽 {len(jm)}）")
            if n:
                print(f"  退出日完全吻合 {same}/{n} = {same/n*100:.1f}%"
                      f"    （目标 >70%）")
                print(f"  退出日 ±1 日    {same+near}/{n} = {(same+near)/n*100:.1f}%"
                      f"    （目标 >90%）")
                if diffs:
                    print(f"  偏差天数 中位 {st.median(diffs):.0f}  "
                          f"均值 {sum(diffs)/len(diffs):.1f}")
                if same / n < 0.70:
                    fails.append("L3 退出日吻合")
                if (same + near) / n < 0.90:
                    fails.append("L4 退出日±1日")
        else:
            print("  （无法配对，跳过）")
    else:
        print("  （缺一方数据，跳过）")

    # ---------- L5 单笔期望差异 ----------
    print()
    print("=" * 88)
    print("L5 单笔期望差异  ★ 关键达标线")
    print("=" * 88)
    if not jq_tr.empty and not loc_tr.empty:
        lp = pair_trades(loc_tr)
        jp = pair_trades(jq_tr)
        if not lp.empty and not jp.empty:
            el = lp["ret"].mean()
            ej = jp["ret"].mean()
            print(f"  本地单笔期望 {el:+.3f}%（{len(lp)} 笔）")
            print(f"  聚宽单笔期望 {ej:+.3f}%（{len(jp)} 笔）")
            print(f"  差异 {el-ej:+.3f}pt")
            if abs(el - ej) < 2.0:
                print("    ✅ 达标（< 2pt）—— 本地结论可用")
            else:
                print("    ❌ 未达标（>= 2pt）—— 本地结论【不可用】")
                fails.append("L5 单笔期望")
    else:
        print("  （缺一方数据，跳过）")

    # ---------- L6 总收益差异 ----------
    print()
    print("=" * 88)
    print("L6 总收益差异")
    print("=" * 88)
    if not jq_eq.empty and not loc_eq.empty:
        # 对齐到共同日期
        c = sorted(set(jq_eq["date"]) & set(loc_eq["date"]))
        if len(c) >= 2:
            je = jq_eq.set_index("date")["total_value"]
            le = loc_eq.set_index("date")["total_value"]
            rj = (je[c[-1]] / je[c[0]] - 1) * 100
            rl = (le[c[-1]] / le[c[0]] - 1) * 100
            print(f"  共同区间 {c[0]} ~ {c[-1]}（{len(c)} 天）")
            print(f"  聚宽 {rj:+.2f}%   本地 {rl:+.2f}%   差异 {rl-rj:+.2f}pt")
            if abs(rl - rj) < 5:
                print("    ✅ 达标（< 5pt）")
            else:
                print("    ⚠️ 差异 >= 5pt")
                fails.append("L6 总收益")
        else:
            print(f"  （共同日期仅 {len(c)} 天，无法比较）")
    else:
        print("  （缺一方数据，跳过）")

    # ---------- 汇总 ----------
    print()
    print("=" * 88)
    if fails:
        print(f"  未达标项：{'、'.join(fails)}")
        print("  >>> 本地引擎保真度不足，结论需谨慎")
        return 1
    print("  >>> 全部达标")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
def cmd_status(_a):
    from jqdata import store, sector as S
    print("=" * 88)
    print("本地数据现状")
    print("=" * 88)
    for k, v in store.summary().items():
        print(f"  {k}: {v}")
    print("  板块数据：")
    for k, v in S.status().items():
        print(f"    {k}: {v}")
    mark = os.path.join(store.META, "IS_FIXTURE")
    if os.path.exists(mark):
        print()
        print("  ⚠️ 当前是 fixture（合成）数据，不是真实数据！")
        print(f"     标记文件：{mark}")
        print("     跑真实回测前请清掉 data/ 并重新抓取。")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="runner_main.py",
                                description="本地回测 / 与聚宽对账")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑回测")
    r.add_argument("--strategy", required=True, help="策略 .py 路径")
    r.add_argument("--cash", type=float, default=350000.0)
    r.add_argument("--start", default=None)
    r.add_argument("--end", default=None)
    r.add_argument("--out", default=None, help="产出目录")
    r.add_argument("--fill-mode", default="next_open",
                   choices=["next_open", "same_close"])
    r.add_argument("--strict-no-future", action="store_true",
                   help="开启前视偏差拦截（聚宽 avoid_future_data 语义）")
    r.add_argument("--quiet", action="store_true", help="不打印日志到屏幕")
    r.add_argument("--force", action="store_true", help="允许覆盖输出目录")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="与聚宽结果对账")
    c.add_argument("--out", required=True, help="本地产出目录（含 equity.csv/fills.csv）")
    c.add_argument("--jq-log", default=None, help="聚宽 log.txt")
    c.add_argument("--jq-csv", default=None, help="聚宽 transaction.csv")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("status", help="查看本地数据现状")
    s.set_defaults(func=cmd_status)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
