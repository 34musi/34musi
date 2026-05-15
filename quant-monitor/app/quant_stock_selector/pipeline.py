"""End-to-end analysis orchestration."""

from __future__ import annotations

import argparse
from typing import List, Tuple

import pandas as pd

from dataclasses import replace

from .backtest import (
    last_bar_dual_ma_golden_cross,
    last_bar_triple_ma_bull_alignment,
    universe_ma_strategy_strength,
)
from .datasources import get_data_source
from .evaluation import evaluate_stock
from .histories import collect_histories
from .models import SectorRecord, StockEvaluation
from .sectors import UNIVERSE_POOL_SECTOR_NAME, load_sector_constituents, select_target_sectors


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
        need_ma_rank = sector_name == UNIVERSE_POOL_SECTOR_NAME and (
            getattr(args, "show_dual_ma_strategy", False) or getattr(args, "show_triple_ma_strategy", False)
        )

        if need_ma_rank:
            ranked: list[tuple[str, str, pd.DataFrame, float]] = []
            req_d = bool(getattr(args, "show_dual_ma_strategy", False))
            req_t = bool(getattr(args, "show_triple_ma_strategy", False))
            for code, name in codes:
                history = histories.get(code)
                if history is None or history.empty:
                    continue
                ok, stren = universe_ma_strategy_strength(
                    history,
                    require_dual=req_d,
                    require_triple=req_t,
                    fast_period=args.fast_period,
                    slow_period=args.slow_period,
                )
                if not ok:
                    continue
                ranked.append((code, name, history, stren))
            ranked.sort(key=lambda x: x[3], reverse=True)
            cap = max(1, int(getattr(args, "flat_universe_top", 200) or 200))
            for code, name, history, stren in ranked[:cap]:
                try:
                    evaluation = evaluate_stock(code, name, sector, history, args)
                except Exception as exc:
                    print(f"跳过 {code} {name}: {exc}")
                    continue
                if args.only_passed and not evaluation.screen_passed:
                    continue
                stock_results.append(
                    replace(evaluation, strategy_pick_strength=round(float(stren), 6))
                )
            continue

        for code, name in codes:
            history = histories.get(code)
            if history is None or history.empty:
                continue
            if getattr(args, "show_dual_ma_strategy", False):
                if not last_bar_dual_ma_golden_cross(history, args.fast_period, args.slow_period):
                    continue
            if getattr(args, "show_triple_ma_strategy", False):
                if not last_bar_triple_ma_bull_alignment(history, args.fast_period, args.slow_period):
                    continue
            try:
                evaluation = evaluate_stock(code, name, sector, history, args)
            except Exception as exc:
                print(f"跳过 {code} {name}: {exc}")
                continue
            if args.only_passed and not evaluation.screen_passed:
                continue
            stock_results.append(evaluation)

    def _stock_sort_key(item: StockEvaluation) -> tuple:
        s = item.strategy_pick_strength
        if s is not None:
            return (0, float(s), item.final_score)
        return (1, float("-inf"), item.final_score)

    ranked_stocks = sorted(stock_results, key=_stock_sort_key, reverse=True)
    return target_sectors, ranked_stocks
