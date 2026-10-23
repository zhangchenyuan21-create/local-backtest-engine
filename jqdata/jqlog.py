"""聚宽 log 对象兼容层。

策略大量使用 log.info / log.warn / log.error / log.debug。
本地实现要：
  1. 与原日志格式一致（便于与聚宽日志逐行对照）
     聚宽格式：`2020-01-02 09:31:00 - INFO  - <msg>`
  2. 支持【日志体积控制】——A 组就因逐股诊断刷了 81,344 行触发 10 万行上限
     截断了前 3.9 年。本地要能按标记抑制高频行。
  3. 线程安全
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
from collections import Counter

_LOCK = threading.RLock()
_HANDLERS = []
_COUNTS = Counter()
_SUPPRESS = set()          # 需要抑制的消息前缀（体积控制）
_SUPPRESS_LIMIT = 0        # 每个前缀最多输出几条（0 = 不限制）
_STATS = Counter()
_LEVEL = "INFO"
_LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRIT": 50}

# 默认抑制的刷屏标记（与聚宽侧修过的同类问题）
DEFAULT_NOISY = (
    "[科技豁免] ",          # 逐股诊断：实测 81,344 行
)


def configure(handlers=None, suppress=None, suppress_limit=0, noisy=None, min_level=None):
    """配置日志。

    suppress:      要抑制的消息前缀集合；None 表示用 DEFAULT_NOISY
    suppress_limit: 每个前缀最多输出多少条（0=全部抑制）
    """
    global _SUPPRESS, _SUPPRESS_LIMIT, _LEVEL
    with _LOCK:
        if handlers is not None:
            _HANDLERS.clear()
            _HANDLERS.extend(handlers)
        _SUPPRESS = set(noisy if noisy is not None
                        else (DEFAULT_NOISY if suppress is None else suppress))
        _SUPPRESS_LIMIT = int(suppress_limit)
        if min_level is not None:
            _LEVEL = str(min_level).upper()


def add_handler(fn):
    with _LOCK:
        _HANDLERS.append(fn)


def add_file_handler(path, encoding="utf-8"):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    f = open(path, "a", encoding=encoding, buffering=1)

    def _w(rec):
        f.write(rec["line"] + "\n")
    add_handler(_w)
    return f


def _now():
    from . import CONTEXT
    t = getattr(CONTEXT, "current_dt", None) if CONTEXT is not None else None
    return t if isinstance(t, dt.datetime) else dt.datetime.now()


def _emit(level, msg):
    msg = "" if msg is None else str(msg)
    if _LEVEL_RANK.get(level, 20) < _LEVEL_RANK.get(_LEVEL, 20):
        with _LOCK:
            _COUNTS[level] += 1
        return
    with _LOCK:
        _COUNTS[level] += 1
        for pre in _SUPPRESS:
            if msg.startswith(pre):
                _STATS[pre] += 1
                if _SUPPRESS_LIMIT and _STATS[pre] <= _SUPPRESS_LIMIT:
                    break
                return
        line = "%s - %-5s - %s" % (_now().strftime("%Y-%m-%d %H:%M:%S"),
                                   level, msg)
        rec = {"time": _now(), "level": level, "msg": msg, "line": line}
        for h in _HANDLERS:
            try:
                h(rec)
            except Exception:
                pass
        if not _HANDLERS:
            try:
                sys.stdout.write(line + "\n")
            except Exception:
                pass


def _enabled(level):
    return _LEVEL_RANK.get(level, 20) >= _LEVEL_RANK.get(_LEVEL, 20)


def info(msg, *a):
    if not _enabled("INFO"):
        with _LOCK:
            _COUNTS["INFO"] += 1
        return
    _emit("INFO", msg % a if a else msg)


def warn(msg, *a):
    if not _enabled("WARN"):
        with _LOCK:
            _COUNTS["WARN"] += 1
        return
    _emit("WARN", msg % a if a else msg)


warning = warn


def error(msg, *a):
    if not _enabled("ERROR"):
        with _LOCK:
            _COUNTS["ERROR"] += 1
        return
    _emit("ERROR", msg % a if a else msg)


def debug(msg, *a):
    _emit("DEBUG", msg % a if a else msg)


def critical(msg, *a):
    _emit("CRIT", msg % a if a else msg)


def set_level(level="INFO", *a, **kw):
    global _LEVEL
    with _LOCK:
        _LEVEL = str(level).upper()


def stats():
    with _LOCK:
        return {"各级条数": dict(_COUNTS), "被抑制的刷屏行": dict(_STATS)}
