"""Pick symbols from top hot sectors with ST / STAR board filtering and rich detail payload."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .datasources import BaseAShareDataSource
from .market_utils import normalize_code


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


def pick_from_hot_sectors(
    datasource: BaseAShareDataSource,
    *,
    top_sectors: int = 5,
    stocks_per_sector: int = 5,
    board_type: str = "all",
    exclude_st: bool = True,
    exclude_kcb: bool = True,
) -> HotPickResult:
    """
    Walk get_sector_rankings in row order; for each of top N sectors load constituents
    and take up to M stocks after filters. Preserves full constituent columns per stock.
    """
    warnings: list[str] = []
    rankings = datasource.get_sector_rankings(board_type)
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

        stocks: list[dict[str, Any]] = []
        stock_rank = 0
        for _, crow in cons.iterrows():
            code = normalize_code(crow.get("code", crow.get("代码", "")))
            if not code or len(code) != 6:
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
