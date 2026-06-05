"""
⑩ 持仓目标测算：距目标进度 + 留仓/换仓 Demo 路径（非投资建议）。

## 功能作用

本模块为控制台 **⑩ 持仓记录** 提供「起始资金 → 目标资金」相关的规则化测算：

1. **目标进度**（`compute_goal_progress`）：汇总全部持仓（含已平仓）浮动/已实现盈亏，
   估算当前权益、距目标差距与完成度 %。
2. **目标路径**（`compute_holding_goal_plan`）：对单条**持仓中**记录，结合离场压力分、
   ④ 适合度，扫描 **② 自选池**备选，给出留/走/换/达标结论与分步说明。
3. **当日测算**（`live_mode` + `check_goal_plan_live_readiness`）：联网现价补写今日 K 线，
   信号与浮盈亏含当日（盘中为参考价，非 tick）。

## 决策逻辑概要

| 输出 | 含义 |
|------|------|
| `position_decision` | `hold` / `watch` / `switch` / `goal_reached` |
| `daily_verdict` | 收盘口径一句话：**留** / **走** / **换** / **达标** |

核心输入来自 `holdings.compute_holding_exit_advice`（离场压力 0–100）与
自选池 `_scan_watchlist_picks`（按适合度排序 Top N）。`_decide_position` 比较
当前适合度与最佳备选差距（`alt_edge`）及 `gap_to_target` 是否已达标。

## 对外接口

| 函数 | 用途 |
|------|------|
| `compute_goal_progress` | `GET /holdings/goal-progress` |
| `compute_holding_goal_plan` | `POST /holdings/{id}/goal-plan` 与 `goal-plan-live` |
| `check_goal_plan_live_readiness` | `GET …/goal-plan-live/preflight` 预检数据是否齐全 |

## 依赖与模式

- **daily 模式**：按最近入库收盘 K 线与列表现价（可选 `current_price`）。
- **live 模式**：`ensure_today_bar_for_live_signal` + `compute_signal(use_today_bar=True)`。
- 附 `_estimate_near_term_price_outlook`：基于 MA20/60 日高/趋势给出近几日参考价区间（规则推算，非预测）。

**Demo 免责声明**：不能预测涨跌，不构成买卖指令；实盘请自行决策并设止损。
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_models import HoldingRow, WatchlistRow
from app.holdings import (
    HOLDING_STATUS_CLOSED,
    HOLDING_STATUS_HOLDING,
    build_holdings_list,
    compute_holding_exit_advice,
)
from app.ingest import (
    backfill_watchlist_today_close_batch,
    ensure_today_bar_for_live_signal,
    fetch_stock_name,
    live_quote_fields_for_codes_enhanced,
    load_bars_df,
    shanghai_today_date,
    watchlist_bar_fields_for_session,
)
from app.schemas import (
    GoalPlanLiveMissingItem,
    HoldingExitAdviceOut,
    HoldingGoalPlanLivePreflightOut,
    HoldingGoalPlanOut,
    HoldingGoalProgressOut,
    HoldingOut,
    NearTermPriceOutlook,
    WatchlistPickOut,
)
from app.signals import compute_signal

PositionDecision = Literal["hold", "switch", "watch", "goal_reached"]
DailyVerdict = Literal["留", "走", "换", "达标"]

# --- 免责声明文案 ---

_GOAL_DISCLAIMER = (
    "以下为规则化 Demo 结论：基于最近收盘 K 线、④适合度与离场压力分，"
    "不能预测次日涨跌，不构成买卖指令；实盘请自行决策并设止损。"
)

_GOAL_DISCLAIMER_LIVE = (
    "以下为规则化 Demo 结论：测算前会用联网现价补写/刷新「今日」日线（盘中为参考价，非 tick）；"
    "趋势、MA20、适合度均基于含当日的入库 K 线。不能预测涨跌，不构成买卖指令。"
)

_DAILY_CLOSE_PRICE_SOURCES = frozenset({"local_daily_close", "daily_close", "daily_bar"})


# --- 交易时段与账户汇总 ---


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
    return round(holding_pnl + closed_pnl, 2), holding_n, closed_n


# --- 目标进度（全账户） ---


def compute_goal_progress(
    session: Session,
    *,
    start_capital: float,
    target_capital: float,
) -> HoldingGoalProgressOut:
    """
    按本机全部持仓（含已平仓）估算当前权益与距目标差距。

    当前权益 ≈ 起始资金 + 浮动盈亏 + 已实现盈亏；返回完成度 % 与中文 summary。
    """
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


# --- 信号文案与近端价位参考 ---


def _pick_reason(sig: Any) -> str:
    """从 SignalOut 提取自选候选的简短理由文案。"""
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


def _estimate_near_term_price_outlook(
    row: HoldingRow,
    exit_advice: HoldingExitAdviceOut,
    *,
    data_source: str | None = None,
) -> NearTermPriceOutlook | None:
    """
    按现价、60 日高点、MA20、趋势/动能 Demo 规则给出近几个交易日的参考价区间。
    非统计预测，不构成收益承诺。
    """
    ref = exit_advice.current_price or exit_advice.ref_price
    if ref is None:
        return None
    try:
        ref_f = float(ref)
    except (TypeError, ValueError):
        return None
    if not (ref_f > 0):
        return None

    high_60: float | None = None
    ret20: float | None = None
    try:
        df = load_bars_df(row.symbol, data_source=data_source)
        if df is not None and not df.empty:
            h = df["high"].astype(float)
            c = df["close"].astype(float)
            high_60 = float(h.iloc[-60:].max()) if len(h) >= 1 else None
            if len(c) >= 21:
                r20 = float(c.iloc[-1] / c.iloc[-21] - 1.0)
                if r20 == r20:  # not nan
                    ret20 = r20
    except Exception:
        pass

    ma20 = exit_advice.reference_exit_ma20
    cost = float(row.cost_price)
    stop_pct = float(exit_advice.stop_loss_demo_pct or 8.0)
    stop_px = round(cost * (1.0 - stop_pct / 100.0), 4) if cost > 0 else None

    trend = exit_advice.trend or "sideways"
    strength = exit_advice.strength or "neutral"
    base_up = 0.03
    if trend == "bullish":
        base_up = 0.065 if strength == "strong" else 0.045
    elif trend == "bearish":
        base_up = 0.015
    if ret20 is not None and ret20 > 0:
        base_up = min(0.12, max(base_up, ret20 * 0.35))

    near_high = high_60 is not None and ref_f >= float(high_60) * 0.97
    if near_high and high_60 is not None:
        target_mid = round(float(high_60), 4)
        target_high = round(float(high_60) * 1.02, 4)
        high_label = "贴近 60 日高点，延伸参考 +2%"
    else:
        target_mid = round(ref_f * (1.0 + base_up), 4)
        cap_high = float(high_60) if high_60 is not None and float(high_60) > ref_f else ref_f * (
            1.0 + base_up * 1.45
        )
        target_high = round(min(cap_high, ref_f * (1.0 + base_up * 1.45)), 4)
        high_label = (
            "60 日高点压力"
            if high_60 is not None and float(high_60) <= target_high * 1.001
            else "动能延伸上限（Demo）"
        )

    supports: list[float] = []
    if ma20 is not None and float(ma20) > 0:
        supports.append(float(ma20))
    if stop_px is not None and stop_px > 0:
        supports.append(stop_px)
    target_low = round(min(supports), 4) if supports else round(ref_f * (1.0 - stop_pct / 100.0), 4)
    low_label = "MA20 与 Demo 止损取较近支撑" if len(supports) >= 2 else (
        "MA20 支撑" if ma20 else "Demo 止损参考"
    )

    upside = round((target_mid / ref_f - 1.0) * 100.0, 2) if target_mid else None

    if trend == "bearish":
        summary = (
            f"结构偏弱：上方反弹参考约 {target_mid} 元（+{upside}%），"
            f"若不能站稳 MA20 {ma20 or '—'}，下方宜关注 {target_low} 元一带。"
        )
    elif near_high:
        summary = (
            f"已贴近 60 日高点 {high_60} 元：中性参考维持前高附近 {target_mid} 元，"
            f"上沿延伸约 {target_high} 元；跌破 {target_low} 元则结构转弱。"
        )
    else:
        summary = (
            f"偏多动能下，近几日中性参考约 {target_mid} 元（较现价约 +{upside}%），"
            f"上方看 {target_high} 元、下方 {target_low} 元。"
        )

    return NearTermPriceOutlook(
        reference_price=round(ref_f, 4),
        target_low=target_low,
        target_mid=target_mid,
        target_high=target_high,
        upside_pct_mid=upside,
        resistance_60d_high=round(float(high_60), 4) if high_60 is not None else None,
        support_ma20=round(float(ma20), 4) if ma20 is not None else None,
        support_stop_demo=stop_px,
        high_label=high_label,
        low_label=low_label,
        summary_zh=summary + " 规则推算，非涨跌承诺。",
    )


def _signal_scores_for_mode(sig: Any, *, live_mode: bool = False) -> tuple[int, str, str, str]:
    """live_mode 时信号已由 compute_signal(use_today_bar=True) 含当日 K 线。"""
    _ = live_mode
    return (
        int(sig.buy_suitability_score),
        str(sig.trend),
        str(sig.strength),
        str(sig.position_hint),
    )


# --- 当日测算预检 ---


def check_goal_plan_live_readiness(
    row: HoldingRow,
    *,
    session: Session,
    data_source: str | None = None,
) -> HoldingGoalPlanLivePreflightOut:
    """当日测算前检查：本地 K 线、联网现价、自选池等。"""
    if row.status != HOLDING_STATUS_HOLDING:
        raise ValueError("仅持仓中记录可测算目标路径")

    sym = row.symbol
    sh_today = shanghai_today_date().isoformat()
    missing: list[GoalPlanLiveMissingItem] = []
    warnings: list[GoalPlanLiveMissingItem] = []

    bar_count = 0
    bars_last_td: str | None = None
    try:
        df = load_bars_df(sym, data_source=data_source)
        if not df.empty:
            bar_count = len(df)
            bars_last_td = str(df["trade_date"].iloc[-1])[:10]
    except Exception:
        bar_count = 0

    if bar_count < 30:
        missing.append(
            GoalPlanLiveMissingItem(
                code="bars_insufficient",
                message=f"{sym} 本地日线仅 {bar_count} 根（需至少 30 根）",
                action="请在 ③「更新行情」拉取该标的日线",
            )
        )

    phase, phase_note = _shanghai_session_phase()
    today_bar_note: str | None = None
    if bar_count >= 30 and shanghai_today_date().weekday() < 5:
        try:
            bf = ensure_today_bar_for_live_signal(sym, data_source=data_source)
            df2 = load_bars_df(sym, data_source=data_source)
            if not df2.empty:
                bar_count = len(df2)
                bars_last_td = str(df2["trade_date"].iloc[-1])[:10]
            if bf.get("provisional"):
                today_bar_note = "今日 K 线为盘中参考价（OHLC=现价快照）"
            elif bf.get("rows_upserted"):
                today_bar_note = "已补写/刷新今日 K 线"
            elif bf.get("skipped_reason") == "non_trading_day":
                today_bar_note = "非交易日，信号按最近一根入库 K 线"
        except ValueError as e:
            missing.append(
                GoalPlanLiveMissingItem(
                    code="today_bar_backfill_failed",
                    message=str(e),
                    action="请点「刷新列表」并确认 ③ 数据源与网络",
                )
            )

    if (
        bars_last_td
        and bars_last_td < sh_today
        and phase in ("after_close", "intraday")
        and shanghai_today_date().weekday() < 5
    ):
        warnings.append(
            GoalPlanLiveMissingItem(
                code="bars_stale",
                message=f"入库 K 线末根仍为 {bars_last_td}，未能写入今日 {sh_today}",
                action="请点「刷新列表」联网更新现价后重试",
            )
        )

    wl_count = int(
        session.execute(select(func.count()).select_from(WatchlistRow)).scalar_one() or 0
    )
    if wl_count == 0:
        warnings.append(
            GoalPlanLiveMissingItem(
                code="watchlist_empty",
                message="自选池为空，无法扫描换仓备选",
                action="可选：在 ② 添加自选并 ③ 拉取 K 线",
            )
        )

    live_px: float | None = None
    live_src: str | None = None
    live_qd: str | None = None
    if bar_count >= 30:
        live = (
            live_quote_fields_for_codes_enhanced(
                [sym], data_source=data_source, force_spot_refresh=True
            ).get(sym)
            or {}
        )
        px_raw = live.get("live_last_price")
        live_src = str(live.get("live_price_source") or "") or None
        live_qd = str(live.get("live_quote_date") or "")[:10] or None
        if px_raw is not None:
            try:
                live_px = round(float(px_raw), 4)
            except (TypeError, ValueError):
                live_px = None

        if live_px is None or live_px <= 0:
            missing.append(
                GoalPlanLiveMissingItem(
                    code="live_quote_missing",
                    message=f"未能获取 {sym} 的联网现价",
                    action="请点列表「刷新列表」，并确认网络与 ③ 数据源可用",
                )
            )
        elif live_src in _DAILY_CLOSE_PRICE_SOURCES:
            missing.append(
                GoalPlanLiveMissingItem(
                    code="live_quote_fallback_daily",
                    message=f"现价回退为日线收盘（{live_src}），非当日联网报价",
                    action="请点「刷新列表」联网更新；盘中需东财/通达信等接口可用",
                )
            )

    ready = len(missing) == 0
    if ready:
        summary = (
            f"{sym} 可测算当日：联网现价 {live_px}（{live_src or '—'}）"
            + (f"，报价日 {live_qd}" if live_qd else "")
            + (f"；{today_bar_note}" if today_bar_note else "")
            + (f"；K 线末根 {bars_last_td}" if bars_last_td else "")
            + f"。{phase_note}"
        )
    else:
        parts = [m.message for m in missing[:3]]
        summary = "尚不能测算当日：" + "；".join(parts)

    return HoldingGoalPlanLivePreflightOut(
        ready=ready,
        symbol=sym,
        shanghai_today=sh_today,
        session_phase=phase,
        bars_count=bar_count if bar_count else None,
        bars_last_trade_date=bars_last_td,
        live_price=live_px,
        live_price_source=live_src,
        live_quote_date=live_qd,
        watchlist_count=wl_count,
        missing=missing,
        warnings=warnings,
        summary_zh=summary,
    )


# --- 自选池扫描 ---


def _scan_watchlist_picks(
    session: Session,
    *,
    exclude_symbol: str,
    data_source: str | None,
    limit: int = 5,
    scan_cap: int = 30,
    live_mode: bool = False,
) -> list[WatchlistPickOut]:
    rows = list(session.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all())
    symbols = [r.symbol for r in rows if r.symbol and r.symbol != exclude_symbol]
    if not symbols:
        return []
    scan_syms = symbols[:scan_cap]
    if live_mode and scan_syms and shanghai_today_date().weekday() < 5:
        try:
            backfill_watchlist_today_close_batch(
                scan_syms,
                data_source=data_source,
                allow_intraday=True,
                force_refresh=True,
            )
        except Exception:
            pass
    bar_by = watchlist_bar_fields_for_session(session, symbols)
    scored: list[tuple[int, WatchlistPickOut]] = []
    for sym in scan_syms:
        try:
            sig = compute_signal(sym, data_source=data_source, use_today_bar=live_mode)
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
        bs, trend, strength, pos_hint = _signal_scores_for_mode(sig, live_mode=live_mode)
        spot_px = (
            float(sig.spot_last_price)
            if live_mode
            and sig.spot_last_price is not None
            and float(sig.spot_last_price) > 0
            else None
        )
        pick = WatchlistPickOut(
            symbol=sym,
            name=name,
            buy_suitability_score=bs,
            trend=trend,
            strength=strength,
            position_hint=pos_hint,
            last_close=bar.get("last_close"),
            spot_last_price=round(spot_px, 4) if spot_px is not None else None,
            reason=_pick_reason(sig),
        )
        scored.append((bs, pick))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


# --- 留/走/换决策 ---


def _decide_position(
    exit_advice: HoldingExitAdviceOut,
    picks: list[WatchlistPickOut],
    *,
    gap: float,
) -> tuple[PositionDecision, str]:
    """
    根据离场压力、当前/备选适合度与距目标 gap，返回决策枚举与中文摘要。

    规则要点：gap≤0 → goal_reached；离场压力≥65 倾向 switch；
    alt_edge 与 cur_score 差距大且 gap>0 时考虑换仓；否则 hold/watch。
    """
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


# --- 主入口：单持仓目标路径 ---


def compute_holding_goal_plan(
    row: HoldingRow,
    *,
    session: Session,
    start_capital: float,
    target_capital: float,
    data_source: str | None = None,
    current_price: float | None = None,
    live_mode: bool = False,
) -> HoldingGoalPlanOut:
    """
    对单条持仓中记录测算「起始→目标」Demo 路径（主入口）。

    流程：汇总账户权益与 gap → 离场建议 → 扫描自选 Top5 → 决策留/换/观察/达标
    → 生成 daily_verdict、近端价位参考、分步 steps 列表。

    参数:
        live_mode: True 时使用当日 K 线（需预检或 ensure today bar）；False 为收盘口径。
        current_price: daily 模式下可选，与列表「当前价格」列一致。

    返回:
        `HoldingGoalPlanOut`（含 exit_advice、watchlist_picks、steps、disclaimer_note）。
    """
    if row.status != HOLDING_STATUS_HOLDING:
        raise ValueError("仅持仓中记录可测算目标路径")
    if target_capital <= start_capital:
        raise ValueError("目标资金须大于起始资金")

    all_rows = list(
        session.execute(select(HoldingRow).order_by(HoldingRow.id.desc()).limit(200)).scalars().all()
    )
    holdings_out = build_holdings_list(
        session, all_rows, force_spot_refresh=live_mode
    )
    current_equity = _portfolio_equity(holdings_out, start_capital)
    gap = round(target_capital - current_equity, 2)
    denom = target_capital - start_capital
    progress_pct: float | None = None
    if denom > 0:
        progress_pct = round((current_equity - start_capital) / denom * 100.0, 2)
        progress_pct = max(0.0, min(100.0, progress_pct))

    exit_advice = compute_holding_exit_advice(
        row,
        session=session,
        data_source=data_source,
        current_price=None if live_mode else current_price,
        live_mode=live_mode,
    )
    picks = _scan_watchlist_picks(
        session,
        exclude_symbol=row.symbol,
        data_source=data_source,
        limit=5,
        live_mode=live_mode,
    )
    decision, decision_summary = _decide_position(exit_advice, picks, gap=gap)
    cur_sc = exit_advice.buy_suitability_score or 0
    best_alt = picks[0].buy_suitability_score if picks else 0
    alt_edge = best_alt - cur_sc
    session_phase, session_phase_note = _shanghai_session_phase()
    if live_mode:
        session_phase_note = (
            "当日测算：已补写今日 K 线（联网现价）；信号与浮盈亏均含当日。"
            + session_phase_note
        )
    daily_verdict, daily_verdict_detail, switch_to_symbol = _resolve_daily_verdict(
        decision,
        exit_advice,
        picks,
        symbol=row.symbol,
        alt_edge=alt_edge,
    )

    item = next((h for h in holdings_out if h.id == row.id), None)
    mv = float(item.market_value) if item and item.market_value is not None else None
    near_term = _estimate_near_term_price_outlook(
        row, exit_advice, data_source=data_source
    )

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
    if near_term is not None:
        steps.append(
            f"②b 近期价位参考（{near_term.horizon_label}）：现价 {near_term.reference_price} 元；"
            f"中性约 {near_term.target_mid} 元"
            + (
                f"（+{near_term.upside_pct_mid}%）"
                if near_term.upside_pct_mid is not None
                else ""
            )
            + f"；上方 {near_term.target_high} 元、下方 {near_term.target_low} 元（规则推算，非涨跌承诺）。"
        )

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
        calculation_mode="live" if live_mode else "daily",
        price_basis=exit_advice.current_price_source or exit_advice.ref_price_source,
        signal_as_of_date=exit_advice.signal_as_of_date,
        near_term_price=near_term,
        steps=steps,
        exit_advice=exit_advice,
        watchlist_picks=picks,
        disclaimer_note=_GOAL_DISCLAIMER_LIVE if live_mode else _GOAL_DISCLAIMER,
    )
