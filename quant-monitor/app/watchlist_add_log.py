"""自选加入日志：按东八区日期记录每次写入自选池的标的，供②按日查询。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_models import WatchlistAddLogRow, WatchlistRow
from app.ingest import shanghai_today_date

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SNAPSHOT_KEYS = (
    "bars_first_ingested_at",
    "bars_last_ingested_at",
    "display_prev_close",
    "display_today_close",
    "spot_last_price",
    "spot_change_pct",
    "bars_last_trade_date",
    "spot_quote_date",
)


def shanghai_today_ymd() -> str:
    return shanghai_today_date().isoformat()


def parse_added_date_param(raw: str | None) -> str | None:
    """校验 YYYY-MM-DD；无效返回 None。"""
    if raw is None:
        return None
    d = str(raw).strip()[:10]
    if not _DATE_RE.match(d):
        return None
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return None
    return d


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    out: dict[str, Any] = {}
    for key in _SNAPSHOT_KEYS:
        if key not in snapshot:
            continue
        val = snapshot[key]
        if key in ("display_prev_close", "display_today_close", "spot_last_price", "spot_change_pct"):
            if val is None:
                out[key] = None
            else:
                try:
                    f = float(val)
                    out[key] = f if f == f else None  # NaN -> None
                except (TypeError, ValueError):
                    out[key] = None
        elif val is None:
            out[key] = None
        else:
            s = str(val).strip()
            out[key] = (
                s[:32]
                if key in ("bars_first_ingested_at", "bars_last_ingested_at")
                else (s[:10] if s else None)
            )
    return out


def watchlist_add_snapshot_for_symbols(
    session: Session,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    """加入自选时抓取与②表格一致的行情/入库快照（写入日志表）。"""
    from app.config import get_settings
    from app.ingest import (
        live_quote_fields_for_codes_enhanced,
        watchlist_bar_fields_for_session,
    )

    syms = [str(s).strip() for s in symbols if str(s).strip()]
    if not syms:
        return {}
    route = get_settings().ingest_data_source
    bar_by = watchlist_bar_fields_for_session(session, syms, data_source=route)
    live_by = live_quote_fields_for_codes_enhanced(
        syms, data_source=route, force_spot_refresh=False
    )
    out: dict[str, dict[str, Any]] = {}
    for sym in syms:
        bar = bar_by.get(sym) or {}
        live = live_by.get(sym) or {}
        out[sym] = _norm_snapshot(
            {
                "bars_first_ingested_at": bar.get("bars_first_ingested_at"),
                "bars_last_ingested_at": bar.get("bars_last_ingested_at"),
                "display_prev_close": bar.get("display_prev_close"),
                "display_today_close": bar.get("display_today_close"),
                "bars_last_trade_date": bar.get("bars_last_trade_date"),
                "spot_last_price": live.get("live_last_price"),
                "spot_change_pct": live.get("live_change_pct"),
                "spot_quote_date": live.get("live_quote_date"),
            }
        )
    return out


def log_row_snapshot_fields(row: WatchlistAddLogRow) -> dict[str, Any]:
    return _norm_snapshot({k: getattr(row, k, None) for k in _SNAPSHOT_KEYS})


def record_watchlist_add(
    session: Session,
    *,
    symbol: str,
    name: str = "",
    origin: str,
    added_at: str | None = None,
    added_date: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """追加一条加入日志（同一日同一代码可有多条，展示时按日去重取最早）。"""
    at = (added_at or _utc_now_iso()).strip()
    if added_date:
        ad = parse_added_date_param(added_date) or shanghai_today_ymd()
    else:
        try:
            dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ad = dt.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        except ValueError:
            ad = shanghai_today_ymd()
    snap = _norm_snapshot(snapshot)
    session.add(
        WatchlistAddLogRow(
            symbol=symbol,
            name=(name or "")[:64],
            origin=(origin or "manual")[:24],
            added_at=at,
            added_date=ad,
            **snap,
        )
    )


def record_watchlist_adds_with_snapshot(
    session: Session,
    pairs: list[tuple[str, str, str]],
) -> None:
    """批量写入加入日志，并为每只新标的附带②同款快照字段。"""
    if not pairs:
        return
    syms = [p[0] for p in pairs]
    snaps = watchlist_add_snapshot_for_symbols(session, syms)
    for sym, nm, origin in pairs:
        record_watchlist_add(
            session,
            symbol=sym,
            name=nm,
            origin=origin,
            snapshot=snaps.get(sym),
        )


def list_watchlist_add_dates(session: Session, *, limit: int = 120) -> list[str]:
    rows = session.execute(
        select(WatchlistAddLogRow.added_date)
        .distinct()
        .order_by(WatchlistAddLogRow.added_date.desc())
        .limit(max(1, int(limit)))
    ).all()
    return [str(r[0]) for r in rows if r and r[0]]


def _dedupe_add_log_rows_by_symbol(rows: list[WatchlistAddLogRow]) -> list[WatchlistAddLogRow]:
    """同一代码仅保留最早一条（调用方需已按 added_at 升序）。"""
    out: list[WatchlistAddLogRow] = []
    seen: set[str] = set()
    for row in rows:
        sym = str(row.symbol or "").strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(row)
    return out


def entries_added_on_date(session: Session, added_date: str) -> list[WatchlistAddLogRow]:
    """同日同代码仅保留首次加入的那条日志行（含快照字段）。"""
    d = parse_added_date_param(added_date)
    if not d:
        return []
    rows = (
        session.execute(
            select(WatchlistAddLogRow)
            .where(WatchlistAddLogRow.added_date == d)
            .order_by(WatchlistAddLogRow.added_at.asc(), WatchlistAddLogRow.id.asc())
        )
        .scalars()
        .all()
    )
    return _dedupe_add_log_rows_by_symbol(list(rows))


def entries_added_in_range(
    session: Session,
    *,
    range_start: str | None = None,
    range_end: str | None = None,
) -> list[WatchlistAddLogRow]:
    """东八区加入日期闭区间；同一代码保留区间内最早加入那条（列表单行展示）。"""
    from app.ingest import normalize_ingest_date_range

    start, end = normalize_ingest_date_range(range_start, range_end)
    if not start and not end:
        return []
    q = select(WatchlistAddLogRow).order_by(
        WatchlistAddLogRow.added_at.asc(), WatchlistAddLogRow.id.asc()
    )
    if start:
        q = q.where(WatchlistAddLogRow.added_date >= start)
    if end:
        q = q.where(WatchlistAddLogRow.added_date <= end)
    rows = session.execute(q).scalars().all()
    return _dedupe_add_log_rows_by_symbol(list(rows))


def symbols_added_on_date(session: Session, added_date: str) -> list[tuple[str, str, str, str]]:
    """返回 (symbol, name, origin, added_at) 按加入时间升序，同日同代码仅保留首次。"""
    return [
        (
            str(r.symbol or "").strip(),
            (r.name or "").strip(),
            (r.origin or "manual").strip(),
            (r.added_at or "").strip(),
        )
        for r in entries_added_on_date(session, added_date)
    ]


def count_adds_on_date(session: Session, added_date: str) -> int:
    d = parse_added_date_param(added_date)
    if not d:
        return 0
    n = session.execute(
        select(func.count()).select_from(WatchlistAddLogRow).where(WatchlistAddLogRow.added_date == d)
    ).scalar_one()
    return int(n or 0)


def latest_added_at_for_symbols(
    session: Session,
    symbols: list[str],
) -> dict[str, str]:
    """返回 symbol -> 最近一次加入自选时间（UTC ISO）。"""
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    if not syms:
        return {}
    rows = (
        session.execute(
            select(
                WatchlistAddLogRow.symbol,
                func.max(WatchlistAddLogRow.added_at),
            )
            .where(WatchlistAddLogRow.symbol.in_(syms))
            .group_by(WatchlistAddLogRow.symbol)
        )
        .all()
    )
    out: dict[str, str] = {}
    for sym, at in rows:
        s = str(sym or "").strip()
        t = str(at or "").strip()
        if s and t:
            out[s] = t[:32]
    return out
