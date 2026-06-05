"""
按只循环批量任务的进度与部分结果缓存（供控制台 loading / 增量渲染）。

## 功能作用

本模块为 **多 scope 并行** 的「逐只处理」任务维护进程内状态，供控制台轮询
`GET /meta/symbols-batch-status` 显示「3/20 只…」，并在需要时通过
`GET /meta/symbols-batch-partial-results` 增量拉取已完成行。

与 `ingest_batch_job` 的分工：

| 场景 | 进度模块 |
|------|----------|
| ③ 联网拉日线（skip_bars=false） | `ingest_batch_job` |
| ③ 仅本地 bars + 现价/扩展因子（skip_bars=true） | 本模块 scope=`ingest` |
| ③ 扩展因子批量 | scope=`fundamentals` |
| ④ 批量算信号 | scope=`signals` |
| ⑤ 变动预览 | scope=`alerts` |
| ② 自选昨收回填 | scope=`backfill_close` |

## 状态字段（symbols_batch_status）

| 字段 | 含义 |
|------|------|
| `active` | 是否有一批任务进行中 |
| `total` / `done` | 计划处理只数 / 已完成只数 |
| `current_symbol` | 当前正在处理的代码 |
| `cancelled` | 本批是否被用户取消 |
| `started_at` | UTC ISO 开始时间 |

## 部分结果（symbols_batch_partial_results）

- `symbols_batch_push_result` 将单条 dict 追加到 scope 对应列表；
- 前端用 `offset` 增量拉取，避免整批完成后才渲染；
- `start` 时清空该 scope 的 `_partial_results`；`finish` 后保留供最后一次拉取。

## 典型生命周期

```
symbols_batch_start(scope, total) → set_current / tick（逐只）
  → [push_result 可选] → symbols_batch_finish
```

## 对外接口

| 函数 | 用途 |
|------|------|
| `symbols_batch_start` | 开始一批，可选写入 meta（如 data_source） |
| `symbols_batch_set_current` | 更新当前标的 |
| `symbols_batch_tick` | done+1，可选更新 current_symbol |
| `symbols_batch_push_result` | 追加单条已完成结果 |
| `symbols_batch_partial_results` | 从 offset 起增量返回 results + meta |
| `symbols_batch_finish` | 结束一批，标记 cancelled |
| `symbols_batch_status` | 查询进度（无记录时返回 inactive 空状态） |

## 约定

- 线程安全：所有读写经 `_lock`；
- scope 字符串由 `main.py` 约定，非法 scope 在路由层校验；
- 与 `batch_cancel` 配合：循环内检查 `is_cancelled(scope)` 后 `finish(cancelled=True)`。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

# --- 进程内状态（按 scope 隔离） ---

_lock = threading.Lock()
_states: dict[str, dict[str, Any]] = {}
_partial_results: dict[str, list[dict[str, Any]]] = {}
_partial_meta: dict[str, dict[str, Any]] = {}


def _empty_state() -> dict[str, Any]:
    """无活跃任务时的默认 status 结构。"""
    return {
        "active": False,
        "total": 0,
        "done": 0,
        "current_symbol": None,
        "cancelled": False,
        "started_at": None,
    }


# --- 生命周期 ---


def symbols_batch_start(scope: str, total: int, *, meta: dict[str, Any] | None = None) -> None:
    """开始一批按只任务；重置该 scope 的部分结果与 meta。"""
    with _lock:
        _states[scope] = {
            "active": True,
            "total": max(0, int(total)),
            "done": 0,
            "current_symbol": None,
            "cancelled": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _partial_results[scope] = []
        _partial_meta[scope] = dict(meta) if meta else {}


def symbols_batch_set_current(scope: str, symbol: str | None) -> None:
    """更新当前正在处理的标的（仅 active 时生效）。"""
    with _lock:
        st = _states.get(scope)
        if st and st.get("active") and symbol:
            st["current_symbol"] = symbol


def symbols_batch_tick(scope: str, symbol: str | None = None) -> None:
    """完成一只：done+1，可选同步 current_symbol；done 不超过 total。"""
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
    """结束一批：active=false，保留 _partial_results 供最后一次增量拉取。"""
    with _lock:
        st = _states.get(scope)
        if not st:
            return
        st["active"] = False
        st["cancelled"] = cancelled
        st["current_symbol"] = None
    # 注意：不清 _partial_results，留给下次 start 时清


def symbols_batch_status(scope: str) -> dict[str, Any]:
    """返回指定 scope 的进度快照；无记录时返回 inactive 空状态。"""
    with _lock:
        st = _states.get(scope)
        if not st:
            return _empty_state()
        return dict(st)


# --- 部分结果增量缓存 ---


def symbols_batch_push_result(scope: str, result: dict[str, Any]) -> None:
    """将单条已完成结果推入部分结果缓存，供前端增量拉取。"""
    with _lock:
        if scope not in _partial_results:
            _partial_results[scope] = []
        _partial_results[scope].append(result)


def symbols_batch_partial_results(scope: str, offset: int = 0) -> dict[str, Any]:
    """返回从 offset 起的增量结果及 meta（start 时写入）。"""
    with _lock:
        results = _partial_results.get(scope, [])
        meta = _partial_meta.get(scope, {})
        off = max(0, int(offset))
        sliced = results[off:]
        return {
            "results": sliced,
            "offset": off,
            "total_available": len(results),
            "meta": dict(meta),
        }
