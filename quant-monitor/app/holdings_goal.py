"""⑩ 持仓目标测算：留仓 vs 换仓 + 自选池备选（Demo，非投资建议）。"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import HoldingRow, WatchlistRow
from app.holdings import (
    HOLDING_STATUS_CLOSED,
    HOLDING_STATUS_HOLDING,
    build_holdings_list,
    compute_holding_exit_advice,
)
from app.ingest import fetch_stock_name, watchlist_bar_fields_for_session
from app.schemas import (
    HoldingExitAdviceOut,
    HoldingGoalPlanOut,
    HoldingGoalProgressOut,
    HoldingOut,
    WatchlistPickOut,
)
from app.signals import compute_signal

PositionDecision = Literal["hold", "switch", "watch", "goal_reached"]
DailyVerdict = Literal["留", "走", "换", "达标"]

_GOAL_DISCLAIMER = (
    "以下为规则化 Demo 结论：基于最近收盘 K 线、④适合度与离场压力分，"
    "不能预测次日涨跌，不构成买卖指令；实盘请自行决策并设止损。"
)


def _shanghai_session_phase() -> tuple[str, str]:
    """返回 (phase, 说明)：intraday / after_close / pre_open / non_trading。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    wd = now.weekday()
    t = now.time()
    if wd >= 5:
        return "non_trading", "周末/非交易时段：结论按最近一根入库收盘 K 线，非「今日实时」"
    if time(9, 30) <= t < time(11, 30) or time(13, 0) <= t < time(15, 0):
        return "intraday", "盘中：收盘价未定格，15:00 后再测算更接近「当日结束」结论"
    if t >= time(15, 0):
        return "after_close", "已收盘：结论按当日/最近收盘与入库 K 线（请先 ③ 拉取最新日线）"
    return "pre_open", "开盘前：结论按上一交易日收盘"

def _portfolio_equity(holdings: list[HoldingOut], start_capital: float) -> float:
    """账户估算权益 = 起始资金 + 各持仓已实现/浮动盈亏之和。"""
    pnl = _portfolio_pnl_amt(holdings)
    return round(start_capital + pnl, 2)


def _portfolio_pnl_amt(holdings: list[HoldingOut]) -> float:
    holding_pnl = 0.0
    closed_pnl = 0.0
    for h in holdings:
        if h.status == HOLDING_STATUS_HOLDING and h.unrealized_pnl_amt is not None:
            holding_pnl += float(h.unrealized_pnl_amt)
        elif h.status == HOLDING_STATUS_CLOSED and h.realized_pnl_amt is not None:
            closed_pnl += float(h.realized_pnl_amt)
    return round(holding_pnl + closed_pnl, 2)


def _portfolio_pnl_breakdown(holdings: list[HoldingOut]) -> tuple[float, float, int, int]:
    holding_pnl = 0.0
    closed_pnl = 0.0
    holding_n = 0
    closed_n = 0
    for h in holdings:
        if h.status == HOLDING_STATUS_HOLDING:
            holding_n += 1
            if h.unrealized_pnl_amt is not None:
                holding_pnl += float(h.unrealized_pnl_amt)
        elif h.status == HOLDING_STATUS_CLOSED:
            closed_n += 1
            if h.realized_pnl_amt is not None:
                closed_pnl += float(h.realized_pnl_amt)
    return round(holding_pnl, 2), round(closed_pnl, 2), holding_n, closed_n


def compute_goal_progress(
    session: Session,
    *,
    start_capital: float,
    target_capital: float,
) -> HoldingGoalProgressOut:
    """按本机全部持仓（含已平仓）估算当前权益与距目标差距。"""
    if target_capital <= start_capital:
        raise ValueError("目标资金须大于起始资金")

    all_rows = list(
        session.execute(select(HoldingRow).order_by(HoldingRow.id.desc()).limit(200)).scalars().all()
    )
    holdings_out = build_holdings_list(session, all_rows)
    holding_pnl, closed_pnl, holding_n, closed_n = _portfolio_pnl_breakdown(holdings_out)
    total_pnl = round(holding_pnl + closed_pnl, 2)
    current_equity = round(start_capital + total_pnl, 2)
    gap = round(target_capital - current_equity, 2)
    denom = target_capital - start_capital
    progress_pct: float | None = None
    if denom > 0:
        progress_pct = round((current_equity - start_capital) / denom * 100.0, 2)

    if not all_rows:
        summary = (
            f"尚无持仓记录：起始 {start_capital:.0f} 元，目标 {target_capital:.0f} 元，"
            f"距目标还差 {gap:.0f} 元（请先录入持仓并刷新列表）。"
        )
    elif gap <= 0:
        summary = (
            f"估算权益 {current_equity:.2f} 元，已达到或超过目标 {target_capital:.0f} 元"
            + (f"（完成度 {progress_pct:.1f}%）" if progress_pct is not None else "")
            + "。"
        )
    else:
        need_ret = round(gap / current_equity * 100.0, 2) if current_equity > 0 else None
        summary = (
            f"估算权益 {current_equity:.2f} 元（盈亏合计 {total_pnl:+.2f} 元），"
            f"距目标 {target_capital:.0f} 元还差 {gap:.2f} 元"
            + (f"（完成度 {progress_pct:.1f}%）" if progress_pct is not None else "")
            + (f"；权益需再增约 {need_ret}%" if need_ret is not None else "")
            + "。"
        )

    return HoldingGoalProgressOut(
        start_capital=round(start_capital, 2),
        target_capital=round(target_capital, 2),
        current_equity=current_equity,
        total_pnl_amt=total_pnl,
        gap_to_target=gap,
        progress_pct=progress_pct,
        holding_pnl_amt=holding_pnl,
        closed_pnl_amt=closed_pnl,
        holding_count=holding_n,
        closed_count=closed_n,
        summary_zh=summary,
    )


def _pick_reason(sig: Any) -> str:
    parts: list[str] = []
    if getattr(sig, "trend", None) == "bullish":
        parts.append("趋势偏多")
    elif getattr(sig, "trend", None) == "bearish":
        parts.append("趋势偏空")
    sc = getattr(sig, "buy_suitability_score", None)
    if sc is not None and sc >= 58:
        parts.append(f"适合度 {sc} 分")
    ph = getattr(sig, "position_hint", None)
    if ph == "moderate":
        parts.append("仓位提示：可适度试错")
    elif ph == "trial":
        parts.append("仓位提示：轻仓试错")
    elif ph == "cautious":
        parts.append("仓位提示：谨慎")
    elif ph == "avoid":
        parts.append("仓位提示：回避")
    if getattr(sig, "strength", None) == "strong":
        parts.append("短期强度偏强")
    return "；".join(parts) if parts else "自选池内综合分居前"


def _scan_watchlist_picks(
    session: Session,
    *,
    exclude_symbol: str,
    data_source: str | None,
    limit: int = 5,
    scan_cap: int = 30,
) -> list[WatchlistPickOut]:
    rows = list(session.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all())
    symbols = [r.symbol for r in rows if r.symbol and r.symbol != exclude_symbol]
    if not symbols:
        return []
    bar_by = watchlist_bar_fields_for_session(session, symbols)
    scored: list[tuple[int, WatchlistPickOut]] = []
    for sym in symbols[:scan_cap]:
        try:
            sig = compute_signal(sym, data_source=data_source)
        except Exception:
            continue
        bar = bar_by.get(sym) or {}
        name = ""
        for r in rows:
            if r.symbol == sym and (r.name or "").strip():
                name = str(r.name).strip()
                break
        if not name:
            name = fetch_stock_name(sym) or ""
        pick = WatchlistPickOut(
            symbol=sym,
            name=name,
            buy_suitability_score=int(sig.buy_suitability_score),
            trend=str(sig.trend),
            strength=str(sig.strength),
            position_hint=str(sig.position_hint),
            last_close=bar.get("last_close"),
            spot_last_price=None,
            reason=_pick_reason(sig),
        )
        scored.append((int(sig.buy_suitability_score), pick))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


def _decide_position(
    exit_advice: HoldingExitAdviceOut,
    picks: list[WatchlistPickOut],
    *,
    gap: float,
) -> tuple[PositionDecision, str]:
    cur_score = exit_advice.buy_suitability_score or 0
    best_alt = picks[0].buy_suitability_score if picks else 0
    alt_edge = best_alt - cur_score

    if gap <= 0:
        return "goal_reached", "当前估算权益已达到或超过目标，可考虑锁定利润或上调目标后再测算。"

    if exit_advice.score >= 65:
        if picks and alt_edge >= 8:
            return "switch", (
                f"离场压力 {exit_advice.score} 分偏高，且自选 {picks[0].symbol} "
                f"适合度 {best_alt} 分明显高于当前持仓（+{alt_edge}），倾向清仓换仓。"
            )
        return "switch", f"离场压力 {exit_advice.score} 分偏高，技术面与风控规则倾向先离场再选更强标的。"

    if exit_advice.suggest_close and alt_edge >= 12:
        return "switch", (
            f"当前适合度 {cur_score} 分，自选 {picks[0].symbol} 为 {best_alt} 分，"
            "换仓有望提高达成目标的概率（Demo 规则）。"
        )

    if (
        exit_advice.score < 35
        and picks
        and alt_edge >= 13
        and best_alt >= 58
        and cur_score < best_alt
        and gap > 0
    ):
        return "switch", (
            f"自选 {picks[0].symbol} 适合度 {best_alt} 分，明显高于持仓 {cur_score} 分（+{alt_edge}），"
            "为趋近目标可考虑盘后/次日换入更强标的。"
        )

    if (
        exit_advice.action == "hold"
        and cur_score >= 55
        and exit_advice.trend == "bullish"
        and exit_advice.score < 35
    ):
        return "hold", "未见明显离场信号且趋势偏多，建议先持有并设好止损，达标前可定期再测算。"

    if exit_advice.action in ("watch", "hold") and exit_advice.score < 45:
        return "watch", "暂不建议立即清仓，但距离目标仍有差距；若浮亏扩大或跌破 MA20 再考虑换仓。"

    if picks and alt_edge >= 15 and cur_score < 50:
        return "switch", f"当前适合度偏弱（{cur_score} 分），自选有更优备选，可考虑换仓。"

    return "watch", "信号分化：可小步观察；若连续测算仍偏弱，再从自选池换入更强标的。"


def _resolve_daily_verdict(
    decision: PositionDecision,
    exit_advice: HoldingExitAdviceOut,
    picks: list[WatchlistPickOut],
    *,
    symbol: str,
    alt_edge: int,
) -> tuple[DailyVerdict, str, str | None]:
    """
    收盘口径的一句话结论：留 / 走 / 换 / 达标。

    - **留**：继续持有当前持仓
    - **走**：卖出离场（暂不换入其它标的）
    - **换**：卖出并优先换入选自选候选
    """
    if decision == "goal_reached":
        return "达标", "已达目标区间，可落袋或上调目标后再测。", None

    best = picks[0] if picks else None
    if decision == "switch":
        if best is not None and alt_edge >= 8:
            nm = (best.name or "").strip()
            label = f"{best.symbol}" + (f" {nm}" if nm else "")
            return (
                "换",
                f"建议卖出 {symbol}，下一交易日优先换入 {label}（适合度 {best.buy_suitability_score} 分）。",
                best.symbol,
            )
        return "走", f"建议卖出 {symbol}，先空仓观望，暂不指定换入标的。", None

    if exit_advice.score >= 45 or exit_advice.action in ("strong_close", "consider_close"):
        return "走", f"离场压力 {exit_advice.score}/100 达阈值，建议卖出 {symbol}。", None

    if decision == "hold":
        return "留", f"规则下建议继续持有 {symbol}，设好止损，次日走弱再测。", None

    if best is not None and alt_edge >= 13:
        return (
            "留",
            f"暂留 {symbol}；但自选 {best.symbol} 更强（+{alt_edge} 分），若连测 2 日仍落后可考虑换。",
            None,
        )

    return "留", f"暂无明确卖出信号，可先持有 {symbol}；收盘后或次日开盘前再测一次。", None


def compute_holding_goal_plan(
    row: HoldingRow,
    *,
    session: Session,
    start_capital: float,
    target_capital: float,
    data_source: str | None = None,
    current_price: float | None = None,
) -> HoldingGoalPlanOut:
    if row.status != HOLDING_STATUS_HOLDING:
        raise ValueError("仅持仓中记录可测算目标路径")
    if target_capital <= start_capital:
        raise ValueError("目标资金须大于起始资金")

    all_rows = list(
        session.execute(select(HoldingRow).order_by(HoldingRow.id.desc()).limit(200)).scalars().all()
    )
    holdings_out = build_holdings_list(session, all_rows)
    current_equity = _portfolio_equity(holdings_out, start_capital)
    gap = round(target_capital - current_equity, 2)
    denom = target_capital - start_capital
    progress_pct: float | None = None
    if denom > 0:
        progress_pct = round((current_equity - start_capital) / denom * 100.0, 2)
        progress_pct = max(0.0, min(100.0, progress_pct))

    exit_advice = compute_holding_exit_advice(
        row, session=session, data_source=data_source, current_price=current_price
    )
    picks = _scan_watchlist_picks(
        session, exclude_symbol=row.symbol, data_source=data_source, limit=5
    )
    decision, decision_summary = _decide_position(exit_advice, picks, gap=gap)
    cur_sc = exit_advice.buy_suitability_score or 0
    best_alt = picks[0].buy_suitability_score if picks else 0
    alt_edge = best_alt - cur_sc
    session_phase, session_phase_note = _shanghai_session_phase()
    daily_verdict, daily_verdict_detail, switch_to_symbol = _resolve_daily_verdict(
        decision,
        exit_advice,
        picks,
        symbol=row.symbol,
        alt_edge=alt_edge,
    )

    item = next((h for h in holdings_out if h.id == row.id), None)
    mv = float(item.market_value) if item and item.market_value is not None else None

    steps: list[str] = [
        f"【收盘结论】{daily_verdict} — {daily_verdict_detail}",
        f"（{session_phase_note}）",
        f"① 目标进度：起始 {start_capital:.0f} 元 → 目标 {target_capital:.0f} 元；"
        f"当前估算权益约 {current_equity:.2f} 元"
        + (f"（完成度约 {progress_pct:.1f}%）" if progress_pct is not None else "")
        + (f"，距目标还差 {gap:.2f} 元。" if gap > 0 else "。"),
        f"② 当前持仓 {row.symbol}"
        + (f" {row.name}" if (row.name or "").strip() else "")
        + f"：适合度 {cur_sc} 分，离场压力 {exit_advice.score}/100。"
        + f" 结论：{decision_summary}",
    ]

    if decision == "goal_reached":
        steps.append("③ 已达目标区间：可分批落袋或提高目标后重新测算；不必为达标而强行换仓。")
    elif decision == "hold":
        steps.append(
            "③ 建议路径：继续持有，设置 Demo 止损并关注 MA20；"
            "每 1～3 个交易日或行情突变时再点「测算」。"
        )
        if gap > 0 and current_equity > 0:
            need_ret = round(gap / current_equity * 100.0, 2)
            steps.append(
                f"④ 达标参考：在不再追加本金前提下，权益需再增长约 {need_ret}% "
                f"（约 {gap:.0f} 元）；此为算术示意，非收益承诺。"
            )
        if picks:
            top = picks[0]
            steps.append(
                f"⑤ 备选（若转弱再换）：自选 {top.symbol} {top.name or ''} "
                f"适合度 {top.buy_suitability_score} 分 — {top.reason}。"
            )
    elif decision == "switch":
        steps.append(
            f"③ 建议路径：先按当前价清仓 {row.symbol}"
            + (f"（约回收 {mv:.0f} 元）" if mv is not None else "")
            + "，再在自选池中选择更适合标的买入。"
        )
        if picks:
            for i, p in enumerate(picks[:3], start=1):
                px = p.last_close
                lot_hint = ""
                if mv and px and float(px) > 0:
                    sh = int(mv // float(px) // 100) * 100
                    if sh >= 100:
                        lot_hint = f"；约可买 {sh} 股（整手示意）"
                steps.append(
                    f"④ 自选候选 {i}：{p.symbol} {p.name or ''} · 适合度 {p.buy_suitability_score} 分 · "
                    f"{p.reason}{lot_hint}"
                )
        else:
            steps.append("④ 自选池暂无其它标的：请先在 ② 添加候选股并 ③ 拉取 K 线后再测算。")
        if gap > 0:
            steps.append(
                f"⑤ 换仓后仍须再赚约 {gap:.0f} 元才能达标；可「买入 → 测算 → 不适则再换」循环，"
                "直至达到目标或你调整目标金额。"
            )
    else:
        steps.append("③ 建议路径：以观察为主；若离场压力升至 ≥45 分或自选出现明显更强标的，再考虑换仓。")
        if picks:
            steps.append(
                f"④ 自选关注：{picks[0].symbol}（适合度 {picks[0].buy_suitability_score} 分）— {picks[0].reason}。"
            )

    return HoldingGoalPlanOut(
        holding_id=row.id,
        symbol=row.symbol,
        name=(row.name or "").strip(),
        start_capital=round(start_capital, 2),
        target_capital=round(target_capital, 2),
        current_equity=current_equity,
        gap_to_target=gap,
        progress_pct=progress_pct,
        position_decision=decision,
        decision_summary_zh=decision_summary,
        daily_verdict=daily_verdict,
        daily_verdict_detail=daily_verdict_detail,
        switch_to_symbol=switch_to_symbol,
        session_phase=session_phase,
        session_phase_note=session_phase_note,
        price_basis=exit_advice.current_price_source or exit_advice.ref_price_source,
        signal_as_of_date=exit_advice.signal_as_of_date,
        steps=steps,
        exit_advice=exit_advice,
        watchlist_picks=picks,
        disclaimer_note=_GOAL_DISCLAIMER,
    )
