# 本地回测引擎（JoinQuant 兼容）


一个从日常量化研究里抽离出来的**本地日线回测引擎**。它用同名 `jqdata` 替身让聚宽风格的策略文件尽量少改地跑在本地，数据落在自己的目录里，不依赖聚宽服务器回测。

> **欢迎提交更好的修改。** 如果你发现撮合、T+1、费用、滑点、数据适配或性能方面有更好的实现，欢迎直接提交 Pull Request，或者在 Issues 里说明思路和复现方式。这个项目就是希望把本地回测做得更透明、更可验证。

## 特性

- 聚宽风格策略加载：`from jqdata import *`、`initialize`、`run_daily`、`g`、`context`
- 日线回测，默认 `next_open` 成交，避免用当日收盘价决策后又在当日成交
- T+1：当日买入的股票当日不可卖
- 停牌：无当日行情则不成交
- 涨跌停与成交量参与率约束
- A 股交易细节：100 股整手、最低佣金、印花税、过户费、滑点
- `order` / `order_target` / `order_target_value` / `order_target_percent`
- 定时任务按时间排序执行：开盘前、开盘后、尾盘、收盘后
- 涨跌停约束：主板 10%、ST 5%、创业板/科创板 20%、北交所 30%，按前一交易日收盘价计算
- 成交量约束：默认最多吃掉当日成交量的 25%，可配置；按整手部分成交
- 公司行动、停牌和受限持仓台账
- v0.2.0：绩效/风险报告、因子 IC/分层评价、参数网格搜索
- 输出 `equity.csv`、`fills.csv`、`run.log` 等可审计产物
- 数据目录可切换，便于对比不同数据源

这不是聚宽官方产品，也不保证与聚宽回测逐笔一致。用于研究复现和策略比较。

## 快速开始

### 1. 安装

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

要求 Python 3.10+。

### 2. 准备数据

引擎不附带行情数据。默认数据根目录是 `./data`，可用 `JQSHIM_DATA` 指定其他目录。

如果使用聚宽 Data API：

```powershell
$env:JQDATA_USERNAME = "你的聚宽账号"
$env:JQDATA_PASSWORD = "你的聚宽密码"

python -m jqdata.fetch_cli status
python -m jqdata.fetch_cli calendar
python -m jqdata.fetch_cli securities
python -m jqdata.fetch_cli sectors
python -m jqdata.fetch_cli daily
python -m jqdata.fetch_cli flow
```

也可以直接执行：

```powershell
python -m jqdata.fetch_cli all
```

全量抓取可能耗时较长，请先确认账号权限、网络和数据源使用条款。数据目录结构见 `jqdata/store.py` 顶部说明，主要文件是：

```text
data/
  daily/<code>.parquet
  daily_qfq/<code>.parquet
  financial/<code>.parquet
  moneyflow/<code>.parquet
  index/<index>.json
  calendar.json
  securities.json
```

默认优先读取前复权日线；可用 `JQSHIM_NO_QFQ=1` 强制使用不复权日线做诊断。

`daily/*.parquet` 的 `volume` 必须是**股数**，不是手数。成交量上限按 `volume × order_volume_ratio` 计算；若数据源给的是手数，请先乘以 100。

## 执行约束配置

在策略的 `initialize` 里可以设置：

```python
set_option("order_volume_ratio", 0.25)   # 最多吃掉当日成交量的 25%
set_option("match_with_volume_limit", True)
set_option("limit_mode", "open")         # open / one_price / off
```

也可以用环境变量覆盖涨跌停模式：

```powershell
$env:JQSHIM_LIMIT_MODE = "open"
$env:JQSHIM_VOLUME_RATIO = "0.25"
# $env:JQSHIM_VOLUME_LIMIT = "off"   # 关闭成交量约束（仅对照实验）
```

- `open`：默认，开盘价触及涨停不买、触及跌停不卖
- `one_price`：只有全天一字板才拒绝，盘中打开的涨停/跌停允许成交
- `off`：关闭涨跌停约束（仅用于对照实验）

成交量上限触发时会自动缩量；买入保持 100 股整手，部分卖出尽量保持整手，清仓时允许零股。

尚未覆盖：新股上市前几日的无涨跌幅限制、退市整理期、集合竞价撮合、盘口队列、逐笔成交和日内部分成交路径。

### 3. 运行示例策略

```powershell
python runner_main.py status
python runner_main.py run `
  --strategy examples/demo_strategy.py `
  --cash 350000 `
  --start 2020-01-01 `
  --end 2020-12-31 `
  --out out/demo `
  --force
```

`examples/demo_strategy.py` 只是一个最小可运行示例，不是投资建议。

### 4. 常用参数

```text
--fill-mode next_open|same_close   成交时点，默认 next_open
--strict-no-future                 更严格的未来数据截断
--start / --end                    回测区间
--cash                             初始资金
--out                              输出目录
--force                            覆盖已有输出目录
```

### 5. 对账

如果你有聚宽侧日志和成交 CSV，可以用：

```powershell
python runner_main.py compare `
  --out out/demo `
  --jq-log logs/joinquant-log.txt `
  --jq-csv logs/transaction.csv
```

对账指标和口径写在 `runner_main.py` 顶部注释里。

## 研究工具（v0.2.0）

以下工具只读取回测产物，不改变 `runner_main.py run` 的撮合行为。

### 绩效/风险报告

```powershell
python -m analytics --out out/demo --format md,json,html
```

输出 `metrics.json`、`report.md`、`report.html`，包含总收益、年化收益、波动、Sharpe、Sortino、Calmar、最大回撤、日胜率、交易胜率、盈亏比、平均持仓天数、换手率和费用。

### 因子 IC/分层评价

```powershell
python -m factors --data data --factor momentum --window 20 --horizon 5 --out out/factor
```

支持 `momentum`、`reversal`、`volatility`、`ma_gap`、`rsi`；输出 IC、RankIC、ICIR、t 统计量、分层收益、多空收益和换手率。

### 参数网格搜索

```powershell
python -m optimize `
  --template variants/demo_template.py `
  --grid grid.json `
  --cash 350000 `
  --start 2020-01-01 `
  --end 2024-12-31 `
  --out out/optimize `
  --objective sharpe `
  --workers 4
```

模板中使用 `{{fast}}`、`{{slow}}` 这样的占位符；也可以直接传入一组策略文件。每个变体使用独立子进程和独立输出目录，不会干扰当前正在运行的回测。
## 输出

一次回测至少产生：

```text
out/demo/
  equity.csv      每日权益、现金、持仓市值、持仓数
  fills.csv       逐笔成交、费用、成交后现金
  run.log         运行日志
  corpaction.json 公司行动/受限持仓摘要（若适用）
```

`equity.csv` 的字段：

```text
date,total_value,positions_value,cash,n_pos
```

`fills.csv` 的字段：

```text
date,code,side,price,amount,value,fee,cash_after,run,run_date
```

## 目录

```text
runner.py            回测驱动器：加载策略、逐日循环、撮合、估值
runner_main.py       命令行：run / compare / status
analytics.py         绩效与风险指标、Markdown/HTML 报告
factors.py           因子计算、IC、分层、多空和换手
optimize.py          参数网格/策略变体批量回测
jqdata/              聚宽 API 替身与本地数据访问层
  api.py             get_price / attribute_history / 财务查询等
  trade.py           组合、订单、费用、T+1、成交
  corpaction.py      公司行动与受限持仓
  store.py           parquet/json 数据访问
  fetch.py           聚宽 Data API 抓取
  fetch_cli.py       数据抓取 CLI
examples/            最小策略示例
tests/               自检与撮合回归测试
```

## 测试

```powershell
python tests/test_shim.py
python tests/test_trade.py
python -m unittest discover -s tests -p "test_smoke.py" -v
python -m unittest tests.test_analytics tests.test_factors tests.test_optimize -v
```

`test_trade.py` 使用自造数据测试撮合链路，不访问网络。`test_shim.py` 验证替身 API 契约和导入链。`test_analytics.py`、`test_factors.py`、`test_optimize.py` 验证新增研究工具。

## 数据与代码边界

仓库不包含：

- 真实行情、财务、资金流或指数成分数据
- 任何账号、密码、Cookie、Tushare token 或其他凭证
- 个人回测输出、日志、研究笔记

请不要把上述内容提交到仓库。数据目录、`out/`、`logs/`、`*.parquet` 和 `.env` 已加入 `.gitignore`。

## 贡献

**如有更好的修改，欢迎上传/提交。**

- 发现 bug：提交 Issue，附最小复现策略、数据字段和预期/实际结果
- 改进撮合、费用、滑点、T+1、公司行动：请附回归测试
- 适配新的数据源：请说明字段映射和缺失值处理
- 性能优化：请附基准数据、耗时对比和内存占用
- 修改公共 API：请保持聚宽策略兼容，并更新 README

提交前请至少运行：

```powershell
python -m unittest discover -s tests -p "test_smoke.py" -v
python tests/test_trade.py
```

## 免责声明

本项目仅用于量化研究和软件工程学习，不构成投资建议。历史回测不代表未来收益。使用任何数据源和交易接口前，请自行确认其授权、条款和合规要求。JoinQuant/聚宽是其权利人的商标，本项目与聚宽官方无隶属关系。



Limit prices are derived from the local previous close and board-specific percentages. With qfq (adjusted) data they are research approximations, not live-order prices.

Current regression: 43/43 execution, 45/45 jqdata shim, 4/4 smoke, 11/11 analytics/factors/optimize.
Release: https://github.com/zhangchenyuan21-create/local-backtest-engine/releases
