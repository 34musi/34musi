"""Pick symbols from top hot sectors with optional technical ranking and filters."""

from __future__ import annotations

import re
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

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


def _pick_history_window() -> tuple[str, str]:
    """A little over 1 year of calendar days to cover 120+ trading sessions."""
    end = date.today()
    start = end - timedelta(days=420)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _avg_turnover_20d(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty or "turnover" not in frame.columns:
        return 0.0
    return safe_float(frame["turnover"].tail(20).mean(), default=0.0)


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
        detail["return_20d"] = screen.return_20d
        detail["distance_to_60d_high"] = screen.distance_to_60d_high
        detail["volume_ratio_20_60"] = screen.volume_ratio_20_60
        detail["drawdown_60d"] = screen.drawdown_60d
        detail["annual_volatility_20d"] = screen.annual_volatility_20d
        detail["avg_turnover_20d"] = round(avg_turnover_20d, 2)
        detail["avg_turnover_20d_100m"] = round(avg_turnover_20d / 1e8, 4)
        detail["latest_close"] = screen.latest_close
        detail["screen_reasons"] = screen.reasons
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
    seen_symbols: set[str] = set()
    symbols_order: list[str] = []

    for i in range(top_n):
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

    return HotPickResult(
        sectors_detail=sectors_detail,
        symbols_for_watchlist=symbols_order,
        warnings=warnings,
    )
