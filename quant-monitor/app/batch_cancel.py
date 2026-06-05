"""
长耗时批量任务的协作式中断标记（供控制台「取消请求」使用）。

## 功能作用

本模块在进程内维护一组按 **scope** 划分的 `threading.Event` 取消标志，
供各批量 API 在循环中轮询 `is_cancelled(scope)`，实现用户可中断的长任务。

典型协作流程：

1. **新任务开始**：调用方在入口调用 `clear(scope)`，清除上次残留的取消标记。
2. **任务进行中**：循环体内调用 `is_cancelled(scope)`，为 True 则提前退出并标记 `cancelled=True`。
3. **用户点「取消」**：控制台 `POST /meta/cancel-batch` → `cancel_many(scopes)` 置位对应 Event。
4. **下次新请求前**：控制台 `POST /meta/clear-batch` → `clear_many(scopes)` 清除标记，
   避免上次取消影响本次任务（进行中的 ingest / watchlist_spot / backfill_close 会跳过 clear）。

## 已知 scope（KNOWN_SCOPES）

| scope | 对应任务 |
|-------|----------|
| `ingest` | ② 批量拉取日线（`/ingest/update`） |
| `signals` | ④ 批量计算信号（`GET /signals`） |
| `alerts` | ⑤ 变动预览（`POST /alerts/preview`） |
| `fundamentals` | 扩展因子批量拉取 |
| `pre_refresh` | 信号/告警前的按只增量日线刷新 |
| `hot_sectors` | 热门板块分析 |
| `sector_screen` | ⑨ 板块选股流水线 |
| `watchlist_spot` | ② 刷新自选列表现价 |
| `backfill_close` | 补全收盘价 |

`scopes` 传 `"all"` 时会展开为上述全部 scope。

## 对外接口

| 函数 / 常量 | 用途 |
|-------------|------|
| `KNOWN_SCOPES` | 所有合法 scope 集合，供 API 校验与文档 |
| `clear` | 清除单个 scope 的取消标记（新批次开始前） |
| `set_cancel` | 置位单个 scope（一般由 `cancel_many` 调用） |
| `is_cancelled` | 查询某 scope 是否已被请求取消 |
| `clear_many` | 批量清除；若 ingest / watchlist_spot / backfill_close 仍在跑则跳过 |
| `cancel_many` | 批量置位；对 ingest / watchlist_spot 额外通知专用 job 模块 |

## 与专用 job 模块的配合

`ingest` 与 `watchlist_spot` 除本模块 Event 外，还在 `ingest_batch_job` /
`watchlist_spot_job` 中维护 **generation** 级取消（防止 clear 后旧循环误继续）。
`cancel_many` 在取消这两个 scope 时会同步调用 `ingest_batch_request_cancel()` /
`watchlist_spot_job_request_cancel()`。
"""

from __future__ import annotations

import threading

# 进程内全局：scope → Event，懒创建，线程安全
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
        "watchlist_spot",
        "backfill_close",
    }
)


def _event(scope: str) -> threading.Event:
    """按 scope 获取或创建对应的 Event 对象（持锁懒初始化）。"""
    with _lock:
        if scope not in _flags:
            _flags[scope] = threading.Event()
        return _flags[scope]


def clear(scope: str) -> None:
    """清除指定 scope 的取消标记，表示新一轮批量任务可以正常开始。"""
    _event(scope).clear()


def set_cancel(scope: str) -> None:
    """置位指定 scope 的取消标记，循环中 `is_cancelled` 将返回 True。"""
    _event(scope).set()


def is_cancelled(scope: str) -> bool:
    """查询指定 scope 是否已被用户或控制台请求取消。"""
    return _event(scope).is_set()


def _expand_scopes(scopes: list[str]) -> list[str]:
    """将 scopes 列表展开：`all` → 全部 KNOWN_SCOPES；非法项忽略。"""
    out: list[str] = []
    for s in scopes:
        if s == "all":
            out.extend(sorted(KNOWN_SCOPES))
        elif s in KNOWN_SCOPES:
            out.append(s)
    return out


def clear_many(scopes: list[str]) -> None:
    """
    批量清除取消标记；供 `POST /meta/clear-batch` 与新请求开始前调用。

    若以下任务仍在进行中（`active=true`），则**跳过**对应 scope 的 clear，
    避免控制台 pre-clear 冲掉用户刚触发的取消：
    - ingest（含 symbols_batch_job 的 ingest 进度）
    - watchlist_spot
    - backfill_close
    """
    expanded = _expand_scopes(scopes)
    skip_ingest = False
    skip_wl_spot = False
    skip_backfill_close = False
    if "ingest" in expanded:
        try:
            from app.ingest_batch_job import ingest_batch_status
            from app.symbols_batch_job import symbols_batch_status

            if ingest_batch_status().get("active") or symbols_batch_status(
                "ingest"
            ).get("active"):
                skip_ingest = True
        except Exception:
            pass
    if "watchlist_spot" in expanded:
        try:
            from app.watchlist_spot_job import watchlist_spot_job_status

            if watchlist_spot_job_status().get("active"):
                skip_wl_spot = True
        except Exception:
            pass
    if "backfill_close" in expanded:
        try:
            from app.symbols_batch_job import symbols_batch_status

            if symbols_batch_status("backfill_close").get("active"):
                skip_backfill_close = True
        except Exception:
            pass
    for s in expanded:
        if skip_ingest and s == "ingest":
            continue
        if skip_wl_spot and s == "watchlist_spot":
            continue
        if skip_backfill_close and s == "backfill_close":
            continue
        clear(s)


def cancel_many(scopes: list[str]) -> list[str]:
    """
    批量置位取消标记；供 `POST /meta/cancel-batch` 与控制台「取消请求」调用。

    返回实际触发的 scope 列表（含 `all` 展开后的各项）。

    对 `ingest` / `watchlist_spot` 会额外调用专用 job 的 request_cancel，
    以标记当前 generation，防止仅 clear Event 后旧循环仍继续执行。
    """
    touched: list[str] = []
    for s in scopes:
        if s == "all":
            for k in KNOWN_SCOPES:
                set_cancel(k)
                touched.append(k)
            # 兼容：确保 ingest 一定被标记（KNOWN_SCOPES 已含 ingest，此处为历史兜底）
            if "ingest" not in touched:
                set_cancel("ingest")
                touched.append("ingest")
            try:
                from app.ingest_batch_job import ingest_batch_request_cancel

                ingest_batch_request_cancel()
            except Exception:
                pass
            try:
                from app.watchlist_spot_job import watchlist_spot_job_request_cancel

                watchlist_spot_job_request_cancel()
            except Exception:
                pass
            continue
        if s in KNOWN_SCOPES:
            set_cancel(s)
            touched.append(s)
            if s == "ingest":
                try:
                    from app.ingest_batch_job import ingest_batch_request_cancel

                    ingest_batch_request_cancel()
                except Exception:
                    pass
            if s == "watchlist_spot":
                try:
                    from app.watchlist_spot_job import watchlist_spot_job_request_cancel

                    watchlist_spot_job_request_cancel()
                except Exception:
                    pass
    return touched
