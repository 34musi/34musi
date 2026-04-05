#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产品化 Demo 验证脚本（需联网）：

1) 校验 fundamental_score_delta 有界逻辑（离线）
2) 对单标的拉取扩展因子并打印摘要（调用 AkShare）

用法（在项目根目录 quant-monitor/ 下）::

    python scripts/validate_fundamentals_demo.py
    python scripts/validate_fundamentals_demo.py 600519
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.fundamentals import (  # noqa: E402
    build_fundamental_panel,
    fundamental_score_delta,
)
from app.ingest import normalize_symbol  # noqa: E402


def _test_delta_offline() -> None:
    d, reasons = fundamental_score_delta(pe=12, pb=2.5, revenue_yoy_pct=12, profit_yoy_pct=20, main_net_inflow=1e6)
    assert -15 <= d <= 15, d
    assert any(r.code == "fund_profit_yoy_strong" for r in reasons)
    d2, _ = fundamental_score_delta(pe=100, pb=12, revenue_yoy_pct=-10, profit_yoy_pct=-20, main_net_inflow=-1e6)
    assert d2 <= 0
    assert -15 <= d2 <= 15
    print("[ok] fundamental_score_delta 有界与符号离线检查通过")


def _test_remote(sym: str) -> None:
    sym = normalize_symbol(sym)
    panel = build_fundamental_panel(sym)
    d, reasons = fundamental_score_delta(
        panel.pe_dynamic,
        panel.pb,
        panel.revenue_yoy_pct,
        panel.profit_yoy_pct,
        panel.main_net_inflow,
    )
    print(f"[remote] {sym} panel:", panel.model_dump())
    print(f"[remote] 合成调整分 fundamental_adjustment={d}，理由条数={len(reasons)}")


def main() -> None:
    _test_delta_offline()
    sym = sys.argv[1] if len(sys.argv) > 1 else "600519"
    _test_remote(sym)


if __name__ == "__main__":
    main()
