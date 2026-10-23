# 本地回测引擎性能改造（opt5 / opt4）

日期：2026-10-20
主目录：`E:\work area\local_backtest_engine_public`（opt5：改造 1-5）
对照目录：`E:\work area\local_backtest_engine_opt4`（opt4：改造 1-4，native 默认关闭）

## 0. 备份与回滚

原引擎已备份为：

- `E:\work area\local_backtest_engine_public_backup_20261020.zip`
- `E:\work area\local_backtest_engine_public_backup_20261020\`

回滚方式：把备份目录内容覆盖回 `local_backtest_engine_public` 即可。
opt4 是从原引擎复制后实施 1-4 的独立副本，不依赖主目录的新代码。

## 1. 五项改造

### 改造 1：信号生成移出回测循环（记录 / 回放）

新增 `jqdata/fastcore/signals.py`，并在 `jqdata/trade.py` 的唯一落单入口
`_append_pending()` 记录每一笔下单意图。记录的是 `(signal_date, signal_time,
code, signed_qty)`，不是成交；回放时仍走原来的 `flush_orders()`，所以 T+1、
涨跌停、成交量上限、费用、滑点、公司行动全部保留。

第一次运行正常跑完整策略，同时记录信号：

    python runner_main.py run --strategy ..\joinquant_strategy_V52_BASE.py --start 2020-01-02 --end 2020-01-31 --out out\e1_record --record-signals out\e1_signals.csv --phase-timing

后续只回放信号，不再执行 initialize / 选股 / 资金流 / 卖出检查：

    python runner_main.py run --replay-signals out\e1_signals.csv --out out\e1_replay --phase-timing

产物：`e1_signals.csv` + `e1_signals_meta.json`（含 cash、fill_mode、费用/滑点
等 `jq_options`，以及 `JQSHIM_SLIP_MODEL` 等执行环境变量）。回放会自动恢复这些元数据。

### 改造 2：截面数组替代逐股 pandas

新增 `jqdata/fastcore/panel.py`，`get_price()` / `get_money_flow()` 在批量
（默认 >=12 只）时改走：

1. 直接读 `store.snap_arrays()` / `store.extra_arrays()` / `store.flow_arrays()` 的 NumPy 数组；
2. 按"每只股票自己的最后 N 根"切片，保持旧语义（停牌/缺失日期不会错位）；
3. 用 `np.concatenate` + 一次 DataFrame 构造替代"逐股 DataFrame 切片 + concat"。

旧的逐股路径保留；小批量或 `count=None` 自动回退。

### 改造 3：资金流预聚合

新增 `jqdata/fastcore/flow_agg.py`：

- 对请求的股票集合建立 `(交易日 x 股票)` 的资金流矩阵；
- 用累计计数矩阵精确选择"每只股票最后 N 条资金流"，再用 `np.nansum` 聚合；
- 结果按旧 `get_money_flow()` 的单位返回（原始净额 / 1e4），现有策略的 `* 10000` 保持一致；
- 面板 LRU 缓存，默认 384MB。

引擎在 `runner.install_fast_flow_patch()` 中把 V52 家族的
`get_money_flow_batch()` 路由到新 API；如果策略不是这种"只做按日求和"的
用法，可设 `JQSHIM_FAST_FLOW=0` 关闭。

### 改造 4：性能模式与审计开关

- `--perf`：把日志级别设为 WARN，并跳过 INFO 的字符串格式化；
- `--no-audit`：跳过只读的 `scan_restricted` / `scan_locked` 扫描（公司行动结算 `settle_terminated` / `apply_conversions` 仍执行）；
- 无持仓时自动跳过受限持仓扫描；
- `--phase-timing` 输出 `phase_timing.json`（initialize / loop / report / total）。

### 改造 5：native / 向量化后端

opt5 新增 `jqdata/fastcore/native.py`：

- 优先使用 Polars（Rust 列式引擎）构造长表；
- 始终使用 NumPy C 层数组内核；
- 可选 Numba JIT（若环境安装）；
- `--native` / `--no-native` 可切换；`native.backend_info()` 可查看后端。

opt4 的 `native.py` 是 NumPy-only stub，且 `DEFAULT_NATIVE=False`，即 opt4
不包含第 5 项的原生/Polars 后端。

## 2. 正确性验证

### 单元测试

- `tests/test_fastcore.py`：4/4 通过（截面窗口、资金流窗口、资金流聚合、信号记录/回放）。
- `tests/test_smoke.py`：4/4 通过。
- `tests/test_trade.py`：43/43 通过。
- `tests/test_shim.py`：44/44 通过。

### 端到端对照（E1_no_gaokai，2020-01-02 ~ 2020-01-31，16 个交易日）

| 版本 | 成交 | 期末资产 | 总收益 | 最大回撤 |
|---|---:|---:|---:|---:|
| 原引擎（legacy） | 15 | 352,143 | +0.61% | -3.69% |
| opt5 fast | 15 | 352,143 | +0.61% | -3.69% |
| opt4 fast | 15 | 352,143 | +0.61% | -3.69% |
| opt5 replay | 15 | 352,143 | +0.61% | -3.69% |

`fills.csv`（排除 `run`/`run_date` 列）与 `equity.csv` 逐行完全一致。

## 3. 性能对比

同一台机器、同一份数据、同一区间。时间包含启动/数据加载；不是完整
2,856 日全周期的实测值。

| 场景 | initialize | 日循环 | total |
|---|---:|---:|---:|
| 原引擎 legacy，16 日 | 31.6s | 168.5s | 200.1s |
| opt5 fast，16 日 | 17.8s | 33.0s | 50.9s |
| opt4 fast，16 日 | 18.0s | 32.5s | 50.5s |
| opt5 replay，16 日 | 0.0s | 0.12s（内部） | 1.1s（进程墙钟） |

关键倍数：

- fast record vs legacy：3.9x 总时间，5.1x 日循环；
- replay vs legacy：182x 进程墙钟（但 replay 跳过了信号生成，不是同工作量）；
- 先 record 再 replay 的总成本：50.9 + 1.1 = 52.0s，仍是 legacy 的 3.8x 加速；
- opt5 vs opt4：50-51s 量级，差异在噪声内；本次工作负载下第 5 项不是瓶颈。

### 单点微基准

- `get_price`：1,250 只 x 20 日
  - legacy：cold 3.99s / warm 1.07s
  - fast：cold 2.5s / warm 0.01s
- 全市场资金流聚合（3,849 只，count=8）
  - legacy：8.0s
  - fast：2.5s（flow 数组已热）
  - 结果逐日完全一致（最大差异 1.2e-10，来自求和顺序）
- `--perf` 日志体积（E1 5 日）：19,065 -> 2,844 字节

### 全周期粗估

原 E1 全周期实测 31,528.5s。按 16 日样本的日循环比例（10.53s/日 -> 2.06s/日）
粗估，fast record 全周期约 1.6-2.2 小时；按 replay 的 0.0069s/日粗估，全周期
replay 约 1-2 分钟（不含首次数据加载/信号生成）。这是估算，不是全周期实测。

## 4. 结论与限制

1. 最大的收益来自改造 1+2+3：把选股/资金流/逐股 pandas 从日循环里拿掉，而不是把撮合循环换成 Rust。
2. 改造 5 本次没有明显收益：热路径已经是 NumPy 数组，Polars 构造长表的开销与 pandas 同量级；若未来要原生撮合，建议直接接 AKQuant 的 Rust 引擎，而不是在本地重写一套。
3. record 仍是完整策略成本：改造 1 是"一次生成、多次回放"，不是把信号生成本身向量化。要压缩首次成本，需要继续改造 `screen_candidates` 的横截面计算。
4. `get_money_flow_batch` 的 fast patch 只适用于"按日求和"的调用方；需要逐股资金流明细的策略请设 `JQSHIM_FAST_FLOW=0`。
5. `--no-audit` 只跳过只读扫描，不改变公司行动结算；但性能对照产物不应作为最终净值裁决。
6. 所有 fast path 都有回退；`JQSHIM_FAST=0` 可随时回到 legacy 行为。
