"""Native/vectorised kernels used by the fast path.

No C/Rust compiler is required on the target machine.  The "native" layer is
therefore built on:

  * NumPy C-level array kernels (always available)
  * Polars (Rust engine, optional) for columnar group-by / frame construction
  * Numba (optional, if installed) for JIT-compiled hot loops

The point is to keep the hot data path out of Python-level per-row loops while
providing a single place to plug in a future compiled extension.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

try:  # optional Rust-backed columnar engine
    import polars as _pl  # type: ignore
except Exception:  # pragma: no cover - optional
    _pl = None

try:  # optional JIT
    import numba as _numba  # type: ignore
except Exception:  # pragma: no cover - optional
    _numba = None

from . import config


def backend_name() -> str:
    if not config.native_enabled():
        return "numpy"
    if _pl is not None:
        return "polars"
    if _numba is not None:
        return "numba"
    return "numpy"


def backend_info() -> dict:
    return {
        "backend": backend_name(),
        "polars": getattr(_pl, "__version__", None),
        "numba": getattr(_numba, "__version__", None),
        "numpy": np.__version__,
    }


def long_frame(
    dates: np.ndarray,
    codes: Sequence[str],
    columns: Dict[str, np.ndarray],
    *,
    code_column: str = "code",
    time_column: str = "time",
    mask: Optional[np.ndarray] = None,
):
    """Build a code-major long DataFrame from a (n_dates, n_codes) matrix.

    This is deliberately NumPy-native: repeat/tile/ravel are C loops, unlike
    the old per-code ``slice_daily`` + ``pd.concat`` pattern.
    """
    import pandas as pd

    n_dates = len(dates)
    n_codes = len(codes)
    if n_dates == 0 or n_codes == 0:
        return pd.DataFrame(columns=[time_column, code_column, *columns])

    time = np.tile(np.asarray(dates, dtype=object), n_codes)
    code = np.repeat(np.asarray(codes, dtype=object), n_dates)
    data = {name: np.asarray(values).T.reshape(-1) for name, values in columns.items()}
    keep = None if mask is None else np.asarray(mask, dtype=bool).reshape(-1)
    frame = {time_column: time, code_column: code}
    frame.update(data)

    # Native columnar materialisation.  Polars is a Rust engine; if it is not
    # available we fall back to pandas (which is itself backed by NumPy).
    if config.native_enabled() and _pl is not None:
        try:
            payload = {k: (v[keep] if keep is not None else v)
                       for k, v in frame.items()}
            return _pl.DataFrame(payload).to_pandas()
        except Exception:
            pass

    df = pd.DataFrame(frame)
    if keep is not None:
        df = df.loc[keep].reset_index(drop=True)
    return df


def frame_from_flat(
    time_values: np.ndarray,
    code_values: np.ndarray,
    columns: Dict[str, np.ndarray],
    *,
    time_column: str = "time",
    code_column: str = "code",
):
    """Materialise an already-flat code-major buffer.

    Polars (Rust) is used when the native backend is enabled; otherwise the
    pandas path is used.  Both receive the same C-level NumPy buffers.
    """
    payload = {time_column: time_values, code_column: code_values}
    payload.update(columns)
    if config.native_enabled() and _pl is not None:
        try:
            return _pl.DataFrame(payload).to_pandas()
        except Exception:
            pass
    import pandas as pd
    return pd.DataFrame(payload)


def group_sum_by_date(
    dates: np.ndarray,
    values: np.ndarray,
    *,
    min_count: int = 1,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Group values by an already-aligned date axis.

    Returns (dates, sums, counts).  NumPy ``nansum`` is used so missing stock
    rows do not poison a sector total.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    sums = np.nansum(arr, axis=1)
    counts = np.sum(~np.isnan(arr), axis=1)
    keep = counts >= int(min_count)
    return np.asarray(dates, dtype=object)[keep], sums[keep], counts[keep]


def rolling_mean(values: Iterable[float]) -> float:
    arr = np.asarray(list(values), dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    return float(arr.mean()) if len(arr) else 0.0
