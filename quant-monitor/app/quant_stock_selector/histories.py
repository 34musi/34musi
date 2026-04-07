"""Fetch or load price histories for a batch of codes."""

from __future__ import annotations

import argparse
from typing import Dict, Iterable, Tuple

import pandas as pd

from .datasources import BaseAShareDataSource
from .market_utils import load_local_history


def collect_histories(
    datasource: BaseAShareDataSource,
    codes: Iterable[Tuple[str, str]],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    histories: Dict[str, pd.DataFrame] = {}
    for code, name in codes:
        local_history = load_local_history(code, args.data_dir)
        if local_history is not None:
            local_history["name"] = name
            histories[code] = local_history
            continue
        history = datasource.get_price_history(code, args.start_date, args.end_date, adjust=args.adjust)
        history["name"] = name
        histories[code] = history
    return histories
