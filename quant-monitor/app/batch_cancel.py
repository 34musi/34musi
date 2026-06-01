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
        "watchlist_spot",
        "backfill_close",
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


def _expand_scopes(scopes: list[str]) -> list[str]:
    out: list[str] = []
    for s in scopes:
        if s == "all":
            out.extend(sorted(KNOWN_SCOPES))
        elif s in KNOWN_SCOPES:
            out.append(s)
    return out


def clear_many(scopes: list[str]) -> None:
    """清除取消标记；批量任务进行中时不 clear 对应 scope，避免冲掉用户取消。"""
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
    """设置取消标记；返回实际触发的 scope 列表。"""
    touched: list[str] = []
    for s in scopes:
        if s == "all":
            for k in KNOWN_SCOPES:
                set_cancel(k)
                touched.append(k)
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
