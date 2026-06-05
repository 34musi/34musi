"""
⑩ 持仓记录：本机 SQLite 读写、行情盈亏估算与进/离场 Demo 建议（非券商回报）。

## 功能作用

本模块管理 `holdings` 表中的自用持仓记录，并为控制台 **⑩ 持仓记录** 提供：

1. **列表与盈亏**：批量拉盘口/日线价，估算市值、浮动/已实现盈亏（非交易所成交回报）。
2. **平仓建议**（`compute_holding_exit_advice`）：成本浮盈亏 + ④ 信号 → 离场压力 0–100 分。
3. **建仓建议**（`compute_holding_entry_advice`）：④ 信号 → 建仓适合度 0–100 分（持仓/已平仓均可评估）。
4. **复盘汇总**（`compute_holdings_review_summary`）：已平仓记录的胜率、合计盈亏、平均持仓天数。

`holdings_goal.py` 依赖本模块的 `build_holdings_list`、`compute_holding_exit_advice` 做目标测算。

## 持仓状态

| 常量 | 含义 |
|------|------|
| `HOLDING_STATUS_HOLDING` | 持仓中：用参考价算浮动盈亏 |
| `HOLDING_STATUS_CLOSED` | 已平仓：用卖出价算已实现盈亏 |

## 参考价优先级

持仓中市值估算：联网 `live_last_price` → `spot_last_price` → 本地 `last_close`。
已平仓展示价优先 `sell_price`。

## 对外接口（常用）

| 函数 | 用途 |
|------|------|
| `build_holdings_list` | `GET /holdings` 列表 enrichment |
| `holding_row_to_out` | 单条 ORM → `HoldingOut` |
| `apply_holding_defaults` | 新建时补名称、时间戳 |
| `validate_holding_sell_price` | 卖出价误填股数等 sanity check |
| `create_closed_holding_record` | `POST /holdings/closed-record` 补录复盘 |
| `compute_holding_exit_advice` | `GET …/exit-advice`、goal-plan 输入 |
| `compute_holding_entry_advice` | `GET …/entry-advice` |
| `compute_holdings_review_summary` | `GET /holdings/review-summary` |
| `holding_days_for_row` | 持仓天数（含首尾日历日） |

## 非投资建议

盈亏为规则估算；进/离场打分为 Demo，不构成买卖指令。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import HoldingRow
from app.config import get_settings
from app.ingest import (
    fetch_stock_name,
    live_quote_fields_for_codes_enhanced,
    watchlist_bar_fields_for_session,
)
from app.schemas import HoldingEntryAdviceOut, HoldingExitAdviceOut, HoldingOut
from app.signals import compute_signal

logger = logging.getLogger(__name__)

# --- 状态常量 ---

HOLDING_STATUS_HOLDING = "holding"
HOLDING_STATUS_CLOSED = "closed"


# --- 持仓天数与校验 ---


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


def validate_holding_sell_price(
    sell_price: float,
    *,
    shares: float,
    cost_price: float | None = None,
) -> float:
    """
    卖出均价合理性校验：防止把「股数」误填为「元/股」（如 300 股填成 300 元）。
    """
    px = float(sell_price)
    if not math.isfinite(px) or px <= 0:
        raise ValueError("卖出均价须大于 0")
    sh = float(shares)
    if sh >= 100 and abs(px - sh) < 1e-6:
        raise ValueError(
            f"卖出均价 {px:g} 与股数 {int(sh)} 相同，疑似误填股数；请填写每股卖出价格（元/股）"
        )
    if px > 2000:
        raise ValueError(f"卖出均价 {px:g} 元/股 异常偏高，请确认单位为元/股而非股数或金额")
    if cost_price is not None and float(cost_price) > 0:
        cp = float(cost_price)
        if px > cp * 50:
            raise ValueError(
                f"卖出均价 {px:g} 相对成本 {cp:g} 元/股 过高，请确认未误填股数或总金额"
            )
    return round(px, 4)


# --- 参考价与盈亏计算 ---


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


# --- ORM → API 与列表构建 ---


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
        ref_price_source=ref_src,
        holding_days=holding_days_for_row(row),
        **pnl,
    )


def build_holdings_list(
    session: Session,
    rows: list[HoldingRow],
    *,
    force_spot_refresh: bool = False,
    persist_snapshots: bool = False,
) -> list[HoldingOut]:
    """
    批量将 HoldingRow 转为 HoldingOut，并 enrichment 行情与盈亏字段。

    一次拉取所有 symbol 的 bar/spot；`persist_snapshots=True` 时回写 mark_price 等到 ORM。
    """
    symbols = list({r.symbol for r in rows})
    bar_by = watchlist_bar_fields_for_session(session, symbols) if symbols else {}
    spot_by = (
        live_quote_fields_for_codes_enhanced(
            symbols,
            data_source=get_settings().ingest_data_source,
            force_spot_refresh=force_spot_refresh,
        )
        if symbols
        else {}
    )
    out: list[HoldingOut] = []
    now = _utc_now_iso()
    for r in rows:
        item = holding_row_to_out(r, bar_by=bar_by, spot_by=spot_by)
        if persist_snapshots:
            if not (r.name or "").strip():
                nm = fetch_stock_name(r.symbol)
                if nm:
                    r.name = nm
            if item.current_price is not None:
                r.mark_price = float(item.current_price)
                r.mark_price_at = now
                r.mark_price_source = item.current_price_source
            r.updated_at = now
        out.append(item)
    return out


# --- 补录与复盘汇总 ---


def create_closed_holding_record(
    session: Session,
    *,
    sym: str,
    shares: float,
    cost_price: float,
    buy_date: str,
    sell_price: float,
    sell_date: str,
    notes: str | None = None,
    name: str | None = None,
) -> HoldingRow:
    """补录一条已平仓记录，供复盘；数据仅存本机。"""
    sell_px = validate_holding_sell_price(
        float(sell_price), shares=float(shares), cost_price=float(cost_price)
    )
    row = HoldingRow(
        symbol=sym,
        name=(name or "").strip(),
        status=HOLDING_STATUS_CLOSED,
        shares=float(shares),
        cost_price=float(cost_price),
        buy_date=buy_date,
        sell_price=sell_px,
        sell_date=sell_date,
        notes=notes.strip() if notes else None,
        created_at="",
        updated_at="",
    )
    apply_holding_defaults(row, sym=sym)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def compute_holdings_review_summary(session: Session) -> dict[str, Any]:
    """汇总全部已平仓记录的已实现盈亏与持仓天数（不联网拉行情）。"""
    rows = list(
        session.execute(
            select(HoldingRow)
            .where(HoldingRow.status == HOLDING_STATUS_CLOSED)
            .order_by(HoldingRow.sell_date.desc(), HoldingRow.id.desc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return {
            "closed_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "flat_count": 0,
            "total_realized_pnl_amt": None,
            "avg_realized_pnl_pct": None,
            "avg_holding_days": None,
        }
    total_amt = 0.0
    pcts: list[float] = []
    days: list[int] = []
    wins = losses = flats = 0
    for row in rows:
        pnl = _pnl_fields(row, ref_price=None)
        amt = pnl.get("realized_pnl_amt")
        pct = pnl.get("realized_pnl_pct")
        if amt is not None:
            total_amt += float(amt)
        if pct is not None:
            pcts.append(float(pct))
            if pct > 0.01:
                wins += 1
            elif pct < -0.01:
                losses += 1
            else:
                flats += 1
        hd = holding_days_for_row(row)
        if hd is not None:
            days.append(hd)
    return {
        "closed_count": len(rows),
        "win_count": wins,
        "loss_count": losses,
        "flat_count": flats,
        "total_realized_pnl_amt": round(total_amt, 2),
        "avg_realized_pnl_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "avg_holding_days": round(sum(days) / len(days), 1) if days else None,
    }


# --- 新建/更新默认值 ---


def apply_holding_defaults(row: HoldingRow, *, sym: str) -> None:
    """补全简称（东财）、created_at/updated_at 时间戳。"""
    if not (row.name or "").strip():
        row.name = fetch_stock_name(sym) or ""
    now = _utc_now_iso()
    if not row.created_at:
        row.created_at = now
    row.updated_at = now


# --- 平仓建议（离场压力分） ---


def compute_holding_exit_advice(
    row: HoldingRow,
    *,
    session: Session,
    data_source: str | None = None,
    current_price: float | None = None,
    live_mode: bool = False,
) -> HoldingExitAdviceOut:
    """
    是否建议平仓：成本浮盈亏 + ④ 信号（趋势/MA20/仓位提示/Demo 止损线）加权打分。

    live_mode=True 时强制拉联网现价，并用 spot 适合度参与打分（当日测算）。
    """
    if row.status != HOLDING_STATUS_HOLDING:
        raise ValueError("仅持仓中记录可计算平仓建议")

    sym = row.symbol
    cost = float(row.cost_price)
    shares = float(row.shares)
    cost_basis = round(shares * cost, 2)
    item = build_holdings_list(
        session, [row], force_spot_refresh=live_mode
    )[0]

    ref_price: float | None
    ref_src: str | None
    pnl_pct: float | None

    if live_mode:
        from app.ingest import live_quote_fields_for_codes_enhanced

        live = (
            live_quote_fields_for_codes_enhanced(
                [sym], data_source=data_source, force_spot_refresh=True
            ).get(sym)
            or {}
        )
        px = live.get("live_last_price")
        src = str(live.get("live_price_source") or "live_quote")
        if px is None or not (isinstance(px, (int, float)) and float(px) > 0):
            raise ValueError(
                f"无法获取 {sym} 的联网现价，请点「刷新列表」或检查 ③ 数据源与网络"
            )
        if src in ("local_daily_close", "daily_close"):
            raise ValueError(
                f"现价仅为本地日线收盘（{src}），非当日联网报价；请点「刷新列表」后再试"
            )
        ref_price = round(float(px), 4)
        ref_src = src
        mv = round(shares * ref_price, 2)
        pnl_pct = (
            round((mv - cost_basis) / cost_basis * 100.0, 2) if cost_basis > 0 else None
        )
    elif current_price is not None:
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
        sig = compute_signal(sym, data_source=data_source, use_today_bar=live_mode)
    except Exception as e:
        raise ValueError(f"无法计算信号（请先 ③ 拉取 {sym} 日线）：{e}") from e

    teg = sig.trial_exit_guidance
    stop_demo = float(teg.stop_loss_pct_from_entry_demo or 8.0)
    ma20 = teg.reference_exit_ma20
    if ma20 is None and sig.meta:
        ma20 = sig.meta.get("ma20")

    suit_score = int(sig.buy_suitability_score)
    pos_hint = sig.position_hint

    score = 0
    reasons: list[str] = []
    if live_mode:
        reasons.append(
            "当日测算：已用联网现价补写今日日线；浮盈亏、趋势、适合度均基于含当日的 K 线"
        )

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

    if pos_hint == "avoid":
        score += 28
        reasons.append("④ 仓位提示为「回避」，新开仓不宜，持仓宜收紧风控")
    elif pos_hint == "cautious":
        score += 12
        reasons.append("④ 仓位提示为「谨慎」，宜观望或极低仓")

    if suit_score < 42:
        score += 14
        reasons.append(f"{'现价' if live_mode else '合成'}适合度仅 {suit_score} 分，技术面偏弱")

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
        buy_suitability_score=suit_score,
        position_hint=pos_hint,
        signal_as_of_date=sig.as_of_date,
    )


# --- 建仓建议（建仓适合度分） ---


def compute_holding_entry_advice(
    row: HoldingRow,
    *,
    session: Session,
    data_source: str | None = None,
    current_price: float | None = None,
) -> HoldingEntryAdviceOut:
    """
    可否建仓：最新市价 + ④ 信号（趋势/MA20/仓位提示/合成适合度）加权打分。
    持仓中与已平仓均可：评估「此刻按规则是否适合新开仓/加仓」。
    """
    sym = row.symbol
    item = build_holdings_list(session, [row])[0]

    ref_price: float | None
    ref_src: str | None

    if current_price is not None:
        try:
            px = float(current_price)
        except (TypeError, ValueError) as e:
            raise ValueError("current_price 无效") from e
        if not (px > 0):
            raise ValueError("current_price 须大于 0")
        ref_price = round(px, 4)
        ref_src = "table_current"
    else:
        ref_price = item.ref_price
        ref_src = item.ref_price_source
        if ref_price is None and item.spot_last_price is not None:
            try:
                sp = float(item.spot_last_price)
            except (TypeError, ValueError):
                sp = 0.0
            if sp > 0:
                ref_price = round(sp, 4)
                ref_src = "spot"

    try:
        sig = compute_signal(sym, data_source=data_source)
    except Exception as e:
        raise ValueError(f"无法计算信号（请先 ③ 拉取 {sym} 日线）：{e}") from e

    teg = sig.trial_exit_guidance
    ma20 = teg.reference_exit_ma20
    if ma20 is None and sig.meta:
        ma20 = sig.meta.get("ma20")

    score = 50
    reasons: list[str] = []
    already = row.status == HOLDING_STATUS_HOLDING

    bs = int(sig.buy_suitability_score)
    if bs >= 65:
        score += 22
        reasons.append(f"合成适合度 {bs} 分，技术面偏多")
    elif bs >= 52:
        score += 12
        reasons.append(f"合成适合度 {bs} 分，尚可")
    elif bs < 42:
        score -= 22
        reasons.append(f"合成适合度仅 {bs} 分，不宜激进建仓")

    if sig.trend == "bullish":
        score += 20
        reasons.append("趋势多头（收盘 > MA20 > MA60）")
    elif sig.trend == "bearish":
        score -= 28
        reasons.append("趋势空头，逆势建仓风险高")
    elif sig.trend == "sideways" and sig.strength == "strong":
        score += 8
        reasons.append("震荡偏强，可轻仓试错")
    elif sig.trend == "sideways" and sig.strength == "weak":
        score -= 10
        reasons.append("震荡偏弱，动能不足")

    if sig.strength == "strong":
        score += 14
    elif sig.strength == "weak":
        score -= 14
        reasons.append("短期强度偏弱")

    if sig.position_hint == "moderate":
        score += 18
        reasons.append("④ 仓位提示「适中」，规则上允许正常仓")
    elif sig.position_hint == "trial":
        score += 12
        reasons.append("④ 仓位提示「试仓」，宜轻仓")
    elif sig.position_hint == "cautious":
        score -= 10
        reasons.append("④ 仓位提示「谨慎」，宜观望或极低仓")
    elif sig.position_hint == "avoid":
        score -= 32
        reasons.append("④ 仓位提示「回避」，不建议新建仓")

    if ref_price is not None and ma20 is not None:
        if float(ref_price) > float(ma20):
            score += 16
            reasons.append(f"现价 {ref_price} 站上 MA20 {ma20}")
        else:
            score -= 18
            reasons.append(f"现价 {ref_price} 低于 MA20 {ma20}（结构偏弱）")

    for tag in (sig.risk_tags or [])[:2]:
        if tag in ("高波动", "距60日高点回撤较大"):
            score -= 8
            reasons.append(f"风险标签：{tag}")

    if already:
        score -= 6
        reasons.append("当前已有持仓记录，请结合总仓位与加仓纪律")

    score = int(max(0, min(100, score)))

    if score >= 65:
        action = "strong_open"
        suggest = True
        summary = "建仓适合度偏高：规则上倾向可建仓或按计划加仓，仍须自定仓位与止损。"
    elif score >= 45:
        action = "consider_open"
        suggest = True
        summary = "多项因子偏多：可考虑轻仓试仓，不宜重仓一次到位。"
    elif score >= 28:
        action = "watch"
        suggest = False
        summary = "暂不建议主动新建仓，保持观察；站上 MA20 或适合度回升后再评估。"
    else:
        action = "avoid"
        suggest = False
        summary = "不宜建仓：趋势/仓位提示/适合度偏弱，宜回避或等待结构修复。"

    return HoldingEntryAdviceOut(
        holding_id=row.id,
        symbol=sym,
        name=(row.name or "").strip(),
        record_status=row.status,
        already_holding=already,
        suggest_open=suggest,
        action=action,
        score=score,
        summary_zh=summary,
        reasons=reasons[:8],
        current_price=ref_price,
        current_price_source=ref_src,
        reference_entry_ma20=ma20,
        trend=sig.trend,
        strength=sig.strength,
        buy_suitability_score=bs,
        position_hint=sig.position_hint,
        signal_as_of_date=sig.as_of_date,
    )


# --- Webhook 通知（⑩ 定时刷新） ---


_WECOM_WEBHOOK_HOST = "qyapi.weixin.qq.com"
_WECOM_WEBHOOK_PATH_PREFIX = "/cgi-bin/webhook/send"


def _is_wecom_webhook_url(url: str) -> bool:
    p = urlparse(url)
    return (
        (p.hostname or "").lower() == _WECOM_WEBHOOK_HOST
        and (p.path or "").startswith(_WECOM_WEBHOOK_PATH_PREFIX)
    )


def _format_holdings_notify_wecom_text(
    *,
    items: list[HoldingOut],
    picked_ids: list[int],
    refreshed_at: str,
) -> str:
    """企业微信机器人 text 消息（单条上限约 3500 字）。"""
    lines = [
        "【持仓现价刷新】",
        f"时间：{refreshed_at}",
        f"勾选 {len(picked_ids)} 条，本次推送 {len(items)} 条",
        "",
    ]
    for it in items:
        px = it.current_price
        px_s = f"{px:.4f}" if px is not None else "—"
        chg = it.spot_change_pct
        chg_s = f"{chg:+.2f}%" if chg is not None else "—"
        if it.status == HOLDING_STATUS_CLOSED:
            pnl = it.realized_pnl_pct
            pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
            lines.append(f"{it.symbol} {it.name or ''} 已平仓 卖价{px_s} 盈亏{pnl_s}")
        else:
            pnl = it.unrealized_pnl_pct
            pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
            lines.append(
                f"{it.symbol} {it.name or ''} 现价{px_s} {chg_s} 浮盈{pnl_s}"
            )
    return "\n".join(lines)[:3500]


def normalize_holdings_notify_url(raw: str) -> str:
    """校验通知地址：仅允许 http(s) 外链，降低 SSRF 风险。"""
    url = (raw or "").strip()
    if not url:
        raise ValueError("通知地址不能为空")
    if len(url) > 2000:
        raise ValueError("通知地址过长")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("通知地址须为 http:// 或 https:// 开头的完整 URL")
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError("通知地址不能为本机回环地址")
    return url


def post_holdings_refresh_webhook(
    url: str,
    *,
    items: list[HoldingOut],
    picked_ids: list[int],
    refreshed_at: str | None = None,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """将定时刷新结果 POST 到通知地址；企业微信机器人自动转 text 格式。"""
    at = (refreshed_at or "").strip() or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if _is_wecom_webhook_url(url):
        body = {
            "msgtype": "text",
            "text": {
                "content": _format_holdings_notify_wecom_text(
                    items=items,
                    picked_ids=picked_ids,
                    refreshed_at=at,
                )
            },
        }
    else:
        body = {
            "event": "holdings_spot_refresh",
            "refreshed_at": at,
            "picked_ids": picked_ids,
            "count": len(items),
            "symbols": [it.symbol for it in items],
            "items": [it.model_dump() for it in items],
        }
    try:
        r = requests.post(
            url,
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
        detail = (r.text or "").strip()
        if r.ok:
            if _is_wecom_webhook_url(url):
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                if data.get("errcode", 0) != 0:
                    msg = str(data.get("errmsg") or detail or "企业微信返回失败")
                    logger.warning(
                        "holdings wecom notify rejected: errcode=%s %s",
                        data.get("errcode"),
                        msg[:120],
                    )
                    return False, msg[:240]
            logger.info(
                "holdings notify sent ok (%s items) -> %s",
                len(items),
                urlparse(url).hostname or url[:40],
            )
            return True, ""
        if len(detail) > 240:
            detail = detail[:240] + "…"
        logger.warning(
            "holdings notify HTTP %s: %s",
            r.status_code,
            detail[:120],
        )
        return False, f"通知地址返回 HTTP {r.status_code}" + (f"：{detail}" if detail else "")
    except Exception as e:
        logger.warning("holdings notify webhook failed: %s", e, exc_info=True)
        return False, str(e) or "通知发送失败"
