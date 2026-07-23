"""
热门板块预览/填充任务进度（进程内状态，供控制台轮询实时日志）。

`POST /watchlist/hot-sectors/preview` 等长任务执行期间，控制台可轮询
`GET /meta/hot-sectors-status`，在请求未完成时也能看到服务端过程日志。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_LOG_CAP = 200

_lock = threading.Lock()
_state: dict[str, Any] = {
    "active": False,
    "total": 0,
    "done": 0,
    "current_sector": None,
    "message": None,
    "cancelled": False,
    "started_at": None,
    "progress_log": [],
}


def hot_sectors_job_start(*, message: str | None = None) -> None:
    """新一批热门板块任务开始：清空进度日志并置 active。"""
    with _lock:
        _state.update(
            {
                "active": True,
                "total": 0,
                "done": 0,
                "current_sector": None,
                "message": message or "任务启动",
                "cancelled": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "progress_log": [],
            }
        )


def hot_sectors_job_set_progress(
    *,
    done: int | None = None,
    total: int | None = None,
    current_sector: str | None = None,
    message: str | None = None,
) -> None:
    """更新板块扫描进度（供 UI 显示「3/41」类信息）。"""
    with _lock:
        if not _state["active"]:
            return
        if done is not None:
            _state["done"] = max(0, int(done))
        if total is not None:
            _state["total"] = max(0, int(total))
        if current_sector is not None:
            _state["current_sector"] = current_sector
        if message is not None:
            _state["message"] = message


def hot_sectors_job_append_log(text: str) -> None:
    """追加一条过程日志（与回传 progress_log 同步）。"""
    line = str(text or "").strip()
    if not line:
        return
    with _lock:
        logs: list[str] = list(_state.get("progress_log") or [])
        logs.append(line)
        excess = len(logs) - _LOG_CAP
        if excess > 0:
            del logs[0:excess]
        _state["progress_log"] = logs
        _state["message"] = line


def hot_sectors_job_finish(*, cancelled: bool = False) -> None:
    """结束当前批次。"""
    with _lock:
        _state["active"] = False
        _state["cancelled"] = bool(cancelled)
        _state["current_sector"] = None
        if cancelled:
            _state["message"] = "已取消"
        elif not _state.get("message"):
            _state["message"] = "已完成"


def hot_sectors_job_status() -> dict[str, Any]:
    """返回状态浅拷贝（含 progress_log 列表副本）。"""
    with _lock:
        out = dict(_state)
        out["progress_log"] = list(_state.get("progress_log") or [])
        return out
