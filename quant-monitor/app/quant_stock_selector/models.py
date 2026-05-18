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
    return_5d: float = 0.0
    ma5: float = 0.0
    ma10: float = 0.0
    ma20_slope_pct: float = 0.0
    vol_ratio_last_day: float = 0.0
    short_term_passed: bool = False
    short_term_score: float = 0.0
    screen_mode: str = "short_term"


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
    return_5d: float = 0.0
    return_20d: float = 0.0
    ma5: float = 0.0
    ma10: float = 0.0
    ma20_slope_pct: float = 0.0
    vol_ratio_last_day: float = 0.0
    short_term_passed: bool = False
    short_term_score: float = 0.0
    screen_mode: str = "short_term"
    # 以下仅当请求体开启对应开关时由服务端填入；默认 None，JSON 中可省略
    dual_ma_total_return_pct: float | None = None
    dual_ma_annual_return_pct: float | None = None
    dual_ma_max_drawdown_pct: float | None = None
    dual_ma_sharpe_ratio: float | None = None
    dual_ma_trade_count: int | None = None
    dual_ma_win_rate_pct: float | None = None
    triple_ma_total_return_pct: float | None = None
    triple_ma_annual_return_pct: float | None = None
    triple_ma_max_drawdown_pct: float | None = None
    triple_ma_sharpe_ratio: float | None = None
    triple_ma_trade_count: int | None = None
    triple_ma_win_rate_pct: float | None = None
    # 与「最新价」同源的快照涨跌幅（%）；mootdx 为 (现价−昨收)/昨收，东财 spot 为列表「涨跌幅」列
    spot_change_pct: float | None = None
    # 全市场+策略筛选时：末根均线复合强度，用于排序（无量纲混合，仅相对比较）
    strategy_pick_strength: float | None = None
    # 启用「站在五日线」策略时：最近 N 根 K 线内收盘>=MA5 的次数（N 见请求 ma5_stand_lookback）
    ma5_stand_count: int | None = None
    # 启用「连续站上五日线且不跌」策略时：末根起连续满足天数
    ma5_consecutive_stand_days: int | None = None
