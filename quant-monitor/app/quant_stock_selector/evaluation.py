"""Per-stock evaluation combining screen + backtest."""

from __future__ import annotations

import argparse

import pandas as pd

from .backtest import (
    compose_final_score,
    consecutive_ma5_stand_no_drop_streak,
    count_stand_on_ma5_bars,
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
    screen_mode = str(getattr(args, "screen_mode", "short_term") or "short_term").strip().lower()
    screen = evaluate_screen(std_hist, mode=screen_mode)
    backtest = run_sma_backtest(
        std_hist,
        fast_period=args.fast_period,
        slow_period=args.slow_period,
        initial_cash=args.initial_cash,
        commission=args.commission,
        stop_loss=args.stop_loss,
    )
    scoring_strategy = getattr(args, "scoring_strategy", None)
    strat = str(scoring_strategy or "v2").strip().lower()
    if screen_mode == "short_term" and strat == "v2":
        strat = "v2_short"
    screen_for_score = (
        screen.short_term_score if screen_mode == "short_term" else screen.screen_score
    )
    final_score = compose_final_score(
        sector.hot_score,
        screen_for_score,
        backtest.backtest_score,
        scoring_strategy=strat,
    )
    if not screen.passed:
        final_score = round(final_score * 0.75, 2)

    show_dual = bool(getattr(args, "show_dual_ma_strategy", False))
    show_triple = bool(getattr(args, "show_triple_ma_strategy", False))
    show_ma5 = bool(getattr(args, "show_ma5_stand_strategy", False))
    show_ma5_3d = bool(getattr(args, "show_ma5_stand_3d_strategy", False))
    ma5_stand_count: int | None = None
    ma5_consecutive_stand_days: int | None = None
    if show_ma5:
        lb = max(10, min(250, int(getattr(args, "ma5_stand_lookback", 60) or 60)))
        ma5_stand_count = count_stand_on_ma5_bars(std_hist, ma_period=5, lookback=lb)
    if show_ma5_3d:
        ma5_consecutive_stand_days = consecutive_ma5_stand_no_drop_streak(std_hist)
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
        return_5d=screen.return_5d,
        return_20d=screen.return_20d,
        ma5=screen.ma5,
        ma10=screen.ma10,
        ma20_slope_pct=screen.ma20_slope_pct,
        vol_ratio_last_day=screen.vol_ratio_last_day,
        short_term_passed=screen.short_term_passed,
        short_term_score=screen.short_term_score,
        screen_mode=screen.screen_mode,
        total_return_pct=backtest.total_return_pct,
        annual_return_pct=backtest.annual_return_pct,
        max_drawdown_pct=backtest.max_drawdown_pct,
        sharpe_ratio=backtest.sharpe_ratio,
        trade_count=backtest.trade_count,
        win_rate_pct=backtest.win_rate_pct,
        final_score=final_score,
        reasons=screen.reasons,
        ma5_stand_count=ma5_stand_count,
        ma5_consecutive_stand_days=ma5_consecutive_stand_days,
        **dual_kw,
        **triple_kw,
    )
