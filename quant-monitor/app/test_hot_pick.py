# -*- coding: utf-8 -*-

import pandas as pd

from quant_stock_selector.datasources import BaseAShareDataSource
from quant_stock_selector.hot_pick import (
    is_st_stock_name,
    is_star_board_code,
    pick_from_hot_sectors,
)


class _MockHotDS(BaseAShareDataSource):
    source_name = "mock"

    def __init__(self, rankings: pd.DataFrame, constituents_map: dict[str, pd.DataFrame]) -> None:
        self._rankings = rankings
        self._constituents_map = constituents_map

    def get_stock_universe(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["code", "name"])

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        return self._rankings.copy()

    def get_sector_constituents(self, sector_name: str, board_type: str | None = None) -> pd.DataFrame:
        return self._constituents_map.get(sector_name, pd.DataFrame()).copy()

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        return pd.DataFrame()


def test_is_star_board_code_688_689():
    assert is_star_board_code("688001") is True
    assert is_star_board_code("689001") is True
    assert is_star_board_code("600000") is False


def test_is_st_stock_name_star_prefix():
    assert is_st_stock_name("*ST某某") is True
    assert is_st_stock_name("万科A") is False


def test_pick_preserves_sector_order_stock_rank_and_full_columns():
    rankings = pd.DataFrame(
        [
            {"sector_name": "Alpha", "hot_score": 10.0, "change_pct": 3.0},
            {"sector_name": "Beta", "hot_score": 9.0, "change_pct": 2.0},
        ]
    )
    cons = {
        "Alpha": pd.DataFrame(
            [
                {"code": "600000", "name": "浦发", "extra_col": 1},
                {"code": "688001", "name": "科创", "extra_col": 2},
                {"code": "000001", "name": "*ST过滤", "extra_col": 3},
                {"code": "000002", "name": "万科", "extra_col": 4},
            ]
        ),
        "Beta": pd.DataFrame([{"code": "600519", "name": "茅台", "vol": 99}]),
    }
    ds = _MockHotDS(rankings, cons)
    out = pick_from_hot_sectors(
        ds, top_sectors=2, stocks_per_sector=2, exclude_st=True, exclude_kcb=True
    )
    assert [b["sector_rank"] for b in out.sectors_detail] == [1, 2]
    assert out.sectors_detail[0]["sector_metrics"]["sector_name"] == "Alpha"
    s0 = out.sectors_detail[0]["stocks"]
    assert len(s0) == 2
    assert s0[0]["code"] == "600000" and s0[0]["stock_rank_in_sector"] == 1
    assert s0[1]["code"] == "000002" and s0[1]["stock_rank_in_sector"] == 2
    assert "extra_col" in s0[0] and s0[0]["extra_col"] == 1
    assert out.symbols_for_watchlist == ["600000", "000002", "600519"]


def test_pick_dedup_symbols_across_sectors():
    rankings = pd.DataFrame(
        [
            {"sector_name": "S1", "hot_score": 1.0},
            {"sector_name": "S2", "hot_score": 0.5},
        ]
    )
    cons = {
        "S1": pd.DataFrame([{"code": "600001", "name": "A"}]),
        "S2": pd.DataFrame(
            [
                {"code": "600001", "name": "A"},
                {"code": "600002", "name": "B"},
            ]
        ),
    }
    ds = _MockHotDS(rankings, cons)
    out = pick_from_hot_sectors(ds, top_sectors=2, stocks_per_sector=5)
    assert out.symbols_for_watchlist == ["600001", "600002"]
    assert len(out.sectors_detail[1]["stocks"]) == 2
