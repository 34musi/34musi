"""②「刷新列表」现价刷新进度（供按钮显示 成功数/总数）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.batch_cancel import is_cancelled, set_cancel

_lock = threading.Lock()
_generation = 0
_cancelled_gens: set[int] = set()
_state: dict[str, Any] = {
    "active": False,
    "total": 0,
    "done": 0,
    "ok": 0,
    "current_symbol": None,
    "cancelled": False,
    "started_at": None,
    "generation": None,
}

SCOPE = "watchlist_spot"


def watchlist_spot_job_start(total: int) -> int:
    global _generation
    with _lock:
        _generation += 1
        gen = _generation
        _state.update(
            {
                "active": True,
                "total": max(0, int(total)),
                "done": 0,
                "ok": 0,
                "current_symbol": None,
                "cancelled": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "generation": gen,
            }
        )
        return gen


def watchlist_spot_job_set_current(symbol: str | None) -> None:
    with _lock:
        if _state["active"] and symbol:
            _state["current_symbol"] = symbol


def watchlist_spot_job_tick(symbol: str | None, *, got_spot: bool) -> None:
    with _lock:
        if not _state["active"]:
            return
        total = int(_state["total"] or 0)
        done = int(_state["done"] or 0) + 1
        ok = int(_state["ok"] or 0) + (1 if got_spot else 0)
        _state["done"] = done if total <= 0 else min(done, total)
        _state["ok"] = ok
        if symbol:
            _state["current_symbol"] = symbol


def watchlist_spot_job_should_cancel(gen: int) -> bool:
    with _lock:
        if gen in _cancelled_gens:
            return True
    return is_cancelled(SCOPE)


def watchlist_spot_job_request_cancel() -> None:
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.add(int(g))
        _state["cancelled"] = True
    set_cancel(SCOPE)


def watchlist_spot_job_finish(*, cancelled: bool = False) -> None:
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.discard(int(g))
        _state["active"] = False
        _state["cancelled"] = cancelled
        _state["current_symbol"] = None


def watchlist_spot_job_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)
