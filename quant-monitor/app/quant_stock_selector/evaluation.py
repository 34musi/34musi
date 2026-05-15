"""Per-stock evaluation combining screen + backtest."""

from __future__ import annotations

import argparse

import pandas as pd

from .backtest import (
    compose_final_score,
    run_pure_dual_ma_cross_backtest,
    run_sma_backtest,
    run_triple_ma_alignment_backtest,
)
from .exceptions import DataSourceError
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
    scoring_strategy = getattr(args, "scoring_strategy", None)
    final_score = compose_final_score(
        sector.hot_score,
        screen.screen_score,
        backtest.backtest_score,
        scoring_strategy=str(scoring_strategy or "v2"),
    )
    if not screen.passed:
        final_score = round(final_score * 0.75, 2)

    show_dual = bool(getattr(args, "show_dual_ma_strategy", False))
    show_triple = bool(getattr(args, "show_triple_ma_strategy", False))
    dual_kw: dict = {}
    triple_kw: dict = {}
    if show_dual:
        try:
            dm = run_pure_dual_ma_cross_backtest(
                std_hist,
                fast_period=args.fast_period,
                slow_period=args.slow_period,
                initial_cash=args.initial_cash,
                commission=args.commission,
                stop_loss=args.stop_loss,
            )
            dual_kw = {
                "dual_ma_total_return_pct": dm.total_return_pct,
                "dual_ma_annual_return_pct": dm.annual_return_pct,
                "dual_ma_max_drawdown_pct": dm.max_drawdown_pct,
                "dual_ma_sharpe_ratio": dm.sharpe_ratio,
                "dual_ma_trade_count": dm.trade_count,
                "dual_ma_win_rate_pct": dm.win_rate_pct,
            }
        except DataSourceError:
            dual_kw = {
                "dual_ma_total_return_pct": None,
                "dual_ma_annual_return_pct": None,
                "dual_ma_max_drawdown_pct": None,
                "dual_ma_sharpe_ratio": None,
                "dual_ma_trade_count": None,
                "dual_ma_win_rate_pct": None,
            }
    if show_triple:
        try:
            tm = run_triple_ma_alignment_backtest(
                std_hist,
                fast_period=args.fast_period,
                slow_period=args.slow_period,
                initial_cash=args.initial_cash,
                commission=args.commission,
                stop_loss=args.stop_loss,
            )
            triple_kw = {
                "triple_ma_total_return_pct": tm.total_return_pct,
                "triple_ma_annual_return_pct": tm.annual_return_pct,
                "triple_ma_max_drawdown_pct": tm.max_drawdown_pct,
                "triple_ma_sharpe_ratio": tm.sharpe_ratio,
                "triple_ma_trade_count": tm.trade_count,
                "triple_ma_win_rate_pct": tm.win_rate_pct,
            }
        except DataSourceError:
            triple_kw = {
                "triple_ma_total_return_pct": None,
                "triple_ma_annual_return_pct": None,
                "triple_ma_max_drawdown_pct": None,
                "triple_ma_sharpe_ratio": None,
                "triple_ma_trade_count": None,
                "triple_ma_win_rate_pct": None,
            }

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
        **dual_kw,
        **triple_kw,
    )
