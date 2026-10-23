"""Fast-path configuration for the local backtest engine.

All switches are environment-variable driven so the same engine can run in
legacy mode for A/B comparison:

  JQSHIM_FAST=0/1          master switch for array/panel fast paths (default 1)
  JQSHIM_NATIVE=0/1        native columnar backend (Polars / NumPy kernels)
  JQSHIM_PERF=0/1          performance mode: INFO logs suppressed
  JQSHIM_FAST_STRICT=0/1   re-raise fast-path errors instead of falling back
  JQSHIM_PANEL_MIN_CODES   minimum batch size before using the array path
  JQSHIM_PANEL_MAX_BYTES   soft cap for cached flow panels
"""
from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on", "y"}

# Set to False in the 1-4 (no-native) engine copy.
DEFAULT_NATIVE = True


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in _TRUE


def fast_enabled() -> bool:
    return env_bool("JQSHIM_FAST", True)


def native_enabled() -> bool:
    return env_bool("JQSHIM_NATIVE", DEFAULT_NATIVE)


def perf_enabled() -> bool:
    return env_bool("JQSHIM_PERF", False)


def strict_fast() -> bool:
    return env_bool("JQSHIM_FAST_STRICT", False)


def panel_min_codes() -> int:
    try:
        return max(2, int(os.environ.get("JQSHIM_PANEL_MIN_CODES", "12")))
    except ValueError:
        return 12


def flow_summary_min_codes() -> int:
    try:
        return max(16, int(os.environ.get("JQSHIM_FLOW_MIN_CODES", "32")))
    except ValueError:
        return 200


def panel_max_bytes() -> int:
    try:
        return max(16 * 1024 * 1024,
                   int(os.environ.get("JQSHIM_PANEL_MAX_BYTES", str(384 * 1024 * 1024))))
    except ValueError:
        return 384 * 1024 * 1024


def phase_timing() -> bool:
    return env_bool("JQSHIM_PHASE_TIMING", False)


def info() -> dict:
    return {
        "fast": fast_enabled(),
        "native": native_enabled(),
        "perf": perf_enabled(),
        "strict": strict_fast(),
        "panel_min_codes": panel_min_codes(),
        "flow_summary_min_codes": flow_summary_min_codes(),
        "panel_max_bytes": panel_max_bytes(),
    }

