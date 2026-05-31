"""② 批量拉取日线任务进度（供控制台刷新后恢复 loading）。"""

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
    "current_symbol": None,
    "cancelled": False,
    "started_at": None,
    "generation": None,
    "phase": None,
}


def ingest_batch_start(total: int) -> int:
    """开始一批拉取；返回本批 generation，供循环内检查是否已取消。"""
    global _generation
    with _lock:
        _generation += 1
        gen = _generation
        _state.update(
            {
                "active": True,
                "total": max(0, int(total)),
                "done": 0,
                "current_symbol": None,
                "cancelled": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "generation": gen,
                "phase": None,
            }
        )
        return gen


def ingest_batch_enter_finalize() -> None:
    """全部标的日线已处理完，进入登记展望等收尾（进度仍为 done/total）。"""
    with _lock:
        if _state.get("active"):
            _state["phase"] = "finalize"


def ingest_batch_set_current(symbol: str | None) -> None:
    with _lock:
        if _state["active"] and symbol:
            _state["current_symbol"] = symbol


def ingest_batch_tick(symbol: str | None = None) -> None:
    with _lock:
        if not _state["active"]:
            return
        total = int(_state["total"] or 0)
        done = int(_state["done"] or 0) + 1
        _state["done"] = done if total <= 0 else min(done, total)
        if symbol:
            _state["current_symbol"] = symbol


def ingest_batch_should_cancel(gen: int) -> bool:
    """本批任务是否应停止（用户取消或 clear 后仍保留 generation 标记）。"""
    with _lock:
        if gen in _cancelled_gens:
            return True
    return is_cancelled("ingest")


def ingest_batch_request_cancel() -> None:
    """用户点「取消拉取」：标记当前 generation 并置 ingest 取消事件。"""
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.add(int(g))
        _state["cancelled"] = True
    set_cancel("ingest")


def ingest_batch_finish(*, cancelled: bool = False) -> None:
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.discard(int(g))
        _state["active"] = False
        _state["cancelled"] = cancelled
        _state["current_symbol"] = None
        _state["phase"] = None


def ingest_batch_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)
