# Changelog

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