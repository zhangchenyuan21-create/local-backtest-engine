"""Pre-aggregated money-flow window sums.

The strategy only needs *aggregates* for most flow calls:

  * sector flow  = sum of constituent stock net amounts per date
  * market mean  = mean of the daily all-market sum

The old path built one dict/DataFrame row per (stock, date) and then grouped
them in pandas.  For 3,800 stocks x 8 days that is ~30k Python rows on every
call.  This module keeps a (date x stock) float matrix plus a cumulative-count
matrix for the requested stock set and reduces it with NumPy ``nansum``.

The cumulative-count matrix is what preserves the old semantics exactly: the
window is the last N *observations* per stock, not the last N calendar days, so
suspended/delisted stocks with gaps behave identically to the original code.

Units are preserved exactly: raw flow values are summed first, then divided by
1e4 to match ``get_money_flow``'s ``net_amount_main`` unit.  Callers that
multiply by 1e4 (the existing strategy) therefore get the same raw total.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from typing import Iterable, List, Sequence, Tuple

import numpy as np

from .. import store
from . import config


class FlowPanel:
    """Dense (calendar x stock) matrix of raw ``netamount`` values."""

    def __init__(self, codes: Sequence[str]):
        self.codes = tuple(codes)
        cal = store.load_calendar()
        self.dates = np.asarray(cal, dtype=object)
        self._date_list = list(cal)
        n_dates, n_codes = len(self.dates), len(self.codes)
        self.net = np.full((n_dates, n_codes), np.nan, dtype=np.float64)
        self.cum = np.zeros((n_dates, n_codes), dtype=np.int32)
        for j, code in enumerate(self.codes):
            darr, narr = store.flow_arrays(code)
            if darr is None or narr is None or len(darr) == 0:
                continue
            idx = np.searchsorted(self.dates, darr, side="left")
            ok = idx < n_dates
            if not ok.any():
                continue
            sel = np.where(ok)[0]
            hit = self.dates[idx[sel]] == darr[sel]
            sel = sel[hit]
            rows = idx[sel]
            self.net[rows, j] = narr[sel]
            self.cum[rows, j] = 1
        if n_dates and n_codes:
            self.cum = np.cumsum(self.cum, axis=0, dtype=np.int32)

    @property
    def nbytes(self) -> int:
        return int(self.net.nbytes + self.cum.nbytes)

    def sum_window(self, end_date: str, count: int):
        end = str(end_date)[:10]
        hi = bisect_right(self._date_list, end)
        if hi <= 0:
            return (np.asarray([], dtype=object), np.asarray([], dtype=float),
                    np.asarray([], dtype=int))
        cum_end = self.cum[hi - 1, :]
        threshold = cum_end - int(count)
        valid = ((self.cum[:hi, :] > threshold[None, :])
                 & (self.cum[:hi, :] > 0)
                 & ~np.isnan(self.net[:hi, :]))
        sub = np.where(valid, self.net[:hi, :], np.nan)
        sums = np.nansum(sub, axis=1)
        counts = np.sum(valid, axis=1)
        keep = counts > 0
        return self.dates[:hi][keep], sums[keep], counts[keep]


_PANELS: "OrderedDict[Tuple[str, ...], FlowPanel]" = OrderedDict()
_PANEL_BYTES = 0


def _norm(codes: Iterable[str]) -> Tuple[str, ...]:
    from .. import normalize_code
    seen = set()
    out: List[str] = []
    for c in codes:
        c = normalize_code(str(c))
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    # order does not matter for a sum; sorted gives stable cache keys
    return tuple(sorted(out))


def get_panel(codes: Iterable[str]) -> FlowPanel:
    global _PANEL_BYTES
    key = _norm(codes)
    if key in _PANELS:
        _PANELS.move_to_end(key)
        return _PANELS[key]
    panel = FlowPanel(key)
    limit = config.panel_max_bytes()
    if panel.nbytes <= limit:
        _PANELS[key] = panel
        _PANEL_BYTES += panel.nbytes
        while _PANEL_BYTES > limit and _PANELS:
            old_key, old = _PANELS.popitem(last=False)
            _PANEL_BYTES -= old.nbytes
    # If the panel is larger than the cache limit, return it un-cached.
    return panel


def raw_summary(codes: Iterable[str], end_date: str, count: int):
    """Return (dates, raw_sum, counts) for the requested stock set."""
    panel = get_panel(codes)
    return panel.sum_window(end_date, count)


def summary_frame(codes: Iterable[str], end_date: str, count: int):
    """DataFrame with the same aggregate unit as get_money_flow()."""
    import pandas as pd
    dates, sums, counts = raw_summary(codes, end_date, count)
    if len(dates) == 0:
        return pd.DataFrame(columns=["date", "net_amount_main"])
    return pd.DataFrame({
        "date": dates.astype(str),
        "net_amount_main": sums / 1e4,
        "n_codes": counts,
    })


def market_mean(codes: Iterable[str], end_date: str, days: int) -> float:
    """Mean of the daily all-market raw flow sum (same as daily.mean())."""
    dates, sums, _ = raw_summary(codes, end_date, days)
    if len(sums) == 0:
        return 0.0
    return float(np.mean(sums))


def info() -> dict:
    return {"panels": len(_PANELS), "bytes": _PANEL_BYTES,
            "limit": config.panel_max_bytes()}
