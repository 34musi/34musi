"""
engine / Session 管理与初始化。

注意：该模块只做数据库连接与建表/补列，不放 ORM 模型与常量。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_database_url
from app.db_migrations import (
    ensure_sqlite_bars_ingested_at_column,
    ensure_sqlite_forward_outlook_stock_name_column,
    ensure_sqlite_fundamental_snapshot_columns,
    ensure_sqlite_watchlist_name_column,
    ensure_sqlite_watchlist_origin_column,
)
from app.db_models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def init_db() -> None:
    """创建引擎、建表（若不存在）、初始化 session 工厂。进程内可重复调用以惰性初始化。"""
    global _engine, _SessionLocal
    url = get_database_url()
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    _engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    ensure_sqlite_watchlist_origin_column(_engine)
    ensure_sqlite_watchlist_name_column(_engine)
    ensure_sqlite_bars_ingested_at_column(_engine)
    ensure_sqlite_forward_outlook_stock_name_column(_engine)
    ensure_sqlite_fundamental_snapshot_columns(_engine)
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    logger.info("Database initialized at %s", url)


def get_engine():
    """返回全局 Engine；尚未初始化时先 init_db。"""
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    提供短生命周期 Session：成功则 commit，异常则 rollback，最后 close。
    业务代码应用 `with session_scope() as s:` 包裹单次请求内的读写。
    """
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

