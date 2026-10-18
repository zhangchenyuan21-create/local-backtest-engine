# Live adapters

This directory contains optional, dry-run-by-default adapters for VN.PY and
QMT/xtquant. They are not imported by the backtest engine and are not installed
by default.

## Safety rules

- `dry_run=True` by default.
- No account, password, token, cookie or broker credential belongs in this repo.
- Run in a broker simulation environment first.
- Validate order status, cancellation, partial fills, reconnect and position
  reconciliation before enabling live orders.
- `submit_order` must never be called by a backtest loop.

## VN.PY

```python
from live import VnpyAdapter

adapter = VnpyAdapter.from_vnpy(main_engine, "CTP", dry_run=True)
print(adapter.get_account())
print(adapter.health())
```

The adapter lazy-imports `vnpy.trader.*` only inside the order methods. Pass
`dry_run=False` only after the gateway and account are verified.

## QMT / xtquant

```python
from live import QmtAdapter

adapter = QmtAdapter.from_xtquant(trader, account, dry_run=True)
print(adapter.get_positions())
```

QMT wrappers differ by broker and xtquant version. The adapter intentionally
keeps order constants and client behavior explicit; verify them against your
QMT build. You can also pass your own client object:

```python
adapter = QmtAdapter(my_client, account_id="...", dry_run=True)
```