#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
import pandas as pd

from quant_stock_selector import evaluate_screen, parse_args, run_sma_backtest, standardize_price_frame


def make_history(days=160, trend=0.002, noise=0.01, volume_base=1_000_000):
    np.random.seed(7)
    dates = pd.bdate_range("2024-01-01", periods=days)
    returns = np.random.normal(trend, noise, days)
    close = 20 * np.cumprod(1 + returns)
    open_ = close * (1 - 0.002)
    high = close * 1.01
    low = close * 0.99
    volume = np.linspace(volume_base, volume_base * 1.4, days)
    return pd.DataFrame({
        "日期": dates,
        "开盘": open_,
        "最高": high,
        "最低": low,
        "收盘": close,
        "成交量": volume,
    })


def test_standardize_price_frame_supports_chinese_columns():
    frame = make_history(days=10)
    standardized = standardize_price_frame(frame)
    assert list(standardized.columns[:6]) == ["date", "open", "high", "low", "close", "volume"]
    assert len(standardized) == 10


def test_evaluate_screen_accepts_trending_stock():
    frame = make_history(days=160, trend=0.003, noise=0.006)
    metrics = evaluate_screen(frame)
    assert metrics.passed is True
    assert metrics.screen_score > 60
    assert metrics.distance_to_60d_high <= 12


def test_run_sma_backtest_returns_positive_metrics_on_uptrend():
    frame = make_history(days=220, trend=0.0025, noise=0.008)
    metrics = run_sma_backtest(frame, fast_period=10, slow_period=30)
    assert metrics.final_value > 100000
    assert metrics.trade_count >= 1
    assert metrics.max_drawdown_pct >= 0


def test_parse_args_defaults_to_hot_sectors():
    args = parse_args([])
    assert args.hot_sectors is True
    assert args.sector is None
    assert args.codes is None
