"""Signal recording and replay.

This is the local-engine equivalent of the AKQuant run: generate orders once,
persist the *exact* order intents, then replay them through the same execution
engine without re-running the strategy's screening/flow logic.

Recording is intentionally at the order-intent layer (code, signed quantity),
not at the fill layer.  Replay therefore still exercises T+1, price limits,
volume caps, fees, slippage and corporate-action handling, but skips the
expensive signal-generation jobs.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional

SIGNAL_COLUMNS = ("signal_date", "signal_time", "seq", "code", "qty")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "obj":
                out["obj_class"] = type(v).__name__
                out["obj_value"] = _json_safe(getattr(v, "value", None))
                continue
            out[str(k)] = _json_safe(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _json_safe(v) for k, v in vars(value).items()}
    return str(value)


class SignalRecorder:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.rows: List[Dict[str, Any]] = []
        self.meta: Dict[str, Any] = {}
        self._seq = 0

    def record(self, date: str, time_str: str, code: str, qty: int,
               source: str = "order") -> None:
        self._seq += 1
        self.rows.append({
            "signal_date": str(date)[:10],
            "signal_time": str(time_str)[:8],
            "seq": self._seq,
            "code": str(code),
            "qty": int(qty),
            "source": source,
        })

    def set_meta(self, **kw: Any) -> None:
        self.meta.update({k: _json_safe(v) for k, v in kw.items()})

    @property
    def meta_path(self) -> str:
        root, _ = os.path.splitext(self.path)
        return root + "_meta.json"

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(SIGNAL_COLUMNS))
            writer.writeheader()
            for r in self.rows:
                writer.writerow({k: r.get(k, "") for k in SIGNAL_COLUMNS})
        meta = dict(self.meta)
        meta["n_signals"] = len(self.rows)
        meta["created_at"] = dt.datetime.now().isoformat(timespec="seconds")
        with open(self.meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        return self.path


def load_meta(path: str) -> Dict[str, Any]:
    root, _ = os.path.splitext(os.path.abspath(path))
    meta_path = root + "_meta.json"
    if not os.path.isfile(meta_path):
        return {}
    with open(meta_path, encoding="utf-8") as fh:
        return json.load(fh)


def load_signals(path: str) -> "OrderedDict[str, List[Dict[str, Any]]]":
    """Return {signal_date: [{code, qty, seq, signal_time}, ...]}."""
    out: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            date = str(row.get("signal_date", ""))[:10]
            if not date:
                continue
            try:
                qty = int(float(row.get("qty", 0)))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            out.setdefault(date, []).append({
                "code": str(row.get("code", "")),
                "qty": qty,
                "seq": int(float(row.get("seq", 0) or 0)),
                "signal_time": str(row.get("signal_time", "")),
            })
    for date in out:
        out[date].sort(key=lambda r: (r["seq"], r["code"]))
    return out


def replay_meta_defaults(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the subset of metadata the replay runner needs."""
    return {
        "start": meta.get("start"),
        "end": meta.get("end"),
        "cash": float(meta.get("cash", 350000.0)),
        "fill_mode": meta.get("fill_mode", "next_open"),
        "jq_options": meta.get("jq_options") or {},
        "env": meta.get("env") or {},
        "strategy": meta.get("strategy", "replay"),
    }
