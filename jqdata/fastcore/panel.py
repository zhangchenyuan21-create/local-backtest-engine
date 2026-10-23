"""Cross-section array builder for the jqdata shim.

The old ``get_price`` / ``get_money_flow`` implementation looped over every
stock, sliced a pandas DataFrame and concatenated one frame per stock.  For a
1,250-stock universe that means 1,250 DataFrame slices and one large concat on
every call.

This module inverts the access pattern: it reads the already-cached per-stock
NumPy arrays from ``jqdata.store``, builds one flat code-major buffer per
field, and materialises the long DataFrame once with C-level concatenate and
repeat.  It deliberately keeps the old *per-code last N rows* semantics, so a
suspended stock with missing dates still gets its last N existing bars rather
than the last N calendar days.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .. import store
from . import config, native


def _daily_arrays(code: str, fields: Sequence[str]):
    # Always read the requested columns as float64.  snap_arrays stores
    # close/pctChg as float32 for the snapshot hot path, which is fine for
    # screening but loses ~1e-6 on prices; get_price must match the legacy
    # float64 pandas path exactly.
    earr, ecols = store.extra_arrays(code, fields)
    if earr is not None and ecols:
        return earr, ecols
    return store.snap_arrays(code)


def _last_n(arr, end_date: str, count: int):
    if arr is None or len(arr) == 0:
        return 0, 0
    hi = int(np.searchsorted(arr, str(end_date)[:10], side="right"))
    if hi <= 0:
        return 0, 0
    lo = max(0, hi - int(count))
    return lo, hi


def price_window(
    codes: Sequence[str],
    end_date: str,
    count: int,
    fields: Sequence[str],
) -> Optional[pd.DataFrame]:
    """Vectorised replacement for the long-table branch of get_price()."""
    if not codes or not fields or not count:
        return None
    dates_chunks: List[np.ndarray] = []
    code_chunks: List[np.ndarray] = []
    field_chunks: Dict[str, List[np.ndarray]] = {f: [] for f in fields}
    for code in codes:
        arr, cols = _daily_arrays(code, fields)
        lo, hi = _last_n(arr, end_date, count)
        if hi <= lo:
            continue
        n = hi - lo
        dates_chunks.append(np.asarray(arr[lo:hi], dtype=object))
        code_chunks.append(np.full(n, code, dtype=object))
        for f in fields:
            vals = cols.get(f) if cols else None
            field_chunks[f].append(
                np.asarray(vals[lo:hi], dtype=np.float64) if vals is not None
                else np.full(n, np.nan, dtype=np.float64)
            )
    if not dates_chunks:
        return pd.DataFrame(columns=["time", "code", *fields])
    time = np.concatenate(dates_chunks)
    code_arr = np.concatenate(code_chunks)
    data = {f: np.concatenate(field_chunks[f]) for f in fields}
    return native.frame_from_flat(time, code_arr, data,
                                   time_column="time", code_column="code")


def price_wide(
    codes: Sequence[str],
    end_date: str,
    count: int,
    field: str,
) -> Optional[pd.DataFrame]:
    df = price_window(codes, end_date, count, [field])
    if df is None or df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="time", columns="code", values=field,
                          aggfunc="last")


def _pct_on_dates(code: str, dates: np.ndarray) -> np.ndarray:
    arr, cols = store.snap_arrays(code)
    out = np.full(len(dates), np.nan, dtype=np.float64)
    if arr is None or cols is None:
        return out
    pc = cols.get("pctChg")
    if pc is None:
        return out
    idx = np.searchsorted(arr, dates, side="left")
    ok = idx < len(arr)
    if not ok.any():
        return out
    sel = np.where(ok)[0]
    hit = arr[idx[sel]] == dates[sel]
    sel = sel[hit]
    out[sel] = pc[idx[sel]]
    return out


def flow_window(
    codes: Sequence[str],
    end_date: str,
    count: int,
    fields: Sequence[str],
) -> Optional[pd.DataFrame]:
    """Vectorised replacement for get_money_flow()'s long-table path."""
    if not codes or not count:
        return None
    fields = list(fields)
    dates_chunks: List[np.ndarray] = []
    code_chunks: List[np.ndarray] = []
    net_chunks: List[np.ndarray] = []
    pct_chunks: List[np.ndarray] = []
    for code in codes:
        darr, narr = store.flow_arrays(code)
        lo, hi = _last_n(darr, end_date, count)
        if hi <= lo or narr is None:
            continue
        d = np.asarray(darr[lo:hi], dtype=object)
        n = hi - lo
        dates_chunks.append(d)
        code_chunks.append(np.full(n, code, dtype=object))
        net_chunks.append(np.asarray(narr[lo:hi], dtype=np.float64))
        pct_chunks.append(_pct_on_dates(code, d))
    if not dates_chunks:
        return pd.DataFrame(columns=fields)
    out = native.frame_from_flat(
        np.concatenate(dates_chunks),
        np.concatenate(code_chunks),
        {"net_amount_main": np.concatenate(net_chunks) / 1e4,
         "change_pct": np.concatenate(pct_chunks)},
        time_column="date", code_column="sec_code",
    )
    for f in fields:
        if f not in out.columns:
            out[f] = np.nan
    return out[fields]


def info() -> dict:
    return {
        "backend": native.backend_name(),
        "min_codes": config.panel_min_codes(),
    }
