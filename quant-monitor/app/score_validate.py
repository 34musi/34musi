"""
本地打分 vs 前向展望 H 日实际收益：分档统计与相关性验证。

## 功能作用

读取 `forward_outlook` 表中 **已结算（settled）** 记录，在每条记录的 `signal_trade_date`
当日截断 K 线，复算选股技术面分、回测分、综合分与 ④ 技术分，再按分档汇总
`actual_return_pct`（与登记时的 horizon 一致）。

用于回答「本地打分对未来 H 日收益有没有区分度」，**不构成投资建议**。

## 对外接口

| 函数 | 用途 |
|------|------|
| `run_score_bucket_validate` | 批量分档统计主入口 |

打分快照逻辑见 `app.local_scores`。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.db_models import ForwardOutlookRow
from app.db_session import session_scope
from app.ingest import load_bars_from_db, normalize_symbol
from app.local_scores import compute_local_scores_at_date

DEFAULT_BUCKETS: list[tuple[str, float, float]] = [
    ("<60", 0.0, 60.0),
    ("60-70", 60.0, 70.0),
    ("70-80", 70.0, 80.0),
    ("80-90", 80.0, 90.0),
    ("90+", 90.0, 101.0),
]

SCORE_FIELD_META: list[tuple[str, str]] = [
    ("short_term_score", "短线技术分（screening.short_term_score）"),
    ("final_score_v2_short", "综合分 v2_short（板块中性50 + 短线50% + 回测30%）"),
    ("final_score_v2_trade", "交易向综合分 v2_trade（板块25% + 短线75%，无回测）"),
    ("signal_technical_score", "④ 信号技术分（不含扩展因子）"),
]

DISCLAIMER = (
    "分档统计基于已结算的前向展望样本，样本量小或板块热度用中性值时结论仅供参考；"
    "回测分仍为信号日前的样本内指标。不构成投资建议。"
)


def _bucket_label(score: float | None, buckets: list[tuple[str, float, float]]) -> str | None:
    if score is None or not math.isfinite(float(score)):
        return None
    s = float(score)
    for label, lo, hi in buckets:
        if lo <= s < hi:
            return label
    return buckets[-1][0] if buckets else None


def _spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(x) != len(y):
        return None
    a = np.asarray(x, dtype=np.float64)
    b = np.asarray(y, dtype=np.float64)
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def _summarize_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "mean_return_pct": None,
            "median_return_pct": None,
            "win_rate_pct": None,
            "mean_abs_return_pct": None,
        }
    rets = [float(r["actual_return_pct"]) for r in rows if r.get("actual_return_pct") is not None]
    if not rets:
        return {
            "count": len(rows),
            "mean_return_pct": None,
            "median_return_pct": None,
            "win_rate_pct": None,
            "mean_abs_return_pct": None,
        }
    arr = np.asarray(rets, dtype=np.float64)
    return {
        "count": len(rets),
        "mean_return_pct": round(float(arr.mean()), 4),
        "median_return_pct": round(float(np.median(arr)), 4),
        "win_rate_pct": round(float((arr > 0).mean() * 100.0), 2),
        "mean_abs_return_pct": round(float(np.abs(arr).mean()), 4),
    }


def _build_field_buckets(
    samples: list[dict[str, Any]],
    field: str,
    buckets: list[tuple[str, float, float]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {label: [] for label, _, _ in buckets}
    grouped["invalid"] = []
    for row in samples:
        sc = row.get(field)
        bl = _bucket_label(sc if sc is not None else None, buckets)
        if bl is None:
            grouped["invalid"].append(row)
        else:
            grouped[bl].append(row)
    out: list[dict[str, Any]] = []
    for label, lo, hi in buckets:
        rows = grouped[label]
        summ = _summarize_bucket(rows)
        out.append(
            {
                "bucket": label,
                "score_min": lo,
                "score_max_exclusive": hi if hi <= 100 else None,
                **summ,
            }
        )
    if grouped["invalid"]:
        summ = _summarize_bucket(grouped["invalid"])
        out.append({"bucket": "invalid", "score_min": None, "score_max_exclusive": None, **summ})
    return out


def run_score_bucket_validate(
    *,
    symbol: str | None = None,
    horizon: int | None = None,
    min_samples: int = 5,
    buckets: list[tuple[str, float, float]] | None = None,
    sector_hot_score: float = 50.0,
    fast_period: int = 10,
    slow_period: int = 30,
    min_turnover_amt: float = 0.0,
    require_screen_pass: bool = False,
    settle_pending: bool = True,
) -> dict[str, Any]:
    """
    对已结算 forward_outlook 记录做分档收益统计。

    返回各分数字段的分档表、Spearman 相关、以及「仅初筛通过」子样本摘要。
    """
    bucket_defs = buckets or DEFAULT_BUCKETS
    if settle_pending:
        from app.forward_outlook import settle_all_pending

        settle_all_pending()

    sym_filter: str | None = None
    if symbol:
        sym_filter = normalize_symbol(symbol)

    with session_scope() as s:
        q = select(ForwardOutlookRow).where(ForwardOutlookRow.status == "settled")
        if sym_filter:
            q = q.where(ForwardOutlookRow.symbol == sym_filter)
        if horizon is not None:
            q = q.where(ForwardOutlookRow.horizon == int(horizon))
        db_rows = s.execute(q.order_by(ForwardOutlookRow.id.asc())).scalars().all()
        row_dicts = [
            {
                "symbol": r.symbol,
                "stock_name": r.stock_name,
                "horizon": int(r.horizon or 0),
                "signal_trade_date": r.signal_trade_date,
                "actual_return_pct": r.actual_return_pct,
                "actual_up": r.actual_up,
                "predicted_up": r.predicted_up,
            }
            for r in db_rows
        ]

    raw_n = len(row_dicts)
    samples: list[dict[str, Any]] = []
    skipped = 0
    for row in row_dicts:
        if row["actual_return_pct"] is None:
            skipped += 1
            continue
        df = load_bars_from_db(row["symbol"])
        snap = compute_local_scores_at_date(
            df,
            row["signal_trade_date"],
            sector_hot_score=sector_hot_score,
            fast_period=fast_period,
            slow_period=slow_period,
            min_turnover_amt=min_turnover_amt,
            relaxed_min_bars=True,
        )
        if snap is None:
            skipped += 1
            continue
        if require_screen_pass and not snap.get("short_term_passed"):
            skipped += 1
            continue
        if min_turnover_amt > 0 and not snap.get("liquidity_ok"):
            skipped += 1
            continue
        samples.append(
            {
                "symbol": row["symbol"],
                "stock_name": row["stock_name"],
                "horizon": row["horizon"],
                "signal_trade_date": row["signal_trade_date"],
                "actual_return_pct": float(row["actual_return_pct"]),
                "actual_up": bool(row["actual_up"]),
                "predicted_up": row["predicted_up"],
                **snap,
            }
        )

    field_results: list[dict[str, Any]] = []
    for key, desc in SCORE_FIELD_META:
        vals = [(float(s[key]), float(s["actual_return_pct"])) for s in samples if s.get(key) is not None]
        sp = _spearman([v[0] for v in vals], [v[1] for v in vals]) if vals else None
        passed_only = [s for s in samples if s.get("short_term_passed") and s.get(key) is not None]
        passed_only_summary: dict[str, Any] = _summarize_bucket(passed_only)
        passed_only_summary["bucket"] = "passed_only"
        field_results.append(
            {
                "field": key,
                "description": desc,
                "spearman_vs_return": round(sp, 4) if sp is not None else None,
                "buckets": _build_field_buckets(samples, key, bucket_defs),
                "passed_only_summary": passed_only_summary,
                "passed_only_count": len(passed_only),
            }
        )

    high_trade = [s for s in samples if float(s.get("final_score_v2_trade") or 0) >= 80]
    low_trade = [s for s in samples if float(s.get("final_score_v2_trade") or 0) < 60]

    return {
        "n_settled_rows": raw_n,
        "n_samples": len(samples),
        "n_skipped": skipped,
        "horizon_filter": horizon,
        "symbol_filter": sym_filter,
        "sector_hot_score_assumed": sector_hot_score,
        "min_turnover_amt": min_turnover_amt,
        "require_screen_pass": require_screen_pass,
        "fast_period": fast_period,
        "slow_period": slow_period,
        "buckets": [{"label": a, "min": b, "max_exclusive": c} for a, b, c in bucket_defs],
        "fields": field_results,
        "trade_score_contrast": {
            "high_gte_80": _summarize_bucket(high_trade),
            "low_lt_60": _summarize_bucket(low_trade),
            "note": "v2_trade ≥80 vs <60 的子样本 H 日收益对比（若有足够样本）",
        },
        "samples_preview": samples[-20:],
        "disclaimer": DISCLAIMER,
        "how_to_read": (
            "若高分档 mean_return_pct 持续高于低分档且 Spearman>0，说明本地打分有一定区分度；"
            "反之或样本过少时不宜直接用于交易。建议配合 require_screen_pass 与 min_turnover_amt 过滤。"
        ),
    }
