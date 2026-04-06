#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI：对单标的打印 walk-forward 方向预测验证结果（与 GET /research/forecast-validate 同源逻辑）。

依赖本地 SQLite 已有足够日线（请先 ingest）；不联网。

用法::

    python scripts/run_forecast_validate.py 600519
    python scripts/run_forecast_validate.py 600519 --horizon 10 --min-train 150
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.forecast_validate import run_forecast_validate  # noqa: E402
from app.ingest import normalize_symbol  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="walk-forward 方向预测验证（仅读库）")
    p.add_argument("symbol", nargs="?", default="600519", help="6 位股票代码")
    p.add_argument("--horizon", type=int, default=5, help="未来 H 个交易日")
    p.add_argument("--min-train", type=int, default=120, dest="min_train", help="最小训练行数再进入 OOS")
    p.add_argument("--retrain-every", type=int, default=20, dest="retrain", help="Logistic 重训间隔（OOS 步）")
    p.add_argument("--trade-limit", type=int, default=25, dest="trade_limit", help="返回最近多少笔完整买卖（示意）")
    p.add_argument("--ma-short", type=int, default=5, dest="ma_short", help="双均线短周期")
    p.add_argument("--ma-long", type=int, default=10, dest="ma_long", help="双均线长周期（须大于短周期）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON（否则人类可读摘要）")
    args = p.parse_args()
    try:
        sym = normalize_symbol(args.symbol)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        out = run_forecast_validate(
            sym,
            horizon=args.horizon,
            min_train_rows=args.min_train,
            retrain_every=args.retrain,
            trade_limit=args.trade_limit,
            ma_short=args.ma_short,
            ma_long=args.ma_long,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"标的 {out['symbol']}  horizon={out['horizon']}  OOS 样本数={out['n_oos']}")
    print(f"区间: {out['first_oos_trade_date']} ~ {out['last_oos_trade_date']}")
    print(f"样本外真实上涨比例: {out['oos_positive_rate']:.4f}")
    print(f"无脑猜多数类准确率: {out['baseline_always_majority_oos']:.4f}")
    for m in out["methods"]:
        print(f"\n--- {m['method']} ---")
        print(m["description"])
        print(
            f"  accuracy={m['accuracy']:.4f}  balanced_acc={m['balanced_accuracy']:.4f}  "
            f"AUC={m.get('auc_roc')}"
        )
        c = m["confusion"]
        print(f"  confusion tp={c['tp']} fp={c['fp']} tn={c['tn']} fn={c['fn']}")
        if m.get("mean_forward_return_pred_up") is not None:
            print(f"  预测涨时 H 日收益均值: {m['mean_forward_return_pred_up']:.6f}")
        if m.get("mean_forward_return_pred_down") is not None:
            print(f"  预测跌时 H 日收益均值: {m['mean_forward_return_pred_down']:.6f}")
        ts = m.get("trade_summary")
        if ts:
            print(
                f"  示意交易: {ts['completed_trades']} 笔  胜率={ts['win_rate']:.4f}  "
                f"均涨跌%={ts['avg_return_pct']:.4f}  简单加总%={ts['total_simple_return_pct']:.4f}"
            )
        for t in (m.get("trades") or [])[-5:]:
            print(
                f"    买 {t['buy_date']}@{t['buy_close']} → 卖 {t['sell_date']}@{t['sell_close']}  {t['return_pct']:+.4f}%"
            )
        if m.get("open_leg"):
            ol = m["open_leg"]
            print(f"  未平仓: {ol['buy_date']} @{ol['buy_close']} — {ol.get('note', '')}")
    print("\n" + out["disclaimer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
