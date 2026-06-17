#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI：本地打分分档 vs 前向展望已结算 H 日收益（与 GET /research/score-bucket-validate 同源）。

依赖本地 SQLite 已有 forward_outlook settled 记录（请先 ③ ingest 并等待展望到期结算）。

用法::

    python scripts/run_score_validate.py
    python scripts/run_score_validate.py --symbol 600519 --horizon 3
    python scripts/run_score_validate.py --require-screen-pass --min-turnover-amt 50000000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.score_validate import run_score_bucket_validate  # noqa: E402


def _print_field(field: dict) -> None:
    print(f"\n=== {field['field']} — {field['description']} ===")
    sp = field.get("spearman_vs_return")
    print(f"Spearman(分, H日收益%): {sp if sp is not None else '—'}")
    print(f"初筛通过子样本: n={field.get('passed_only_count', 0)}")
    po = field.get("passed_only_summary") or {}
    if po.get("count"):
        print(
            f"  通过组 mean={po.get('mean_return_pct')}%  median={po.get('median_return_pct')}%  "
            f"胜率={po.get('win_rate_pct')}%"
        )
    print("分档:")
    for b in field.get("buckets") or []:
        if not b.get("count"):
            continue
        print(
            f"  [{b['bucket']:>6}] n={b['count']:3d}  "
            f"mean={b.get('mean_return_pct')}%  median={b.get('median_return_pct')}%  "
            f"胜率={b.get('win_rate_pct')}%"
        )


def main() -> int:
    p = argparse.ArgumentParser(description="本地打分分档 vs 前向展望 H 日收益")
    p.add_argument("--symbol", default=None, help="可选：仅统计单标的")
    p.add_argument("--horizon", type=int, default=None, help="可选：仅统计指定 H")
    p.add_argument("--sector-hot", type=float, default=50.0, dest="sector_hot", help="假设板块热度")
    p.add_argument(
        "--min-turnover-amt",
        type=float,
        default=0.0,
        dest="min_turnover",
        help="20 日均成交额下限（元）",
    )
    p.add_argument(
        "--require-screen-pass",
        action="store_true",
        dest="require_pass",
        help="仅统计短线初筛通过样本",
    )
    p.add_argument("--fast-period", type=int, default=10, dest="fast")
    p.add_argument("--slow-period", type=int, default=30, dest="slow")
    p.add_argument("--no-settle", action="store_true", help="统计前不尝试结算 pending")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = p.parse_args()

    raw = run_score_bucket_validate(
        symbol=args.symbol,
        horizon=args.horizon,
        sector_hot_score=args.sector_hot,
        min_turnover_amt=args.min_turnover,
        require_screen_pass=args.require_pass,
        fast_period=args.fast,
        slow_period=args.slow,
        settle_pending=not args.no_settle,
    )
    if args.json:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return 0

    print(
        f"已结算行={raw['n_settled_rows']}  有效样本={raw['n_samples']}  "
        f"跳过={raw['n_skipped']}  horizon={raw.get('horizon_filter')}"
    )
    if raw["n_samples"] < 5:
        print("[注意] 样本过少，分档结论仅供参考")
    for field in raw.get("fields") or []:
        _print_field(field)
    tc = raw.get("trade_score_contrast") or {}
    hi = tc.get("high_gte_80") or {}
    lo = tc.get("low_lt_60") or {}
    if hi.get("count") or lo.get("count"):
        print("\n--- v2_trade 高低分对比 ---")
        print(f"  ≥80: n={hi.get('count')} mean={hi.get('mean_return_pct')}%")
        print(f"  <60: n={lo.get('count')} mean={lo.get('mean_return_pct')}%")
    print("\n" + raw.get("how_to_read", ""))
    print(raw.get("disclaimer", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
