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


def _backtest_metrics_from_equity(
    data: pd.DataFrame,
    equity_curve: list[float],
    trades: list[float],
    initial_cash: float,
) -> BacktestMetrics:
    """由权益曲线与单笔交易收益列表合成与 `run_sma_backtest` 一致的指标。"""
    cash = float(equity_curve[-1]) if equity_curve else initial_cash
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


def run_pure_dual_ma_cross_backtest(
    frame: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    initial_cash: float = 100_000.0,
    commission: float = 0.001,
    stop_loss: float = 0.08,
) -> BacktestMetrics:
    """
    纯双均线：上一根 K 线非多头、本根快慢线金叉买入；死叉卖出；持仓期间触发止损价则卖出。
    与默认 `run_sma_backtest`（另含 MA20/MA60 过滤）不同，仅供对照展示。
    """
    data = standardize_price_frame(frame).copy()
    need = max(fast_period, slow_period) + 3
    if len(data) < need:
        raise DataSourceError("历史数据不足，无法运行纯双均线回测")
    data["ma_fast"] = data["close"].rolling(fast_period).mean()
    data["ma_slow"] = data["close"].rolling(slow_period).mean()
    data["prev_fast"] = data["ma_fast"].shift(1)
    data["prev_slow"] = data["ma_slow"].shift(1)

    cash = initial_cash
    shares = 0.0
    entry_price = 0.0
    entry_value = 0.0
    equity_curve: List[float] = []
    trades: List[float] = []

    for row in data.itertuples():
        close_price = float(row.close)
        mf, ms = float(row.ma_fast), float(row.ma_slow)
        pf, ps = row.prev_fast, row.prev_slow
        current_value = cash + shares * close_price
        if any(pd.isna(v) for v in (pf, ps, mf, ms)):
            equity_curve.append(current_value)
            continue
        pf_f, ps_f = float(pf), float(ps)
        cross_up = pf_f <= ps_f and mf > ms
        cross_down = pf_f >= ps_f and mf < ms

        if shares == 0.0 and cross_up:
            shares = (cash * (1.0 - commission)) / close_price
            entry_price = close_price
            entry_value = cash
            cash = 0.0
            current_value = shares * close_price
        elif shares > 0.0:
            stop_hit = close_price <= entry_price * (1.0 - stop_loss)
            if cross_down or stop_hit:
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
        equity_curve[-1] = cash

    return _backtest_metrics_from_equity(data, equity_curve, trades, initial_cash)


def _triple_ma_periods(fast_p: int, slow_p: int) -> tuple[int, int, int]:
    lo, hi = min(fast_p, slow_p), max(fast_p, slow_p)
    if hi <= lo + 2:
        hi = lo + 2
    mid = max(lo + 1, min(hi - 1, (lo + hi) // 2))
    return lo, mid, hi


def last_bar_dual_ma_golden_cross(frame: pd.DataFrame, fast_period: int, slow_period: int) -> bool:
    """
    末根 K 线是否出现快慢线金叉：上一根快线≤慢线，本根快线>慢线（与纯双均线回测开仓条件一致）。
    数据不足或均线无效时视为不满足。
    """
    data = standardize_price_frame(frame).copy()
    need = max(fast_period, slow_period) + 2
    if len(data) < need:
        return False
    data["ma_fast"] = data["close"].rolling(fast_period).mean()
    data["ma_slow"] = data["close"].rolling(slow_period).mean()
    data["prev_fast"] = data["ma_fast"].shift(1)
    data["prev_slow"] = data["ma_slow"].shift(1)
    row = data.iloc[-1]
    pf, ps, mf, ms = row["prev_fast"], row["prev_slow"], row["ma_fast"], row["ma_slow"]
    if any(pd.isna(v) for v in (pf, ps, mf, ms)):
        return False
    pf_f, ps_f, mf_f, ms_f = float(pf), float(ps), float(mf), float(ms)
    return pf_f <= ps_f and mf_f > ms_f


def last_bar_triple_ma_bull_alignment(frame: pd.DataFrame, fast_period: int, slow_period: int) -> bool:
    """
    末根 K 线是否三均线多头排列：收盘 > MA短 > MA中 > MA长（周期与三均线回测一致）。
    """
    short_p, mid_p, long_p = _triple_ma_periods(fast_period, slow_period)
    data = standardize_price_frame(frame).copy()
    if len(data) < long_p + 1:
        return False
    c = data["close"]
    data["ma_s"] = c.rolling(short_p).mean()
    data["ma_m"] = c.rolling(mid_p).mean()
    data["ma_l"] = c.rolling(long_p).mean()
    row = data.iloc[-1]
    close_price = float(row["close"])
    ms, mm, ml = row["ma_s"], row["ma_m"], row["ma_l"]
    if any(pd.isna(v) for v in (ms, mm, ml)):
        return False
    ms_f, mm_f, ml_f = float(ms), float(mm), float(ml)
    return close_price > ms_f > mm_f > ml_f


def dual_ma_golden_strength_at_last(frame: pd.DataFrame, fast_period: int, slow_period: int) -> float | None:
    """末根为金叉时返回 (ma_fast-ma_slow)/|ma_slow|，否则 None。"""
    if not last_bar_dual_ma_golden_cross(frame, fast_period, slow_period):
        return None
    data = standardize_price_frame(frame).copy()
    data["ma_fast"] = data["close"].rolling(fast_period).mean()
    data["ma_slow"] = data["close"].rolling(slow_period).mean()
    row = data.iloc[-1]
    mf, ms = row["ma_fast"], row["ma_slow"]
    if pd.isna(mf) or pd.isna(ms):
        return None
    mf_f, ms_f = float(mf), float(ms)
    den = max(abs(ms_f), 1e-9)
    return (mf_f - ms_f) / den


def triple_ma_bull_strength_at_last(frame: pd.DataFrame, fast_period: int, slow_period: int) -> float | None:
    """末根满足三均线多头时返回 min(收盘-短, 短-中, 中-长)/收盘，否则 None。"""
    if not last_bar_triple_ma_bull_alignment(frame, fast_period, slow_period):
        return None
    short_p, mid_p, long_p = _triple_ma_periods(fast_period, slow_period)
    data = standardize_price_frame(frame).copy()
    if len(data) < long_p + 1:
        return None
    c = data["close"]
    data["ma_s"] = c.rolling(short_p).mean()
    data["ma_m"] = c.rolling(mid_p).mean()
    data["ma_l"] = c.rolling(long_p).mean()
    row = data.iloc[-1]
    close_price = float(row["close"])
    ms, mm, ml = row["ma_s"], row["ma_m"], row["ma_l"]
    if any(pd.isna(v) for v in (ms, mm, ml)):
        return None
    ms_f, mm_f, ml_f = float(ms), float(mm), float(ml)
    g1 = close_price - ms_f
    g2 = ms_f - mm_f
    g3 = mm_f - ml_f
    m = min(g1, g2, g3)
    if close_price <= 0:
        return None
    return m / close_price


def universe_ma_strategy_strength(
    frame: pd.DataFrame,
    *,
    require_dual: bool,
    require_triple: bool,
    fast_period: int,
    slow_period: int,
) -> tuple[bool, float]:
    """
    是否满足已勾选策略；若满足返回 (True, 综合强度)，强度为各启用项子强度的平均值（量纲不同，仅用于排序）。
    """
    if require_dual and not last_bar_dual_ma_golden_cross(frame, fast_period, slow_period):
        return (False, 0.0)
    if require_triple and not last_bar_triple_ma_bull_alignment(frame, fast_period, slow_period):
        return (False, 0.0)
    parts: list[float] = []
    if require_dual:
        d = dual_ma_golden_strength_at_last(frame, fast_period, slow_period)
        if d is None:
            return (False, 0.0)
        parts.append(float(d))
    if require_triple:
        t = triple_ma_bull_strength_at_last(frame, fast_period, slow_period)
        if t is None:
            return (False, 0.0)
        parts.append(float(t))
    if not parts:
        return (True, 0.0)
    return (True, sum(parts) / len(parts))


def run_triple_ma_alignment_backtest(
    frame: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    initial_cash: float = 100_000.0,
    commission: float = 0.001,
    stop_loss: float = 0.08,
) -> BacktestMetrics:
    """
    三均线：短/中/长周期取 fast、(fast+slow)//2 夹逼、slow；收盘 > MA短 > MA中 > MA长 时买入；
    收盘跌破慢线或多头排列破坏时卖出；另有止损。
    """
    short_p, mid_p, long_p = _triple_ma_periods(fast_period, slow_period)
    data = standardize_price_frame(frame).copy()
    need = long_p + 3
    if len(data) < need:
        raise DataSourceError("历史数据不足，无法运行三均线回测")
    c = data["close"]
    data["ma_s"] = c.rolling(short_p).mean()
    data["ma_m"] = c.rolling(mid_p).mean()
    data["ma_l"] = c.rolling(long_p).mean()

    cash = initial_cash
    shares = 0.0
    entry_price = 0.0
    entry_value = 0.0
    equity_curve: List[float] = []
    trades: List[float] = []

    for row in data.itertuples():
        close_price = float(row.close)
        ms, mm, ml = row.ma_s, row.ma_m, row.ma_l
        current_value = cash + shares * close_price
        if any(pd.isna(v) for v in (ms, mm, ml)):
            equity_curve.append(current_value)
            continue
        ms_f, mm_f, ml_f = float(ms), float(mm), float(ml)
        aligned = close_price > ms_f > mm_f > ml_f
        broken = close_price < ml_f or ms_f <= mm_f or mm_f <= ml_f

        if shares == 0.0 and aligned:
            shares = (cash * (1.0 - commission)) / close_price
            entry_price = close_price
            entry_value = cash
            cash = 0.0
            current_value = shares * close_price
        elif shares > 0.0:
            stop_hit = close_price <= entry_price * (1.0 - stop_loss)
            if broken or stop_hit:
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
        equity_curve[-1] = cash

    return _backtest_metrics_from_equity(data, equity_curve, trades, initial_cash)


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
