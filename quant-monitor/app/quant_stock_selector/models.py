"""Dataclasses for sector screening and evaluation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SectorRecord:
    sector_name: str
    board_type: str
    change_pct: float
    advancers_ratio: float
    leader_change_pct: float
    turnover_rate: float
    hot_score: float
    source: str


@dataclass
class ScreenMetrics:
    passed: bool
    trend_score: float
    volume_score: float
    risk_score: float
    screen_score: float
    latest_close: float
    ma20: float
    ma60: float
    ma120: float
    return_20d: float
    distance_to_60d_high: float
    volume_ratio_20_60: float
    drawdown_60d: float
    annual_volatility_20d: float
    reasons: str


@dataclass
class BacktestMetrics:
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trade_count: int
    win_rate_pct: float
    final_value: float
    backtest_score: float


@dataclass
class StockEvaluation:
    sector_name: str
    board_type: str
    code: str
    name: str
    # 与 latest_close 同一根 K 线的交易日（YYYY-MM-DD），供 API/表格展示
    latest_trade_date: str
    sector_hot_score: float
    screen_passed: bool
    trend_score: float
    volume_score: float
    risk_score: float
    screen_score: float
    latest_close: float
    distance_to_60d_high: float
    volume_ratio_20_60: float
    drawdown_60d: float
    annual_volatility_20d: float
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trade_count: int
    win_rate_pct: float
    final_score: float
    reasons: str
