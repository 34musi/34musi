"""
SQLite 轻量“迁移”逻辑：用于对已有库文件补列（create_all 不会修改旧表）。
"""

from __future__ import annotations

from sqlalchemy import text

from app.db_constants import FUNDAMENTAL_SNAPSHOT_SQLITE_ALTER, WATCHLIST_ORIGIN_MANUAL


def ensure_sqlite_watchlist_origin_column(engine) -> None:
    """已有 SQLite 库为 watchlist 追加 origin。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(watchlist)"))
        existing = {row[1] for row in cur.fetchall()}
        if "origin" not in existing:
            conn.execute(
                text(
                    f"ALTER TABLE watchlist ADD COLUMN origin TEXT DEFAULT '{WATCHLIST_ORIGIN_MANUAL}'"
                )
            )
            conn.commit()


def ensure_sqlite_watchlist_name_column(engine) -> None:
    """已有 SQLite 库为 watchlist 追加 name。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(watchlist)"))
        existing = {row[1] for row in cur.fetchall()}
        if "name" not in existing:
            conn.execute(text("ALTER TABLE watchlist ADD COLUMN name TEXT DEFAULT ''"))
            conn.commit()


def ensure_sqlite_bars_ingested_at_column(engine) -> None:
    """已有 SQLite 库为 bars 追加 ingested_at。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(bars)"))
        existing = {row[1] for row in cur.fetchall()}
        if "ingested_at" not in existing:
            conn.execute(text("ALTER TABLE bars ADD COLUMN ingested_at TEXT"))
            conn.commit()


def ensure_sqlite_forward_outlook_stock_name_column(engine) -> None:
    """已有 SQLite 库为 forward_outlook 追加 stock_name。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(forward_outlook)"))
        existing = {row[1] for row in cur.fetchall()}
        if "stock_name" not in existing:
            conn.execute(text("ALTER TABLE forward_outlook ADD COLUMN stock_name TEXT"))
            conn.commit()


def ensure_sqlite_holdings_mark_columns(engine) -> None:
    """已有 SQLite 库为 holdings 追加刷新现价快照列。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    alters = (
        ("mark_price", "REAL"),
        ("mark_price_at", "TEXT"),
        ("mark_price_source", "TEXT"),
    )
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(holdings)"))
        existing = {row[1] for row in cur.fetchall()}
        for col, sqlt in alters:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE holdings ADD COLUMN {col} {sqlt}"))
                conn.commit()


WATCHLIST_ADD_LOG_SQLITE_ALTER: tuple[tuple[str, str], ...] = (
    ("bars_last_ingested_at", "TEXT"),
    ("display_prev_close", "REAL"),
    ("display_today_close", "REAL"),
    ("spot_last_price", "REAL"),
    ("spot_change_pct", "REAL"),
    ("bars_last_trade_date", "TEXT"),
    ("spot_quote_date", "TEXT"),
)


def ensure_sqlite_watchlist_add_log_columns(engine) -> None:
    """已有 SQLite 库为 watchlist_add_log 追加行情/入库快照列。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(watchlist_add_log)"))
        existing = {row[1] for row in cur.fetchall()}
        for col, sqlt in WATCHLIST_ADD_LOG_SQLITE_ALTER:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE watchlist_add_log ADD COLUMN {col} {sqlt}"))
                conn.commit()


def ensure_sqlite_fundamental_snapshot_columns(engine) -> None:
    """已有库文件升级：为 fundamental_snapshots 追加列。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cur = conn.execute(text("PRAGMA table_info(fundamental_snapshots)"))
        existing = {row[1] for row in cur.fetchall()}
        for col, sqlt in FUNDAMENTAL_SNAPSHOT_SQLITE_ALTER:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE fundamental_snapshots ADD COLUMN {col} {sqlt}"))
                conn.commit()

