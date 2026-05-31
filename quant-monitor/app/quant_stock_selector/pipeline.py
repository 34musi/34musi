"""End-to-end analysis orchestration."""

from __future__ import annotations

import argparse
import logging
from typing import List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

from dataclasses import replace

from .backtest import (
    last_bar_dual_ma_golden_cross,
    last_bar_on_ma5,
    last_ma5_stand_nd_no_drop,
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
        skipped = len(codes) - len(histories)
        if skipped:
            logger.info(
                "sector-screen batch: sector=%s source=%s requested=%d fetched=%d skipped=%d",
                sector_name,
                getattr(datasource, "source_name", args.data_source),
                len(codes),
                len(histories),
                skipped,
            )
        req_d = bool(getattr(args, "show_dual_ma_strategy", False))
        req_t = bool(getattr(args, "show_triple_ma_strategy", False))
        req_m5 = bool(getattr(args, "show_ma5_stand_strategy", False))
        req_m5_3d = bool(getattr(args, "show_ma5_stand_3d_strategy", False))
        ma5_lb = int(getattr(args, "ma5_stand_lookback", 60) or 60)
        ma5_3d_min = int(getattr(args, "ma5_stand_3d_min_days", 3) or 3)
        need_ma_rank = sector_name == UNIVERSE_POOL_SECTOR_NAME and (
            req_d or req_t or req_m5 or req_m5_3d
        )

        if need_ma_rank:
            ranked: list[tuple[str, str, pd.DataFrame, float, int | None, int | None]] = []
            for code, name in codes:
                history = histories.get(code)
                if history is None or history.empty:
                    continue
                ok, stren, ma5_cnt, ma5_streak = universe_ma_strategy_strength(
                    history,
                    require_dual=req_d,
                    require_triple=req_t,
                    require_ma5_stand=req_m5,
                    require_ma5_stand_3d=req_m5_3d,
                    fast_period=args.fast_period,
                    slow_period=args.slow_period,
                    ma5_stand_lookback=ma5_lb,
                    ma5_stand_3d_min_days=ma5_3d_min,
                )
                if not ok:
                    continue
                ranked.append((code, name, history, stren, ma5_cnt, ma5_streak))
            ranked.sort(key=lambda x: x[3], reverse=True)
            cap = max(1, int(getattr(args, "flat_universe_top", 200) or 200))
            for code, name, history, stren, ma5_cnt, ma5_streak in ranked[:cap]:
                try:
                    evaluation = evaluate_stock(code, name, sector, history, args)
                except Exception as exc:
                    logger.warning(
                        "sector-screen evaluate skip: sector=%s code=%s name=%s reason=%s",
                        sector_name,
                        code,
                        name,
                        exc,
                    )
                    continue
                if args.only_passed and not evaluation.screen_passed:
                    continue
                ev_kw: dict = {"strategy_pick_strength": round(float(stren), 6)}
                if req_m5 and ma5_cnt is not None:
                    ev_kw["ma5_stand_count"] = int(ma5_cnt)
                if req_m5_3d and ma5_streak is not None:
                    ev_kw["ma5_consecutive_stand_days"] = int(ma5_streak)
                stock_results.append(replace(evaluation, **ev_kw))
            continue

        for code, name in codes:
            history = histories.get(code)
            if history is None or history.empty:
                continue
            if req_d and not last_bar_dual_ma_golden_cross(
                history, args.fast_period, args.slow_period
            ):
                continue
            if req_t and not last_bar_triple_ma_bull_alignment(
                history, args.fast_period, args.slow_period
            ):
                continue
            if req_m5 and not last_bar_on_ma5(history):
                continue
            if req_m5_3d and not last_ma5_stand_nd_no_drop(history, min_days=ma5_3d_min):
                continue
            try:
                evaluation = evaluate_stock(code, name, sector, history, args)
            except Exception as exc:
                logger.warning(
                    "sector-screen evaluate skip: sector=%s code=%s name=%s reason=%s",
                    sector_name,
                    code,
                    name,
                    exc,
                )
                continue
            if args.only_passed and not evaluation.screen_passed:
                continue
            stock_results.append(evaluation)

    def _stock_sort_key(item: StockEvaluation) -> tuple:
        mst = item.ma5_consecutive_stand_days
        if mst is not None:
            return (0, float(mst), float(item.strategy_pick_strength or 0), item.final_score)
        mc = item.ma5_stand_count
        if mc is not None:
            return (0, float(mc), float(item.strategy_pick_strength or 0), item.final_score)
        s = item.strategy_pick_strength
        if s is not None:
            return (1, float(s), item.final_score)
        if getattr(item, "screen_mode", "") == "short_term":
            return (2, float(item.short_term_score or 0), item.final_score)
        return (3, float("-inf"), item.final_score)

    ranked_stocks = sorted(stock_results, key=_stock_sort_key, reverse=True)
    return target_sectors, ranked_stocks
