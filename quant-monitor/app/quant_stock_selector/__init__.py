"""
A-share hot sector selector and stock evaluator (canonical pipeline).

板块 → 选股 → 回测 → 导出。拆分子模块见包内各文件；ingest/API 等辅助层可依赖本包，
本包不依赖 FastAPI 或控制台。
"""

from __future__ import annotations

from .backtest import BacktestMetrics, compose_final_score, run_sma_backtest
from .constants import DEFAULT_END_DATE, DEFAULT_START_DATE, PRICE_COLUMN_ALIASES, TRADING_DAYS_PER_YEAR
from .datasources import (
    AkShareDataSource,
    BaseAShareDataSource,
    MootdxDataSource,
    TushareDataSource,
    get_data_source,
)
from .evaluation import evaluate_stock
from .exceptions import DataSourceError
from .hot_pick import HotPickResult, pick_from_hot_sectors
from .export_io import export_results, print_stock_rankings
from .histories import collect_histories
from .market_utils import (
    compute_max_drawdown,
    load_local_history,
    normalize_code,
    normalize_score,
    read_codes_file,
    safe_float,
    standardize_price_frame,
    to_tushare_code,
)
from .models import ScreenMetrics, SectorRecord, StockEvaluation
from .pipeline import run_analysis
from .screening import evaluate_screen
from .sectors import (
    build_sector_records,
    load_sector_constituents,
    print_sector_rankings,
    select_target_sectors,
)

# CLI
from .cli import main, parse_args, validate_args

__all__ = [
    "AkShareDataSource",
    "BacktestMetrics",
    "BaseAShareDataSource",
    "DEFAULT_END_DATE",
    "DEFAULT_START_DATE",
    "DataSourceError",
    "MootdxDataSource",
    "PRICE_COLUMN_ALIASES",
    "ScreenMetrics",
    "SectorRecord",
    "StockEvaluation",
    "TRADING_DAYS_PER_YEAR",
    "TushareDataSource",
    "build_sector_records",
    "collect_histories",
    "compose_final_score",
    "evaluate_screen",
    "evaluate_stock",
    "export_results",
    "HotPickResult",
    "get_data_source",
    "pick_from_hot_sectors",
    "load_local_history",
    "load_sector_constituents",
    "main",
    "normalize_code",
    "normalize_score",
    "parse_args",
    "print_stock_rankings",
    "print_sector_rankings",
    "read_codes_file",
    "run_analysis",
    "run_sma_backtest",
    "safe_float",
    "select_target_sectors",
    "standardize_price_frame",
    "to_tushare_code",
    "validate_args",
    "compute_max_drawdown",
]
