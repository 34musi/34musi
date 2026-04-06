#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自用路线图 · 单规则历史检验（示例）：

规则（Demo，非投资建议）：当日收盘 > MA20，且 MA20 高于 5 日前 MA20 时记为「趋势偏多」信号；
统计信号日为 True 时，**未来第 5 个交易日** 的收盘相对当日收盘的收益率分布，
并在 **前 70% 样本（样本内）** 与 **后 30%（样本外）** 分别打印均值（简单分割，非严谨 walk-forward）。

依赖本地 SQLite 已有足够日线（请先 ingest）。

用法::

    python scripts/backtest_sample_rule.py 600519

更完整的 walk-forward 与多方法对比见 ``scripts/run_forecast_validate.py`` 或 ``GET /research/forecast-validate``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from app.ingest import load_bars_df, normalize_symbol


def _split_stats(fwd: pd.Series, sig: pd.Series, *, cut: float = 0.7) -> None:
    valid = sig.notna() & fwd.notna()
    idx = np.where(valid.to_numpy())[0]
    if len(idx) < 20:
        print("有效样本过少，请先拉取更长历史日线。")
        return
    cut_i = int(len(idx) * cut)
    ins = idx[:cut_i]
    oos = idx[cut_i:]
    for name, subset in ("样本内(前70%)", ins), ("样本外(后30%)", oos):
        m = fwd.iloc[subset]
        s = sig.iloc[subset].astype(bool)
        on = m[s].dropna()
        off = m[~s].dropna()
        print(f"\n=== {name} 行数={len(subset)} ===")
        if len(on):
            print(f"  信号=True  未来5日收益均值: {float(on.mean()):.4f}  次数: {len(on)}")
        if len(off):
            print(f"  信号=False 未来5日收益均值: {float(off.mean()):.4f}  次数: {len(off)}")


def main() -> int:
    sym = normalize_symbol(sys.argv[1] if len(sys.argv) > 1 else "600879")
    df = load_bars_df(sym, min_bars=120)
    if df.empty or len(df) < 80:
        print(f"{sym}: K 线不足（需约 80+ 根），请先 POST /ingest/update")
        return 1
    c = df["close"].astype(float)
    ma20 = c.rolling(20, min_periods=20).mean()
    ma20_lag5 = ma20.shift(5)
    sig = (c > ma20) & (ma20 > ma20_lag5)
    fwd5 = c.shift(-5) / c - 1.0
    print(f"标的 {sym} 总行数={len(df)}  规则: close>MA20 且 MA20>MA20[-5]")
    _split_stats(fwd5, sig)
    print("\n说明：此为教学向粗分割，不等价于专业回测；不构成投资建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
