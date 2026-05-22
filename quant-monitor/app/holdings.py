"""⑩ 持仓记录：读写与行情盈亏估算（非交易所成交回报）。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import HoldingRow
from app.ingest import (
    fetch_stock_name,
    live_quote_fields_for_codes_enhanced,
    watchlist_bar_fields_for_session,
)
from app.schemas import HoldingExitAdviceOut, HoldingOut
from app.signals import compute_signal

HOLDING_STATUS_HOLDING = "holding"
HOLDING_STATUS_CLOSED = "closed"


def holding_days_for_row(row: HoldingRow) -> int | None:
    """
    持仓天数（含首尾日历日）：买入日算第 1 天。
    持仓中：买入日～今天；已平仓：买入日～卖出日。无买入日返回 None。
    """
    raw = (row.buy_date or "").strip()
    if not raw:
        return None
    try:
        d0 = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    end_raw = (row.sell_date or "").strip() if row.status == HOLDING_STATUS_CLOSED else ""
    if end_raw:
        try:
            d1 = date.fromisoformat(end_raw[:10])
        except ValueError:
            d1 = date.today()
    else:
        d1 = date.today()
    if d1 < d0:
        return None
    return (d1 - d0).days + 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _price_from_bar_spot(
    sym: str,
    *,
    bar_by: dict[str, dict[str, Any]],
    spot_by: dict[str, dict[str, Any]],
) -> tuple[float | None, str | None]:
    """优先盘口现价（live/spot），否则最新日线收盘。"""
    spot = spot_by.get(sym) or {}
    px = spot.get("live_last_price")
    if px is None or not (isinstance(px, (int, float)) and float(px) > 0):
        px = spot.get("spot_last_price")
    src = "spot"
    if px is None or not (isinstance(px, (int, float)) and float(px) > 0):
        bar = bar_by.get(sym, {})
        px = bar.get("last_close")
        src = "daily_close"
    if px is None:
        return None, None
    try:
        v = float(px)
    except (TypeError, ValueError):
        return None, None
    if not (v > 0):
        return None, None
    return round(v, 4), src


def _ref_price(
    sym: str,
    *,
    bar_by: dict[str, dict[str, Any]],
    spot_by: dict[str, dict[str, Any]],
) -> tuple[float | None, str | None]:
    """持仓中用于估算市值的参考价（与当前价同源）。"""
    return _price_from_bar_spot(sym, bar_by=bar_by, spot_by=spot_by)


def _pnl_fields(
    row: HoldingRow,
    *,
    ref_price: float | None,
) -> dict[str, Any]:
    shares = float(row.shares)
    cost = float(row.cost_price)
    cost_basis = round(shares * cost, 2)
    out: dict[str, Any] = {
        "cost_basis": cost_basis,
        "market_value": None,
        "unrealized_pnl_amt": None,
        "unrealized_pnl_pct": None,
        "realized_pnl_amt": None,
        "realized_pnl_pct": None,
    }
    if row.status == HOLDING_STATUS_CLOSED:
        sp = row.sell_price
        if sp is not None and float(sp) > 0:
            mv = round(shares * float(sp), 2)
            amt = round(mv - cost_basis, 2)
            pct = round((amt / cost_basis) * 100.0, 2) if cost_basis > 0 else None
            out["market_value"] = mv
            out["realized_pnl_amt"] = amt
            out["realized_pnl_pct"] = pct
        return out
    if ref_price is not None:
        mv = round(shares * ref_price, 2)
        amt = round(mv - cost_basis, 2)
        pct = round((amt / cost_basis) * 100.0, 2) if cost_basis > 0 else None
        out["market_value"] = mv
        out["unrealized_pnl_amt"] = amt
        out["unrealized_pnl_pct"] = pct
    return out


def holding_row_to_out(
    row: HoldingRow,
    *,
    bar_by: dict[str, dict[str, Any]],
    spot_by: dict[str, dict[str, Any]],
) -> HoldingOut:
    sym = row.symbol
    bar = bar_by.get(sym, {})
    spot = spot_by.get(sym) or {}
    ref_price, ref_src = _ref_price(sym, bar_by=bar_by, spot_by=spot_by)
    if row.status == HOLDING_STATUS_CLOSED and row.sell_price is not None and float(row.sell_price) > 0:
        cur_px = round(float(row.sell_price), 4)
        cur_src = "sell"
    else:
        cur_px, cur_src = ref_price, ref_src
    pnl = _pnl_fields(row, ref_price=ref_price)
    return HoldingOut(
        id=row.id,
        symbol=sym,
        name=(row.name or "").strip(),
        status=row.status,
        shares=float(row.shares),
        cost_price=round(float(row.cost_price), 4),
        buy_date=row.buy_date,
        sell_price=round(float(row.sell_price), 4) if row.sell_price is not None else None,
        sell_date=row.sell_date,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_close=bar.get("last_close"),
        bars_last_trade_date=bar.get("bars_last_trade_date"),
        spot_last_price=spot.get("spot_last_price") or spot.get("live_last_price"),
        spot_change_pct=spot.get("spot_change_pct") or spot.get("live_change_pct"),
        current_price=cur_px,
        current_price_source=cur_src,
        ref_price=ref_price,
        ref_price_source=ref_src if row.status == HOLDING_STATUS_HOLDING else "sell",
        holding_days=holding_days_for_row(row),
        **pnl,
    )


def build_holdings_list(session: Session, rows: list[HoldingRow]) -> list[HoldingOut]:
    symbols = list({r.symbol for r in rows})
    bar_by = watchlist_bar_fields_for_session(session, symbols) if symbols else {}
    spot_by = (
        live_quote_fields_for_codes_enhanced(symbols, force_spot_refresh=True)
        if symbols
        else {}
    )
    return [holding_row_to_out(r, bar_by=bar_by, spot_by=spot_by) for r in rows]


def apply_holding_defaults(row: HoldingRow, *, sym: str) -> None:
    if not (row.name or "").strip():
        row.name = fetch_stock_name(sym) or ""
    now = _utc_now_iso()
    if not row.created_at:
        row.created_at = now
    row.updated_at = now


def compute_holding_exit_advice(
    row: HoldingRow,
    *,
    session: Session,
    data_source: str | None = None,
    current_price: float | None = None,
) -> HoldingExitAdviceOut:
    """
    是否建议平仓：成本浮盈亏 + ④ 信号（趋势/MA20/仓位提示/Demo 止损线）加权打分。
    """
    if row.status != HOLDING_STATUS_HOLDING:
        raise ValueError("仅持仓中记录可计算平仓建议")

    sym = row.symbol
    cost = float(row.cost_price)
    shares = float(row.shares)
    cost_basis = round(shares * cost, 2)
    item = build_holdings_list(session, [row])[0]

    ref_price: float | None
    ref_src: str | None
    pnl_pct: float | None

    if current_price is not None:
        try:
            px = float(current_price)
        except (TypeError, ValueError) as e:
            raise ValueError("current_price 无效") from e
        if not (px > 0):
            raise ValueError("current_price 须大于 0")
        ref_price = round(px, 4)
        ref_src = "table_current"
        mv = round(shares * ref_price, 2)
        pnl_pct = (
            round((mv - cost_basis) / cost_basis * 100.0, 2) if cost_basis > 0 else None
        )
    else:
        ref_price = item.current_price if item.current_price is not None else item.ref_price
        ref_src = item.current_price_source or item.ref_price_source
        pnl_pct = item.unrealized_pnl_pct
        if ref_price is not None and pnl_pct is None and cost_basis > 0:
            mv = round(shares * float(ref_price), 2)
            pnl_pct = round((mv - cost_basis) / cost_basis * 100.0, 2)

    try:
        sig = compute_signal(sym, data_source=data_source)
    except Exception as e:
        raise ValueError(f"无法计算信号（请先 ③ 拉取 {sym} 日线）：{e}") from e

    teg = sig.trial_exit_guidance
    stop_demo = float(teg.stop_loss_pct_from_entry_demo or 8.0)
    ma20 = teg.reference_exit_ma20
    if ma20 is None and sig.meta:
        ma20 = sig.meta.get("ma20")

    score = 0
    reasons: list[str] = []

    if pnl_pct is not None:
        if pnl_pct <= -stop_demo:
            score += 38
            reasons.append(
                f"相对成本浮亏 {pnl_pct:.2f}%，已达/超过 Demo 止损参考线 −{stop_demo:.0f}%"
            )
        elif pnl_pct <= -stop_demo * 0.55:
            score += 22
            reasons.append(f"相对成本浮亏 {pnl_pct:.2f}%，接近 Demo 止损参考 −{stop_demo:.0f}%")
        elif pnl_pct >= 15:
            score -= 8
            reasons.append(f"相对成本浮盈 {pnl_pct:.2f}%，可设移动止盈、不必急于离场")

    if ref_price is not None and ma20 is not None and float(ref_price) < float(ma20):
        score += 24
        reasons.append(f"当前价 {ref_price} 低于 MA20 {ma20}（结构偏弱）")

    if sig.trend == "bearish":
        score += 22
        reasons.append("趋势判定为空头排列（收盘 < MA20 < MA60）")
    elif sig.trend == "sideways" and sig.strength == "weak":
        score += 10
        reasons.append("震荡偏弱，动能不足")

    if sig.strength == "weak":
        score += 14
        reasons.append("短期强度偏弱（近 5/20 日收益或量能不佳）")

    if sig.position_hint == "avoid":
        score += 28
        reasons.append("④ 仓位提示为「回避」，新开仓不宜，持仓宜收紧风控")
    elif sig.position_hint == "cautious":
        score += 12
        reasons.append("④ 仓位提示为「谨慎」，宜观望或极低仓")

    if sig.buy_suitability_score < 42:
        score += 14
        reasons.append(f"合成适合度仅 {sig.buy_suitability_score} 分，技术面偏弱")

    for tag in (sig.risk_tags or [])[:2]:
        if tag in ("高波动", "距60日高点回撤较大"):
            score += 6
            reasons.append(f"风险标签：{tag}")

    score = int(max(0, min(100, score)))

    if score >= 65:
        action = "strong_close"
        suggest = True
        summary = "离场压力偏高：倾向考虑减仓或平仓，并复核自有止损纪律。"
    elif score >= 45:
        action = "consider_close"
        suggest = True
        summary = "出现多项不利因子：可考虑部分减仓或收紧止损，不必一次清仓。"
    elif score >= 28:
        action = "watch"
        suggest = False
        summary = "暂不建议主动平仓，保持观察；跌破 MA20 或扩大浮亏时再评估。"
    else:
        action = "hold"
        suggest = False
        summary = "未见明显离场信号，可继续持有并设好 Demo 止损线。"

    if teg.applies and teg.note and len(reasons) < 8:
        reasons.append(teg.note[:120] + ("…" if len(teg.note) > 120 else ""))

    return HoldingExitAdviceOut(
        holding_id=row.id,
        symbol=sym,
        name=(row.name or "").strip(),
        suggest_close=suggest,
        action=action,
        score=score,
        summary_zh=summary,
        reasons=reasons[:8],
        cost_price=round(cost, 4),
        current_price=ref_price,
        current_price_source=ref_src,
        ref_price=ref_price,
        ref_price_source=ref_src,
        unrealized_pnl_pct=pnl_pct,
        stop_loss_demo_pct=stop_demo,
        reference_exit_ma20=ma20,
        trend=sig.trend,
        strength=sig.strength,
        buy_suitability_score=sig.buy_suitability_score,
        position_hint=sig.position_hint,
        signal_as_of_date=sig.as_of_date,
    )
