"""
信号计算：基于库内日线计算趋势、强度、技术面 0–100 分；若存在 fundamental_snapshots，
再叠加扩展因子有界调整（估值/财报同比/主力净流入，Demo 规则），得到合成总分。

依赖 ingest.load_bars_df（数据不足时会触发拉取）；`data_source` 与 ingest 枚举一致（含 mootdx/tushare 等），
K 线入库路径可与核心包 app.quant_stock_selector 对齐。名称展示用 fetch_stock_name。
非投资建议。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from app.fundamentals import fundamental_score_delta, load_fundamental_panel_from_db
from app.ingest import (
    fetch_stock_name,
    live_quote_fields_for_codes_enhanced,
    load_bars_df,
    normalize_symbol,
)
from app.schemas import (
    PositionHint,
    SignalOut,
    SignalReason,
    StrengthRegime,
    SuggestedPositionPctOut,
    TrialExitGuidanceOut,
    TrendRegime,
)

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


def _build_signal_metrics(
    df: pd.DataFrame,
    sym: str,
    fund_panel: Any,
    *,
    last_close_override: float | None = None,
) -> dict[str, Any]:
    """
    基于日线 DataFrame 计算趋势/强度/评分/仓位提示等。
    last_close_override：用快照现价等替代最后一根收盘参与计算（ret、趋势、得分）。
    """
    df = df.reset_index(drop=True)
    c = df["close"].astype(float).copy()
    if last_close_override is not None and math.isfinite(float(last_close_override)):
        c.iloc[-1] = float(last_close_override)
    v = df["volume"].astype(float)
    h = df["high"].astype(float)
    low = df["low"].astype(float)

    ma5, ma20, ma60 = _ma(c, 5), _ma(c, 20), _ma(c, 60)
    last_close = float(c.iloc[-1])
    last_date = str(df["trade_date"].iloc[-1])

    ma20_now = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else last_close
    ma60_now = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else last_close
    ma20_prev = float(ma20.iloc[-6]) if len(ma20) > 6 and pd.notna(ma20.iloc[-6]) else ma20_now
    ma20_slope = (ma20_now - ma20_prev) / (abs(ma20_prev) + 1e-9)

    ret5 = _pct_change(c, 5)
    ret10 = _pct_change(c, 10)
    ret20 = _pct_change(c, 20)
    vol20_mean = float(v.iloc[-20:].mean()) if len(v) >= 20 else float(v.mean())
    vol_ratio = float(v.iloc[-1] / vol20_mean) if vol20_mean > 0 else 1.0

    high_60 = float(h.iloc[-60:].max()) if len(h) >= 60 else float(h.max())
    low_60 = float(low.iloc[-60:].min()) if len(low) >= 60 else float(low.min())
    dd_from_high = (last_close - high_60) / (abs(high_60) + 1e-9)

    vol_sigma = _rolling_vol(c, 20)

    trend: TrendRegime = "sideways"
    if last_close > ma20_now > ma60_now and ma20_slope > 0.001:
        trend = "bullish"
    elif last_close < ma20_now < ma60_now and ma20_slope < -0.001:
        trend = "bearish"

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
        prev_close = float(df["close"].astype(float).iloc[-2])
        limit_up = prev_close * 1.095
        if last_close >= limit_up * 0.98:
            risk_tags.append("临近涨停区")

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

    technical_score = int(max(0, min(100, score)))

    fund_adj = 0
    if fund_panel is None:
        reasons.append(
            SignalReason(
                code="fund_missing",
                text="扩展因子未缓存：请对自选执行 POST /ingest/fundamentals 后再看合成得分",
            )
        )
    else:
        fund_adj, fund_reasons = fundamental_score_delta(fund_panel)
        reasons.extend(fund_reasons)

    combined = int(max(0, min(100, technical_score + fund_adj)))

    position_hint: PositionHint
    if combined >= 72 and trend == "bullish" and strength != "weak":
        position_hint = "moderate"
        position_range_text = "模型提示：可结合自身风险承受能力考虑中等以下试错仓位（示例区间 10%–30% 总资金）"
        suggested_position_pct = SuggestedPositionPctOut(low_pct=10.0, high_pct=30.0)
    elif combined >= 55 and trend != "bearish":
        position_hint = "trial"
        position_range_text = "模型提示：轻仓试错更合适（示例区间 0%–10% 总资金）"
        suggested_position_pct = SuggestedPositionPctOut(low_pct=0.0, high_pct=10.0)
    elif combined >= 40:
        position_hint = "cautious"
        position_range_text = "模型提示：信号一般，建议观望或极低仓观察"
        suggested_position_pct = SuggestedPositionPctOut(low_pct=0.0, high_pct=5.0)
    else:
        position_hint = "avoid"
        position_range_text = "模型提示：当前信号偏弱，不建议新开仓（非指令）"
        suggested_position_pct = SuggestedPositionPctOut(low_pct=0.0, high_pct=0.0)

    ma20_exit_ref = round(ma20_now, 4)
    if position_hint == "trial":
        trial_exit_guidance = TrialExitGuidanceOut(
            applies=True,
            stop_loss_pct_from_entry_demo=8.0,
            reference_exit_ma20=ma20_exit_ref,
            note=(
                "轻仓试错若方向看错：Demo 可在**建仓成本**下方约 **8%** 设纪律止损；"
                "或价格**有效跌破 MA20**（参考收盘约见 reference_exit_ma20）时考虑减仓/离场。"
                "系统不记录您的买入价，请自行对照成本执行。**非卖出指令。**"
            ),
        )
    elif position_hint == "moderate":
        trial_exit_guidance = TrialExitGuidanceOut(
            applies=True,
            stop_loss_pct_from_entry_demo=10.0,
            reference_exit_ma20=ma20_exit_ref,
            note=(
                "中等试错：可放宽至成本下方约 **10%** 止损；若**持续走弱并跌破 MA20**（参考价见 reference_exit_ma20），宜收紧风控。"
                "**非卖出指令。**"
            ),
        )
    elif position_hint == "cautious":
        trial_exit_guidance = TrialExitGuidanceOut(
            applies=True,
            stop_loss_pct_from_entry_demo=6.0,
            reference_exit_ma20=ma20_exit_ref,
            note=(
                "极低仓观察：错判宜快认错，Demo 参考成本下方约 **6%**，或跌破 **MA20** 时离场。"
                "**非卖出指令。**"
            ),
        )
    else:
        trial_exit_guidance = TrialExitGuidanceOut(
            applies=False,
            stop_loss_pct_from_entry_demo=None,
            reference_exit_ma20=ma20_exit_ref,
            note=(
                "当前为**回避**为主：不建议新开仓，故不提供「试错卖出位」。"
                "若您仍有历史持仓，请按自有止损规则；MA20 参考收盘见 reference_exit_ma20，仅作结构参照。"
            ),
        )

    meta: dict[str, Any] = {
        "ma20": round(ma20_now, 4),
        "ma60": round(ma60_now, 4),
        "ret_5d": None if np.isnan(ret5) else round(float(ret5), 6),
        "ret_10d": None if np.isnan(ret10) else round(float(ret10), 6),
        "ret_20d": None if np.isnan(ret20) else round(float(ret20), 6),
        "vol_ratio_1d_vs_20d": round(vol_ratio, 4),
        "drawdown_from_60d_high": round(float(dd_from_high), 6),
        "realized_vol_20d": None if np.isnan(vol_sigma) else round(float(vol_sigma), 6),
        "technical_score": technical_score,
        "fundamental_adjustment": fund_adj,
    }
    if last_close_override is not None:
        meta["price_basis"] = "spot_snapshot"
        meta["spot_close_used"] = round(last_close, 4)

    return {
        "as_of_date": last_date,
        "close": round(last_close, 4),
        "trend": trend,
        "strength": strength,
        "buy_suitability_score": combined,
        "technical_score": technical_score,
        "fundamental_adjustment": fund_adj,
        "position_hint": position_hint,
        "suggested_position_pct": suggested_position_pct,
        "trial_exit_guidance": trial_exit_guidance,
        "position_range_text": position_range_text,
        "risk_tags": risk_tags,
        "reasons": reasons,
        "meta": meta,
    }


def _spot_overlay_for_symbol(
    sym: str,
    df: pd.DataFrame,
    fund_panel: Any,
    *,
    data_source: str | None = None,
) -> dict[str, Any]:
    """
    「现价（日线）」列：优先东财单股/列表或通达信快照；失败时回退本地日线末根收盘。
    截止日/收盘列仍用 base 中最后一根已入库 K 线，与现价可不同日。
    """
    if df is None or df.empty:
        return {}
    sym_n = normalize_symbol(sym)
    live = (
        live_quote_fields_for_codes_enhanced(
            [sym_n], data_source=data_source, force_spot_refresh=True
        ).get(sym_n)
        or {}
    )
    px_live = live.get("live_last_price")
    use_live = px_live is not None and math.isfinite(float(px_live)) and float(px_live) > 0
    if use_live:
        px_f = float(px_live)
        chg = live.get("live_change_pct")
        if chg is not None and math.isfinite(float(chg)):
            chg_f: float | None = round(float(chg), 2)
        else:
            chg_f = None
            bar_c = float(df["close"].iloc[-1])
            if math.isfinite(bar_c) and bar_c > 0:
                chg_f = round((px_f / bar_c - 1) * 100, 2)
        spot_m = _build_signal_metrics(df, sym, fund_panel, last_close_override=px_f)
        out: dict[str, Any] = {
            "spot_last_price": round(px_f, 4),
            "spot_buy_suitability_score": spot_m["buy_suitability_score"],
            "spot_buy_hint": spot_m["position_range_text"],
            "spot_position_hint": spot_m["position_hint"],
            "spot_price_source": live.get("live_price_source") or "live_quote",
            "spot_price_basis": "live_quote",
        }
        if chg_f is not None:
            out["spot_change_pct"] = chg_f
        return out

    px = float(df["close"].iloc[-1])
    if not math.isfinite(px) or px <= 0:
        return {}
    chg: float | None = None
    if len(df) >= 2:
        prev = float(df["close"].iloc[-2])
        if math.isfinite(prev) and prev > 0:
            chg = round((px / prev - 1) * 100, 2)
    spot_m = _build_signal_metrics(df, sym, fund_panel, last_close_override=px)
    out = {
        "spot_last_price": round(px, 4),
        "spot_buy_suitability_score": spot_m["buy_suitability_score"],
        "spot_buy_hint": spot_m["position_range_text"],
        "spot_position_hint": spot_m["position_hint"],
        "spot_price_source": "daily_close",
        "spot_price_basis": "daily_bar",
    }
    if chg is not None:
        out["spot_change_pct"] = chg
    return out


def compute_signal(symbol: str, *, data_source: str | None = None) -> SignalOut:
    """
    对单标的计算完整 SignalOut。

    要求至少约 30 根有效 K 线；不足则 ValueError（需先 ingest）。
    data_source：与 ingest 路线一致时传入，便于 load_bars_df 内自动补拉使用同一路线。
    """
    sym = normalize_symbol(symbol)
    df = load_bars_df(sym, data_source=data_source)
    if df.empty or len(df) < 30:
        raise ValueError("K 线数据不足，请先执行更新 ingest")

    fund_panel = load_fundamental_panel_from_db(sym)
    base = _build_signal_metrics(df, sym, fund_panel)
    spot_extra = _spot_overlay_for_symbol(sym, df, fund_panel, data_source=data_source)
    src = spot_extra.pop("spot_price_source", None)
    basis = spot_extra.pop("spot_price_basis", None)
    if src or basis:
        meta = dict(base.get("meta") or {})
        if src:
            meta["spot_price_source"] = src
        if basis:
            meta["spot_price_basis"] = basis
        base["meta"] = meta
    name = fetch_stock_name(sym)

    return SignalOut(
        symbol=sym,
        name=name,
        fundamentals=fund_panel,
        **base,
        **spot_extra,
    )
