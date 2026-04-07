"""Technical screen (trend / volume / risk scores)."""

from __future__ import annotations

import math

import pandas as pd

from .constants import TRADING_DAYS_PER_YEAR
from .exceptions import DataSourceError
from .market_utils import compute_max_drawdown, safe_float, standardize_price_frame
from .models import ScreenMetrics


def evaluate_screen(frame: pd.DataFrame) -> ScreenMetrics:
    data = standardize_price_frame(frame).copy()
    if len(data) < 120:
        raise DataSourceError("历史数据少于 120 个交易日，无法做完整的趋势筛选")

    close = data["close"]
    volume = data["volume"].fillna(0)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    ret20 = close.pct_change(20).iloc[-1]
    rolling_high60 = close.rolling(60).max().iloc[-1]
    distance_to_high = max((rolling_high60 - close.iloc[-1]) / rolling_high60, 0.0)
    avg_volume_20 = volume.rolling(20).mean().iloc[-1]
    avg_volume_60 = volume.rolling(60).mean().iloc[-1]
    volume_ratio = avg_volume_20 / avg_volume_60 if avg_volume_60 else 0.0
    annual_volatility = close.pct_change().tail(20).std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown_60d = compute_max_drawdown(close.tail(60))

    latest_close = float(close.iloc[-1])
    latest_ma20 = float(ma20.iloc[-1])
    latest_ma60 = float(ma60.iloc[-1])
    latest_ma120 = float(ma120.iloc[-1])
    latest_ret20 = float(ret20)
    annual_volatility = safe_float(annual_volatility)

    trend_score = 0.0
    if latest_close > latest_ma20:
        trend_score += 20.0
    if latest_ma20 > latest_ma60:
        trend_score += 20.0
    if latest_ma60 > latest_ma120:
        trend_score += 15.0
    if latest_ret20 > 0:
        trend_score += 15.0

    volume_score = 0.0
    if volume_ratio >= 1.2:
        volume_score += 20.0
    elif volume_ratio >= 0.95:
        volume_score += 12.0
    elif volume_ratio >= 0.8:
        volume_score += 6.0
    if distance_to_high <= 0.05:
        volume_score += 15.0
    elif distance_to_high <= 0.10:
        volume_score += 8.0

    risk_score = 0.0
    if drawdown_60d <= 0.10:
        risk_score += 20.0
    elif drawdown_60d <= 0.18:
        risk_score += 10.0
    if annual_volatility <= 0.25:
        risk_score += 15.0
    elif annual_volatility <= 0.35:
        risk_score += 8.0

    reasons = []
    if latest_close <= latest_ma20 or latest_ma20 <= latest_ma60:
        reasons.append("均线趋势不够强")
    if distance_to_high > 0.12:
        reasons.append("离 60 日新高较远")
    if drawdown_60d > 0.18:
        reasons.append("近 60 日回撤偏大")
    if annual_volatility > 0.35:
        reasons.append("近 20 日波动偏大")
    if volume_ratio < 0.8:
        reasons.append("量能偏弱")

    essential_pass = (
        latest_close > latest_ma20 > latest_ma60
        and latest_ret20 > 0
        and distance_to_high <= 0.12
        and drawdown_60d <= 0.18
    )

    return ScreenMetrics(
        passed=bool(essential_pass),
        trend_score=round(trend_score, 2),
        volume_score=round(volume_score, 2),
        risk_score=round(risk_score, 2),
        screen_score=round(trend_score + volume_score + risk_score, 2),
        latest_close=round(latest_close, 2),
        ma20=round(latest_ma20, 2),
        ma60=round(latest_ma60, 2),
        ma120=round(latest_ma120, 2),
        return_20d=round(latest_ret20 * 100.0, 2),
        distance_to_60d_high=round(distance_to_high * 100.0, 2),
        volume_ratio_20_60=round(volume_ratio, 2),
        drawdown_60d=round(drawdown_60d * 100.0, 2),
        annual_volatility_20d=round(annual_volatility * 100.0, 2),
        reasons="、".join(reasons) if reasons else "趋势、量能和风险指标均达标",
    )
