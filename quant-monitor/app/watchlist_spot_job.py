"""
②「刷新列表」现价刷新进度（供按钮显示 成功数/总数）。

## 功能作用

控制台 **② 自选** 点击「刷新列表」时，`main._live_spot_refresh_with_progress`
分块联网拉取各标的现价；本模块维护该批任务的进程内进度，供
`GET /meta/watchlist-spot-refresh-status` 轮询，在按钮上显示如 **3/20**
（`done/total`）及成功拿到现价的只数（`ok`）。

与 `ingest_batch_job`（拉日线）、`symbols_batch_job`（按只循环）并列，
**仅服务 watchlist 现价刷新** 这一条链路。

## 与 batch_cancel 的配合

除 `batch_cancel.is_cancelled("watchlist_spot")` 外，本模块还维护 **generation** 级取消：

- 每批 `watchlist_spot_job_start` 递增 generation 并返回 gen；
- `watchlist_spot_job_request_cancel` 将当前 gen 写入 `_cancelled_gens` 并 `set_cancel(SCOPE)`；
- 循环内用 `watchlist_spot_job_should_cancel(gen)` 检查，避免仅 `clear` 后旧循环误继续。

`batch_cancel.cancel_many` 取消 watchlist_spot 时会调用 `watchlist_spot_job_request_cancel()`。

## 状态字段（watchlist_spot_job_status）

| 字段 | 含义 |
|------|------|
| `active` | 是否有一批刷新进行中 |
| `total` / `done` | 自选总数 / 已处理只数 |
| `ok` | 成功拿到有效现价的只数 |
| `current_symbol` | 当前正在处理的代码 |
| `cancelled` | 本批是否被用户取消 |
| `generation` | 本批序号（取消检查用） |
| `started_at` | UTC ISO 开始时间 |

## 典型生命周期

```
watchlist_spot_job_start(total) → set_current / tick(got_spot)（逐只）
  → watchlist_spot_job_finish
```

## 对外接口

| 函数 | 用途 |
|------|------|
| `watchlist_spot_job_start` | 开始一批，返回 generation |
| `watchlist_spot_job_set_current` | 更新当前标的 |
| `watchlist_spot_job_tick` | done+1，按 got_spot 更新 ok |
| `watchlist_spot_job_should_cancel` | 循环内是否应停止 |
| `watchlist_spot_job_request_cancel` | 用户取消（generation + Event） |
| `watchlist_spot_job_finish` | 结束一批，active=false |
| `watchlist_spot_job_status` | 返回状态 dict 副本（供 API 轮询） |

## 约定

- 线程安全：所有读写经 `_lock`；
- `SCOPE = "watchlist_spot"` 与 `batch_cancel` 键一致；
- 同时仅允许一批 active（`main` 在 start 前检查并 409）。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.batch_cancel import is_cancelled, set_cancel

# --- 进程内状态 ---

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


# --- 生命周期 ---


def watchlist_spot_job_start(total: int) -> int:
    """开始一批现价刷新；返回 generation 供 should_cancel 使用。"""
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
    """更新当前正在拉取现价的标的（仅 active 时生效）。"""
    with _lock:
        if _state["active"] and symbol:
            _state["current_symbol"] = symbol


def watchlist_spot_job_tick(symbol: str | None, *, got_spot: bool) -> None:
    """完成一只：done+1；got_spot 为 true 时 ok+1。"""
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
    """本批 generation 已标记取消，或 batch_cancel Event 已触发。"""
    with _lock:
        if gen in _cancelled_gens:
            return True
    return is_cancelled(SCOPE)


def watchlist_spot_job_request_cancel() -> None:
    """用户取消：标记当前 generation 并 set_cancel(SCOPE)。"""
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.add(int(g))
        _state["cancelled"] = True
    set_cancel(SCOPE)


def watchlist_spot_job_finish(*, cancelled: bool = False) -> None:
    """结束一批：active=false，清理 generation 取消标记。"""
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.discard(int(g))
        _state["active"] = False
        _state["cancelled"] = cancelled
        _state["current_symbol"] = None


def watchlist_spot_job_status() -> dict[str, Any]:
    """返回当前进度快照（dict 副本，供 GET /meta/watchlist-spot-refresh-status）。"""
    with _lock:
        return dict(_state)
