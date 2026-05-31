"""
增强信号层（Demo）：在技术面 + 扩展因子之上叠加量能/流动性、技术确认（RSI/MACD/突破）、
相对沪深300超额、事件风险提示、简易历史分位与多条件买入门控。

供 GET /signals 与「④ 查看信号」展示；非投资建议。
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.schemas import (
    SignalBuyGateOut,
    SignalEnhancedOut,
    SignalReason,
    StrengthRegime,
    SuggestedPositionPctOut,
    TrendRegime,
)

logger = logging.getLogger(__name__)

BuyVerdict = Literal["strong_trial", "trial", "watch", "avoid"]

_BENCH_CACHE: dict[str, Any] = {"ret_5d": None, "ret_10d": None, "ret_20d": None, "fetched_at": 0.0}
_BENCH_TTL_SEC = 3600.0
_BENCH_LABEL = "沪深300(东财日线)"


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 2:
        return float("nan")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain.iloc[-1] / (avg_loss.iloc[-1] + 1e-12)
    if not math.isfinite(float(rs)):
        return float("nan")
    return float(100 - (100 / (1 + rs)))


def _macd_hist(close: pd.Series) -> tuple[float, float]:
    """返回 (macd_hist, macd_line) 末值。"""
    if len(close) < 35:
        return float("nan"), float("nan")
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    h = float(hist.iloc[-1])
    m = float(macd.iloc[-1])
    return h, m


def _atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 2:
        return float("nan")
    prev_c = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_c).abs(),
            (low - prev_c).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=max(3, period // 2)).mean().iloc[-1]
    last = float(close.iloc[-1])
    if not math.isfinite(float(atr)) or last <= 0:
        return float("nan")
    return float(atr / last * 100.0)


def _pct_change_series(s: pd.Series, n: int) -> float:
    if len(s) < n + 1:
        return float("nan")
    prev = float(s.iloc[-(n + 1)])
    cur = float(s.iloc[-1])
    if prev == 0 or not math.isfinite(prev) or not math.isfinite(cur):
        return float("nan")
    return (cur - prev) / abs(prev)


def _benchmark_returns() -> dict[str, float | None]:
    now = time.time()
    if now - float(_BENCH_CACHE.get("fetched_at") or 0) < _BENCH_TTL_SEC:
        return {
            "ret_5d": _BENCH_CACHE.get("ret_5d"),
            "ret_10d": _BENCH_CACHE.get("ret_10d"),
            "ret_20d": _BENCH_CACHE.get("ret_20d"),
        }
    out: dict[str, float | None] = {"ret_5d": None, "ret_10d": None, "ret_20d": None}
    try:
        import akshare as ak

        from app.config import get_settings
        from app.ingest import _temporary_clear_proxy_env

        s = get_settings()
        with _temporary_clear_proxy_env(enabled=bool(s.ingest_eastmoney_bypass_proxy)):
            df = ak.stock_zh_index_daily_em(symbol="sh000300")
        if df is None or df.empty:
            raise ValueError("empty index df")
        col = "close" if "close" in df.columns else df.columns[-1]
        c = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(c) < 25:
            raise ValueError("index series too short")
        out["ret_5d"] = _pct_change_series(c, 5)
        out["ret_10d"] = _pct_change_series(c, 10)
        out["ret_20d"] = _pct_change_series(c, 20)
        _BENCH_CACHE.update({**out, "fetched_at": now})
    except Exception as e:
        logger.debug("benchmark sh000300: %s", e)
        _BENCH_CACHE["fetched_at"] = now
    return out


def _holding_hint_for_symbol(sym: str) -> dict[str, Any] | None:
    try:
        from sqlalchemy import select

        from app.db_models import HoldingRow
        from app.db_session import session_scope
        from app.ingest import normalize_symbol

        ns = normalize_symbol(sym)
        with session_scope() as s:
            row = s.execute(
                select(HoldingRow)
                .where(HoldingRow.symbol == ns, HoldingRow.status == "holding")
                .order_by(HoldingRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "holding_shares": float(row.shares),
            "holding_cost_price": float(row.cost_price),
            "holding_buy_date": row.buy_date,
        }
    except Exception:
        return None


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _liquidity_adjustment(
    df: pd.DataFrame,
    live_liquidity: dict[str, Any] | None,
) -> tuple[int, list[SignalReason], dict[str, Any]]:
    raw = 0
    reasons: list[SignalReason] = []
    meta: dict[str, Any] = {}
    amt = df["amount"].astype(float) if "amount" in df.columns else pd.Series(dtype=float)
    if len(amt) >= 5:
        avg20 = float(amt.iloc[-20:].mean()) if len(amt) >= 20 else float(amt.mean())
        avg20_100m = avg20 / 1e8 if math.isfinite(avg20) else None
        meta["avg_amount_20d_100m"] = (
            round(avg20_100m, 4) if avg20_100m is not None and math.isfinite(avg20_100m) else None
        )
        if avg20_100m is not None:
            if avg20_100m < 0.3:
                raw -= 8
                reasons.append(
                    SignalReason(code="liq_low", text="近20日日均成交额偏低（<0.3亿），流动性风险加分减")
                )
            elif avg20_100m < 0.8:
                raw -= 3
                reasons.append(SignalReason(code="liq_moderate_low", text="近20日日均成交额一般（0.3–0.8亿）"))
            elif avg20_100m >= 3.0:
                raw += 4
                reasons.append(SignalReason(code="liq_strong", text="近20日日均成交额充足（≥3亿）"))
            elif avg20_100m >= 1.0:
                raw += 2
                reasons.append(SignalReason(code="liq_ok", text="近20日日均成交额尚可（≥1亿）"))

    spot_amt = None
    if live_liquidity:
        v = live_liquidity.get("spot_amount")
        if v is not None and math.isfinite(float(v)):
            spot_amt = float(v)
            meta["spot_amount_yuan"] = round(spot_amt, 2)
            spot_100m = spot_amt / 1e8
            if spot_100m < 0.15 and raw > -8:
                raw -= 2
                reasons.append(
                    SignalReason(code="liq_spot_thin", text="当日快照成交额偏小，短线冲击成本需留意")
                )
        tr = live_liquidity.get("spot_turnover_rate")
        if tr is not None and math.isfinite(float(tr)):
            tr_f = float(tr)
            meta["spot_turnover_rate"] = round(tr_f, 4)
            if tr_f >= 20:
                raw -= 4
                reasons.append(
                    SignalReason(code="liq_turnover_very_high", text=f"东财换手率≈{tr_f:.2f}%，短线过热风险")
                )
            elif tr_f >= 12:
                raw -= 2
                reasons.append(
                    SignalReason(code="liq_turnover_high", text=f"东财换手率≈{tr_f:.2f}%，交投活跃")
                )
            elif tr_f <= 1.0:
                raw -= 1
                reasons.append(
                    SignalReason(code="liq_turnover_low", text=f"东财换手率≈{tr_f:.2f}%，交投偏冷")
                )
    return int(max(-10, min(10, raw))), reasons, meta


def _tech_confirm_adjustment(
    df: pd.DataFrame,
    *,
    last_close: float,
    high_60: float,
) -> tuple[int, list[SignalReason], dict[str, Any]]:
    raw = 0
    reasons: list[SignalReason] = []
    meta: dict[str, Any] = {}
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    low = df["low"].astype(float)

    rsi = _rsi(c, 14)
    if math.isfinite(rsi):
        meta["rsi_14"] = round(rsi, 2)
        if rsi >= 80:
            raw -= 6
            reasons.append(SignalReason(code="rsi_overbought", text=f"RSI(14)≈{rsi:.1f}，短线超买风险"))
        elif rsi <= 35:
            raw += 4
            reasons.append(SignalReason(code="rsi_oversold_bounce", text=f"RSI(14)≈{rsi:.1f}，超卖区反弹潜力（需趋势配合）"))
        elif 45 <= rsi <= 65:
            raw += 2
            reasons.append(SignalReason(code="rsi_neutral_ok", text=f"RSI(14)≈{rsi:.1f}，未处极端区"))

    hist, macd_line = _macd_hist(c)
    if math.isfinite(hist):
        meta["macd_hist"] = round(hist, 6)
        meta["macd_line"] = round(macd_line, 6) if math.isfinite(macd_line) else None
        if hist > 0 and macd_line > 0:
            raw += 4
            reasons.append(SignalReason(code="macd_bull", text="MACD 柱为正且 DIF>0，动能偏多"))
        elif hist < 0 and macd_line < 0:
            raw -= 4
            reasons.append(SignalReason(code="macd_bear", text="MACD 柱为负且 DIF<0，动能偏弱"))
        elif hist > 0:
            raw += 2
            reasons.append(SignalReason(code="macd_hist_up", text="MACD 柱转正，短线动能改善"))

    atr_pct = _atr_pct(h, low, c, 14)
    if math.isfinite(atr_pct):
        meta["atr_14_pct"] = round(atr_pct, 4)
        if atr_pct > 5.0:
            raw -= 3
            reasons.append(SignalReason(code="atr_high", text=f"ATR(14)占价约 {atr_pct:.1f}%，波动偏大"))
        elif atr_pct < 2.0:
            raw += 1
            reasons.append(SignalReason(code="atr_moderate", text=f"ATR(14)占价约 {atr_pct:.1f}%，波动相对温和"))

    if math.isfinite(high_60) and high_60 > 0 and last_close >= high_60 * 0.995:
        raw += 3
        reasons.append(SignalReason(code="breakout_60d", text="收盘接近或突破60日高点，趋势突破确认（需防假突破）"))
        meta["near_60d_high"] = True

    return int(max(-12, min(12, raw))), reasons, meta


def _relative_strength_adjustment(ret5: float, ret10: float, ret20: float) -> tuple[int, list[SignalReason], dict[str, Any]]:
    raw = 0
    reasons: list[SignalReason] = []
    meta: dict[str, Any] = {"benchmark_label": _BENCH_LABEL}
    bench = _benchmark_returns()
    b5, b10, b20 = bench.get("ret_5d"), bench.get("ret_10d"), bench.get("ret_20d")
    meta["benchmark_ret_5d"] = round(b5, 6) if b5 is not None and math.isfinite(b5) else None
    meta["benchmark_ret_20d"] = round(b20, 6) if b20 is not None and math.isfinite(b20) else None

    if ret20 is not None and not np.isnan(ret20) and b20 is not None and math.isfinite(b20):
        excess20 = float(ret20) - float(b20)
        meta["excess_ret_20d_vs_benchmark"] = round(excess20, 6)
        if excess20 > 0.08:
            raw += 6
            reasons.append(
                SignalReason(
                    code="rs_strong",
                    text=f"近20日相对{_BENCH_LABEL}超额约 {excess20*100:.1f}%",
                )
            )
        elif excess20 > 0.02:
            raw += 3
            reasons.append(
                SignalReason(code="rs_ok", text=f"近20日相对大盘略强（超额 {excess20*100:.1f}%）")
            )
        elif excess20 < -0.08:
            raw -= 6
            reasons.append(
                SignalReason(code="rs_weak", text=f"近20日明显跑输大盘（超额 {excess20*100:.1f}%）")
            )
        elif excess20 < -0.03:
            raw -= 3
            reasons.append(SignalReason(code="rs_lag", text="近20日弱于大盘，慎追"))

    if ret5 is not None and not np.isnan(ret5) and b5 is not None and math.isfinite(b5):
        excess5 = float(ret5) - float(b5)
        meta["excess_ret_5d_vs_benchmark"] = round(excess5, 6)
        if excess5 > 0.05:
            raw += 2
        elif excess5 < -0.05:
            raw -= 2

    return int(max(-8, min(8, raw))), reasons, meta


def _event_risk_adjustment(
    name: str | None,
    fund_panel: Any,
    as_of_date: str,
    risk_tags: list[str],
) -> tuple[int, list[SignalReason], dict[str, Any]]:
    raw = 0
    reasons: list[SignalReason] = []
    meta: dict[str, Any] = {}
    nm = (name or "").upper()
    if "ST" in nm or "退" in (name or ""):
        raw -= 10
        meta["is_st_risk"] = True
        reasons.append(SignalReason(code="event_st", text="名称含 ST/退市风险字样，大幅降权"))
    if "临近涨停区" in risk_tags:
        raw -= 4
        reasons.append(SignalReason(code="event_limit_up", text="临近涨停区，新开仓性价比差"))
    if fund_panel and getattr(fund_panel, "financial_report_date", None):
        rd = _parse_ymd(str(fund_panel.financial_report_date))
        ad = _parse_ymd(as_of_date)
        if rd and ad:
            days = abs((ad - rd).days)
            meta["days_since_financial_report"] = days
            if days <= 45:
                raw -= 2
                reasons.append(
                    SignalReason(
                        code="event_earnings_window",
                        text=f"距最近财报期约 {days} 天，业绩兑现/预期波动窗口",
                    )
                )
    return int(max(-10, min(0, raw))), reasons, meta


def _momentum_percentile_adjustment(df: pd.DataFrame, ret20: float) -> tuple[int, list[SignalReason], dict[str, Any]]:
    """用自身历史 20 日收益分位衡量「是否过热」。"""
    meta: dict[str, Any] = {}
    if np.isnan(ret20) or len(df) < 80:
        return 0, [], meta
    c = df["close"].astype(float)
    rets20: list[float] = []
    for i in range(20, len(c)):
        prev = float(c.iloc[i - 20])
        cur = float(c.iloc[i])
        if prev > 0 and math.isfinite(prev) and math.isfinite(cur):
            rets20.append((cur - prev) / abs(prev))
    if len(rets20) < 30:
        return 0, [], meta
    pct = sum(1 for r in rets20 if r < ret20) / len(rets20) * 100.0
    meta["ret_20d_self_percentile"] = round(pct, 2)
    raw = 0
    reasons: list[SignalReason] = []
    if pct >= 92:
        raw -= 4
        reasons.append(
            SignalReason(code="mom_extreme_high", text=f"当前20日涨幅处于自身近史约 {pct:.0f}% 分位，追高风险")
        )
    elif pct >= 75:
        raw += 2
        reasons.append(SignalReason(code="mom_strong_hist", text="20日动能在自身历史中偏强"))
    elif pct <= 15:
        raw += 1
        reasons.append(SignalReason(code="mom_cheap_hist", text="20日动能在自身历史中偏弱，或处修复区"))
    return int(max(-5, min(5, raw))), reasons, meta


def _atr_scaled_position(
    base: SuggestedPositionPctOut,
    atr_pct: float | None,
) -> SuggestedPositionPctOut:
    """按 ATR 占价比缩放示例仓位上限（Demo）。"""
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        return base
    # 目标：ATR≈2% 时保持原上限；ATR 每 +1% 上限约降 15%
    scale = max(0.35, min(1.0, 2.0 / atr_pct))
    hi = round(min(base.high_pct, base.high_pct * scale), 2)
    lo = round(min(base.low_pct, hi * 0.5), 2)
    if hi < lo:
        hi = lo
    return SuggestedPositionPctOut(low_pct=lo, high_pct=hi)


def _build_buy_gates(
    *,
    enhanced_score: int,
    trend: TrendRegime,
    strength: StrengthRegime,
    fund_panel: Any,
    meta_enh: dict[str, Any],
    risk_tags: list[str],
    avg_amt_20d_100m: float | None,
    rsi_14: float | None,
) -> list[SignalBuyGateOut]:
    gates: list[SignalBuyGateOut] = []

    gates.append(
        SignalBuyGateOut(
            code="score_enhanced",
            label="增强适合度",
            passed=enhanced_score >= 55,
            detail=f"当前 {enhanced_score}，开仓试错建议 ≥55",
        )
    )
    gates.append(
        SignalBuyGateOut(
            code="trend",
            label="趋势非空头",
            passed=trend != "bearish",
            detail="bearish 时 Demo 不建议新开仓",
        )
    )
    gates.append(
        SignalBuyGateOut(
            code="strength",
            label="强度非偏弱",
            passed=strength != "weak",
            detail="weak 时慎追",
        )
    )
    liq_ok = avg_amt_20d_100m is None or avg_amt_20d_100m >= 0.25
    gates.append(
        SignalBuyGateOut(
            code="liquidity",
            label="流动性",
            passed=liq_ok,
            detail=(
                f"近20日均额 {avg_amt_20d_100m:.2f}亿"
                if avg_amt_20d_100m is not None
                else "无成交额序列，未校验"
            ),
        )
    )
    excess = meta_enh.get("excess_ret_20d_vs_benchmark")
    rs_ok = excess is None or float(excess) >= -0.05
    gates.append(
        SignalBuyGateOut(
            code="relative_strength",
            label="相对大盘",
            passed=rs_ok,
            detail=(
                f"20日超额 {float(excess)*100:.1f}%"
                if excess is not None
                else "基准未拉取，跳过"
            ),
        )
    )
    rsi_ok = rsi_14 is None or not math.isfinite(rsi_14) or float(rsi_14) < 82
    gates.append(
        SignalBuyGateOut(
            code="rsi",
            label="RSI 未极端超买",
            passed=rsi_ok,
            detail=f"RSI(14)={rsi_14:.1f}" if rsi_14 is not None else "—",
        )
    )
    flow_ok = True
    flow_detail = "未拉扩展因子"
    if fund_panel is not None:
        inf = getattr(fund_panel, "main_net_inflow", None)
        if inf is not None and math.isfinite(float(inf)):
            flow_ok = float(inf) >= 0
            flow_detail = f"主力净流入 {float(inf)/1e8:.2f}亿" if abs(float(inf)) >= 1e8 else f"主力净流入 {inf:.0f}元"
    gates.append(
        SignalBuyGateOut(code="fund_flow", label="资金流向", passed=flow_ok, detail=flow_detail)
    )
    gates.append(
        SignalBuyGateOut(
            code="event",
            label="事件风险",
            passed="临近涨停区" not in risk_tags and not meta_enh.get("is_st_risk"),
            detail="ST/涨停区过滤",
        )
    )
    return gates


def _resolve_verdict(
    enhanced_score: int,
    trend: TrendRegime,
    strength: StrengthRegime,
    gates: list[SignalBuyGateOut],
) -> tuple[BuyVerdict, str]:
    passed_n = sum(1 for g in gates if g.passed)
    critical = {"score_enhanced", "trend", "event"}
    critical_ok = all(g.passed for g in gates if g.code in critical)

    if (
        enhanced_score >= 72
        and trend == "bullish"
        and strength != "weak"
        and passed_n >= 6
        and critical_ok
    ):
        return (
            "strong_trial",
            "多门控通过且增强分偏高：可考虑中等以下试错仓位（须结合自身风控）",
        )
    if enhanced_score >= 55 and trend != "bearish" and critical_ok and passed_n >= 5:
        return ("trial", "条件基本满足：轻仓试错区间（0%–10% 总资金示例）")
    if enhanced_score >= 40 and passed_n >= 3:
        return ("watch", "信号一般：观望或极低仓，等待趋势/资金确认")
    return ("avoid", "门控或分数不足：Demo 不建议新开仓")


def build_signal_enhanced(
    df: pd.DataFrame,
    *,
    sym: str,
    name: str | None,
    fund_panel: Any,
    base: dict[str, Any],
    live_liquidity: dict[str, Any] | None = None,
) -> SignalEnhancedOut:
    """
    在 _build_signal_metrics 的 base 结果上叠加增强层。
    base 需含 trend/strength/risk_tags/meta/technical_score/fundamental_adjustment/suggested_position_pct 等。
    """
    meta = dict(base.get("meta") or {})
    technical = int(base.get("technical_score") or 0)
    fund_adj = int(base.get("fundamental_adjustment") or 0)
    legacy_combined = int(base.get("buy_suitability_score") or 0)

    last_close = float(base.get("close") or df["close"].astype(float).iloc[-1])
    h = df["high"].astype(float)
    high_60 = float(h.iloc[-60:].max()) if len(h) >= 60 else float(h.max())

    ret5 = meta.get("ret_5d")
    ret10 = meta.get("ret_10d")
    ret20 = meta.get("ret_20d")
    r5 = float(ret5) if ret5 is not None else float("nan")
    r10 = float(ret10) if ret10 is not None else float("nan")
    r20 = float(ret20) if ret20 is not None else float("nan")

    liq_adj, liq_reasons, liq_meta = _liquidity_adjustment(df, live_liquidity)
    tech_adj, tech_reasons, tech_meta = _tech_confirm_adjustment(
        df, last_close=last_close, high_60=high_60
    )
    rs_adj, rs_reasons, rs_meta = _relative_strength_adjustment(r5, r10, r20)
    evt_adj, evt_reasons, evt_meta = _event_risk_adjustment(
        name,
        fund_panel,
        str(base.get("as_of_date") or ""),
        list(base.get("risk_tags") or []),
    )
    mom_adj, mom_reasons, mom_meta = _momentum_percentile_adjustment(df, r20)

    enh_meta: dict[str, Any] = {}
    enh_meta.update(liq_meta)
    enh_meta.update(tech_meta)
    enh_meta.update(rs_meta)
    enh_meta.update(evt_meta)
    enh_meta.update(mom_meta)

    # legacy_combined = technical + fund_adj；在其上叠加量能/确认/相对/事件/分位
    enhanced_score = int(
        max(0, min(100, legacy_combined + liq_adj + tech_adj + rs_adj + evt_adj + mom_adj))
    )

    holding = _holding_hint_for_symbol(sym)
    if holding:
        enh_meta["holding"] = holding
        cost = holding.get("holding_cost_price")
        if cost and last_close > 0:
            pnl_pct = (last_close - float(cost)) / float(cost) * 100.0
            enh_meta["holding_pnl_pct_vs_cost"] = round(pnl_pct, 2)

    trend: TrendRegime = base.get("trend") or "sideways"
    strength: StrengthRegime = base.get("strength") or "neutral"
    gates = _build_buy_gates(
        enhanced_score=enhanced_score,
        trend=trend,
        strength=strength,
        fund_panel=fund_panel,
        meta_enh=enh_meta,
        risk_tags=list(base.get("risk_tags") or []),
        avg_amt_20d_100m=liq_meta.get("avg_amount_20d_100m"),
        rsi_14=tech_meta.get("rsi_14"),
    )
    verdict, verdict_text = _resolve_verdict(enhanced_score, trend, strength, gates)

    base_sp = base.get("suggested_position_pct")
    if isinstance(base_sp, SuggestedPositionPctOut):
        atr_scaled = _atr_scaled_position(base_sp, tech_meta.get("atr_14_pct"))
    elif isinstance(base_sp, dict):
        atr_scaled = _atr_scaled_position(
            SuggestedPositionPctOut(low_pct=float(base_sp["low_pct"]), high_pct=float(base_sp["high_pct"])),
            tech_meta.get("atr_14_pct"),
        )
    else:
        atr_scaled = SuggestedPositionPctOut(low_pct=0.0, high_pct=0.0)

    all_reasons = (
        liq_reasons + tech_reasons + rs_reasons + evt_reasons + mom_reasons
    )
    meta["legacy_buy_suitability_score"] = legacy_combined
    meta["enhanced_adjustments"] = {
        "liquidity": liq_adj,
        "tech_confirm": tech_adj,
        "relative_strength": rs_adj,
        "event_risk": evt_adj,
        "momentum_percentile": mom_adj,
    }
    meta["enhanced_meta"] = enh_meta

    return SignalEnhancedOut(
        enhanced_buy_score=enhanced_score,
        legacy_buy_suitability_score=legacy_combined,
        liquidity_adjustment=liq_adj,
        tech_confirm_adjustment=tech_adj,
        relative_strength_adjustment=rs_adj,
        event_risk_adjustment=evt_adj,
        momentum_percentile_adjustment=mom_adj,
        buy_verdict=verdict,
        buy_verdict_text=verdict_text,
        buy_gates=gates,
        atr_suggested_position_pct=atr_scaled,
        rsi_14=tech_meta.get("rsi_14"),
        macd_hist=tech_meta.get("macd_hist"),
        atr_14_pct=tech_meta.get("atr_14_pct"),
        avg_amount_20d_100m=liq_meta.get("avg_amount_20d_100m"),
        spot_amount_yuan=liq_meta.get("spot_amount_yuan"),
        excess_ret_20d_vs_benchmark=rs_meta.get("excess_ret_20d_vs_benchmark"),
        ret_20d_self_percentile=mom_meta.get("ret_20d_self_percentile"),
        benchmark_label=_BENCH_LABEL,
        holding_cost_price=holding.get("holding_cost_price") if holding else None,
        holding_pnl_pct_vs_cost=enh_meta.get("holding_pnl_pct_vs_cost"),
        enhancement_reasons=all_reasons,
        meta=meta,
    )


def apply_enhanced_to_signal_dict(base: dict[str, Any], enhanced: SignalEnhancedOut) -> None:
    """把增强层写回 compute_signal 用的 dict（reasons/meta/仓位）。"""
    base["enhanced"] = enhanced
    base["enhanced_buy_score"] = enhanced.enhanced_buy_score
    base["buy_verdict"] = enhanced.buy_verdict
    base["buy_verdict_text"] = enhanced.buy_verdict_text
    base["buy_gates"] = enhanced.buy_gates
    meta = dict(base.get("meta") or {})
    meta.update(enhanced.meta or {})
    base["meta"] = meta
    if enhanced.enhancement_reasons:
        base["reasons"] = list(base.get("reasons") or []) + enhanced.enhancement_reasons
    # 用 ATR 缩放后的示例仓位覆盖（仅当有有效区间）
    asp = enhanced.atr_suggested_position_pct
    if asp and asp.high_pct > 0:
        base["suggested_position_pct"] = asp
        pr = base.get("position_range_text") or ""
        if "ATR" not in pr:
            base["position_range_text"] = (
                str(pr).rstrip()
                + f"（已按 ATR(14)≈{enhanced.atr_14_pct or '—'}% 缩放示例仓位上限）"
            )
