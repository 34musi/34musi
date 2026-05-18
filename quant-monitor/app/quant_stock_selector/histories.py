"""Fetch or load price histories for a batch of codes."""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Iterable, Tuple

import pandas as pd

from .datasources import BaseAShareDataSource
from .exceptions import DataSourceError
from .market_utils import load_local_history

logger = logging.getLogger(__name__)

_PROGRESS_LOG_EVERY = 200


def collect_histories(
    datasource: BaseAShareDataSource,
    codes: Iterable[Tuple[str, str]],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    code_list = list(codes)
    total = len(code_list)
    histories: Dict[str, pd.DataFrame] = {}
    source = getattr(datasource, "source_name", None) or getattr(args, "data_source", "?")

    processed = 0
    ok_api = 0
    ok_local = 0
    skipped = 0
    last_sym = ""

    def _log_progress(*, done: bool = False) -> None:
        tag = "done" if done else "progress"
        logger.info(
            "sector-screen history %s: source=%s processed=%d/%d ok_api=%d ok_local=%d skipped=%d last=%s",
            tag,
            source,
            processed,
            total,
            ok_api,
            ok_local,
            skipped,
            last_sym or "—",
        )

    for code, name in code_list:
        processed += 1
        sym = str(code).strip()
        nm = str(name or "").strip()
        last_sym = sym
        try:
            local_history = load_local_history(sym, args.data_dir)
            if local_history is not None:
                local_history["name"] = nm
                histories[sym] = local_history
                ok_local += 1
            else:
                history = datasource.get_price_history(
                    sym, args.start_date, args.end_date, adjust=args.adjust
                )
                history["name"] = nm
                histories[sym] = history
                ok_api += 1
        except DataSourceError as exc:
            skipped += 1
            logger.debug(
                "sector-screen history skip: source=%s code=%s name=%s reason=%s",
                source,
                sym,
                nm,
                exc,
            )
        except Exception as exc:
            skipped += 1
            logger.debug(
                "sector-screen history skip: source=%s code=%s name=%s unexpected=%s",
                source,
                sym,
                nm,
                exc,
                exc_info=True,
            )

        if processed % _PROGRESS_LOG_EVERY == 0:
            _log_progress(done=False)

    if total > 0 and processed % _PROGRESS_LOG_EVERY != 0:
        _log_progress(done=True)

    return histories
