"""
信号计算：基于库内日线计算趋势、强度、0–100 评分、仓位提示文案与风险标签。

依赖 ingest.load_bars_df（数据不足时会触发拉取）；名称展示用 fetch_stock_name。
规则以均线排列、斜率、短期涨跌、量能比、回撤等启发式组合，非投资建议。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.ingest import fetch_stock_name, load_bars_df, normalize_symbol
from app.schemas import PositionHint, SignalOut, SignalReason, StrengthRegime, TrendRegime

logger = logging.getLogger(__name__)


def _ma(s: pd.Series, n: int) -> pd.Series:
    """滚动均线；min_periods 取 n 与 n//3 的较大值，避免初期全 NaN。"""
    return s.rolling(n, min_periods=max(3, n // 3)).mean()


def _pct_change(s: pd.Series, n: int) -> float:
    """最近一根相对 n 根之前的收益率；(s[-1]-s[-(n+1)])/|s[-(n+1)]|；数据不足返回 nan。"""
    if len(s) < n + 1 or pd.isna(s.iloc[-1]) or pd.isna(s.iloc[-(n + 1)]):
        return float("nan")
    prev = float(s.iloc[-(n + 1)])
    if prev == 0:
        return float("nan")
    return (float(s.iloc[-1]) - prev) / abs(prev)


def _rolling_vol(close: pd.Series, n: int = 20) -> float:
    """最近 n 根收盘日收益率的标准差（ddof=0），作已实现波动率近似。"""
    r = close.pct_change()
    if len(r) < n:
        return float("nan")
    return float(r.iloc[-n:].std(ddof=0) or 0.0)


def compute_signal(symbol: str) -> SignalOut:
    """
    对单标的计算完整 SignalOut。

    要求至少约 30 根有效 K 线；不足则 ValueError（需先 ingest）。
    """
    sym = normalize_symbol(symbol)
    df = load_bars_df(sym)
    if df.empty or len(df) < 30:
        raise ValueError("K 线数据不足，请先执行更新 ingest")

    df = df.reset_index(drop=True)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    h = df["high"].astype(float)
    low = df["low"].astype(float)

    ma5, ma20, ma60 = _ma(c, 5), _ma(c, 20), _ma(c, 60)
    last_close = float(c.iloc[-1])
    last_date = str(df["trade_date"].iloc[-1])

    ma20_now = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else last_close
    ma60_now = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else last_close
    # 用约 5 日前的 ma20 估计斜率，减弱单日噪声
    ma20_prev = float(ma20.iloc[-6]) if len(ma20) > 6 and pd.notna(ma20.iloc[-6]) else ma20_now
    ma20_slope = (ma20_now - ma20_prev) / (abs(ma20_prev) + 1e-9)

    ret5 = _pct_change(c, 5)
    ret20 = _pct_change(c, 20)
    vol20_mean = float(v.iloc[-20:].mean()) if len(v) >= 20 else float(v.mean())
    vol_ratio = float(v.iloc[-1] / vol20_mean) if vol20_mean > 0 else 1.0

    high_60 = float(h.iloc[-60:].max()) if len(h) >= 60 else float(h.max())
    low_60 = float(low.iloc[-60:].min()) if len(low) >= 60 else float(low.min())
    dd_from_high = (last_close - high_60) / (abs(high_60) + 1e-9)
    bounce_from_low = (last_close - low_60) / (abs(low_60) + 1e-9)

    vol_sigma = _rolling_vol(c, 20)

    # --- 趋势：均线多空排列 + 20 日均线斜率 ---
    trend: TrendRegime = "sideways"
    if last_close > ma20_now > ma60_now and ma20_slope > 0.001:
        trend = "bullish"
    elif last_close < ma20_now < ma60_now and ma20_slope < -0.001:
        trend = "bearish"
    else:
        trend = "sideways"

    # --- 强度：短期涨跌、量能、距阶段高点的距离 ---
    strength: StrengthRegime = "neutral"
    strong_up = (not np.isnan(ret5) and ret5 > 0.03) or (not np.isnan(ret20) and ret20 > 0.08)
    strong_vol = vol_ratio > 1.4
    near_high = dd_from_high > -0.02
    if (strong_up or near_high) and strong_vol:
        strength = "strong"
    elif (not np.isnan(ret5) and ret5 < -0.03) or (not np.isnan(ret20) and ret20 < -0.08):
        strength = "weak"
    elif last_close < ma20_now and ma20_slope < 0:
        strength = "weak"
    else:
        strength = "neutral"

    # --- 风险标签：波动、回撤、放量、震荡、接近涨停区等 ---
    risk_tags: list[str] = []
    if not np.isnan(vol_sigma) and vol_sigma > 0.025:
        risk_tags.append("高波动")
    if not np.isnan(dd_from_high) and dd_from_high < -0.15:
        risk_tags.append("距60日高点回撤较大")
    if vol_ratio > 2.2:
        risk_tags.append("放量异动")
    if trend == "sideways":
        risk_tags.append("趋势未确认")
    if len(df) >= 2:
        prev_close = float(c.iloc[-2])
        limit_up = prev_close * 1.095
        if last_close >= limit_up * 0.98:
            risk_tags.append("临近涨停区")

    # --- 0–100 分：在 50 基准上按趋势/强度/斜率/波动与回撤加减 ---
    score = 50
    reasons: list[SignalReason] = []

    if trend == "bullish":
        score += 18
        reasons.append(SignalReason(code="trend_bull", text="日线均线结构偏多，价格在短期与中期均线上方"))
    elif trend == "bearish":
        score -= 18
        reasons.append(SignalReason(code="trend_bear", text="日线均线结构偏空，价格在短期与中期均线下方"))
    else:
        reasons.append(SignalReason(code="trend_side", text="趋势震荡，多空未形成清晰排列"))

    if strength == "strong":
        score += 12
        reasons.append(SignalReason(code="mom_strong", text="短期动能偏强（涨幅或量能放大）"))
    elif strength == "weak":
        score -= 12
        reasons.append(SignalReason(code="mom_weak", text="短期动能偏弱（跌幅或量价走弱）"))

    if ma20_slope > 0.002:
        score += 6
        reasons.append(SignalReason(code="ma_slope_up", text="20 日均线斜率向上"))
    elif ma20_slope < -0.002:
        score -= 6
        reasons.append(SignalReason(code="ma_slope_down", text="20 日均线斜率向下"))

    if not np.isnan(vol_sigma) and vol_sigma > 0.03:
        score -= 10
        reasons.append(SignalReason(code="risk_vol", text="近期波动率较高，注意回撤风险"))

    if "距60日高点回撤较大" in risk_tags:
        score -= 8
        reasons.append(SignalReason(code="risk_dd", text="价格相对阶段高点回撤明显"))

    score = int(max(0, min(100, score)))

    # --- 仓位提示：与评分、趋势、强度联动；文案为模型提示非交易指令 ---
    position_hint: PositionHint
    position_range_text: str
    if score >= 72 and trend == "bullish" and strength != "weak":
        position_hint = "moderate"
        position_range_text = "模型提示：可结合自身风险承受能力考虑中等以下试错仓位（示例区间 10%–30% 总资金）"
    elif score >= 55 and trend != "bearish":
        position_hint = "trial"
        position_range_text = "模型提示：轻仓试错更合适（示例区间 0%–10% 总资金）"
    elif score >= 40:
        position_hint = "cautious"
        position_range_text = "模型提示：信号一般，建议观望或极低仓观察"
    else:
        position_hint = "avoid"
        position_range_text = "模型提示：当前信号偏弱，不建议新开仓（非指令）"

    name = fetch_stock_name(sym)
    meta: dict[str, Any] = {
        "ma20": round(ma20_now, 4),
        "ma60": round(ma60_now, 4),
        "ret_5d": None if np.isnan(ret5) else round(float(ret5), 6),
        "ret_20d": None if np.isnan(ret20) else round(float(ret20), 6),
        "vol_ratio_1d_vs_20d": round(vol_ratio, 4),
        "drawdown_from_60d_high": round(float(dd_from_high), 6),
        "realized_vol_20d": None if np.isnan(vol_sigma) else round(float(vol_sigma), 6),
    }

    return SignalOut(
        symbol=sym,
        name=name,
        as_of_date=last_date,
        close=round(last_close, 4),
        trend=trend,
        strength=strength,
        buy_suitability_score=score,
        position_hint=position_hint,
        position_range_text=position_range_text,
        risk_tags=risk_tags,
        reasons=reasons,
        meta=meta,
    )
