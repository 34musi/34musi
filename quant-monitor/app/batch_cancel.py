"""长耗时批量任务的中断标记（供控制台「取消请求」触发）。"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_flags: dict[str, threading.Event] = {}

KNOWN_SCOPES = frozenset(
    {
        "ingest",
        "signals",
        "alerts",
        "fundamentals",
        "pre_refresh",
        "hot_sectors",
        "sector_screen",
    }
)


def _event(scope: str) -> threading.Event:
    with _lock:
        if scope not in _flags:
            _flags[scope] = threading.Event()
        return _flags[scope]


def clear(scope: str) -> None:
    _event(scope).clear()


def set_cancel(scope: str) -> None:
    _event(scope).set()


def is_cancelled(scope: str) -> bool:
    return _event(scope).is_set()


def clear_many(scopes: list[str]) -> None:
    for s in scopes:
        clear(s)


def cancel_many(scopes: list[str]) -> list[str]:
    """设置取消标记；返回实际触发的 scope 列表。"""
    touched: list[str] = []
    for s in scopes:
        if s == "all":
            for k in KNOWN_SCOPES:
                set_cancel(k)
                touched.append(k)
            continue
        if s in KNOWN_SCOPES:
            set_cancel(s)
            touched.append(s)
    return touched
