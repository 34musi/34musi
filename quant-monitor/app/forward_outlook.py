"""
③ 行情更新后的自动前向展望：K 线质量审计 +「未来 H 日」方向演示 + 到期自动结算。

## 功能作用

在 `POST /ingest/update`（③ 批量拉日线）成功后，本模块对成功更新的标的**自动**：

1. **审计**本地 K 线质量（根数、末根成交量、与③返回收盘偏差、日期间隔等）；
2. **登记**基于末根及此前历史的 H 日方向演示预测（默认 H=3），写入 `forward_outlook` 表；
3. **结算**已到期的 pending 记录：当库内已有 `signal_trade_date + H` 个交易日收盘时，
   计算实际涨跌并与预测对比，状态变为 `settled`。

用户无需手填复盘；`GET /forward-outlook` 可查询 pending / settled 列表（③ 更新后自动登记）。

## 预测方法（末根投票）

复用 `forecast_validate` 的特征与模型逻辑，对**最后一根有效 K 线**综合：

| 来源 | 说明 |
|------|------|
| `logistic` | 全历史训练 Logistic，对末根特征预测 prob_up |
| `rule_trend` | ret20>0 且 MA20 斜率>0 → 看多 |
| `dual_ma` | 5/10 日双均线 → 看多/空 |
| `signal` | `compute_signal` 的 trend（bullish 计多票；③ 自动 sync 时跳过以免联网） |

多数票决定 `predicted_up`；数据不足时 `predicted_up=None`。

## 生命周期

```
③ ingest 成功 → sync_after_ingest → sync_symbol_outlook (pending)
                                              ↓
                         库内 K 线增至 signal_date + H 日
                                              ↓
                    _settle_row → settled（actual_return_pct / 命中与否）
```

同一 `(symbol, signal_trade_date, horizon)` 仅一条记录；**已 settled 不再覆盖**预测与结算结果。

## 对外接口

| 函数 | 用途 |
|------|------|
| `sync_symbol_outlook` | 单只：审计 + 预测 + upsert + 尝试结算 |
| `sync_after_ingest` | ③ 批量成功后逐只 sync（`main` 自动调用） |
| `settle_all_pending` | 扫描全部 pending 并结算到期项 |
| `row_to_dict` | ORM 行 → API / 控制台 dict |
| `isfinite_pair` | 浮点有限性判断（审计用，可被测试引用） |

## 非投资建议

展望为算法演示与数据质量跟踪，短周期方向噪声大，**不构成投资建议**。
与 `forecast_validate` 的 walk-forward 回测用途不同：本案是「登记一次、等待 H 日后验收」。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.db_models import ForwardOutlookRow, WatchlistRow
from app.db_session import session_scope
from app.forecast_validate import (
    FEATURE_NAMES,
    _apply_standardize,
    _dual_ma_signal_series,
    _fit_logistic,
    _sigmoid,
    _standardize,
    build_feature_matrix,
)
from app.ingest import fetch_stock_name, fetch_stock_names_map, load_bars_from_db, normalize_symbol
from app.signals import compute_signal

logger = logging.getLogger(__name__)

# --- 模块常量 ---

DEFAULT_HORIZON = 3
"""默认展望跨度：未来 H 个交易日（与 ingest 后 auto-sync 一致）。"""

MIN_BARS_FOR_OUTLOOK = 30
"""K 线少于该根数时审计报 issue；Logistic 需更多样本见 MIN_TRAIN_ROWS。"""

MIN_TRAIN_ROWS = 80
"""末根 Logistic 预测至少需要的有效训练行数。"""


# --- 工具 ---


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def isfinite_pair(a: float, b: float) -> bool:
    """两浮点均为有限值时返回 True（用于收盘偏差审计）。"""
    return bool(np.isfinite(a) and np.isfinite(b))


# --- K 线质量审计 ---


def _audit_bars(df: pd.DataFrame, *, ingest_last_close: float | None = None) -> dict[str, Any]:
    """
    审计本地 K 线是否适合生成信号/展望。

    检查项：是否为空、根数、末根成交量、与③ ingest 返回收盘偏差（>2%）、
    自然日期间隔是否超过 10 天（可能缺日）。

    返回 dict 含 ok、bars_count、first/last_trade_date、last_close、issues 列表。
    """
    issues: list[str] = []
    if df.empty:
        return {
            "ok": False,
            "bars_count": 0,
            "first_trade_date": None,
            "last_trade_date": None,
            "last_close": None,
            "issues": ["本地无 K 线"],
        }
    n = len(df)
    last_td = str(df["trade_date"].iloc[-1])
    first_td = str(df["trade_date"].iloc[0])
    last_close = float(df["close"].iloc[-1])
    if n < MIN_BARS_FOR_OUTLOOK:
        issues.append(f"K 线仅 {n} 根，信号/展望建议至少 {MIN_BARS_FOR_OUTLOOK} 根")
    vol_last = df["volume"].iloc[-1]
    try:
        if vol_last is None or float(vol_last) <= 0:
            issues.append("末根成交量为 0 或缺失")
    except (TypeError, ValueError):
        issues.append("末根成交量异常")
    if ingest_last_close is not None and isfinite_pair(last_close, ingest_last_close):
        rel = abs(last_close - ingest_last_close) / (abs(ingest_last_close) + 1e-9)
        if rel > 0.02:
            issues.append(
                f"③返回收盘 {ingest_last_close:.4f} 与库内末根 {last_close:.4f} 偏差 {rel*100:.2f}%"
            )
    dates = pd.to_datetime(df["trade_date"].astype(str), errors="coerce")
    if len(dates) > 5:
        gaps = dates.diff().dt.days
        big = gaps[gaps > 10]
        if len(big) > 0:
            issues.append(f"存在超过 10 个自然日的日期间隔（{len(big)} 处），请核对是否缺日")
    return {
        "ok": len(issues) == 0,
        "bars_count": n,
        "first_trade_date": first_td,
        "last_trade_date": last_td,
        "last_close": round(last_close, 4),
        "issues": issues,
    }


# --- 方向预测（末根） ---


def _predict_last_bar(
    sym: str,
    df: pd.DataFrame,
    horizon: int,
    *,
    skip_compute_signal: bool = False,
) -> dict[str, Any]:
    """
    基于末根及此前历史，给出 H 日方向演示预测（非投资建议）。

    参数:
        skip_compute_signal: True 时跳过 ``compute_signal``，避免 ③ 自动收尾里
            ``skip_bars=True`` 场景下 K 线不足触发 ``incremental_refresh`` 联网。
            跳过后 ``signal.trend`` 不参与投票，仅 Logistic/规则/双均线生效。

    返回:
        含 signal_trade_date、methods（各子预测）、predicted_up、summary_zh 的 dict。
    """
    sym_date = str(df["trade_date"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    out: dict[str, Any] = {
        "signal_trade_date": sym_date,
        "signal_close": round(last_close, 4),
        "horizon": horizon,
        "methods": {},
    }
    if skip_compute_signal:
        sig = None
    else:
        try:
            sig = compute_signal(sym)
        except Exception:
            sig = None
    feat, y_up, _fwd = build_feature_matrix(df, horizon)
    train_mask = feat.notna().all(axis=1) & y_up.notna()
    if int(train_mask.sum()) < MIN_TRAIN_ROWS:
        out["methods"]["note"] = f"可训练样本不足（{int(train_mask.sum())} < {MIN_TRAIN_ROWS}），仅使用信号/规则"
    else:
        X_train = feat.loc[train_mask].to_numpy(dtype=np.float64)
        y_train = y_up.loc[train_mask].to_numpy(dtype=np.int64)
        Xs, mu, std = _standardize(X_train)
        w = _fit_logistic(Xs, y_train)
        use_idx = len(df) - 1
        feat_row = feat.iloc[use_idx]
        if feat_row.isna().any():
            complete = feat.notna().all(axis=1)
            if not complete.any():
                raise ValueError("末根特征无效")
            use_idx = int(np.where(complete.to_numpy())[0][-1])
            feat_row = feat.iloc[use_idx]
            out["signal_trade_date"] = str(df["trade_date"].iloc[use_idx])
            out["signal_close"] = round(float(df["close"].iloc[use_idx]), 4)
        x_last = feat_row.to_numpy(dtype=np.float64)
        xk = _apply_standardize(x_last, mu, std)
        z = float(w[0] + np.dot(w[1:], xk))
        p = float(_sigmoid(np.array([z]))[0])
        out["methods"]["logistic"] = {
            "prob_up": round(p, 4),
            "pred_up": p >= 0.5,
        }
    i_ret20 = FEATURE_NAMES.index("ret20")
    i_slope = FEATURE_NAMES.index("ma20_slope")
    use_idx_rule = len(df) - 1
    if feat.iloc[use_idx_rule].isna().any():
        complete_r = feat.notna().all(axis=1)
        if complete_r.any():
            use_idx_rule = int(np.where(complete_r.to_numpy())[0][-1])
    if feat.iloc[use_idx_rule].notna().all():
        row_last = feat.iloc[use_idx_rule].to_numpy(dtype=np.float64)
        rule_up = bool(row_last[i_ret20] > 0 and row_last[i_slope] > 0)
        out["methods"]["rule_trend"] = {"pred_up": rule_up}
    closes = df["close"].astype(float).to_numpy()
    ma_sig = _dual_ma_signal_series(closes, 5, 10)
    out["methods"]["dual_ma"] = {"pred_up": bool(ma_sig[-1] > 0)}
    if sig is not None:
        out["methods"]["signal"] = {
            "trend": sig.trend,
            "strength": sig.strength,
            "score": sig.buy_suitability_score,
        }
    # 多数票合成 predicted_up（signal.trend bullish 加票，bearish 仅增分母）
    votes_up = 0
    votes = 0
    for key in ("logistic", "rule_trend", "dual_ma"):
        m = out["methods"].get(key)
        if m and "pred_up" in m:
            votes += 1
            if m["pred_up"]:
                votes_up += 1
    if sig is not None and sig.trend == "bullish":
        votes_up += 1
        votes += 1
    elif sig is not None and sig.trend == "bearish":
        votes += 1
    pred_up = votes_up > votes / 2 if votes else None
    out["predicted_up"] = pred_up
    if pred_up is None:
        out["summary_zh"] = f"数据不足，无法在 {sym_date} 给出 H={horizon} 方向展望"
    elif pred_up:
        out["summary_zh"] = f"自 {out['signal_trade_date']} 起约 {horizon} 个交易日：算法演示偏多（非实盘建议）"
    else:
        out["summary_zh"] = f"自 {out['signal_trade_date']} 起约 {horizon} 个交易日：算法演示偏空或震荡（非实盘建议）"
    return out


def _predict_for_symbol(sym: str, horizon: int) -> dict[str, Any]:
    """读库并预测（含 compute_signal；供手动 sync 等场景）。"""
    df = load_bars_from_db(sym)
    if df.empty:
        raise ValueError("本地无 K 线")
    return _predict_last_bar(sym, df, horizon)


# --- 到期结算 ---


def _trade_date_index(dates: list[str], td: str) -> int | None:
    try:
        return dates.index(td)
    except ValueError:
        return None


def _settle_row(row: ForwardOutlookRow, df: pd.DataFrame) -> bool:
    """
    若 pending 且库内已有 signal 日 + H 根收盘，则写入实际收益并标记 settled。

    返回 True 表示本次完成了结算。
    """
    if row.status == "settled":
        return False
    dates = [str(x) for x in df["trade_date"].tolist()]
    idx = _trade_date_index(dates, row.signal_trade_date)
    if idx is None:
        return False
    h = int(row.horizon or DEFAULT_HORIZON)
    if idx + h >= len(dates):
        return False
    c0 = float(df["close"].iloc[idx])
    c1 = float(df["close"].iloc[idx + h])
    if c0 <= 0:
        return False
    ret = (c1 - c0) / c0
    row.actual_return_pct = round(ret * 100.0, 4)
    row.actual_up = ret > 0
    row.status = "settled"
    row.settled_at = _utc_now_iso()
    row.updated_at = row.settled_at
    hit = row.predicted_up is not None and bool(row.predicted_up) == bool(row.actual_up)
    row.outlook_summary_zh = (
        f"{row.signal_trade_date} 起 {h} 日后收益 {row.actual_return_pct:+.2f}% · "
        f"实际{'涨' if row.actual_up else '跌/平'} · "
        f"预测{'命中' if hit else '未中' if row.predicted_up is not None else '—'}"
    )
    return True


# --- 名称解析 ---


def _resolve_stock_name(sym: str, preferred: str | None = None) -> str:
    """优先用传入简称，否则拉东财名称。"""
    nm = (preferred or "").strip()
    if nm and nm.lower() != "nan":
        return nm[:64]
    return (fetch_stock_name(sym) or "")[:64]


def _stock_names_for_symbols(symbols: list[str], session) -> dict[str, str]:
    """自选简称优先，缺失则批量拉东财简称（列表 API 展示用）。"""
    syms = list(dict.fromkeys(symbols))
    out: dict[str, str] = {}
    if not syms:
        return out
    wl_rows = session.execute(select(WatchlistRow).where(WatchlistRow.symbol.in_(syms))).scalars().all()
    for r in wl_rows:
        nm = (r.name or "").strip()
        if nm and nm.lower() != "nan":
            out[r.symbol] = nm[:64]
    missing = [s for s in syms if s not in out]
    if missing:
        try:
            for sym, nm in fetch_stock_names_map(missing).items():
                if nm:
                    out[sym] = str(nm).strip()[:64]
        except Exception as e:
            logger.debug("fetch_stock_names_map for outlook: %s", e)
    return out


# --- 对外接口 ---


def sync_symbol_outlook(
    sym: str,
    *,
    horizon: int = DEFAULT_HORIZON,
    ingest_last_close: float | None = None,
    ingest_last_trade_date: str | None = None,
    stock_name: str | None = None,
) -> ForwardOutlookRow | None:
    """
    为单标的写入/更新 pending 展望，并尝试结算该标的到期记录。

    唯一键：(symbol, signal_trade_date, horizon)。已 settled 的记录不覆盖预测与结算，
    仅补全 stock_name。数据质量有问题时在 summary 前加「【数据待核对】」前缀。

    ③ 自动 sync 时 `skip_compute_signal=True`，避免 skip_bars 路径下联网。
    """
    sym = normalize_symbol(sym)
    h = max(1, min(60, int(horizon)))
    df = load_bars_from_db(sym)
    audit = _audit_bars(df, ingest_last_close=ingest_last_close)
    now = _utc_now_iso()
    if df.empty:
        return None
    try:
        pred = _predict_last_bar(sym, df, h, skip_compute_signal=True)
    except Exception as e:
        logger.debug("forward outlook predict failed %s: %s", sym, e)
        pred = {
            "signal_trade_date": audit.get("last_trade_date") or ingest_last_trade_date or "",
            "summary_zh": f"无法生成展望：{e}",
            "predicted_up": None,
            "methods": {},
        }
    signal_td = str(pred.get("signal_trade_date") or audit.get("last_trade_date") or "")
    if not signal_td:
        return None
    predicted_up = pred.get("predicted_up")
    summary = str(pred.get("summary_zh") or "")
    if not audit["ok"]:
        summary = "【数据待核对】" + "；".join(audit["issues"][:3]) + "。" + summary
    outlook_json = json.dumps({"audit": audit, "prediction": pred}, ensure_ascii=False)
    data_quality_json = json.dumps(audit, ensure_ascii=False)
    with session_scope() as s:
        existing = s.execute(
            select(ForwardOutlookRow).where(
                ForwardOutlookRow.symbol == sym,
                ForwardOutlookRow.signal_trade_date == signal_td,
                ForwardOutlookRow.horizon == h,
            )
        ).scalar_one_or_none()
        if existing:
            row = existing
            row.updated_at = now
            # 已结算：保留历史展望与结算结果，仅补全名称（③ 再次更新同信号日也不覆盖）
            if row.status == "settled":
                row.stock_name = _resolve_stock_name(sym, stock_name)
                s.flush()
                s.refresh(row)
                return row
        else:
            row = ForwardOutlookRow(
                symbol=sym,
                horizon=h,
                signal_trade_date=signal_td,
                created_at=now,
                updated_at=now,
                status="pending",
            )
            s.add(row)
        row.stock_name = _resolve_stock_name(sym, stock_name)
        row.bars_count = int(audit.get("bars_count") or 0)
        row.signal_close = float(pred.get("signal_close") or audit.get("last_close") or 0)
        row.data_quality_json = data_quality_json
        row.outlook_json = outlook_json
        row.predicted_up = predicted_up
        row.outlook_summary_zh = summary[:500]
        if row.status != "settled":
            row.status = "pending"
        s.flush()
        _settle_row(row, df)
        s.refresh(row)
        return row


def settle_all_pending() -> int:
    """
    扫描 `forward_outlook` 中全部 pending，对 K 线已够 H 日的记录执行结算。

    返回本次新结算的条数。`GET /forward-outlook` 列表前也会调用。
    """
    n = 0
    with session_scope() as s:
        rows = s.execute(
            select(ForwardOutlookRow).where(ForwardOutlookRow.status == "pending")
        ).scalars().all()
        for row in rows:
            df = load_bars_from_db(row.symbol)
            if _settle_row(row, df):
                n += 1
    return n


def sync_after_ingest(
    symbols: list[str],
    *,
    horizon: int = DEFAULT_HORIZON,
    ingest_meta_by_sym: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    ③ 批量更新成功后：逐只登记展望并尝试结算。

    `ingest_meta_by_sym` 可传每只的 last_close、last_trade_date、watchlist_name，
    用于数据质量审计与简称。结束后额外 `settle_all_pending` 处理其它到期 pending。

    由 `main` 在 ingest batch finish 之后调用；结果写入响应 `forward_outlook_sync`。
    """
    meta = ingest_meta_by_sym or {}
    created = 0
    failed: list[str] = []
    for sym in symbols:
        try:
            m = meta.get(sym) or {}
            lc = m.get("last_close")
            ingest_lc = float(lc) if lc is not None and str(lc) != "" else None
            row = sync_symbol_outlook(
                sym,
                horizon=horizon,
                ingest_last_close=ingest_lc,
                ingest_last_trade_date=m.get("last_trade_date"),
                stock_name=m.get("watchlist_name"),
            )
            if row is not None:
                created += 1
            else:
                failed.append(sym)
        except Exception as e:
            logger.warning("forward outlook sync %s: %s", sym, e)
            failed.append(sym)
    extra_settled = settle_all_pending()
    return {
        "synced": created,
        "failed_symbols": failed,
        "extra_settled": extra_settled,
        "horizon": horizon,
    }


def row_to_dict(row: ForwardOutlookRow, *, stock_name: str | None = None) -> dict[str, Any]:
    """将 `ForwardOutlookRow` 转为 `ForwardOutlookOut` 兼容的 dict。"""
    audit = None
    if row.data_quality_json:
        try:
            audit = json.loads(row.data_quality_json)
        except json.JSONDecodeError:
            audit = None
    nm = (stock_name or row.stock_name or "").strip()
    return {
        "id": row.id,
        "symbol": row.symbol,
        "stock_name": nm or None,
        "horizon": row.horizon,
        "signal_trade_date": row.signal_trade_date,
        "signal_close": row.signal_close,
        "bars_count": row.bars_count,
        "data_quality": audit,
        "data_quality_ok": bool(audit.get("ok")) if isinstance(audit, dict) else None,
        "predicted_up": row.predicted_up,
        "outlook_summary_zh": row.outlook_summary_zh,
        "status": row.status,
        "actual_return_pct": row.actual_return_pct,
        "actual_up": row.actual_up,
        "settled_at": row.settled_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
