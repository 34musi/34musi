"""Sector ranking, constituent loading, console print."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Dict, List, Sequence

import pandas as pd

from .datasources import BaseAShareDataSource
from .market_utils import read_codes_file, safe_float
from .models import SectorRecord


def build_sector_records(frame: pd.DataFrame) -> List[SectorRecord]:
    records: List[SectorRecord] = []
    for row in frame.itertuples():
        records.append(
            SectorRecord(
                sector_name=row.sector_name,
                board_type=row.board_type,
                change_pct=round(safe_float(row.change_pct), 2),
                advancers_ratio=round(safe_float(row.advancers_ratio) * 100.0, 2),
                leader_change_pct=round(safe_float(row.leader_change_pct), 2),
                turnover_rate=round(safe_float(row.turnover_rate), 2),
                hot_score=round(safe_float(row.hot_score), 2),
                source=getattr(row, "source", "unknown"),
            )
        )
    return records


def print_sector_rankings(sectors: Sequence[SectorRecord]) -> None:
    if not sectors:
        print("没有可展示的热门板块")
        return
    frame = pd.DataFrame([asdict(record) for record in sectors])
    display = frame[
        [
            "sector_name",
            "board_type",
            "hot_score",
            "change_pct",
            "advancers_ratio",
            "leader_change_pct",
            "turnover_rate",
        ]
    ]
    print("\n热门板块排名:")
    print(display.to_string(index=False))


def select_target_sectors(datasource: BaseAShareDataSource, args: argparse.Namespace) -> List[SectorRecord]:
    if args.sector:
        constituents = datasource.get_sector_constituents(
            args.sector, args.board_type if args.board_type != "all" else None
        )
        return [
            SectorRecord(
                sector_name=constituents["sector_name"].iloc[0],
                board_type=constituents["board_type"].iloc[0],
                change_pct=0.0,
                advancers_ratio=0.0,
                leader_change_pct=0.0,
                turnover_rate=0.0,
                hot_score=60.0,
                source=args.data_source,
            )
        ]

    if args.hot_sectors:
        rankings = datasource.get_sector_rankings(args.board_type)
        return build_sector_records(rankings.head(args.top_sectors))

    return [
        SectorRecord(
            sector_name="自定义股票池",
            board_type="custom",
            change_pct=0.0,
            advancers_ratio=0.0,
            leader_change_pct=0.0,
            turnover_rate=0.0,
            hot_score=50.0,
            source="custom",
        )
    ]


def load_sector_constituents(
    datasource: BaseAShareDataSource,
    sectors: Sequence[SectorRecord],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    sector_map: Dict[str, pd.DataFrame] = {}
    if args.codes:
        codes = read_codes_file(args.codes)
        sector_map["自定义股票池"] = codes.assign(sector_name="自定义股票池", board_type="custom")
        return sector_map

    for sector in sectors:
        constituents = datasource.get_sector_constituents(
            sector.sector_name, sector.board_type if sector.board_type != "custom" else None
        )
        if args.max_stocks_per_sector:
            constituents = constituents.head(args.max_stocks_per_sector)
        sector_map[sector.sector_name] = constituents
    return sector_map
