"""Pick symbols from top hot sectors with optional technical ranking and filters."""

from __future__ import annotations

import re
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from .backtest import consecutive_close_on_ma5_streak, last_n_days_close_on_ma5
from .datasources import BaseAShareDataSource
from .market_utils import is_listed_a_share_equity, normalize_code, safe_float
from .screening import evaluate_screen


_ST_PATTERN = re.compile(r"(\*ST|＊ST|\bST\b)", re.IGNORECASE)


def is_st_stock_name(name: object) -> bool:
    """Heuristic: *ST / ST in name (EastMoney style)."""
    if name is None:
        return False
    s = str(name).strip()
    if not s:
        return False
    return bool(_ST_PATTERN.search(s))


def is_star_board_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == 6 and (c.startswith("688") or c.startswith("689"))


def _series_to_jsonable_dict(series: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in series.items():
        k = str(key)
        try:
            if pd.isna(val):
                out[k] = None
            elif isinstance(val, (np.integer, np.floating)):
                out[k] = val.item()
            elif isinstance(val, np.bool_):
                out[k] = bool(val)
            elif hasattr(val, "item") and not isinstance(val, (str, bytes)):
                try:
                    out[k] = val.item()
                except Exception:
                    out[k] = str(val)
            else:
                out[k] = val
        except (TypeError, ValueError):
            out[k] = str(val) if val is not None else None
    return out


def _ranking_row_to_metrics(row: pd.Series, sector_rank: int) -> dict[str, Any]:
    base = _series_to_jsonable_dict(row)
    base["sector_rank"] = sector_rank
    return base


@dataclass
class HotPickResult:
    """Ordered sector bundles + flat symbol list for watchlist upsert."""

    sectors_detail: list[dict[str, Any]]
    symbols_for_watchlist: list[str]
    warnings: list[str] = field(default_factory=list)
    ma5_capital_sectors_detail: list[dict[str, Any]] = field(default_factory=list)


def _pick_history_window() -> tuple[str, str]:
    """A little over 1 year of calendar days to cover 120+ trading sessions."""
    end = date.today()
    start = end - timedelta(days=420)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _avg_turnover_nd(frame: pd.DataFrame, days: int) -> float:
    if frame is None or frame.empty or "turnover" not in frame.columns:
        return 0.0
    n = max(1, int(days))
    return safe_float(frame["turnover"].tail(n).mean(), default=0.0)


def _avg_turnover_20d(frame: pd.DataFrame) -> float:
    return _avg_turnover_nd(frame, 20)


def _basic_pick_from_constituents(
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int,
    exclude_st: bool,
    exclude_kcb: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Legacy fast path: preserve row order after basic ST / STAR filters."""
    stocks: list[dict[str, Any]] = []
    symbols: list[str] = []
    stock_rank = 0
    for _, crow in constituents.iterrows():
        code = normalize_code(crow.get("code", crow.get("代码", "")))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = crow.get("name", crow.get("名称", ""))
        if exclude_kcb and is_star_board_code(code):
            continue
        if exclude_st and is_st_stock_name(name):
            continue
        stock_rank += 1
        if stock_rank > stocks_per_sector:
            break
        detail = _series_to_jsonable_dict(crow)
        detail["stock_rank_in_sector"] = stock_rank
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        stocks.append(detail)
        symbols.append(code)
    return stocks, symbols


def _technical_pick_from_constituents(
    datasource: BaseAShareDataSource,
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int,
    exclude_st: bool,
    exclude_kcb: bool,
    sort_by_trend_strength: bool,
    require_technical_pass: bool,
    exclude_overextended: bool,
    max_return_20d_pct: float,
    enable_liquidity_filter: bool,
    min_avg_turnover_20d_100m: float,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Advanced path: fetch price history per stock, compute screen metrics, then rank/filter.

    `min_avg_turnover_20d_100m` is expressed in 100 million CNY (亿元).
    """
    start_date, end_date = _pick_history_window()
    min_turnover_amt = max(0.0, float(min_avg_turnover_20d_100m)) * 1e8
    candidates: list[dict[str, Any]] = []
    filtered_overextended = 0
    filtered_liquidity = 0
    filtered_technical = 0
    failed_history = 0

    for _, crow in constituents.iterrows():
        code = normalize_code(crow.get("code", crow.get("代码", "")))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = crow.get("name", crow.get("名称", ""))
        if exclude_kcb and is_star_board_code(code):
            continue
        if exclude_st and is_st_stock_name(name):
            continue
        try:
            hist = datasource.get_price_history(code, start_date, end_date, adjust="qfq")
            screen = evaluate_screen(hist)
        except Exception as exc:  # noqa: BLE001
            failed_history += 1
            warnings.append(f"板块「{sector_name}」成分股 {code} 技术面计算失败：{exc}")
            continue

        avg_turnover_5d = _avg_turnover_nd(hist, 5)
        avg_turnover_10d = _avg_turnover_nd(hist, 10)
        avg_turnover_20d = _avg_turnover_20d(hist)
        if exclude_overextended and screen.return_20d > max_return_20d_pct:
            filtered_overextended += 1
            continue
        if enable_liquidity_filter and avg_turnover_20d < min_turnover_amt:
            filtered_liquidity += 1
            continue
        if require_technical_pass and not screen.passed:
            filtered_technical += 1
            continue

        detail = _series_to_jsonable_dict(crow)
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        detail["screen_passed"] = bool(screen.passed)
        detail["trend_score"] = screen.trend_score
        detail["volume_score"] = screen.volume_score
        detail["risk_score"] = screen.risk_score
        detail["screen_score"] = screen.screen_score
        detail["return_5d"] = screen.return_5d
        detail["return_10d"] = screen.return_10d
        detail["return_20d"] = screen.return_20d
        detail["distance_to_60d_high"] = screen.distance_to_60d_high
        detail["volume_ratio_20_60"] = screen.volume_ratio_20_60
        detail["vol_ratio_last_day"] = screen.vol_ratio_last_day
        detail["drawdown_60d"] = screen.drawdown_60d
        detail["annual_volatility_20d"] = screen.annual_volatility_20d
        detail["avg_turnover_5d"] = round(avg_turnover_5d, 2)
        detail["avg_turnover_10d"] = round(avg_turnover_10d, 2)
        detail["avg_turnover_20d"] = round(avg_turnover_20d, 2)
        detail["avg_turnover_5d_100m"] = round(avg_turnover_5d / 1e8, 4)
        detail["avg_turnover_10d_100m"] = round(avg_turnover_10d / 1e8, 4)
        detail["avg_turnover_20d_100m"] = round(avg_turnover_20d / 1e8, 4)
        detail["latest_close"] = screen.latest_close
        detail["screen_reasons"] = screen.reasons
        detail["short_term_passed"] = bool(screen.short_term_passed)
        detail["short_term_score"] = screen.short_term_score
        detail["long_term_passed"] = bool(screen.long_term_passed)
        detail["long_term_score"] = screen.long_term_score
        detail["ma20_slope_pct"] = screen.ma20_slope_pct
        candidates.append(detail)

    if sort_by_trend_strength:
        # 主排序：技术面与趋势；同分下略偏好更低波动（常见组合构建中的风险约束近似）。
        candidates.sort(
            key=lambda x: (
                1 if x.get("screen_passed") else 0,
                safe_float(x.get("screen_score")),
                safe_float(x.get("trend_score")),
                -safe_float(x.get("annual_volatility_20d")),
                safe_float(x.get("volume_score")),
                safe_float(x.get("avg_turnover_20d")),
            ),
            reverse=True,
        )

    selected = candidates[:stocks_per_sector]
    for idx, detail in enumerate(selected, start=1):
        detail["stock_rank_in_sector"] = idx

    if filtered_overextended:
        warnings.append(
            f"板块「{sector_name}」因近 20 日涨幅过大被过滤 {filtered_overextended} 只"
        )
    if filtered_liquidity:
        warnings.append(f"板块「{sector_name}」因流动性不足被过滤 {filtered_liquidity} 只")
    if filtered_technical:
        warnings.append(f"板块「{sector_name}」因技术面未通过被过滤 {filtered_technical} 只")
    if failed_history:
        warnings.append(f"板块「{sector_name}」有 {failed_history} 只历史行情不足或计算失败")

    return selected, [normalize_code(x.get("code", "")) for x in selected if x.get("code")]


def _main_net_inflow_from_flow_row(row: dict[str, Any]) -> float | None:
    for key in ("主力净流入-净额", "main_net_inflow"):
        if key not in row:
            continue
        raw = row[key]
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            return v
    return None


def evaluate_capital_support(
    sym: str,
    *,
    lookback_days: int = 3,
    min_positive_days: int = 2,
) -> tuple[bool, float, dict[str, Any]]:
    """
    资金承接：最近 lookback_days 个交易日中至少 min_positive_days 日主力净流入为正，且合计为正。
    资金流为东财日级（与行情数据源路线无关）。
    """
    from app.fundamentals import fetch_individual_fund_flow_latest_metrics, fetch_individual_fund_flow_recent_rows

    lb = max(1, int(lookback_days))
    need_pos = max(1, min(int(min_positive_days), lb))
    rows = fetch_individual_fund_flow_recent_rows(sym, limit_rows=lb + 2)
    meta: dict[str, Any] = {
        "fund_flow_lookback_days": lb,
        "fund_flow_positive_days": 0,
        "main_net_inflow_3d": None,
        "main_net_inflow": None,
        "fund_flow_date": None,
        "em_large_net_pct": None,
        "capital_support_score": 0.0,
    }
    if not rows:
        return False, 0.0, meta
    tail = rows[-lb:]
    inflows: list[float] = []
    for r in tail:
        v = _main_net_inflow_from_flow_row(r)
        inflows.append(v if v is not None else 0.0)
    positive = sum(1 for x in inflows if x > 0)
    total = float(sum(inflows))
    meta["fund_flow_positive_days"] = positive
    meta["main_net_inflow_3d"] = round(total, 2)
    latest = fetch_individual_fund_flow_latest_metrics(sym) or {}
    from app.fundamentals import fetch_latest_main_flow

    main_last, flow_d, _basis = fetch_latest_main_flow(sym)
    meta["main_net_inflow"] = round(main_last, 2) if main_last is not None else None
    meta["fund_flow_date"] = flow_d
    meta["em_large_net_pct"] = latest.get("em_large_net_pct")
    ok = positive >= need_pos and total > 0
    if ok and meta["em_large_net_pct"] is not None:
        try:
            if float(meta["em_large_net_pct"]) < 0:
                ok = False
        except (TypeError, ValueError):
            pass
    score = total + positive * 5e7
    if meta["em_large_net_pct"] is not None:
        try:
            score += float(meta["em_large_net_pct"]) * 1e6
        except (TypeError, ValueError):
            pass
    meta["capital_support_score"] = round(score, 2)
    return ok, score, meta


def _ma5_capital_pick_from_constituents(
    datasource: BaseAShareDataSource,
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int,
    exclude_st: bool,
    exclude_kcb: bool,
    ma5_stand_min_days: int,
    capital_lookback_days: int,
    capital_min_positive_days: int,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """连续站上 MA5 + 资金承接强的候选（与上方热门条件分开展示）。"""
    start_date, end_date = _pick_history_window()
    min_days = max(1, int(ma5_stand_min_days))
    candidates: list[dict[str, Any]] = []
    failed_history = 0
    failed_ma5 = 0
    failed_capital = 0
    failed_fund = 0

    for _, crow in constituents.iterrows():
        code = normalize_code(crow.get("code", crow.get("代码", "")))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = crow.get("name", crow.get("名称", ""))
        if exclude_kcb and is_star_board_code(code):
            continue
        if exclude_st and is_st_stock_name(name):
            continue
        try:
            hist = datasource.get_price_history(code, start_date, end_date, adjust="qfq")
        except Exception as exc:  # noqa: BLE001
            failed_history += 1
            warnings.append(f"「五日+资金」板块「{sector_name}」{code} 日线失败：{exc}")
            continue
        streak = consecutive_close_on_ma5_streak(hist)
        if not last_n_days_close_on_ma5(hist, min_days=min_days):
            failed_ma5 += 1
            continue
        try:
            cap_ok, cap_score, cap_meta = evaluate_capital_support(
                code,
                lookback_days=capital_lookback_days,
                min_positive_days=capital_min_positive_days,
            )
        except Exception as exc:  # noqa: BLE001
            failed_fund += 1
            warnings.append(f"「五日+资金」板块「{sector_name}」{code} 资金流失败：{exc}")
            continue
        if not cap_ok:
            failed_capital += 1
            continue

        detail = _series_to_jsonable_dict(crow)
        detail["pick_group"] = "ma5_capital"
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        detail["ma5_consecutive_days"] = streak
        detail["ma5_stand_min_days"] = min_days
        detail["capital_support_score"] = cap_meta.get("capital_support_score")
        detail["fund_flow_positive_days"] = cap_meta.get("fund_flow_positive_days")
        detail["main_net_inflow_3d"] = cap_meta.get("main_net_inflow_3d")
        detail["main_net_inflow"] = cap_meta.get("main_net_inflow")
        detail["fund_flow_date"] = cap_meta.get("fund_flow_date")
        detail["em_large_net_pct"] = cap_meta.get("em_large_net_pct")
        try:
            screen = evaluate_screen(hist)
            detail["latest_close"] = screen.latest_close
            detail["return_5d"] = screen.return_5d
            detail["return_10d"] = screen.return_10d
            detail["return_20d"] = screen.return_20d
        except Exception:
            pass
        detail["_sort_capital"] = cap_score
        detail["_sort_ma5"] = streak
        candidates.append(detail)

    candidates.sort(
        key=lambda x: (
            safe_float(x.get("_sort_capital")),
            safe_float(x.get("_sort_ma5")),
            safe_float(x.get("main_net_inflow_3d")),
        ),
        reverse=True,
    )
    selected = candidates[:stocks_per_sector]
    for idx, detail in enumerate(selected, start=1):
        detail["stock_rank_in_sector"] = idx
        detail.pop("_sort_capital", None)
        detail.pop("_sort_ma5", None)

    if failed_ma5:
        warnings.append(f"「五日+资金」板块「{sector_name}」未满足连续 {min_days} 日站上 MA5：{failed_ma5} 只")
    if failed_capital:
        warnings.append(f"「五日+资金」板块「{sector_name}」资金承接不足：{failed_capital} 只")
    if failed_fund:
        warnings.append(f"「五日+资金」板块「{sector_name}」资金流拉取失败：{failed_fund} 只")
    if failed_history:
        warnings.append(f"「五日+资金」板块「{sector_name}」日线不足：{failed_history} 只")

    return selected, [normalize_code(x.get("code", "")) for x in selected if x.get("code")]


def pick_from_hot_sectors(
    datasource: BaseAShareDataSource,
    *,
    top_sectors: int = 5,
    stocks_per_sector: int = 5,
    board_type: str = "all",
    exclude_st: bool = True,
    exclude_kcb: bool = True,
    rankings_override: pd.DataFrame | None = None,
    sort_by_trend_strength: bool = True,
    require_technical_pass: bool = False,
    exclude_overextended: bool = False,
    max_return_20d_pct: float = 25.0,
    enable_liquidity_filter: bool = False,
    min_avg_turnover_20d_100m: float = 1.0,
    should_cancel: Callable[[], bool] | None = None,
    enable_ma5_capital_pick: bool = True,
    ma5_stand_min_days: int = 3,
    capital_flow_lookback_days: int = 3,
    capital_min_positive_days: int = 2,
) -> HotPickResult:
    """
    Walk top hot sectors and choose up to M symbols per sector.

    Fast path: if all advanced technical toggles are off, preserve original constituent row order.
    Advanced path: fetch price history, compute `evaluate_screen()`, optionally filter overextended /
    illiquid / technical-fail names, then rank by trend strength inside each sector.
    """
    warnings: list[str] = []
    rankings = rankings_override.copy() if rankings_override is not None else datasource.get_sector_rankings(board_type)
    if rankings is None or rankings.empty:
        return HotPickResult(sectors_detail=[], symbols_for_watchlist=[], warnings=["热门板块列表为空"])

    top_n = min(max(1, top_sectors), len(rankings))
    sectors_detail: list[dict[str, Any]] = []
    ma5_capital_sectors_detail: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    symbols_order: list[str] = []

    for i in range(top_n):
        if should_cancel and should_cancel():
            warnings.append("用户已取消热门板块任务，仅返回已处理板块")
            break
        srow = rankings.iloc[i]
        sector_rank = i + 1
        sector_name = str(srow.get("sector_name", "") or "")
        bt = srow.get("board_type")
        bt_arg = str(bt) if bt is not None and str(bt) not in ("", "nan") else None

        sector_metrics = _ranking_row_to_metrics(srow, sector_rank)

        try:
            cons = datasource.get_sector_constituents(sector_name, bt_arg)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"板块「{sector_name}」成分股获取失败：{exc}")
            sectors_detail.append(
                {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": []}
            )
            continue

        if cons is None or cons.empty:
            warnings.append(f"板块「{sector_name}」无成分股数据")
            sectors_detail.append(
                {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": []}
            )
            continue

        advanced_enabled = (
            sort_by_trend_strength
            or require_technical_pass
            or exclude_overextended
            or enable_liquidity_filter
        )
        if advanced_enabled:
            stocks, sector_symbols = _technical_pick_from_constituents(
                datasource,
                cons,
                sector_rank=sector_rank,
                sector_name=sector_name,
                stocks_per_sector=stocks_per_sector,
                exclude_st=exclude_st,
                exclude_kcb=exclude_kcb,
                sort_by_trend_strength=sort_by_trend_strength,
                require_technical_pass=require_technical_pass,
                exclude_overextended=exclude_overextended,
                max_return_20d_pct=max_return_20d_pct,
                enable_liquidity_filter=enable_liquidity_filter,
                min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
                warnings=warnings,
            )
        else:
            stocks, sector_symbols = _basic_pick_from_constituents(
                cons,
                sector_rank=sector_rank,
                sector_name=sector_name,
                stocks_per_sector=stocks_per_sector,
                exclude_st=exclude_st,
                exclude_kcb=exclude_kcb,
            )

        for st in stocks:
            if isinstance(st, dict):
                st["pick_group"] = "sector_hot"

        stocks_mc: list[dict[str, Any]] = []
        if enable_ma5_capital_pick:
            stocks_mc, _mc_syms = _ma5_capital_pick_from_constituents(
                datasource,
                cons,
                sector_rank=sector_rank,
                sector_name=sector_name,
                stocks_per_sector=stocks_per_sector,
                exclude_st=exclude_st,
                exclude_kcb=exclude_kcb,
                ma5_stand_min_days=ma5_stand_min_days,
                capital_lookback_days=capital_flow_lookback_days,
                capital_min_positive_days=capital_min_positive_days,
                warnings=warnings,
            )
            for code in _mc_syms:
                if code not in seen_symbols:
                    seen_symbols.add(code)
                    symbols_order.append(code)

        for code in sector_symbols:
            if code not in seen_symbols:
                seen_symbols.add(code)
                symbols_order.append(code)

        if len(stocks) < stocks_per_sector:
            warnings.append(
                f"板块「{sector_name}」过滤后仅 {len(stocks)} 只（目标 {stocks_per_sector}）"
            )

        sectors_detail.append(
            {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": stocks}
        )
        ma5_capital_sectors_detail.append(
            {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": stocks_mc}
        )

    return HotPickResult(
        sectors_detail=sectors_detail,
        symbols_for_watchlist=symbols_order,
        warnings=warnings,
        ma5_capital_sectors_detail=ma5_capital_sectors_detail,
    )
