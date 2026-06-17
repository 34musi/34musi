"""因果截断 K 线后的本地打分快照（选股 + ④ 技术分）。"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.quant_stock_selector.backtest import compose_final_score, run_sma_backtest
from app.quant_stock_selector.exceptions import DataSourceError as QDataSourceError
from app.quant_stock_selector.screening import evaluate_screen
from app.signals import _build_signal_metrics


def truncate_bars_to_signal_date(df: pd.DataFrame, signal_trade_date: str) -> pd.DataFrame:
    """保留 trade_date <= signal_trade_date 的 K 线（含信号日）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    td = str(signal_trade_date).strip()[:10]
    work = df.copy()
    work["trade_date"] = work["trade_date"].astype(str).str[:10]
    out = work[work["trade_date"] <= td]
    if out.empty:
        return out
    last = str(out["trade_date"].iloc[-1])
    if last != td:
        return pd.DataFrame()
    return out.reset_index(drop=True)


def _avg_turnover_20d(frame: pd.DataFrame) -> float | None:
    if frame is None or len(frame) < 20:
        return None
    if "amount" not in frame.columns:
        return None
    amt = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    val = float(amt.tail(20).mean())
    return val if math.isfinite(val) and val > 0 else None


def compute_local_scores_at_date(
    df: pd.DataFrame,
    signal_trade_date: str,
    *,
    sector_hot_score: float = 50.0,
    fast_period: int = 10,
    slow_period: int = 30,
    screen_mode: str = "short_term",
    min_turnover_amt: float = 0.0,
    relaxed_min_bars: bool = False,
) -> dict[str, Any] | None:
    """
    在 signal_trade_date 当日（因果截断后）计算本地打分快照。

    板块热度默认 50（中性），因历史板块热度通常未入库。
    """
    hist = truncate_bars_to_signal_date(df, signal_trade_date)
    if hist.empty:
        return None
    try:
        mb = 30 if relaxed_min_bars else None
        screen = evaluate_screen(hist, mode=screen_mode, min_bars=mb)
    except QDataSourceError:
        return None

    backtest_score: float | None = None
    if len(hist) >= slow_period + 5:
        try:
            bt = run_sma_backtest(
                hist,
                fast_period=fast_period,
                slow_period=slow_period,
            )
            backtest_score = float(bt.backtest_score)
        except QDataSourceError:
            backtest_score = None

    if len(hist) < 30:
        return None

    sector = float(sector_hot_score)
    st = float(screen.short_term_score)
    bt_val = float(backtest_score) if backtest_score is not None else 50.0

    final_v2_short = compose_final_score(
        sector,
        st,
        bt_val,
        scoring_strategy="v2_short",
    )
    if not screen.passed:
        final_v2_short = round(final_v2_short * 0.75, 2)

    final_v2_trade = compose_final_score(
        sector,
        st,
        0.0,
        scoring_strategy="v2_trade",
    )
    if not screen.passed:
        final_v2_trade = round(final_v2_trade * 0.75, 2)

    sig_m = _build_signal_metrics(hist, "", None)
    technical = int(sig_m.get("technical_score") or 0)

    avg_turn = _avg_turnover_20d(hist)
    liquidity_ok = True
    if min_turnover_amt > 0 and avg_turn is not None:
        liquidity_ok = avg_turn >= min_turnover_amt
    elif min_turnover_amt > 0:
        liquidity_ok = False

    return {
        "signal_trade_date": str(signal_trade_date)[:10],
        "short_term_score": st,
        "screen_score": float(screen.screen_score),
        "screen_passed": bool(screen.passed),
        "short_term_passed": bool(screen.short_term_passed),
        "backtest_score": backtest_score,
        "final_score_v2_short": final_v2_short,
        "final_score_v2_trade": final_v2_trade,
        "signal_technical_score": technical,
        "avg_turnover_20d": round(avg_turn, 2) if avg_turn is not None else None,
        "liquidity_ok": liquidity_ok,
        "sector_hot_score_assumed": sector,
    }
