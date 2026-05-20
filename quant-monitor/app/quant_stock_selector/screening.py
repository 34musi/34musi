"""Technical screen (trend / volume / risk scores); short_term mode for ⑨ 短线选股。"""

from __future__ import annotations

import math

import pandas as pd

from .constants import TRADING_DAYS_PER_YEAR
from .exceptions import DataSourceError
from .market_utils import compute_max_drawdown, safe_float, standardize_price_frame
from .models import ScreenMetrics


def _pct_ret(close: pd.Series, n: int) -> float:
    if len(close) < n + 1:
        return float("nan")
    prev = float(close.iloc[-(n + 1)])
    last = float(close.iloc[-1])
    if prev == 0 or not math.isfinite(prev) or not math.isfinite(last):
        return float("nan")
    return (last - prev) / abs(prev)


def evaluate_screen(frame: pd.DataFrame, *, mode: str = "short_term") -> ScreenMetrics:
    """
    技术面初筛。

    - legacy：原规则（≥120 根），passed = 收盘>MA20>MA60 等。
    - short_term（默认）：叠加短线常用条件（MA5/10、5 日涨跌、MA20 斜率、末根量比等）。
    """
    screen_mode = (mode or "short_term").strip().lower()
    if screen_mode not in ("legacy", "short_term"):
        screen_mode = "short_term"

    data = standardize_price_frame(frame).copy()
    min_bars = 60 if screen_mode == "short_term" else 120
    if len(data) < min_bars:
        raise DataSourceError(f"历史数据少于 {min_bars} 个交易日，无法做技术面筛选")

    close = data["close"]
    volume = data["volume"].fillna(0)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean() if len(data) >= 120 else ma60

    ret5 = _pct_ret(close, 5)
    ret10 = _pct_ret(close, 10)
    ret20 = close.pct_change(20).iloc[-1]
    rolling_high60 = close.rolling(60).max().iloc[-1]
    distance_to_high = max((rolling_high60 - close.iloc[-1]) / rolling_high60, 0.0) if rolling_high60 else 0.0
    avg_volume_20 = volume.rolling(20).mean().iloc[-1]
    avg_volume_60 = volume.rolling(60).mean().iloc[-1]
    volume_ratio = avg_volume_20 / avg_volume_60 if avg_volume_60 else 0.0
    last_vol = float(volume.iloc[-1])
    vol_ratio_last = last_vol / avg_volume_20 if avg_volume_20 and avg_volume_20 > 0 else 0.0
    annual_volatility = close.pct_change().tail(20).std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown_60d = compute_max_drawdown(close.tail(60))

    latest_close = float(close.iloc[-1])
    latest_ma5 = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else latest_close
    latest_ma10 = float(ma10.iloc[-1]) if pd.notna(ma10.iloc[-1]) else latest_close
    latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else latest_close
    latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else latest_close
    latest_ma120 = float(ma120.iloc[-1]) if pd.notna(ma120.iloc[-1]) else latest_ma60
    latest_ret5 = float(ret5) if pd.notna(ret5) else float("nan")
    latest_ret10 = float(ret10) if pd.notna(ret10) else float("nan")
    latest_ret20 = float(ret20) if pd.notna(ret20) else float("nan")
    annual_volatility = safe_float(annual_volatility)

    ma20_prev = float(ma20.iloc[-6]) if len(ma20) > 6 and pd.notna(ma20.iloc[-6]) else latest_ma20
    ma20_slope_pct = (latest_ma20 - ma20_prev) / (abs(ma20_prev) + 1e-9) * 100.0

    # --- 分项得分（legacy 三维度，保留兼容） ---
    trend_score = 0.0
    if latest_close > latest_ma20:
        trend_score += 20.0
    if latest_ma20 > latest_ma60:
        trend_score += 20.0
    if latest_ma60 > latest_ma120:
        trend_score += 15.0
    if not math.isnan(latest_ret20) and latest_ret20 > 0:
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
    if vol_ratio_last >= 1.2:
        volume_score += 8.0
    elif vol_ratio_last >= 1.0:
        volume_score += 4.0

    risk_score = 0.0
    if drawdown_60d <= 0.10:
        risk_score += 20.0
    elif drawdown_60d <= 0.18:
        risk_score += 10.0
    if annual_volatility <= 0.25:
        risk_score += 15.0
    elif annual_volatility <= 0.35:
        risk_score += 8.0
    elif screen_mode == "short_term" and annual_volatility <= 0.45:
        risk_score += 4.0

    legacy_reasons: list[str] = []
    if latest_close <= latest_ma20 or latest_ma20 <= latest_ma60:
        legacy_reasons.append("均线趋势不够强")
    if distance_to_high > 0.12:
        legacy_reasons.append("离 60 日新高较远")
    if drawdown_60d > 0.18:
        legacy_reasons.append("近 60 日回撤偏大")
    if annual_volatility > 0.35:
        legacy_reasons.append("近 20 日波动偏大")
    if volume_ratio < 0.8:
        legacy_reasons.append("量能偏弱")

    legacy_pass = (
        latest_close > latest_ma20 > latest_ma60
        and not math.isnan(latest_ret20)
        and latest_ret20 > 0
        and distance_to_high <= 0.12
        and drawdown_60d <= 0.18
    )

    # --- 短线专用分（0–100，基准 50）---
    st_score = 50.0
    st_notes: list[str] = []
    st_fail: list[str] = []

    ma_stack_short = latest_close > latest_ma5 > latest_ma10 > latest_ma20
    if ma_stack_short:
        st_score += 15.0
        st_notes.append("短线均线多头(收>MA5>10>20)")
    elif latest_close > latest_ma20:
        st_score += 6.0
    else:
        st_fail.append("收盘未站稳 MA20")

    if latest_ma20 > latest_ma60:
        st_score += 8.0
    else:
        st_fail.append("MA20 未高于 MA60")

    if not math.isnan(latest_ret5) and latest_ret5 > 0.01:
        st_score += 8.0
        st_notes.append("5 日动能为正")
    elif not math.isnan(latest_ret5) and latest_ret5 > -0.02:
        st_score += 3.0
    else:
        st_fail.append("近 5 日走弱")

    if not math.isnan(latest_ret20) and latest_ret20 > 0:
        st_score += 6.0
    else:
        st_fail.append("近 20 日收益非正")

    if ma20_slope_pct > 0.08:
        st_score += 8.0
        st_notes.append("MA20 向上")
    elif ma20_slope_pct > 0:
        st_score += 4.0
    else:
        st_fail.append("MA20 斜率非正")

    if distance_to_high <= 0.05:
        st_score += 10.0
        st_notes.append("贴近 60 日新高")
    elif distance_to_high <= 0.12:
        st_score += 5.0
    else:
        st_fail.append("离 60 日新高过远")

    if vol_ratio_last >= 1.2:
        st_score += 10.0
        st_notes.append("末根放量")
    elif vol_ratio_last >= 1.0:
        st_score += 5.0
    elif vol_ratio_last >= 0.85:
        st_score += 2.0
    else:
        st_fail.append("末根量比不足")

    if volume_ratio >= 1.2:
        st_score += 5.0
    elif volume_ratio < 0.8:
        st_fail.append("20/60 日均量比偏弱")

    if drawdown_60d <= 0.10:
        st_score += 5.0
    elif drawdown_60d > 0.18:
        st_score -= 10.0
        st_fail.append("60 日回撤过大")

    if annual_volatility > 0.45:
        st_score -= 8.0
        st_fail.append("波动过高")
    elif annual_volatility > 0.35:
        st_score -= 4.0

    short_term_score = round(max(0.0, min(100.0, st_score)), 2)

    short_term_pass = (
        latest_close > latest_ma20 > latest_ma60
        and latest_close > latest_ma5
        and (ma_stack_short or (latest_ma10 > latest_ma20 and ma20_slope_pct > 0))
        and not math.isnan(latest_ret20)
        and latest_ret20 > 0
        and (math.isnan(latest_ret5) or latest_ret5 > -0.02)
        and ma20_slope_pct > 0
        and distance_to_high <= 0.12
        and drawdown_60d <= 0.18
        and vol_ratio_last >= 0.85
        and volume_ratio >= 0.8
        and annual_volatility <= 0.45
    )

    if screen_mode == "short_term":
        passed = bool(short_term_pass)
        reasons_parts = []
        if passed:
            reasons_parts.append("短线初筛通过：" + ("；".join(st_notes) if st_notes else "量价趋势达标"))
        else:
            reasons_parts.append("短线初筛未通过：" + ("、".join(st_fail) if st_fail else "条件不足"))
        if legacy_reasons:
            reasons_parts.append("参考：" + "、".join(legacy_reasons))
        reasons = "。".join(reasons_parts)
        # 综合技术分：短线分与三维度分各半，便于排序
        screen_score = round((short_term_score + trend_score + volume_score + risk_score) / 2.0, 2)
    else:
        passed = bool(legacy_pass)
        short_term_pass = passed
        short_term_score = round(trend_score + volume_score + risk_score, 2)
        reasons = "、".join(legacy_reasons) if legacy_reasons else "趋势、量能和风险指标均达标"

    long_term_passed = bool(legacy_pass)
    long_term_score = round(trend_score + volume_score + risk_score, 2)

    return ScreenMetrics(
        passed=passed,
        trend_score=round(trend_score, 2),
        volume_score=round(volume_score, 2),
        risk_score=round(risk_score, 2),
        screen_score=round(screen_score, 2),
        latest_close=round(latest_close, 2),
        ma20=round(latest_ma20, 2),
        ma60=round(latest_ma60, 2),
        ma120=round(latest_ma120, 2),
        return_20d=round(latest_ret20 * 100.0, 2) if not math.isnan(latest_ret20) else 0.0,
        distance_to_60d_high=round(distance_to_high * 100.0, 2),
        volume_ratio_20_60=round(volume_ratio, 2),
        drawdown_60d=round(drawdown_60d * 100.0, 2),
        annual_volatility_20d=round(annual_volatility * 100.0, 2),
        reasons=reasons,
        return_5d=round(latest_ret5 * 100.0, 2) if not math.isnan(latest_ret5) else 0.0,
        return_10d=round(latest_ret10 * 100.0, 2) if not math.isnan(latest_ret10) else 0.0,
        ma5=round(latest_ma5, 2),
        ma10=round(latest_ma10, 2),
        ma20_slope_pct=round(ma20_slope_pct, 2),
        vol_ratio_last_day=round(vol_ratio_last, 2),
        short_term_passed=bool(short_term_pass),
        short_term_score=short_term_score,
        long_term_passed=long_term_passed,
        long_term_score=long_term_score,
        screen_mode=screen_mode,
    )
