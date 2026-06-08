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


class SymbolIngestMetaRow(Base):
    """单标的 K 线首次入库时间（写入后不再更新）；与 bars.ingested_at 互补。"""

    __tablename__ = "symbol_ingest_meta"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    first_ingested_at: Mapped[str] = mapped_column(String(40))


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


class WatchlistAddLogRow(Base):
    """自选加入日志：每次新写入自选池记一条，按东八区 added_date 查询。"""

    __tablename__ = "watchlist_add_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="", server_default="")
    origin: Mapped[str] = mapped_column(String(24), default=WATCHLIST_ORIGIN_MANUAL, index=True)
    added_at: Mapped[str] = mapped_column(String(32), index=True)
    added_date: Mapped[str] = mapped_column(String(10), index=True)
    bars_first_ingested_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bars_last_ingested_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_prev_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_today_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_last_trade_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    spot_quote_date: Mapped[str | None] = mapped_column(String(10), nullable=True)


class SignalCacheRow(Base):
    """存每个标的最近一次信号摘要（JSON 字符串）；历史遗留表，当前无写入接口。"""

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


class ForwardOutlookRow(Base):
    """③ 更新后自动登记的前向展望：数据质量 + 未来 H 日方向演示，到期后自动结算。"""

    __tablename__ = "forward_outlook"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    stock_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    horizon: Mapped[int] = mapped_column(Integer, default=3)
    signal_trade_date: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
    bars_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    outlook_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    predicted_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outlook_summary_zh: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    actual_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


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


class HoldingRow(Base):
    """⑩ 持仓记录：成本、数量、状态；行情盈亏由服务端按本地日线/盘口估算。"""

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="holding", index=True)
    shares: Mapped[float] = mapped_column(Float)
    cost_price: Mapped[float] = mapped_column(Float)
    buy_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), index=True)
    updated_at: Mapped[str] = mapped_column(String(32))
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mark_price_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
