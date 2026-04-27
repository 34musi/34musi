"""
ORM 模型定义：只放表结构与 Base，不负责 engine/session 初始化、不做迁移。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db_constants import WATCHLIST_ORIGIN_MANUAL


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
    ingested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class WatchlistRow(Base):
    """自选池：每个 symbol 一条记录。"""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    origin: Mapped[str] = mapped_column(
        String(24),
        default=WATCHLIST_ORIGIN_MANUAL,
        server_default=WATCHLIST_ORIGIN_MANUAL,
    )
    name: Mapped[str] = mapped_column(String(64), default="", server_default="")


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
    roe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    roa_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_assets_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    quick_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocf_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)


class DecisionJournalRow(Base):
    """自用决策日志：标题/正文、可选标的、信号快照、计划仓位与执行一致性。"""

    __tablename__ = "decision_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    signal_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    planned_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_as_planned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    actual_action: Mapped[str | None] = mapped_column(String(256), nullable=True)

