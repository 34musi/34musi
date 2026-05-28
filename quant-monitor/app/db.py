"""
数据库对外统一出口（兼容层）。

历史上 `app.db` 同时承载：
- ORM 模型定义
- 常量/静态数据
- SQLite 旧库补列
- engine / Session 初始化

为便于模块化管理，上述职责已拆分到：
- `app.db_constants`
- `app.db_models`
- `app.db_migrations`
- `app.db_session`

此文件保留原导入路径不变：外部继续 `from app.db import ...`。
"""

from app.db_constants import (
    FUNDAMENTAL_SNAPSHOT_SQLITE_ALTER,
    WATCHLIST_ORIGIN_AUTO_HOT,
    WATCHLIST_ORIGIN_AUTO_QUANT,
    WATCHLIST_ORIGIN_MANUAL,
)
from app.db_models import (
    BarRow,
    Base,
    DecisionJournalRow,
    ForwardOutlookRow,
    FundamentalSnapshotRow,
    HoldingRow,
    SignalCacheRow,
    SymbolIngestMetaRow,
    WatchlistAddLogRow,
    WatchlistRow,
)
from app.db_migrations import (
    ensure_sqlite_bars_ingested_at_column,
    ensure_sqlite_fundamental_snapshot_columns,
    ensure_sqlite_holdings_mark_columns,
    ensure_sqlite_watchlist_name_column,
    ensure_sqlite_watchlist_origin_column,
)
from app.db_session import get_engine, init_db, session_scope

__all__ = [
    # 常量
    "WATCHLIST_ORIGIN_MANUAL",
    "WATCHLIST_ORIGIN_AUTO_HOT",
    "WATCHLIST_ORIGIN_AUTO_QUANT",
    "FUNDAMENTAL_SNAPSHOT_SQLITE_ALTER",
    # 模型
    "Base",
    "BarRow",
    "SymbolIngestMetaRow",
    "WatchlistRow",
    "WatchlistAddLogRow",
    "SignalCacheRow",
    "FundamentalSnapshotRow",
    "DecisionJournalRow",
    "ForwardOutlookRow",
    "HoldingRow",
    # 会话/引擎
    "init_db",
    "get_engine",
    "session_scope",
    # 迁移（如有脚本/测试需要可直接调用）
    "ensure_sqlite_watchlist_origin_column",
    "ensure_sqlite_watchlist_name_column",
    "ensure_sqlite_bars_ingested_at_column",
    "ensure_sqlite_fundamental_snapshot_columns",
    "ensure_sqlite_holdings_mark_columns",
]
