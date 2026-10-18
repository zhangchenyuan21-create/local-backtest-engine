# Changelog

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
