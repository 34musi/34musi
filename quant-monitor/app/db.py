"""
数据库层：SQLAlchemy 声明式模型、引擎与会话。

表含义：
- bars：按 symbol + trade_date 唯一的日线 OHLCV。
- watchlist：用户监控的 6 位 A 股代码列表。
- signal_cache：告警预览用的「上一版信号」JSON 快照。
- fundamental_snapshots：自选扩展因子快照（估值、财务同比、主力净流入等），供信号合成使用。
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import get_database_url

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


class BarRow(Base):
    """单标的单日 K 线；symbol + trade_date 唯一。"""

    __tablename__ = "bars"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", name="uq_bars_symbol_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    trade_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float, default=0.0)


class WatchlistRow(Base):
    """自选池：每个 symbol 一条记录。"""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)


class SignalCacheRow(Base):
    """与 alerts 配合：存每个标的最近一次用于对比的信号摘要（JSON 字符串）。"""

    __tablename__ = "signal_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(32))


class FundamentalSnapshotRow(Base):
    """单标的最新扩展因子快照；由 /ingest/fundamentals 写入，信号侧只读。"""

    __tablename__ = "fundamental_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    updated_at: Mapped[str] = mapped_column(String(32))
    pe_dynamic: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_yoy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_yoy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    financial_report_date: Mapped[str | None] = mapped_column(String(24), nullable=True)
    main_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    fund_flow_date: Mapped[str | None] = mapped_column(String(24), nullable=True)


_engine = None
_SessionLocal = None


def init_db() -> None:
    """创建引擎、建表（若不存在）、初始化 session 工厂。进程内可重复调用以惰性初始化。"""
    global _engine, _SessionLocal
    url = get_database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        # 允许多线程通过连接池访问同一 SQLite 文件（FastAPI 同步路由常见场景）
        connect_args["check_same_thread"] = False
    _engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
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
