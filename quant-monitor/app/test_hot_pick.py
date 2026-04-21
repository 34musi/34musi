# -*- coding: utf-8 -*-

import pandas as pd

from app.quant_stock_selector.datasources import BaseAShareDataSource
from app.quant_stock_selector.hot_pick import (
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


class _MockHotTechDS(_MockHotDS):
    def __init__(
        self,
        rankings: pd.DataFrame,
        constituents_map: dict[str, pd.DataFrame],
        histories: dict[str, pd.DataFrame],
    ) -> None:
        super().__init__(rankings, constituents_map)
        self._histories = histories

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        frame = self._histories.get(code)
        if frame is None:
            raise RuntimeError(f"missing history for {code}")
        return frame.copy()


def _make_hist(
    closes: list[float],
    *,
    volume: float = 1_000_000,
    turnover: float = 200_000_000,
) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    rows = []
    for d, c in zip(dates, closes):
        rows.append(
            {
                "date": d,
                "open": c * 0.99,
                "high": c * 1.01,
                "low": c * 0.985,
                "close": c,
                "volume": volume,
                "turnover": turnover,
            }
        )
    return pd.DataFrame(rows)


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
        ds,
        top_sectors=2,
        stocks_per_sector=2,
        exclude_st=True,
        exclude_kcb=True,
        sort_by_trend_strength=False,
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
    out = pick_from_hot_sectors(ds, top_sectors=2, stocks_per_sector=5, sort_by_trend_strength=False)
    assert out.symbols_for_watchlist == ["600001", "600002"]
    assert len(out.sectors_detail[1]["stocks"]) == 2


def test_pick_sorts_by_trend_strength_inside_sector():
    rankings = pd.DataFrame([{"sector_name": "Alpha", "hot_score": 10.0, "change_pct": 3.0}])
    cons = {
        "Alpha": pd.DataFrame(
            [
                {"code": "000001", "name": "平安银行"},
                {"code": "000002", "name": "万科A"},
            ]
        )
    }
    histories = {
        "000001": _make_hist([10 + i * 0.02 for i in range(140)], turnover=120_000_000),
        # 复合上行，与 000001 的缓涨在「综合 screen_score」上易并列，故用略低波动打破同分逆序
        "000002": _make_hist([10 * (1.004**i) for i in range(140)], turnover=500_000_000),
    }
    ds = _MockHotTechDS(rankings, cons, histories)
    out = pick_from_hot_sectors(ds, top_sectors=1, stocks_per_sector=2, sort_by_trend_strength=True)
    got = [s["code"] for s in out.sectors_detail[0]["stocks"]]
    assert got == ["000002", "000001"]
    assert out.sectors_detail[0]["stocks"][0]["stock_rank_in_sector"] == 1


def test_pick_skips_index_and_non_equity_codes():
    """板块成分中的指数码（如 399001）不应进入选股，避免 mootdx 等源解析失败。"""
    rankings = pd.DataFrame([{"sector_name": "Alpha", "hot_score": 10.0, "change_pct": 3.0}])
    cons = {
        "Alpha": pd.DataFrame(
            [
                {"code": "399001", "name": "深证成指"},
                {"code": "880001", "name": "自定义板块示例"},
                {"code": "600000", "name": "浦发"},
                {"code": "000002", "name": "万科A"},
            ]
        ),
    }
    ds = _MockHotDS(rankings, cons)
    out = pick_from_hot_sectors(
        ds,
        top_sectors=1,
        stocks_per_sector=2,
        exclude_st=True,
        exclude_kcb=True,
        sort_by_trend_strength=False,
    )
    got = [s["code"] for s in out.sectors_detail[0]["stocks"]]
    assert got == ["600000", "000002"]


def test_pick_can_filter_by_technical_pass_overextended_and_liquidity():
    rankings = pd.DataFrame([{"sector_name": "Alpha", "hot_score": 10.0, "change_pct": 3.0}])
    cons = {
        "Alpha": pd.DataFrame(
            [
                {"code": "000001", "name": "趋势合格"},
                {"code": "000002", "name": "涨幅过大"},
                {"code": "000003", "name": "成交额太低"},
                {"code": "000004", "name": "技术不通过"},
            ]
        )
    }
    histories = {
        "000001": _make_hist([10 + i * 0.04 for i in range(140)], turnover=250_000_000),
        "000002": _make_hist([10 + i * 0.02 for i in range(120)] + [20 + i * 1.0 for i in range(20)], turnover=800_000_000),
        "000003": _make_hist([10 + i * 0.05 for i in range(140)], turnover=20_000_000),
        "000004": _make_hist([10 - i * 0.03 for i in range(140)], turnover=250_000_000),
    }
    ds = _MockHotTechDS(rankings, cons, histories)
    out = pick_from_hot_sectors(
        ds,
        top_sectors=1,
        stocks_per_sector=5,
        sort_by_trend_strength=True,
        require_technical_pass=True,
        exclude_overextended=True,
        max_return_20d_pct=25.0,
        enable_liquidity_filter=True,
        min_avg_turnover_20d_100m=1.0,
    )
    assert out.symbols_for_watchlist == ["000001"]
    warnings = " ".join(out.warnings)
    assert "涨幅过大" in warnings
    assert "流动性不足" in warnings
    assert "技术面未通过" in warnings
