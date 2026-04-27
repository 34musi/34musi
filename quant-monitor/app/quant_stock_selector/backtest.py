"""Dual-SMA backtest and score composition."""

from __future__ import annotations

import math
from typing import List

import numpy as np
import pandas as pd

from .constants import TRADING_DAYS_PER_YEAR
from .exceptions import DataSourceError
from .market_utils import compute_max_drawdown, standardize_price_frame
from .models import BacktestMetrics


def run_sma_backtest(
    frame: pd.DataFrame,
    fast_period: int = 10,
    slow_period: int = 30,
    initial_cash: float = 100000.0,
    commission: float = 0.001,
    stop_loss: float = 0.08,
) -> BacktestMetrics:
    data = standardize_price_frame(frame).copy()
    if len(data) < slow_period + 5:
        raise DataSourceError("历史数据不足，无法运行双均线回测")

    data["ma_fast"] = data["close"].rolling(fast_period).mean()
    data["ma_slow"] = data["close"].rolling(slow_period).mean()
    data["ma20"] = data["close"].rolling(20).mean()
    data["ma60"] = data["close"].rolling(60).mean()

    cash = initial_cash
    shares = 0.0
    entry_price = 0.0
    entry_value = 0.0
    equity_curve: List[float] = []
    trades: List[float] = []

    for row in data.itertuples():
        close_price = float(row.close)
        current_value = cash + shares * close_price
        can_trade = not any(pd.isna(value) for value in (row.ma_fast, row.ma_slow, row.ma20, row.ma60))

        if can_trade and shares == 0.0:
            if row.ma_fast > row.ma_slow and close_price > row.ma20 > row.ma60:
                shares = (cash * (1.0 - commission)) / close_price
                entry_price = close_price
                entry_value = cash
                cash = 0.0
                current_value = shares * close_price
        elif can_trade and shares > 0.0:
            should_exit = (
                row.ma_fast < row.ma_slow
                or close_price < row.ma60
                or close_price <= entry_price * (1.0 - stop_loss)
            )
            if should_exit:
                cash = shares * close_price * (1.0 - commission)
                if entry_value:
                    trades.append(cash / entry_value - 1.0)
                shares = 0.0
                entry_price = 0.0
                entry_value = 0.0
                current_value = cash

        equity_curve.append(current_value)

    if shares > 0.0:
        final_close = float(data["close"].iloc[-1])
        cash = shares * final_close * (1.0 - commission)
        if entry_value:
            trades.append(cash / entry_value - 1.0)
        shares = 0.0
        equity_curve[-1] = cash

    equity = pd.Series(equity_curve, index=data["date"])
    daily_returns = equity.pct_change().fillna(0.0)
    total_return = cash / initial_cash - 1.0
    years = max(len(data) / TRADING_DAYS_PER_YEAR, 1.0 / TRADING_DAYS_PER_YEAR)
    annual_return = (cash / initial_cash) ** (1.0 / years) - 1.0
    max_drawdown = compute_max_drawdown(equity)
    sharpe_ratio = 0.0
    if daily_returns.std(ddof=0) > 0:
        sharpe_ratio = daily_returns.mean() / daily_returns.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
    trade_count = len(trades)
    win_rate = sum(1 for trade in trades if trade > 0) / trade_count if trade_count else 0.0

    return_score = np.clip(annual_return * 150.0 + 50.0, 0.0, 100.0)
    sharpe_score = np.clip(sharpe_ratio * 20.0 + 50.0, 0.0, 100.0)
    drawdown_score = np.clip(100.0 - max_drawdown * 200.0, 0.0, 100.0)
    backtest_score = round(return_score * 0.45 + sharpe_score * 0.25 + drawdown_score * 0.30, 2)

    return BacktestMetrics(
        total_return_pct=round(total_return * 100.0, 2),
        annual_return_pct=round(annual_return * 100.0, 2),
        max_drawdown_pct=round(max_drawdown * 100.0, 2),
        sharpe_ratio=round(sharpe_ratio, 3),
        trade_count=trade_count,
        win_rate_pct=round(win_rate * 100.0, 2),
        final_value=round(cash, 2),
        backtest_score=backtest_score,
    )


def compose_final_score(
    sector_hot_score: float,
    screen_score: float,
    backtest_score: float,
    *,
    scoring_strategy: str = "v2",
) -> float:
    """
    组合总分策略（可扩展）。

    - v1: 旧版默认（板块热度占比较高）
    - v2: 新版默认（更偏向个股技术面与回测表现）
    """
    key = (scoring_strategy or "").strip().lower() or "v2"
    if key == "v1":
        w_sector, w_screen, w_backtest = 0.25, 0.35, 0.40
    else:
        w_sector, w_screen, w_backtest = 0.15, 0.45, 0.40
    return round(
        sector_hot_score * w_sector + screen_score * w_screen + backtest_score * w_backtest,
        2,
    )
