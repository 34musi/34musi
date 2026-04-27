"""End-to-end analysis orchestration."""

from __future__ import annotations

import argparse
from typing import List, Tuple

from .datasources import get_data_source
from .evaluation import evaluate_stock
from .histories import collect_histories
from .models import SectorRecord, StockEvaluation
from .sectors import load_sector_constituents, select_target_sectors


def run_analysis(args: argparse.Namespace) -> Tuple[List[SectorRecord], List[StockEvaluation]]:
    datasource = get_data_source(
        args.data_source,
        tushare_token=args.tushare_token,
        hot_chain_prefer_cache=getattr(args, "hot_chain_prefer_cache", True),
        hot_chain_force_refresh=getattr(args, "hot_chain_force_refresh", False),
    )
    target_sectors = select_target_sectors(datasource, args)
    sector_constituents = load_sector_constituents(datasource, target_sectors, args)

    hot_score_map = {sector.sector_name: sector for sector in target_sectors}
    stock_results: List[StockEvaluation] = []

    for sector_name, constituents in sector_constituents.items():
        sector = hot_score_map[sector_name]
        codes = list(constituents[["code", "name"]].itertuples(index=False, name=None))
        histories = collect_histories(datasource, codes, args)
        for code, name in codes:
            history = histories.get(code)
            if history is None or history.empty:
                continue
            try:
                evaluation = evaluate_stock(code, name, sector, history, args)
            except Exception as exc:
                print(f"跳过 {code} {name}: {exc}")
                continue
            if args.only_passed and not evaluation.screen_passed:
                continue
            stock_results.append(evaluation)

    ranked_stocks = sorted(stock_results, key=lambda item: item.final_score, reverse=True)
    return target_sectors, ranked_stocks
