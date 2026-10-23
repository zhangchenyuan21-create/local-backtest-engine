# Changelog

## v0.8.0 - 2026-10-23

### Performance fast path

- Add `jqdata/fastcore/`: cross-section NumPy array path for `get_price` / `get_money_flow`, pre-aggregated money-flow panels, and signal record/replay.
- Add `runner_main.py` flags: `--record-signals`, `--replay-signals`, `--legacy`, `--native`/`--no-native`, `--perf`, `--no-audit`, `--phase-timing`.
- Legacy behavior remains available with `--legacy`; all fast paths fall back on errors.
- E1 16-day validation: fills/equity identical to legacy; fast record 3.9x total, 5.1x daily loop; signal replay ~1.1s for the same window.

### Tushare data compatibility

- Add runtime data-root overrides: `JQSHIM_DATA`, `JQSHIM_DAILY_DIR`, `JQSHIM_QFQ_DIR`, `JQSHIM_SECTOR_DIR`, `JQSHIM_NEWDATA_ROOT`.
- Money-flow parquet keeps Tushare's raw 万元 unit; `FLOW_RAW_SCALE` converts on read/write and is now configurable via `JQSHIM_FLOW_RAW_SCALE` (set `1` when the file is already in 元).
- `JQSHIM_KEEP_BJSE` controls BJSE handling; `JQSHIM_NO_QFQ` selects unadjusted data.

### Backtest correctness and operability

- Add checkpoint/resume: `--resume`, `--checkpoint-every`, `--checkpoint-path`; checkpoint includes portfolio, `g`, equity/fills and corporate-action audit state.
- Separate valuation price from tradable price: suspended positions are marked at the last known price (or index proxy) and cannot trade; `equity.csv` now includes `susp_value`, `susp_weight`, `val_basis`.
- End-of-backtest close settlement for pending sell orders (`flush_orders_at_close`), so the final day's sell signals are not lost.
- `acceptance.json`, `corpaction.json` and `unresolved_ledger.csv` are written for audit; `report()` no longer marks a run invalid merely because `corpaction.json` is written after the report.
- Log level/noise controls: `JQSHIM_LOG_SUPPRESS`, `--perf`.

### Tests

- 45 jqdata shim, 43 execution, 4 smoke, 4 fastcore, 37 research/report/broker/Qlib/live = 133 tests passing.

## v0.7.0

- Add `analysis_report.py`: post-backtest report covering performance/risk, factor IC, optimization/DSR, walk-forward, ML, paper, reconciliation, risk flags and readiness.
- Add `runner_main.py run --report` as an opt-in; default backtest behavior is unchanged.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 37 report/research/broker/Qlib/live (1 optional ML dependency may skip).

## v0.6.0

- Add optional `ml_models.py` for sklearn Ridge/HistGradientBoosting and LightGBM; Ridge remains the default.
- Add `reconcile.py` for read-only PaperBroker vs THS order/fill reconciliation.
- Add `qlib_bridge.py` for Qlib-friendly CSV export and prediction-signal import.
- Add `live/` dry-run-by-default VN.PY and QMT adapter skeletons with lazy SDK imports.
- Default `runner_main.py run` and `jqdata/trade.py` behavior remains unchanged.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 35 research/broker/Qlib/live (1 optional ML dependency may skip).

## v0.5.0

- Add `broker.py`: broker protocol, in-memory PaperBroker, and lazy ThsSimBroker adapter (dry-run by default).
- Add `paper_trade.py`: signal-driven paper replay with T+1, lot sizing, price limits, volume participation and fees.
- Default `runner_main.py run` and `jqdata/trade.py` behavior remains unchanged.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 24 analytics/broker/factors/ml/optimize/overfit/walkforward.

## v0.4.0

- Add `ml.py`: walk-forward Ridge training, out-of-sample `signal.csv` / `signal.parquet`, `ml_summary.json`.
- Add `examples/signal_strategy.py`: optional strategy that consumes the signal file through the existing execution engine.
- Default `runner_main.py run` and `jqdata/trade.py` behavior remains unchanged.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 20 analytics/factors/ml/optimize/overfit/walkforward.

## v0.3.0

- Add `overfit.py`: PSR, expected maximum Sharpe and Deflated Sharpe Ratio.
- Add `walkforward.py`: purge/embargo splits, rolling/expanding windows, train-only parameter selection and out-of-sample evaluation.
- Add Deflated Sharpe diagnostics to `optimize.py` `best.json`.
- No changes to `runner_main.py run` defaults or execution semantics.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 17 analytics/factors/optimize/overfit/walkforward.

## v0.2.0

- Add `analytics.py`: return/risk metrics, trade FIFO reconstruction, Markdown/JSON/HTML reports.
- Add `factors.py`: momentum/reversal/volatility/MA-gap/RSI factors, IC/RankIC/ICIR, quantile and long-short evaluation, turnover.
- Add `optimize.py`: template grid expansion, parallel subprocess backtests, objective ranking, `trials.csv`/`best.json`.
- No changes to existing `runner_main.py run` defaults or execution semantics.
- Tests: 43 execution, 45 jqdata shim, 4 smoke, 11 analytics/factors/optimize.

## v0.1.0

- First public release.
- JoinQuant-compatible daily engine.
- T+1, lot size, commission, stamp duty, transfer fee, slippage.
- Board-aware price limits and volume participation constraints.
- Corporate-action and restricted-position ledger.
