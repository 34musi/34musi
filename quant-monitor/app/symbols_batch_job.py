"""④⑤ 及扩展因子等按只循环任务的进度（供控制台按钮 loading）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_states: dict[str, dict[str, Any]] = {}


def _empty_state() -> dict[str, Any]:
    return {
        "active": False,
        "total": 0,
        "done": 0,
        "current_symbol": None,
        "cancelled": False,
        "started_at": None,
    }


def symbols_batch_start(scope: str, total: int) -> None:
    with _lock:
        _states[scope] = {
            "active": True,
            "total": max(0, int(total)),
            "done": 0,
            "current_symbol": None,
            "cancelled": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }


def symbols_batch_set_current(scope: str, symbol: str | None) -> None:
    with _lock:
        st = _states.get(scope)
        if st and st.get("active") and symbol:
            st["current_symbol"] = symbol


def symbols_batch_tick(scope: str, symbol: str | None = None) -> None:
    with _lock:
        st = _states.get(scope)
        if not st or not st.get("active"):
            return
        total = int(st.get("total") or 0)
        done = int(st.get("done") or 0) + 1
        st["done"] = done if total <= 0 else min(done, total)
        if symbol:
            st["current_symbol"] = symbol


def symbols_batch_finish(scope: str, *, cancelled: bool = False) -> None:
    with _lock:
        st = _states.get(scope)
        if not st:
            return
        st["active"] = False
        st["cancelled"] = cancelled
        st["current_symbol"] = None


def symbols_batch_status(scope: str) -> dict[str, Any]:
    with _lock:
        st = _states.get(scope)
        if not st:
            return _empty_state()
        return dict(st)
