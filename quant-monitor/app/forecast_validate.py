"""
可验证的「未来 H 日涨跌方向」walk-forward 评估（仅历史日线，无前视）。

- 特征：仅用当日及以前收盘价/量构造（与 signals 同源风格：ret5/20、MA20 斜率、量比、距 60 日高回撤、20 日实现波动）。
- 标签：t 日收盘至 t+H 日收盘的累计收益是否 > 0（H 为交易日跨度）。
- 方法对比：双均线多空（教材常见）、固定趋势规则、walk-forward Logistic（NumPy）、因果多数类基线。
- 扩展因子：`fundamental_snapshots` 仅为每标的**最新快照**，无法对齐历史每个交易日，故**不**并入 walk-forward 特征（见响应 `fundamentals_backtest`）。

与常见「pandas/numpy → 回测验证 → 仿真/实盘」入门路径对齐；非投资建议。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.fundamentals import load_fundamental_panel_from_db
from app.ingest import load_bars_from_db, normalize_symbol

FEATURE_NAMES = ("ret5", "ret20", "ma20_slope", "vol_ratio", "dd_from_high", "vol_sigma")


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _fit_logistic(
    X: np.ndarray,
    y: np.ndarray,
    *,
    n_iter: int = 600,
    lr: float = 0.2,
    l2: float = 1e-2,
) -> np.ndarray:
    """X: (n, d) 已标准化；返回 w 长度 d+1，含截距。"""
    n, d = X.shape
    y = y.astype(np.float64)
    if n < 15 or d < 1:
        return np.zeros(d + 1)
    if float(y.min()) == float(y.max()):
        w = np.zeros(d + 1)
        w[0] = 8.0 if y[0] >= 0.5 else -8.0
        return w
    Xb = np.c_[np.ones(n), X]
    w = np.zeros(d + 1)
    for _ in range(n_iter):
        p = _sigmoid(Xb @ w)
        grad = Xb.T @ (p - y) / n
        grad[1:] += l2 * w[1:]
        w -= lr * grad
    return w


def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    std = X.std(axis=0) + 1e-9
    return (X - mu) / std, mu, std


def _apply_standardize(x: np.ndarray, mu: np.ndarray, std: np.ndarray) -> np.ndarray:
    return (x - mu) / (std + 1e-9)


def _auc_binary(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """秩 AUC；全同标签时返回 None。"""
    y_true = np.asarray(y_true, dtype=int)
    scores = np.asarray(scores, dtype=float)
    pos = y_true == 1
    neg = y_true == 0
    n_pos = int(pos.sum())
    n_neg = int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    sum_ranks_pos = float(ranks[pos].sum())
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


def build_feature_matrix(df: pd.DataFrame, horizon: int) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """
    与 df 行对齐的特征与标签。

    返回 (features, y_up, fwd_ret)；无效行在调用方 dropna。
    """
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    low = df["low"].astype(float)
    v = df["volume"].astype(float)

    ma20 = c.rolling(20, min_periods=20).mean()
    ma20_lag5 = ma20.shift(5)
    ma20_slope = (ma20 - ma20_lag5) / (np.abs(ma20_lag5) + 1e-9)

    ret5 = c / c.shift(5) - 1.0
    ret20 = c / c.shift(20) - 1.0
    vol20 = v.rolling(20, min_periods=20).mean()
    vol_ratio = v / (vol20 + 1e-9)
    high60 = h.rolling(60, min_periods=60).max()
    dd_from_high = (c - high60) / (np.abs(high60) + 1e-9)
    r = c.pct_change()
    vol_sigma = r.rolling(20, min_periods=20).std(ddof=0)

    fwd_ret = c.shift(-horizon) / c - 1.0
    y_up = (fwd_ret > 0).astype(int)

    feat = pd.DataFrame(
        {
            "ret5": ret5,
            "ret20": ret20,
            "ma20_slope": ma20_slope,
            "vol_ratio": vol_ratio,
            "dd_from_high": dd_from_high,
            "vol_sigma": vol_sigma,
        }
    )
    return feat, y_up, fwd_ret


def _metrics_block(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    fwd: np.ndarray,
    scores: np.ndarray | None = None,
) -> dict[str, Any]:
    y_true = np.asarray(y_true, dtype=int)
    y_pred = np.asarray(y_pred, dtype=int)
    fwd = np.asarray(fwd, dtype=float)
    n = len(y_true)
    if n == 0:
        return {
            "n_oos": 0,
            "accuracy": 0.0,
            "balanced_accuracy": 0.0,
            "precision_up": None,
            "recall_up": None,
            "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
            "mean_forward_return_pred_up": None,
            "mean_forward_return_pred_down": None,
            "auc_roc": None,
        }

    acc = float((y_true == y_pred).mean())
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    sens = tp / (tp + fn) if (tp + fn) else 0.0
    spec = tn / (tn + fp) if (tn + fp) else 0.0
    bal = 0.5 * (sens + spec)
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = sens

    up_mask = y_pred == 1
    down_mask = y_pred == 0
    m_up = float(fwd[up_mask].mean()) if up_mask.any() else None
    m_dn = float(fwd[down_mask].mean()) if down_mask.any() else None

    auc = _auc_binary(y_true, scores) if scores is not None else None

    return {
        "n_oos": n,
        "accuracy": acc,
        "balanced_accuracy": float(bal),
        "precision_up": float(prec) if prec is not None else None,
        "recall_up": float(rec),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "mean_forward_return_pred_up": m_up,
        "mean_forward_return_pred_down": m_dn,
        "auc_roc": auc,
    }


def _trades_from_predictions(
    dates: np.ndarray,
    closes: np.ndarray,
    preds: np.ndarray,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    预测 1=未来 H 日看涨、0=看跌。示意规则：
    空仓遇 1 → 当日收盘「买入点」；持仓遇 0 → 当日收盘「卖出点」。
    返回 (completed_trades, open_leg_or_none)。
    """
    dates = np.asarray(dates)
    closes = np.asarray(closes, dtype=np.float64)
    preds = np.asarray(preds, dtype=int)
    m = len(preds)
    if m == 0 or len(closes) != m or len(dates) != m:
        return [], None

    trades: list[dict[str, Any]] = []
    state = 0
    buy_i: int | None = None

    for i in range(m):
        p = int(preds[i])
        if state == 0 and p == 1:
            state = 1
            buy_i = i
        elif state == 1 and p == 0:
            if buy_i is not None:
                bi, si = buy_i, i
                rb = float(closes[bi])
                rs = float(closes[si])
                trades.append(
                    {
                        "buy_date": str(dates[bi]),
                        "sell_date": str(dates[si]),
                        "buy_close": round(rb, 4),
                        "sell_close": round(rs, 4),
                        "return_pct": round((rs / rb - 1.0) * 100.0, 4),
                    }
                )
            state = 0
            buy_i = None

    open_leg: dict[str, Any] | None = None
    if state == 1 and buy_i is not None:
        rb = float(closes[buy_i])
        open_leg = {
            "buy_date": str(dates[buy_i]),
            "buy_close": round(rb, 4),
            "note": "样本外序列末尾仍为「看多」，尚未出现对应的卖出日（示意未平仓）",
        }

    return trades, open_leg


def _trade_summary(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not trades:
        return None
    rets = [float(t["return_pct"]) for t in trades]
    wins = sum(1 for r in rets if r > 0)
    return {
        "completed_trades": len(trades),
        "win_rate": round(wins / len(trades), 4),
        "avg_return_pct": round(float(np.mean(rets)), 4),
        "total_simple_return_pct": round(float(np.sum(rets)), 4),
    }


def _dual_ma_signal_series(closes: np.ndarray, short: int, long: int) -> np.ndarray:
    """
    每个收盘时点：短均线 > 长均线 → 1（看多），否则 0。
    仅用当日及以前收盘，与经典双均线教材一致；不足长均线窗口处为 0。
    """
    c = np.asarray(closes, dtype=np.float64)
    n = len(c)
    if n == 0:
        return np.array([], dtype=int)
    s = pd.Series(c).rolling(short, min_periods=short).mean()
    lg = pd.Series(c).rolling(long, min_periods=long).mean()
    out = np.zeros(n, dtype=int)
    mask = s.notna() & lg.notna()
    if mask.any():
        bull = (s > lg).to_numpy()
        m = mask.to_numpy()
        out[m] = bull[m].astype(int)
    return out


def _attach_trades_to_method(
    base: dict[str, Any],
    dates_oos: np.ndarray,
    closes_oos: np.ndarray,
    preds: np.ndarray,
    *,
    max_trades: int,
) -> dict[str, Any]:
    all_trades, open_leg = _trades_from_predictions(dates_oos, closes_oos, preds)
    summary = _trade_summary(all_trades)
    tail = all_trades[-max_trades:] if len(all_trades) > max_trades else all_trades
    out = {**base, "trade_summary": summary, "trades": tail, "open_leg": open_leg}
    return out


def run_forecast_validate(
    symbol: str,
    *,
    horizon: int = 5,
    min_train_rows: int = 120,
    retrain_every: int = 20,
    trade_limit: int = 25,
    ma_short: int = 5,
    ma_long: int = 10,
) -> dict[str, Any]:
    """
    对单标的做 walk-forward OOS 评估；数据仅来自本地 bars。

    horizon：预测未来 H 个交易日累计涨跌方向。
    min_train_rows：从该样本索引起进入 OOS（前段仅用于训练逻辑回归）。
    retrain_every：每隔多少根 OOS 步长重训一次 logistic（中间沿用上一权重）。
    ma_short / ma_long：双均线策略周期（短 < 长），与常见教材 5/10 类似。
    """
    sym = normalize_symbol(symbol)
    if horizon < 1 or horizon > 60:
        raise ValueError("horizon 应在 1～60 个交易日之间")
    if min_train_rows < 80:
        raise ValueError("min_train_rows 建议至少 80，否则特征不稳定")
    if retrain_every < 1:
        raise ValueError("retrain_every 至少为 1")
    if trade_limit < 1 or trade_limit > 200:
        raise ValueError("trade_limit 应在 1～200 之间")
    if ma_short < 2 or ma_long < 3:
        raise ValueError("ma_short 至少 2，ma_long 至少 3")
    if ma_short >= ma_long:
        raise ValueError("双均线要求 ma_short < ma_long")
    if ma_long > 250:
        raise ValueError("ma_long 过大（上限 250）")
    need_warm = ma_long + 5
    if min_train_rows < need_warm:
        raise ValueError(f"min_train_rows 至少应为 ma_long+5 = {need_warm}，以便双均线与特征同时有效")

    df = load_bars_from_db(sym)
    if df.empty:
        raise ValueError("本地无 K 线，请先 POST /ingest/update")
    need_len = min_train_rows + horizon + 65
    if len(df) < need_len:
        raise ValueError(
            f"K 线过短（当前 {len(df)}），建议至少约 {need_len} 根以便 walk-forward（含 {horizon} 日持有期与 60 日高点特征）"
        )

    feat, y_up, fwd_ret = build_feature_matrix(df, horizon)
    valid = feat.notna().all(axis=1) & y_up.notna() & fwd_ret.notna()
    feat = feat.loc[valid]
    y_up = y_up.loc[valid]
    fwd_ret = fwd_ret.loc[valid]
    dates = df.loc[valid.index, "trade_date"].astype(str)

    X = feat.to_numpy(dtype=np.float64)
    y = y_up.to_numpy(dtype=np.int64)
    fwd = fwd_ret.to_numpy(dtype=np.float64)
    n = len(y)

    if min_train_rows >= n - 5:
        raise ValueError("有效样本不足，无法留出 OOS 段")

    oos_start = min_train_rows
    oos_idx = np.arange(oos_start, n)

    # --- 多数类基线（每个时点用历史标签的众数预测当日标签，严格因果）---
    maj_pred = np.zeros(len(oos_idx), dtype=int)
    for j, k in enumerate(oos_idx):
        hist = y[:k]
        maj_pred[j] = 1 if hist.mean() >= 0.5 else 0

    # --- 规则：ret20>0 且 ma20 斜率>0 ---
    rule_pred = np.zeros(len(oos_idx), dtype=int)
    i_ret20 = FEATURE_NAMES.index("ret20")
    i_slope = FEATURE_NAMES.index("ma20_slope")
    for j, k in enumerate(oos_idx):
        row = X[k]
        rule_pred[j] = 1 if (row[i_ret20] > 0 and row[i_slope] > 0) else 0

    # --- Logistic walk-forward ---
    log_pred = np.zeros(len(oos_idx), dtype=int)
    log_scores = np.zeros(len(oos_idx), dtype=float)
    state: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    last_w: np.ndarray | None = None
    for j, k in enumerate(oos_idx):
        need_fit = state is None or (j % retrain_every == 0)
        if need_fit:
            Xt = X[:k]
            yt = y[:k]
            Xs, mu, std = _standardize(Xt)
            last_w = _fit_logistic(Xs, yt)
            state = (last_w, mu, std)
        assert state is not None
        w, mu, std = state
        xk = _apply_standardize(X[k], mu, std)
        z = float(w[0] + np.dot(w[1:], xk))
        p = float(_sigmoid(np.array([z]))[0])
        log_scores[j] = p
        log_pred[j] = 1 if p >= 0.5 else 0

    y_oos = y[oos_idx]
    fwd_oos = fwd[oos_idx]
    dates_oos = dates.iloc[oos_idx].to_numpy()
    close_series = df.loc[valid.index, "close"].astype(float)
    closes_oos = close_series.iloc[oos_idx].to_numpy(dtype=np.float64)
    close_all = close_series.to_numpy(dtype=np.float64)
    ma_full = _dual_ma_signal_series(close_all, ma_short, ma_long)
    ma_pred = ma_full[oos_idx]

    baseline_acc = float(max(y_oos.mean(), 1.0 - y_oos.mean()))

    how_to_read = (
        "【准确率】在样本外每个交易日，先产生一个「多空信号」（双均线 / 规则 / Logistic），"
        "再单独用同一套日期去检验「往后 H 个交易日累计涨跌是否为正」是否猜对；可与「无脑猜多数类」对比。"
        "【双均线】收盘时若短均线 > 长均线则视为看多，否则看空（与许多入门教程中的均线排列一致）。"
        "【买入点 / 卖出点】当看多/看空信号发生切换时：转多看多记买入收盘，转空记卖出收盘；区间涨跌%为两收盘价差，非委托价，未计手续费/滑点。"
    )

    m_ma = {
        "method": "dual_ma_cross",
        "description": f"双均线：{ma_short} 日收盘均线 > {ma_long} 日收盘均线 → 当日信号看多（教材常见金叉/多头排列思路的日频版）",
        **_metrics_block(y_oos, ma_pred, fwd_oos, scores=None),
    }
    m_maj = {
        "method": "majority_causal",
        "description": "每个预测日仅根据该日之前的历史涨跌标签取众数（因果多数类）",
        **_metrics_block(y_oos, maj_pred, fwd_oos, scores=None),
    }
    m_rule = {
        "method": "rule_trend",
        "description": "近 20 日涨幅>0 且 20 日均线相对 5 日前向上 → 预测未来 H 日看涨",
        **_metrics_block(y_oos, rule_pred, fwd_oos, scores=None),
    }
    m_log = {
        "method": "logistic_walkforward",
        "description": f"用历史特征训练 Logistic，每 {retrain_every} 步用更长的过去数据重训一次",
        **_metrics_block(y_oos, log_pred, fwd_oos, scores=log_scores),
    }

    methods = [
        _attach_trades_to_method(m_ma, dates_oos, closes_oos, ma_pred, max_trades=trade_limit),
        _attach_trades_to_method(m_log, dates_oos, closes_oos, log_pred, max_trades=trade_limit),
        _attach_trades_to_method(m_rule, dates_oos, closes_oos, rule_pred, max_trades=trade_limit),
        _attach_trades_to_method(m_maj, dates_oos, closes_oos, maj_pred, max_trades=trade_limit),
    ]

    fp = load_fundamental_panel_from_db(sym)
    snapshot_cached = fp is not None
    if snapshot_cached:
        fb_note = (
            "【扩展因子与本案回测】本地**已有**该标的扩展因子快照，但本 walk-forward **仍未**把估值/财务等并入 Logistic 的逐日特征。"
            "原因：`fundamental_snapshots` 仅存**最新一行**，缺少历史上每个交易日「当时可知」的基本面面板；"
            "若用今日快照去解释过去几年每一天，会产生前视或伪精度。"
            "【是否因此更不准】不一定。短周期 H 日方向本身噪声大，基本面未必提升命中率；"
            "「④ 查看信号」里的合成得分使用当前快照，与本案**历史**回测是不同用途，请勿混为一谈。"
        )
    else:
        fb_note = (
            "【扩展因子】当前标的**尚未**在本地缓存扩展因子（请先对自选执行 POST /ingest/fundamentals）。"
            "本回测仍**有效**，只是特征为纯 K 线/技术面。"
            "【没有扩展因子 ≠ 回测算错】只表示未使用基本面；是否「更准」取决于任务与噪声，不是必然。"
            "要做带基本面的**严格历史回测**，需按财报报告期或公告日对齐并存储时间序列，当前库结构未支持。"
        )

    pedagogy = {
        "title": "与常见 Python 量化入门路线对照",
        "workflow_steps": [
            "形成策略假设（如双均线强弱、动量因子）",
            "用 pandas / numpy 在本地历史日线上计算信号（本服务使用 SQLite 存 K 线）",
            "样本外 walk-forward 检验：严格按时间推进、不使用未来数据",
            "扩展因子（估值/财务）需「当时可知」对齐后再做历史检验；当前快照仅用于最新信号，未混入本案回测",
            "若效果稳定，再到聚宽、米筐、优矿等平台做仿真与费用模型（本接口未替代平台回测）",
        ],
        "stack_note": "数据：pandas DataFrame；数值：numpy；分类模型：自实现 Logistic 梯度下降。与公开笔记中「NumPy + pandas + 回测」层次一致，体量更小、便于本地实验。",
        "reading": {
            "title": "读书笔记：python 数据分析与量化交易（游小刀 · 博客园）",
            "url": "https://www.cnblogs.com/yxiaodao/p/10732824.html",
            "anchor_hint": "文中 NumPy / pandas / 双均线与回测框架等章节（如 #_label4 附近）",
        },
    }

    return {
        "symbol": sym,
        "horizon": horizon,
        "n_bars_db": int(len(df)),
        "n_valid_rows": int(n),
        "first_oos_trade_date": str(dates.iloc[oos_start]) if len(dates) else None,
        "last_oos_trade_date": str(dates.iloc[oos_idx[-1]]) if len(oos_idx) else None,
        "n_oos": int(len(oos_idx)),
        "min_train_rows": min_train_rows,
        "retrain_every": retrain_every,
        "target_definition": f"close[t+{horizon}]/close[t]-1 > 0（交易日）",
        "oos_positive_rate": float(y_oos.mean()),
        "baseline_always_majority_oos": baseline_acc,
        "feature_names": list(FEATURE_NAMES),
        "methods": methods,
        "disclaimer": "历史回测不等价未来表现；短线方向噪声大，本接口为方法论演示，不构成投资建议。",
        "how_to_read": how_to_read,
        "ui_focus_method": "dual_ma_cross",
        "pedagogy": pedagogy,
        "strategy_params": {
            "horizon": horizon,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "min_train_rows": min_train_rows,
            "retrain_every": retrain_every,
            "trade_limit": trade_limit,
        },
        "fundamentals_backtest": {
            "merged_into_walkforward": False,
            "snapshot_cached": snapshot_cached,
            "note": fb_note,
        },
    }
