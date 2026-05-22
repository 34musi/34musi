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


def last_bar_on_ma5(frame: pd.DataFrame, *, ma_period: int = 5) -> bool:
    """末根 K 线是否站在 MA5 上：收盘 >= MA5。"""
    data = standardize_price_frame(frame).copy()
    if len(data) < ma_period + 1:
        return False
    data["ma5"] = data["close"].rolling(ma_period).mean()
    row = data.iloc[-1]
    ma5 = row["ma5"]
    if pd.isna(ma5):
        return False
    return float(row["close"]) >= float(ma5)


def count_stand_on_ma5_bars(
    frame: pd.DataFrame,
    *,
    ma_period: int = 5,
    lookback: int = 60,
) -> int:
    """最近 lookback 根有效 K 线中，收盘 >= MA5 的根数（站上五日线次数）。"""
    data = standardize_price_frame(frame).copy()
    if len(data) < ma_period + 1:
        return 0
    lb = max(1, int(lookback))
    data["ma5"] = data["close"].rolling(ma_period).mean()
    tail = data.tail(lb)
    mask = (~tail["ma5"].isna()) & (tail["close"] >= tail["ma5"])
    return int(mask.sum())


def consecutive_close_on_ma5_streak(
    frame: pd.DataFrame,
    *,
    ma_period: int = 5,
) -> int:
    """从末根向前：连续「收盘 >= MA5」的交易日数（不要求收涨）。"""
    data = standardize_price_frame(frame).copy()
    if len(data) < ma_period + 1:
        return 0
    data["ma5"] = data["close"].rolling(ma_period).mean()
    streak = 0
    for i in range(len(data) - 1, ma_period - 2, -1):
        row = data.iloc[i]
        ma5_v = row["ma5"]
        close_v = float(row["close"])
        if pd.isna(ma5_v) or close_v < float(ma5_v):
            break
        streak += 1
    return streak


def last_n_days_close_on_ma5(
    frame: pd.DataFrame,
    *,
    min_days: int = 3,
    ma_period: int = 5,
) -> bool:
    """末根起至少 min_days 个交易日收盘站在 MA5 上。"""
    return consecutive_close_on_ma5_streak(frame, ma_period=ma_period) >= max(1, int(min_days))


def consecutive_ma5_stand_no_drop_streak(
    frame: pd.DataFrame,
    *,
    ma_period: int = 5,
) -> int:
    """
    从末根向前统计：连续满足「收盘>=MA5」且「收盘>=前一交易日收盘」（当日不跌落）的根数。
    """
    data = standardize_price_frame(frame).copy()
    if len(data) < ma_period + 1:
        return 0
    data["ma5"] = data["close"].rolling(ma_period).mean()
    streak = 0
    for i in range(len(data) - 1, ma_period - 2, -1):
        row = data.iloc[i]
        ma5_v = row["ma5"]
        close_v = float(row["close"])
        if pd.isna(ma5_v) or close_v < float(ma5_v):
            break
        if i > 0:
            prev_close = float(data.iloc[i - 1]["close"])
            if close_v < prev_close:
                break
        streak += 1
    return streak


def last_ma5_stand_nd_no_drop(
    frame: pd.DataFrame,
    *,
    min_days: int = 3,
    ma_period: int = 5,
) -> bool:
    """末根起连续 min_days 日站在 MA5 上且每日收盘不较前一日跌落。"""
    need = max(1, int(min_days))
    return consecutive_ma5_stand_no_drop_streak(frame, ma_period=ma_period) >= need


def ma5_stand_3d_strength_at_last(
    frame: pd.DataFrame,
    *,
    min_days: int = 3,
    ma_period: int = 5,
) -> tuple[bool, float, int]:
    """满足连续站上且不跌时返回 (True, 强度, 连续天数)。"""
    streak = consecutive_ma5_stand_no_drop_streak(frame, ma_period=ma_period)
    need = max(1, int(min_days))
    if streak < need:
        return (False, 0.0, streak)
    # 强度：连续天数归一化，3 日为及格，10 日以上饱和
    strength = min(1.0, float(streak) / 10.0)
    return (True, strength, streak)


def ma5_stand_strength_at_last(
    frame: pd.DataFrame,
    *,
    lookback: int = 60,
    ma_period: int = 5,
) -> tuple[bool, float, int]:
    """末根站上 MA5 时返回 (True, 次数/lookback, 次数)；否则 (False, 0, 0)。"""
    if not last_bar_on_ma5(frame, ma_period=ma_period):
        return (False, 0.0, 0)
    cnt = count_stand_on_ma5_bars(frame, ma_period=ma_period, lookback=lookback)
    lb = max(1, int(lookback))
    return (True, float(cnt) / lb, cnt)


def universe_ma_strategy_strength(
    frame: pd.DataFrame,
    *,
    require_dual: bool,
    require_triple: bool,
    require_ma5_stand: bool = False,
    require_ma5_stand_3d: bool = False,
    fast_period: int,
    slow_period: int,
    ma5_stand_lookback: int = 60,
    ma5_stand_3d_min_days: int = 3,
) -> tuple[bool, float, int | None, int | None]:
    """
    是否满足已勾选策略；若满足返回 (True, 综合强度, ma5站上次数, 连续站上且不跌天数)。
    强度为各启用项子强度的平均值（量纲不同，仅用于排序）。
    """
    ma5_cnt: int | None = None
    ma5_streak: int | None = None
    if require_dual and not last_bar_dual_ma_golden_cross(frame, fast_period, slow_period):
        return (False, 0.0, None, None)
    if require_triple and not last_bar_triple_ma_bull_alignment(frame, fast_period, slow_period):
        return (False, 0.0, None, None)
    if require_ma5_stand:
        ok5, s5, cnt5 = ma5_stand_strength_at_last(
            frame, lookback=ma5_stand_lookback
        )
        if not ok5:
            return (False, 0.0, None, None)
        ma5_cnt = cnt5
    if require_ma5_stand_3d:
        ok3, s3, streak3 = ma5_stand_3d_strength_at_last(
            frame, min_days=ma5_stand_3d_min_days
        )
        if not ok3:
            return (False, 0.0, ma5_cnt, streak3)
        ma5_streak = streak3
    parts: list[float] = []
    if require_dual:
        d = dual_ma_golden_strength_at_last(frame, fast_period, slow_period)
        if d is None:
            return (False, 0.0, ma5_cnt, ma5_streak)
        parts.append(float(d))
    if require_triple:
        t = triple_ma_bull_strength_at_last(frame, fast_period, slow_period)
        if t is None:
            return (False, 0.0, ma5_cnt, ma5_streak)
        parts.append(float(t))
    if require_ma5_stand and ma5_cnt is not None:
        lb = max(1, int(ma5_stand_lookback))
        parts.append(float(ma5_cnt) / lb)
    if require_ma5_stand_3d and ma5_streak is not None:
        parts.append(min(1.0, float(ma5_streak) / 10.0))
    if not parts:
        return (True, 0.0, ma5_cnt, ma5_streak)
    return (True, sum(parts) / len(parts), ma5_cnt, ma5_streak)


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
    - v2_short: ⑨ 短线模式（板块 20% + 短线技术分 50% + 回测 30%）
    """
    key = (scoring_strategy or "").strip().lower() or "v2"
    if key == "v1":
        w_sector, w_screen, w_backtest = 0.25, 0.35, 0.40
    elif key in ("v2_short", "short", "short_term"):
        w_sector, w_screen, w_backtest = 0.20, 0.50, 0.30
    else:
        w_sector, w_screen, w_backtest = 0.15, 0.45, 0.40
    return round(
        sector_hot_score * w_sector + screen_score * w_screen + backtest_score * w_backtest,
        2,
    )
