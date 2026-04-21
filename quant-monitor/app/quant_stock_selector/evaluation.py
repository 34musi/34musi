"""Per-stock evaluation combining screen + backtest."""

from __future__ import annotations

import argparse

import pandas as pd

from .backtest import compose_final_score, run_sma_backtest
from .market_utils import standardize_price_frame
from .models import SectorRecord, StockEvaluation
from .screening import evaluate_screen


def evaluate_stock(
    code: str,
    name: str,
    sector: SectorRecord,
    history: pd.DataFrame,
    args: argparse.Namespace,
) -> StockEvaluation:
    std_hist = standardize_price_frame(history)
    last_ts = std_hist["date"].iloc[-1]
    latest_trade_date = pd.Timestamp(last_ts).strftime("%Y-%m-%d")
    screen = evaluate_screen(std_hist)
    backtest = run_sma_backtest(
        std_hist,
        fast_period=args.fast_period,
        slow_period=args.slow_period,
        initial_cash=args.initial_cash,
        commission=args.commission,
        stop_loss=args.stop_loss,
    )
    final_score = compose_final_score(sector.hot_score, screen.screen_score, backtest.backtest_score)
    if not screen.passed:
        final_score = round(final_score * 0.75, 2)

    return StockEvaluation(
        sector_name=sector.sector_name,
        board_type=sector.board_type,
        code=code,
        name=name,
        latest_trade_date=latest_trade_date,
        sector_hot_score=sector.hot_score,
        screen_passed=screen.passed,
        trend_score=screen.trend_score,
        volume_score=screen.volume_score,
        risk_score=screen.risk_score,
        screen_score=screen.screen_score,
        latest_close=screen.latest_close,
        distance_to_60d_high=screen.distance_to_60d_high,
        volume_ratio_20_60=screen.volume_ratio_20_60,
        drawdown_60d=screen.drawdown_60d,
        annual_volatility_20d=screen.annual_volatility_20d,
        total_return_pct=backtest.total_return_pct,
        annual_return_pct=backtest.annual_return_pct,
        max_drawdown_pct=backtest.max_drawdown_pct,
        sharpe_ratio=backtest.sharpe_ratio,
        trade_count=backtest.trade_count,
        win_rate_pct=backtest.win_rate_pct,
        final_score=final_score,
        reasons=screen.reasons,
    )
