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
from jqdata.fastcore import config as _fc_config  # noqa: E402
from jqdata.fastcore import signals as _fc_signals  # noqa: E402


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


_REPLAY_ENV_KEYS = (
    "JQSHIM_SLIP_MODEL",
    "JQSHIM_LIMIT_MODE",
    "JQSHIM_VOLUME_RATIO",
    "JQSHIM_VOLUME_LIMIT",
    "JQSHIM_REL_SLIPPAGE",
    "JQSHIM_CA_MODE",
)


def _capture_replay_env():
    return {k: os.environ[k] for k in _REPLAY_ENV_KEYS if k in os.environ}


def install_fast_flow_patch(mod):
    """Route the strategy's batch money-flow helper through the aggregate API.

    The V52 family defines ``get_money_flow_batch(stocks, end_date, count)``
    and only ever uses its result as ``groupby('date').sum()``.  The fast API
    returns that aggregate directly, avoiding tens of thousands of per-stock
    rows.  If the helper is absent (other strategy family) or the fast path is
    disabled, the original function is used unchanged.
    """
    if not _fc_config.env_bool("JQSHIM_FAST_FLOW", True):
        return mod
    orig = getattr(mod, "get_money_flow_batch", None)
    if orig is None or getattr(orig, "_jq_fast_patched", False):
        return mod

    def _fast_flow_batch(stocks, end_date, count):
        try:
            from jqdata import get_money_flow_summary
            if (_fc_config.fast_enabled()
                    and len(stocks) >= _fc_config.flow_summary_min_codes()):
                agg = get_money_flow_summary(stocks, end_date=end_date, count=count)
                if agg is not None and len(agg):
                    return agg
        except Exception:
            if _fc_config.strict_fast():
                raise
        return orig(stocks, end_date, count)

    _fast_flow_batch._jq_fast_patched = True
    _fast_flow_batch._jq_original = orig
    mod.get_money_flow_batch = _fast_flow_batch
    return mod


# ═══════════════════════════════════════════════════════════════════════════
# 回测主循环
# ═══════════════════════════════════════════════════════════════════════════
class Backtest:
    def __init__(self, strategy_path, cash=350000.0, start=None, end=None,
                 fill_mode="next_open", strict_no_future=False,
                 log_file=None, quiet=False, setup_date=None,
                 record_signals=None, replay_signals=None,
                 perf_mode=False, no_audit=False, phase_timing=False):
        if record_signals and replay_signals:
            raise ValueError("record_signals 与 replay_signals 不能同时使用")
        self.record_signals = record_signals
        self.replay_signals = replay_signals
        self.perf_mode = bool(perf_mode)
        self.no_audit = bool(no_audit)
        self.phase_timing = bool(phase_timing)
        self.phase_timings = {}
        self.signal_recorder = None
        self.signal_map = {}
        self.signal_meta = {}
        self.outdir = None

        if replay_signals:
            self.signal_map = _fc_signals.load_signals(replay_signals)
            self.signal_meta = _fc_signals.replay_meta_defaults(
                _fc_signals.load_meta(replay_signals))
            for _k, _v in (self.signal_meta.get("env") or {}).items():
                if _v is not None:
                    os.environ[str(_k)] = str(_v)
            self.mod = None
            self.strategy_name = "replay:" + os.path.basename(replay_signals)
            if start is None:
                start = self.signal_meta.get("start")
            if end is None:
                end = self.signal_meta.get("end")
            if cash == 350000.0 and self.signal_meta.get("cash") is not None:
                cash = float(self.signal_meta["cash"])
            if fill_mode == "next_open" and self.signal_meta.get("fill_mode"):
                fill_mode = self.signal_meta["fill_mode"]
        else:
            self.mod = load_strategy(strategy_path)
            install_fast_flow_patch(self.mod)
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
        if replay_signals and self.signal_meta.get("jq_options"):
            self.ctx.jq_options = dict(self.signal_meta["jq_options"])
        if record_signals:
            self.signal_recorder = _fc_signals.SignalRecorder(record_signals)
            self.ctx.signal_recorder = self.signal_recorder
        self.equity = []            # [(date, total_value, positions_value, cash, n_pos, susp_value, susp_weight, val_basis)]
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
        if not self.quiet and not self.perf_mode:
            handlers.append(lambda rec: sys.stdout.write(rec["line"] + "\n"))
        # Step 4: optional log-noise suppression for fast research runs.
        # Set JQSHIM_LOG_SUPPRESS to a pipe-separated list of message prefixes.
        _noisy = None
        _env = os.environ.get("JQSHIM_LOG_SUPPRESS", "")
        if _env:
            _noisy = tuple([x.strip() for x in _env.split("|") if x.strip()])
        jqlog.configure(handlers=handlers, noisy=_noisy,
                        min_level=("WARN" if self.perf_mode else "INFO"))

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
        if self.replay_signals:
            # Replay mode deliberately does not load or execute the strategy.
            # Fees/slippage/options were captured in the signal metadata.
            self.ctx.jobs = []
            self.ctx.jq_options = dict(self.signal_meta.get("jq_options") or self.ctx.jq_options)
            jqlog.info("[本地回测] 信号回放模式：%d 个信号日，跳过策略 initialize"
                       % len(self.signal_map))
            return

        fn = getattr(self.mod, "initialize", None)
        if fn is None:
            raise RuntimeError("策略文件缺少 initialize(context)")
        # ★ 方案④（2026-10-19 实测修正）：续跑【必须】调用策略 initialize。
        #   原因：策略 initialize 里注册 run_daily 作业；跳过它就等于没有任何
        #   每日作业 ⇒ 每天空转、永不交易（实测：续跑 42 天 0 成交）。
        #   它造成的 g/组合副作用随后由 load_checkpoint 覆盖，因此是安全的。
        fn(self.ctx)
        if self.signal_recorder is not None:
            self.signal_recorder.set_meta(
                strategy=self.strategy_name,
                start=self.cal[0], end=self.cal[-1],
                cash=self.ctx.portfolio.starting_cash,
                fill_mode=self.ctx.fill_mode,
                jq_options=self.ctx.jq_options,
                env=_capture_replay_env(),
            )

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

        # ── ② 回放模式：直接注入当日已记录的下单意图
        if self.replay_signals:
            self._inject_replay_orders(date)
        else:
            # 依次运行各定时任务
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
        # ★ 期末强制结算：09:31 产生的卖单默认要到下一交易日开盘才成交，
        #   若回测恰好在信号日结束，则该卖单永远不会进入 fills.csv。
        #   在最后一日收盘按收盘价结算卖单，保证期末持仓/已实现盈亏可对账。
        if self.ctx.fill_mode == "next_open" and i == len(self.cal) - 1:
            _forced = jqtrade.flush_orders_at_close(date)
            for _f in _forced:
                _f["run"] = self.strategy_name
                _f["run_date"] = date
            self.fills.extend(_forced)
        self.ctx.portfolio.mark_price(date)
        # ★ 受限持仓扫描（P0 v2）：无当日行情的持仓 ⇒ 记录估值口径（报价日期 + 状态），
        #   不得静默当作正常可交易价格。期末仍受限 ⇒ 回测不得用于最终净值裁决。
        if not self.no_audit and self.ctx.portfolio.positions:
            try:
                from jqdata import corpaction as _ca
                _ca.scan_restricted(self.ctx, date)
                _ca.scan_locked(self.ctx, date)  # 有行情但处于锁定期的批次（单列一类）
            except Exception as _e:
                jqlog.error("[受限持仓] %s 扫描失败：%s" % (date, _e))
                raise
        p = self.ctx.portfolio
        _snap = p.snapshot()
        self.equity.append((date, p.total_value, p.positions_value,
                            p.available_cash, len(p.positions),
                            _snap.get("susp_value", 0.0),
                            _snap.get("susp_weight", 0.0),
                            _snap.get("val_basis", "")))
        self.day_stats.append({"date": date, "cash": p.available_cash,
                               "tv": p.total_value, "n": len(p.positions)})

    def _inject_replay_orders(self, date):
        """Set pending orders recorded for this signal date.

        The engine still flushes them on the next open (next_open) or at the
        same close (same_close), so execution constraints remain identical to
        the original run.
        """
        orders = self.signal_map.get(date, [])
        self.ctx.pending_orders = [
            {"code": o["code"], "qty": int(o["qty"])} for o in orders
        ]
        if orders:
            t = str(orders[-1].get("signal_time") or "09:31")
            hh, _, mm = t.partition(":")
            try:
                self.ctx.current_dt = dt.datetime(
                    int(date[:4]), int(date[5:7]), int(date[8:10]),
                    int(hh or 9), int(mm or 31))
            except Exception:
                pass

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
    # ── 方案④：断点续跑（checkpoint / resume）────────────────────────────
    def _ck_ca(self):
        try:
            from jqdata import corpaction as _cam
            # ★ R38：优先用 save_state()（可导出 PERF_INVALID_FROM 等审计状态）
            if hasattr(_cam, "save_state"):
                return _cam.save_state()
            if hasattr(_cam, "dump_state"):
                return _cam.dump_state()
        except Exception:
            pass
        return {}

    def save_checkpoint(self, path):
        """写检查点：组合(含 lots)+策略 g(可序列化部分)+日序+流水+事件簿。"""
        import pickle
        import time as _t
        p = self.ctx.portfolio
        pos = {}
        for code, ps in p.positions.items():
            pos[code] = {"total_amount": ps.total_amount,
                         "closeable_amount": ps.closeable_amount,
                         "cost_basis": ps.cost_basis, "price": ps.price,
                         "init_time": getattr(ps, "init_time", None),
                         "last_buy_date": getattr(ps, "last_buy_date", None),
                         "lots": [list(x) for x in getattr(ps, "lots", [])]}
        g_ok, g_bad = {}, []
        for k, v in vars(getattr(self.mod, "g", None) or {}).items():
            try:
                pickle.dumps(v, protocol=4)
                g_ok[k] = v
            except Exception:
                g_bad.append(k)
        st = {"i": int(getattr(self, "_ck_i", 0)),
              "cash": p.available_cash, "positions": pos,
              "g": g_ok, "g_dropped": g_bad,
              "equity": self.equity, "fills": self.fills,
              "corpaction": self._ck_ca(),
              "saved_at": _t.strftime("%Y-%m-%d %H:%M:%S")}
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(st, fh, protocol=4)
        os.replace(tmp, path)
        meta = {"i": st["i"], "date": self.cal[min(st["i"], len(self.cal) - 1)],
                "cash": st["cash"], "n_pos": len(pos),
                "n_equity": len(self.equity), "n_fills": len(self.fills),
                "g_dropped": g_bad, "saved_at": st["saved_at"]}
        with open(path + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        return meta

    def load_checkpoint(self, path):
        import pickle
        from jqdata.trade import Position
        st = pickle.load(open(path, "rb"))
        p = self.ctx.portfolio
        p.available_cash = st["cash"]
        p.positions.clear()
        for code, d in st["positions"].items():
            ps = Position(code)
            ps.total_amount = d["total_amount"]
            ps.closeable_amount = d["closeable_amount"]
            ps.cost_basis = d["cost_basis"]
            ps.price = d["price"]
            if d.get("init_time") is not None:
                ps.init_time = d["init_time"]
            if d.get("last_buy_date") is not None:
                ps.last_buy_date = d["last_buy_date"]
            ps.lots = [list(x) for x in d.get("lots", [])]  # ★ R36：必须可变（execute_sell 就地扣减）
            p.positions[code] = ps
        for k, v in st["g"].items():
            setattr(self.mod.g, k, v)
        # ★ equity 行是元组；fills 是【dict】——不可 tuple()，否则被拆成键列表
        self.equity = [tuple(x) for x in st["equity"]]
        self.fills = [dict(x) for x in st["fills"]]
        if st.get("corpaction"):
            try:
                from jqdata import corpaction as _cam
                if hasattr(_cam, "load_state"):
                    _cam.load_state(st["corpaction"])
            except Exception:
                pass
        self._ck_i = int(st["i"])
        return st

    def run(self):
        self.setup_logging()
        self.t0 = time.time()
        t_phase = time.time()
        _rp = getattr(self, "_resume_path", None)
        _start_i = 0
        # ★ 方案④：无论续跑与否都必须 initialize（它负责上下文与全局绑定）；
        #   续跑路径下策略 initialize 的副作用随后由 load_checkpoint 覆盖，避免 g 被重置。
        self.initialize()
        self.phase_timings["initialize"] = time.time() - t_phase
        t_phase = time.time()
        if _rp and os.path.isfile(_rp):
            self.load_checkpoint(_rp)
            _start_i = self._ck_i + 1
            jqlog.info("[续跑] 自 %s 之后继续：第 %d/%d 个交易日"
                       % (self.cal[min(self._ck_i, len(self.cal) - 1)],
                          _start_i + 1, len(self.cal)))
        else:
            jqlog.info("[本地回测] 启动 %s  区间 %s ~ %s  初始资金 %.0f"
                       % (self.strategy_name, self.cal[0], self.cal[-1],
                          self.ctx.portfolio.starting_cash))
            jqlog.info("[本地回测] 数据源 jqdata 替身  成交模式 %s"
                       % self.ctx.fill_mode)
        _every = int(getattr(self, "_ck_every", 0) or 0)
        for i in range(_start_i, len(self.cal)):
            self._ck_i = i
            self.run_day(i)
            if not self.quiet and not self.perf_mode and (i + 1) % 250 == 0:
                jqlog.info("[本地回测] 进度 %d/%d  %s  tv=%.0f"
                           % (i + 1, len(self.cal), self.cal[i],
                              self.ctx.portfolio.total_value))
            if _every and (i - _start_i + 1) % _every == 0:
                try:
                    _m = self.save_checkpoint(getattr(self, "_ck_path", "ckpt.pkl"))
                    jqlog.info("[检查点] %s（成交 %d 笔，持仓 %d）"
                               % (_m["date"], _m["n_fills"], _m["n_pos"]))
                except Exception as _e:
                    jqlog.warn("[检查点] 写失败：%s" % _e)
        self._ck_i = len(self.cal) - 1
        self.phase_timings["loop"] = time.time() - t_phase
        if self.signal_recorder is not None:
            t_phase = time.time()
            self.signal_recorder.save()
            self.phase_timings["signal_save"] = time.time() - t_phase
            jqlog.info("[本地回测] 信号已记录：%s (%d 条)"
                       % (self.signal_recorder.path, len(self.signal_recorder.rows)))
        t_phase = time.time()
        self.report()
        self.phase_timings["report"] = time.time() - t_phase
        self.phase_timings["total"] = time.time() - self.t0
        return self

    # ---------- 报告 ----------
    def report(self):
        _outdir = getattr(self, "outdir", None)
        eq = pd.DataFrame(self.equity, columns=["date", "total_value",
                                                "positions_value", "cash", "n_pos",
                                                "susp_value", "susp_weight",
                                                "val_basis"])
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
                _cp = (os.path.join(_outdir, "corpaction.json")
                       if _outdir else "")
                if _cp and os.path.isfile(_cp):
                    import json as _json
                    _cad = _json.load(open(_cp, encoding="utf-8"))
                    _keys = ("n_restricted_codes", "n_locked_lots",
                             "n_valuation_pending", "n_odd_lot_unresolved",
                             "n_fractional_unresolved", "n_conv_failures")
                    _unres = any(int(_cad.get(_k, 0) or 0) for _k in _keys)
                    if _cad.get("perf_invalid_from"):
                        _unres, _bad_reason = True, "绩效自 %s 起失效" % _cad["perf_invalid_from"]
                elif _cp:
                    # report() 在 save() 之前运行；此时 corpaction.json 尚未写出，
                    # 不能因此把本次绩效标成无效。最终以 save() 的 acceptance 为准。
                    _unres, _bad_reason = False, ""
                else:
                    _unres, _bad_reason = False, ""
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
                                                "positions_value", "cash", "n_pos",
                                                "susp_value", "susp_weight",
                                                "val_basis"])
        fl = pd.DataFrame(self.fills)
        return eq, fl

    def save(self, outdir):
        self.outdir = outdir
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
        if self.phase_timing or _fc_config.phase_timing():
            self.phase_timings["total"] = time.time() - self.t0
            try:
                with open(os.path.join(outdir, "phase_timing.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(self.phase_timings, fh, ensure_ascii=False, indent=1)
            except Exception as _e:
                jqlog.warn("[本地回测] phase_timing.json 写出失败：%s" % _e)
        return outdir
