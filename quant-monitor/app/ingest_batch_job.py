"""
② 批量拉取日线任务进度（进程内状态，供控制台刷新后恢复 loading）。

## 功能作用

`POST /ingest/update` 在 **skip_bars=false**（实际联网拉日线）时，用本模块维护
一批任务的进度与取消状态，供控制台 **②** 轮询 `GET /meta/ingest-batch-status`，
在刷新页面后仍能显示「正在拉取 3/20 只…」。

当 **skip_bars=true**（仅本地 bars + 现价）时，改用 `symbols_batch_job` 的 `ingest`
scope，**不**走本模块。

## 与 batch_cancel 的配合

除 `batch_cancel.is_cancelled("ingest")` 外，本模块还维护 **generation** 级取消：

- 每批 `ingest_batch_start` 递增 generation；
- `ingest_batch_request_cancel` 将当前 generation 写入 `_cancelled_gens` 并 `set_cancel("ingest")`；
- 循环内用 `ingest_batch_should_cancel(gen)` 检查，避免仅 `clear("ingest")` 后旧循环误继续。

`batch_cancel.cancel_many` 取消 ingest 时会调用 `ingest_batch_request_cancel()`。

## 状态字段（ingest_batch_status）

| 字段 | 含义 |
|------|------|
| `active` | 是否有一批任务进行中 |
| `total` / `done` | 自选总数 / 已完成只数 |
| `current_symbol` | 当前正在处理的代码 |
| `cancelled` | 本批是否被用户取消 |
| `generation` | 本批序号（取消检查用） |
| `phase` | `None` 或 `finalize`（日线拉完后的收尾阶段，UI 显示「正在收尾」） |
| `started_at` | UTC ISO 开始时间 |

## 典型生命周期

```
ingest_batch_start(total) → set_current / tick（逐只）→ [enter_finalize] → ingest_batch_finish
```

## 对外接口

| 函数 | 用途 |
|------|------|
| `ingest_batch_start` | 开始一批，返回 generation |
| `ingest_batch_set_current` | 更新当前标的 |
| `ingest_batch_tick` | done+1，可选更新 current_symbol |
| `ingest_batch_should_cancel` | 循环内是否应停止 |
| `ingest_batch_request_cancel` | 用户取消（generation + Event） |
| `ingest_batch_enter_finalize` | 标记 phase=finalize（前向展望等收尾） |
| `ingest_batch_finish` | 结束一批，active=false |
| `ingest_batch_status` | 返回状态 dict 副本（供 API 轮询） |
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.batch_cancel import is_cancelled, set_cancel

# --- 进程内全局状态（线程安全） ---

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


# --- 批次生命周期 ---


def ingest_batch_start(total: int) -> int:
    """
    开始一批 ③ 日线拉取任务。

    重置 done/current_symbol/cancelled，递增 generation 并置 active=True。
    返回本批 generation，供循环内 `ingest_batch_should_cancel(gen)` 使用。
    """
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
    """
    全部标的日线已处理完，进入登记展望等收尾阶段。

    进度 done/total 不变；控制台见 phase=finalize 时显示「正在收尾 N/N 只…」。
    """
    with _lock:
        if _state.get("active"):
            _state["phase"] = "finalize"


def ingest_batch_set_current(symbol: str | None) -> None:
    """更新当前正在拉取日线的标的（active 且 symbol 非空时）。"""
    with _lock:
        if _state["active"] and symbol:
            _state["current_symbol"] = symbol


def ingest_batch_tick(symbol: str | None = None) -> None:
    """完成一只：done+1（不超过 total），可选更新 current_symbol。"""
    with _lock:
        if not _state["active"]:
            return
        total = int(_state["total"] or 0)
        done = int(_state["done"] or 0) + 1
        _state["done"] = done if total <= 0 else min(done, total)
        if symbol:
            _state["current_symbol"] = symbol


def ingest_batch_finish(*, cancelled: bool = False) -> None:
    """
    结束当前批次：active=False，清理 current_symbol/phase，移除 generation 取消标记。

    `cancelled` 写入状态供前端展示本批是否被用户中断。
    """
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.discard(int(g))
        _state["active"] = False
        _state["cancelled"] = cancelled
        _state["current_symbol"] = None
        _state["phase"] = None


# --- 取消检查 ---


def ingest_batch_should_cancel(gen: int) -> bool:
    """
    本批任务是否应停止。

    为 True 当：本 generation 在 `_cancelled_gens` 中，或 `batch_cancel.is_cancelled("ingest")`。
    """
    with _lock:
        if gen in _cancelled_gens:
            return True
    return is_cancelled("ingest")


def ingest_batch_request_cancel() -> None:
    """
    用户点「取消拉取」时调用：标记当前 generation 并置 ingest 取消 Event。

    由 `batch_cancel.cancel_many` 在 scope=ingest 或 all 时触发。
    """
    with _lock:
        g = _state.get("generation")
        if g is not None:
            _cancelled_gens.add(int(g))
        _state["cancelled"] = True
    set_cancel("ingest")


# --- 状态查询 ---


def ingest_batch_status() -> dict[str, Any]:
    """返回当前进度状态 dict 的浅拷贝（供 `GET /meta/ingest-batch-status`）。"""
    with _lock:
        return dict(_state)
