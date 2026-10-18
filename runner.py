"""本地回测驱动器 —— 按交易日循环，驱动策略的 4 个定时任务。

═══════════════════════════════════════════════════════════════════════════
策略注册的任务（静态提取自 initialize）
═══════════════════════════════════════════════════════════════════════════
  L759: run_daily(prefetch_daily_flow, time="15:30")
  L761: run_daily(before_market,      time="09:00")
  L763: run_daily(market_open_trade,  time="09:31")
  L765: run_daily(late_session_check, time="14:50")

═══════════════════════════════════════════════════════════════════════════
撮合时序（避免前视偏差）
═══════════════════════════════════════════════════════════════════════════
  第 T 日循环：
    ① 开盘：执行【T-1 日挂下的】订单，成交价 = T 日开盘价
       —— 因为 09:31 决策时无法知道当日开盘价之后的走势，
          聚宽默认次bar成交；用 T 日开盘价成交是最接近的口径
    ② 09:00 / 09:31 / 14:50 / 15:30 依次运行策略任务（生成新订单）
    ③ 收盘：按 T 日收盘价估值，记录日终快照
  第 T+1 日：执行 T 日挂下的订单

  可用 fill_mode 切换：
    "next_open"（默认，对齐聚宽）
    "same_close"（当日收盘成交，用于快速对照）
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import time
import types
from types import SimpleNamespace
from typing import Callable

import pandas as pd

# ── 让 `from jqdata import *` 解析到本包的替身 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import jqdata  # noqa: E402
from jqdata import store, trade as jqtrade, jqlog  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 聚宽 context 对象
# ═══════════════════════════════════════════════════════════════════════════
class Context:
    def __init__(self, cash, start, end, fill_mode="next_open",
                 strict_no_future=False):
        self.portfolio = jqtrade.Portfolio(cash)
        self.start_date = start
        self.end_date = end
        self.current_dt = None          # datetime
        self.current_date = None        # str YYYY-MM-DD
        self.next_date = None
        self.previous_date = None
        self.jobs = []
        self.records = {}
        self.jq_options = dict(jqtrade_mod_options())
        self.pending_orders = []
        self.fill_mode = fill_mode
        self.strict_no_future = strict_no_future
        self.run_id = None
        # 聚宽里 context 也常被当成任意命名空间用
        self.universe = None

    def __repr__(self):
        return (f"<Context {self.current_date} cash={self.portfolio.available_cash:,.0f} "
                f"tv={self.portfolio.total_value:,.0f}>")


def jqtrade_mod_options():
    from jqdata.setup import DEFAULTS
    return dict(DEFAULTS)


# ═══════════════════════════════════════════════════════════════════════════
# 加载策略文件
# ═══════════════════════════════════════════════════════════════════════════
def load_strategy(path: str):
    """把策略 .py 作为模块加载（它内部 `from jqdata import *` 会拿到替身）。

    ★ 关键：必须先注入 `g` 命名空间
    ─────────────────────────────────────────────────────────────────────
    静态扫描确认：`g` 在【所有交付文件里都没有定义】—— 它是聚宽运行环境
    注入的全局命名空间对象（31 个属性 / 188 次引用，纯属性容器，无方法调用）。
    策略在 initialize 里第一件事就是 `g.cfg = copy.deepcopy(DEFAULT_CONFIG)`，
    所以 `g` 必须在 exec_module【之前】就存在于模块的 __dict__ 中，
    否则会 NameError。
    """
    path = os.path.abspath(path)
    name = "jq_strategy_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["g"] = SimpleNamespace()      # ★ 必须在 exec_module 之前
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    if "g" not in mod.__dict__:                # 双保险
        mod.__dict__["g"] = SimpleNamespace()
    return mod


# ═══════════════════════════════════════════════════════════════════════════
# 回测主循环
# ═══════════════════════════════════════════════════════════════════════════
class Backtest:
    def __init__(self, strategy_path, cash=350000.0, start=None, end=None,
                 fill_mode="next_open", strict_no_future=False,
                 log_file=None, quiet=False, setup_date=None):
        self.mod = load_strategy(strategy_path)
        self.strategy_name = os.path.basename(strategy_path)
        cal = store.load_calendar()
        if not cal:
            raise RuntimeError(
                "交易日历为空。请先抓数据：python -m jqdata.fetch_cli calendar")
        self.cal = [d for d in cal
                    if (not start or d >= start) and (not end or d <= end)]
        if not self.cal:
            raise RuntimeError(f"区间 {start}~{end} 内无交易日")
        # initialize 的运行日：首个交易日的前一天（与聚宽一致）
        if setup_date:
            self.setup_date = setup_date
        else:
            d0 = dt.date.fromisoformat(self.cal[0])
            self.setup_date = (d0 - dt.timedelta(days=1)).isoformat()
        self.ctx = Context(cash, self.cal[0], self.cal[-1],
                           fill_mode=fill_mode,
                           strict_no_future=strict_no_future)
        self.equity = []            # [(date, total_value, positions_value, cash, n_pos)]
        self.fills = []             # 成交明细
        self.day_stats = []
        self.log_file = log_file
        self.quiet = quiet
        self.t0 = None

    # ---------- 初始化 ----------
    def setup_logging(self):
        handlers = []
        if self.log_file:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_file)),
                        exist_ok=True)
            f = open(self.log_file, "w", encoding="utf-8", buffering=1)
            handlers.append(lambda rec: f.write(rec["line"] + "\n"))
            self._logfh = f
        if not self.quiet:
            handlers.append(lambda rec: sys.stdout.write(rec["line"] + "\n"))
        jqlog.configure(handlers=handlers)

    def initialize(self):
        # ★ initialize 里会调 context.current_dt.date()（见策略 L723/L744），
        #   所以必须先设好 current_dt。
        #   聚宽语义：initialize 运行在回测起始日 00:00（实测 2020-01-01，
        #   即首个交易日的前一天），此时可正常取历史数据。
        sd = self.setup_date
        self.ctx.current_date = sd
        self.ctx.current_dt = dt.datetime(
            int(sd[:4]), int(sd[5:7]), int(sd[8:10]), 0, 0, 0)

        jqdata.CONTEXT = self.ctx
        import jqdata.api as jqapi
        jqapi.CONTEXT = self.ctx
        import jqdata.trade as jqt
        jqt.CONTEXT = self.ctx
        import jqdata.setup as jqs
        jqs.CONTEXT = self.ctx
        import jqdata.jqlog as jql
        jql.CONTEXT = self.ctx
        fn = getattr(self.mod, "initialize", None)
        if fn is None:
            raise RuntimeError("策略文件缺少 initialize(context)")
        fn(self.ctx)

    # ---------- 单日 ----------
    def run_day(self, i):
        date = self.cal[i]
        nxt = self.cal[i + 1] if i + 1 < len(self.cal) else None
        prev = self.cal[i - 1] if i > 0 else None
        self.ctx.current_date = date
        self.ctx.next_date = nxt
        self.ctx.previous_date = prev

        # ── ① 开盘：执行【前一日挂下】的订单（T+1 可卖量在此之前更新）
        self._roll_closeable()
        day_fills = []
        if self.ctx.fill_mode == "next_open":
            day_fills = jqtrade.flush_orders(date)
        # ★★ 公司行动 / 缺失行情处理（P0 重做 v2，2026-10-17 06:10）★★
        #   v1 曾在此处"按最后收盘价强制了结并释放现金"—— 被审计否决：
        #   公告只规定 2023-03-17 终止上市 + 1:0.6647 换股，并未赋予 02-08 变现的机会；
        #   那会把"永久冻结旧价"换成"过早获得可再投资现金"，同样改变路径。
        #   v2：默认【不了结】，只把无行情持仓标记为 restricted（不可交易、按最后已知价挂账），
        #       并写入可验收的估值记录；期末仍有受限持仓 ⇒ 该次回测不得用于最终净值裁决。
        #       仅当 JQSHIM_CA_MODE=settle_last_price 时才走敏感性情景。
        try:
            from jqdata import corpaction as _ca
            if i == 0:
                _ca.reset()
                # ★ 框架 §1：event_id 需含运行标识，单次运行内唯一
                #   run_day 内没有 outdir，用启动时刻 + 进程号构造运行标识
                _ca.RUN_ID = "run%s" % str(getattr(self, "t0", "")
                                           ).replace(".", "")[:12]
            day_fills = list(day_fills) + _ca.settle_terminated(self.ctx, date)
            # P0 step2: swap conversion (no cash created)
            _ca.apply_conversions(self.ctx, date)
        except Exception as _e:
            # ★ 审计要求：公司行动处理失败应使该次回测验收失败，不得静默继续
            jqlog.error("[公司行动] %s 处理失败：%s" % (date, _e))
            raise
        for f in day_fills:
            f["run"] = self.strategy_name
            f["run_date"] = date
        self.fills.extend(day_fills)

        # ── ② 依次运行各定时任务
        for job in sorted(self.ctx.jobs, key=lambda j: str(j.get("time", ""))):
            t = str(job.get("time", "09:31"))
            hh, _, mm = t.partition(":")
            try:
                self.ctx.current_dt = dt.datetime(
                    int(date[:4]), int(date[5:7]), int(date[8:10]),
                    int(hh or 9), int(mm or 31))
            except Exception:
                self.ctx.current_dt = dt.datetime(
                    int(date[:4]), int(date[5:7]), int(date[8:10]), 9, 31)
            try:
                job["func"](self.ctx)
            except Exception as e:
                import traceback
                jqlog.error("[回测] %s %s 执行失败：%s" % (date, job.get("func"), e))
                if os.environ.get("JQSHIM_TRACE"):
                    traceback.print_exc()

        # ── ③ 收盘估值
        if self.ctx.fill_mode == "same_close":
            self.fills.extend(jqtrade.flush_orders(date))
        self.ctx.portfolio.mark_price(date)
        # ★ 受限持仓扫描（P0 v2）：无当日行情的持仓 ⇒ 记录估值口径（报价日期 + 状态），
        #   不得静默当作正常可交易价格。期末仍受限 ⇒ 回测不得用于最终净值裁决。
        try:
            from jqdata import corpaction as _ca
            _ca.scan_restricted(self.ctx, date)
            _ca.scan_locked(self.ctx, date)      # 有行情但处于锁定期的批次（单列一类）
        except Exception as _e:
            jqlog.error("[受限持仓] %s 扫描失败：%s" % (date, _e))
            raise
        p = self.ctx.portfolio
        self.equity.append((date, p.total_value, p.positions_value,
                            p.available_cash, len(p.positions)))
        self.day_stats.append({"date": date, "cash": p.available_cash,
                               "tv": p.total_value, "n": len(p.positions)})

    def _roll_closeable(self):
        """每日开盘前重算可卖量 —— ★ P1-f 完整修法（2026-10-17）。

        新口径：`closeable_amount = Σ 批次股数`，条件为
            · 买入日 < 当日（T+1）
            · 且已过该批次的锁定期（换股新券上市流通日前不可交易）

        为什么不再"每日无条件提升为 total_amount"：
          旧实现无法表达【按批次/按锁定期】的可卖量，导致两个真实缺陷：
            ① 老仓加仓后 T+1 失效（当日新买的股数立刻可卖）——审计 P1-f
            ② 覆盖换股新券的锁定期（实测 2023-03-28 卖出了 04-19 才上市的 600372）
          现已统一为 `jqtrade.closeable_of(pos, date)`，两处共用同一实现。
        """
        for pos in self.ctx.portfolio.positions.values():
            pos.closeable_amount = jqtrade.closeable_of(pos, self.ctx.current_date)

    # ---------- 跑 ----------
    def run(self):
        self.setup_logging()
        self.t0 = time.time()
        jqlog.info("[本地回测] 启动 %s  区间 %s ~ %s  初始资金 %.0f"
                   % (self.strategy_name, self.cal[0], self.cal[-1],
                      self.ctx.portfolio.starting_cash))
        jqlog.info("[本地回测] 数据源 jqdata 替身  成交模式 %s"
                   % self.ctx.fill_mode)
        self.initialize()
        for i in range(len(self.cal)):
            self.run_day(i)
            if not self.quiet and (i + 1) % 250 == 0:
                jqlog.info("[本地回测] 进度 %d/%d  %s  tv=%.0f"
                           % (i + 1, len(self.cal), self.cal[i],
                              self.ctx.portfolio.total_value))
        self.report()
        return self

    # ---------- 报告 ----------
    def report(self):
        eq = pd.DataFrame(self.equity, columns=["date", "total_value",
                                                "positions_value", "cash", "n_pos"])
        fl = pd.DataFrame(self.fills)
        jqlog.info("=" * 78)
        # ★ 第 17 轮：此处【不得】声明"完成"——写出尚未发生，
        #   写失败会留下误导性的成功标记。完成标记移到 save() 末尾。
        jqlog.info("[本地回测] 计算阶段结束（产物尚未写出）")
        if not eq.empty:
            s0, s1 = eq["total_value"].iloc[0], eq["total_value"].iloc[-1]
            peak = eq["total_value"].cummax()
            mdd = (eq["total_value"] / peak - 1).min()
            # ★ 第 13 轮：存在未决事项或绩效失效时，不得输出全程收益/回撤
            #   （数字一旦打印就会脱离警告被引用；原始净值仍在 equity.csv 供查账）
            _unres, _bad_reason = False, ""
            try:
                _cp = os.path.join(outdir, "corpaction.json")
                if os.path.isfile(_cp):
                    import json as _json
                    _cad = _json.load(open(_cp, encoding="utf-8"))
                    _keys = ("n_restricted_codes", "n_locked_lots",
                             "n_valuation_pending", "n_odd_lot_unresolved",
                             "n_fractional_unresolved", "n_conv_failures")
                    _unres = any(int(_cad.get(_k, 0) or 0) for _k in _keys)
                    if _cad.get("perf_invalid_from"):
                        _unres, _bad_reason = True, "绩效自 %s 起失效" % _cad["perf_invalid_from"]
                else:
                    _unres, _bad_reason = True, "无 corpaction.json，无法核验绩效有效性"
            except Exception as _e:
                _unres, _bad_reason = True, "绩效有效性未知（%s）" % type(_e).__name__
            if _unres:
                jqlog.warn("[本地回测] 期末总资产 %.0f ｜ 收益与回撤【不可比较】：%s"
                           " ⇒ 仅保留 equity.csv 原始净值作诊断，不得用于优劣比较"
                           % (s1, _bad_reason))
            else:
                jqlog.info("[本地回测] 期末总资产 %.0f   总收益 %+.2f%%   最大回撤 %.2f%%"
                           % (s1, (s1 / self.ctx.portfolio.starting_cash - 1) * 100,
                              mdd * 100))
            jqlog.info("[本地回测] 交易日 %d   平均持仓 %.2f 只   空仓 %d 天"
                       % (len(eq), eq["n_pos"].mean(),
                          int((eq["n_pos"] == 0).sum())))
        if not fl.empty:
            jqlog.info("[本地回测] 成交 %d 笔（买 %d / 卖 %d）"
                       % (len(fl), (fl["side"] == "buy").sum(),
                          (fl["side"] == "sell").sum()))
        st = jqlog.stats()
        jqlog.info("[本地回测] 日志 %s" % st)
        jqlog.info("=" * 78)

    def to_frames(self):
        eq = pd.DataFrame(self.equity, columns=["date", "total_value",
                                                "positions_value", "cash", "n_pos"])
        fl = pd.DataFrame(self.fills)
        return eq, fl

    def save(self, outdir):
        os.makedirs(outdir, exist_ok=True)
        eq, fl = self.to_frames()
        eq.to_csv(os.path.join(outdir, "equity.csv"), index=False)
        fl.to_csv(os.path.join(outdir, "fills.csv"), index=False)
        # ★ P0 v2：写出公司行动/受限持仓汇总，供验收核查
        #   （期末仍存在受限持仓 ⇒ 该次回测不得用于最终净值裁决）
        try:
            from jqdata import corpaction as _ca
            with open(os.path.join(outdir, "corpaction.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(_ca.summary(), fh, ensure_ascii=False, indent=1)
            # ★ 未决登记簿（复核第 9 轮要求）：与 corpaction.json 的未决计数可对账
            _ca.write_ledger(os.path.join(outdir, "unresolved_ledger.csv"))
        except Exception as _e:
            # ★ 复核第 11 轮：写失败不得静默（缺登记簿会使验收无法核查）
            jqlog.error("[公司行动] 汇总/登记簿写出失败：%s" % _e)
            raise
        # ★ 第 19 轮：统一准入结果单一来源 acceptance.json
        try:
            import json as _j
            _cad = _j.load(open(os.path.join(outdir, "corpaction.json"),
                                 encoding="utf-8"))
            _acc = {
                "run_status": "completed",        # 产物全部写出成功 ⇒ 运行完成
                "comparable": not bool(_cad.get("perf_invalid_from")),
                "performance_status":
                    ("comparable" if not _cad.get("perf_invalid_from")
                     else "diagnostic"),
                "invalid_from": _cad.get("invalid_from"),
                "invalid_reason": _cad.get("invalid_reason"),
                "history_affected": bool(_cad.get("history_affected")),
                "currently_unresolved": bool(_cad.get("currently_unresolved")),
                "unresolved": {
                    k: int(_cad.get(k, 0) or 0) for k in
                    ("n_restricted_codes", "n_locked_lots",
                     "n_fractional_unresolved", "n_odd_lot_unresolved",
                     "n_valuation_pending", "n_conv_failures")},
            }
            _j.dump(_acc, open(os.path.join(outdir, "acceptance.json"), "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as _e:
            jqlog.error("[准入] acceptance.json 写出失败：%s" % _e)
            raise
        # ★ 第 17 轮：全部产物写出【成功】后才打"完成"标记
        #   （若上方抛错，本行不会执行 ⇒ 日志中不会出现成功标记）
        jqlog.info("[本地回测] 产物已写出，运行完成  用时 %.1f 秒"
                   % (time.time() - self.t0))
        return outdir
