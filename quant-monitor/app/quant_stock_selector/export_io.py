"""Terminal tables and Excel export."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .models import SectorRecord, StockEvaluation


def print_stock_rankings(stocks: Sequence[StockEvaluation], top_n: int) -> None:
    if not stocks:
        print("\n没有筛选出符合条件的股票")
        return
    frame = pd.DataFrame([asdict(stock) for stock in stocks]).sort_values("final_score", ascending=False)
    display = frame[
        [
            "sector_name",
            "code",
            "name",
            "screen_passed",
            "sector_hot_score",
            "screen_score",
            "annual_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "final_score",
        ]
    ].head(top_n)
    print("\n候选股票排名:")
    print(display.to_string(index=False))


def export_results(
    sectors: Sequence[SectorRecord],
    stocks: Sequence[StockEvaluation],
    output_path: Optional[Path],
) -> None:
    if output_path is None:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path) as writer:
        if sectors:
            pd.DataFrame([asdict(record) for record in sectors]).to_excel(writer, sheet_name="hot_sectors", index=False)
        if stocks:
            pd.DataFrame([asdict(stock) for stock in stocks]).sort_values("final_score", ascending=False).to_excel(
                writer,
                sheet_name="candidate_stocks",
                index=False,
            )
    print(f"\n结果已导出到: {output_path}")
