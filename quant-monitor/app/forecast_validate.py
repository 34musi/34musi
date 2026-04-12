"""
可验证的「未来 H 日涨跌方向」walk-forward 评估（仅历史日线，无前视）。

- 特征：仅用当日及以前收盘价/量构造（与 signals 同源风格：ret5/20、MA20 斜率、量比、距 60 日高回撤、20 日实现波动）。
- 标签：t 日收盘至 t+H 日收盘的累计收益是否 > 0（H 为交易日跨度）。
- 方法对比：双均线多空（教材常见）、固定趋势规则、walk-forward Logistic（NumPy）、因果多数类基线。
- 扩展因子：`fundamental_snapshots` 仅为每标的**最新快照**，无法对齐历史每个交易日，故**不**并入 walk-forward 特征（见响应 `fundamentals_backtest`）。

与常见「pandas/numpy → 回测验证 → 仿真/实盘」入门路径对齐；非投资建议。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.fundamentals import load_fundamental_panel_from_db
from app.ingest import load_bars_for_forecast, normalize_symbol

FEATURE_NAMES = ("ret5", "ret20", "ma20_slope", "vol_ratio", "dd_from_high", "vol_sigma")
FORECAST_METHOD_KEYS = ("dual_ma_cross", "logistic_walkforward", "rule_trend", "majority_causal")
TRADING_DAYS_PER_YEAR = 252
DEFAULT_COMMISSION_BPS = 3.0
DEFAULT_SELL_TAX_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_INITIAL_CASH = 100000.0
DEFAULT_LOT_SIZE = 100
DEFAULT_MIN_COMMISSION_CNY = 5.0


def _bps_to_ratio(bps: float) -> float:
    return float(bps) / 10000.0


def _normalize_forecast_methods(requested: list[str] | None) -> list[str]:
    """返回要计算并输出的方法键名列表（保序去重）；None 表示四种全部。"""
    if requested is None:
        return list(FORECAST_METHOD_KEYS)
    allowed = set(FORECAST_METHOD_KEYS)
    out: list[str] = []
    for m in requested:
        if m not in allowed:
            raise ValueError(f"未知的方法 {m!r}，可选：{', '.join(FORECAST_METHOD_KEYS)}")
        if m not in out:
            out.append(m)
    if not out:
        raise ValueError("至少选择一种回测方法")
    return out


def _normalize_oos_bound(s: str | None, name: str) -> str | None:
    """样本外日期边界：YYYY-MM-DD；空串视为未指定。"""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    if len(t) != 10 or t[4] != "-" or t[7] != "-":
        raise ValueError(f"{name} 须为 YYYY-MM-DD")
    date.fromisoformat(t)
    return t


def _parse_live_as_of_date(s: str | None) -> date | None:
    """联网增量截止日；空则 None（由 ingest 侧用今天）。"""
    raw = _normalize_oos_bound(s, "live_as_of")
    return date.fromisoformat(raw) if raw else None


def _trade_cost_ratios(*, commission_bps: float, sell_tax_bps: float, slippage_bps: float) -> tuple[float, float]:
    buy_cost = _bps_to_ratio(commission_bps + slippage_bps)
    sell_cost = _bps_to_ratio(commission_bps + slippage_bps + sell_tax_bps)
    return buy_cost, sell_cost


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


def _max_drawdown_pct(returns: np.ndarray) -> float | None:
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return None
    equity = np.cumprod(1.0 + r)
    peaks = np.maximum.accumulate(equity)
    dd = equity / (peaks + 1e-12) - 1.0
    return round(float(np.min(dd)) * 100.0, 4)


def _annualized_return_pct(returns: np.ndarray) -> float | None:
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return None
    equity_end = float(np.prod(1.0 + r))
    if equity_end <= 0:
        return None
    return round((equity_end ** (TRADING_DAYS_PER_YEAR / max(1, r.size)) - 1.0) * 100.0, 4)


def _sharpe_ratio(returns: np.ndarray) -> float | None:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return None
    sigma = float(r.std(ddof=0))
    if sigma < 1e-12:
        return None
    return float(round(float(r.mean()) / sigma * np.sqrt(TRADING_DAYS_PER_YEAR), 4))


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


def _commission_fee(trade_value: float, *, commission_bps: float, min_commission_cny: float) -> float:
    if trade_value <= 0:
        return 0.0
    return max(float(min_commission_cny), float(trade_value) * _bps_to_ratio(commission_bps))


def _trades_from_predictions(
    dates: np.ndarray,
    opens: np.ndarray,
    closes: np.ndarray,
    preds: np.ndarray,
    *,
    initial_cash: float,
    lot_size: int,
    commission_bps: float,
    sell_tax_bps: float,
    slippage_bps: float,
    min_commission_cny: float,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    """
    预测 1=未来 H 日看涨、0=看跌。

    交易近似规则：
    - t 日收盘后根据当日可见数据产生信号；
    - 若信号要求调仓，则在 t+1 日开盘按开盘价+/-滑点成交；
    - 买入按 100 股整手、默认满仓；卖出一次性清仓；
    - 佣金/印花税/滑点都计入。
    """
    dates = np.asarray(dates)
    opens = np.asarray(opens, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)
    preds = np.asarray(preds, dtype=int)
    m = len(preds)
    if m == 0 or len(closes) != m or len(dates) != m or len(opens) != m:
        return [], None, [], {
            "completed_trades": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "total_simple_return_pct": None,
            "gross_return_pct": None,
            "total_net_return_pct": None,
            "compounded_return_pct": None,
            "annualized_return_pct": None,
            "max_drawdown_pct": None,
            "sharpe_ratio": None,
            "profit_factor": None,
            "avg_holding_days": None,
            "avg_win_return_pct": None,
            "avg_loss_return_pct": None,
            "total_cost_pct": None,
            "daily_compounded_return_pct": None,
            "final_nav": None,
            "ending_equity": None,
            "ending_cash": None,
            "ending_shares": 0,
            "total_fee_cny": None,
            "total_slippage_cny": None,
        }

    cash = float(initial_cash)
    shares = 0
    pending_signal: int | None = None
    pending_signal_date: str | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    current_leg: dict[str, Any] | None = None
    slip_ratio = _bps_to_ratio(slippage_bps)
    tax_ratio = _bps_to_ratio(sell_tax_bps)
    total_fee_cny = 0.0
    total_slippage_cny = 0.0

    for i in range(m):
        if pending_signal is not None:
            raw_open = float(opens[i])
            if pending_signal == 1 and shares == 0:
                exec_price = raw_open * (1.0 + slip_ratio)
                max_lots = int(cash // (exec_price * lot_size)) if exec_price > 0 else 0
                qty = 0
                buy_fee = 0.0
                trade_value = 0.0
                while max_lots > 0:
                    qty = max_lots * lot_size
                    trade_value = qty * exec_price
                    buy_fee = _commission_fee(
                        trade_value,
                        commission_bps=commission_bps,
                        min_commission_cny=min_commission_cny,
                    )
                    if trade_value + buy_fee <= cash + 1e-9:
                        break
                    max_lots -= 1
                if max_lots > 0 and qty > 0:
                    cash_before = cash
                    cash -= trade_value + buy_fee
                    shares = qty
                    total_fee_cny += buy_fee
                    total_slippage_cny += (exec_price - raw_open) * qty
                    current_leg = {
                        "buy_signal_date": pending_signal_date,
                        "buy_date": str(dates[i]),
                        "buy_close": round(exec_price, 4),
                        "buy_open_raw": round(raw_open, 4),
                        "shares": int(qty),
                        "buy_fee": round(buy_fee, 4),
                        "cash_before_buy": cash_before,
                        "cash_after_buy": cash,
                    }
            elif pending_signal == 0 and shares > 0:
                exec_price = raw_open * (1.0 - slip_ratio)
                qty = int(shares)
                trade_value = qty * exec_price
                sell_commission = _commission_fee(
                    trade_value,
                    commission_bps=commission_bps,
                    min_commission_cny=min_commission_cny,
                )
                sell_tax = trade_value * tax_ratio
                sell_fee = sell_commission + sell_tax
                cash_before = cash
                cash += trade_value - sell_fee
                total_fee_cny += sell_fee
                total_slippage_cny += (raw_open - exec_price) * qty
                if current_leg is not None:
                    buy_notional = float(current_leg["buy_close"]) * qty
                    gross_ret = exec_price / float(current_leg["buy_close"]) - 1.0
                    total_cost = float(current_leg["buy_fee"]) + sell_fee
                    net_ret = (trade_value - sell_fee) / (buy_notional + float(current_leg["buy_fee"])) - 1.0
                    trades.append(
                        {
                            "buy_signal_date": current_leg.get("buy_signal_date"),
                            "sell_signal_date": pending_signal_date,
                            "buy_date": current_leg["buy_date"],
                            "sell_date": str(dates[i]),
                            "buy_close": current_leg["buy_close"],
                            "sell_close": round(exec_price, 4),
                            "buy_open_raw": current_leg.get("buy_open_raw"),
                            "sell_open_raw": round(raw_open, 4),
                            "shares": qty,
                            "holding_days": int(i - np.where(dates == current_leg["buy_date"])[0][0]) if current_leg.get("buy_date") in dates else None,
                            "gross_return_pct": round(gross_ret * 100.0, 4),
                            "cost_pct": round(total_cost / max(1e-9, buy_notional) * 100.0, 4),
                            "net_return_pct": round(net_ret * 100.0, 4),
                            "return_pct": round(net_ret * 100.0, 4),
                            "buy_fee": round(float(current_leg["buy_fee"]), 4),
                            "sell_fee": round(sell_fee, 4),
                            "slippage_cost_cny": round(
                                ((float(current_leg["buy_close"]) - float(current_leg.get("buy_open_raw", current_leg["buy_close"]))) * qty)
                                + ((raw_open - exec_price) * qty),
                                4,
                            ),
                            "fee_total_cny": round(total_cost, 4),
                            "cash_before_buy": round(float(current_leg.get("cash_before_buy", 0.0)), 4),
                            "cash_after_buy": round(float(current_leg.get("cash_after_buy", 0.0)), 4),
                            "cash_after_sell": round(cash, 4),
                        }
                    )
                shares = 0
                current_leg = None
        market_value = shares * float(closes[i])
        equity = cash + market_value
        equity_curve.append(
            {
                "trade_date": str(dates[i]),
                "cash": round(cash, 4),
                "shares": int(shares),
                "market_value": round(market_value, 4),
                "equity": round(equity, 4),
                "nav": round(equity / initial_cash, 6),
            }
        )
        if i < m - 1:
            pending_signal = int(preds[i])
            pending_signal_date = str(dates[i])
        else:
            pending_signal = None
            pending_signal_date = None

    open_leg: dict[str, Any] | None = None
    if shares > 0 and current_leg is not None:
        last_close = float(closes[-1])
        buy_px = float(current_leg["buy_close"])
        open_leg = {
            "buy_signal_date": current_leg.get("buy_signal_date"),
            "buy_date": current_leg["buy_date"],
            "buy_close": round(buy_px, 4),
            "holding_days": int(m - np.where(dates == current_leg["buy_date"])[0][0] - 1) if current_leg.get("buy_date") in dates else None,
            "shares": int(shares),
            "unrealized_return_pct": round((last_close / buy_px - 1.0) * 100.0, 4),
            "market_value": round(shares * last_close, 4),
            "note": "样本外序列末尾仍持仓；已按当日收盘做市值估算，尚未出现下一次卖出开盘。",
        }

    equity_vals = np.array([float(x["equity"]) for x in equity_curve], dtype=np.float64)
    daily_returns = (
        equity_vals[1:] / np.maximum(equity_vals[:-1], 1e-9) - 1.0 if equity_vals.size >= 2 else np.array([], dtype=np.float64)
    )
    net_rets = np.array([float(t["return_pct"]) for t in trades], dtype=np.float64) if trades else np.array([], dtype=np.float64)
    gross_rets = (
        np.array([float(t.get("gross_return_pct", t["return_pct"])) for t in trades], dtype=np.float64)
        if trades
        else np.array([], dtype=np.float64)
    )
    hold_days = (
        np.array([float(t.get("holding_days", 0)) for t in trades], dtype=np.float64)
        if trades
        else np.array([], dtype=np.float64)
    )
    wins = net_rets > 0
    losses = net_rets < 0
    gross_profit = float(net_rets[wins].sum()) if wins.any() else 0.0
    gross_loss = float(-net_rets[losses].sum()) if losses.any() else 0.0
    ending_equity = float(equity_vals[-1]) if equity_vals.size else float(initial_cash)
    final_nav = ending_equity / float(initial_cash) if initial_cash > 1e-9 else 1.0
    summary = {
        "completed_trades": len(trades),
        "win_rate": round(float(wins.mean()), 4) if trades else None,
        "avg_return_pct": round(float(net_rets.mean()), 4) if trades else None,
        "total_simple_return_pct": round(float(net_rets.sum()), 4) if trades else None,
        "gross_return_pct": round(float(gross_rets.sum()), 4) if trades else None,
        "total_net_return_pct": round((final_nav - 1.0) * 100.0, 4),
        "compounded_return_pct": round((final_nav - 1.0) * 100.0, 4),
        "annualized_return_pct": _annualized_return_pct(daily_returns),
        "max_drawdown_pct": _max_drawdown_pct(daily_returns),
        "sharpe_ratio": _sharpe_ratio(daily_returns),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else None,
        "avg_holding_days": round(float(hold_days.mean()), 2) if hold_days.size else None,
        "avg_win_return_pct": round(float(net_rets[wins].mean()), 4) if wins.any() else None,
        "avg_loss_return_pct": round(float(net_rets[losses].mean()), 4) if losses.any() else None,
        "total_cost_pct": round((total_fee_cny + total_slippage_cny) / max(1e-9, initial_cash) * 100.0, 4),
        "daily_compounded_return_pct": round((final_nav - 1.0) * 100.0, 4),
        "final_nav": round(final_nav, 6),
        "ending_equity": round(ending_equity, 4),
        "ending_cash": round(cash, 4),
        "ending_shares": int(shares),
        "total_fee_cny": round(total_fee_cny, 4),
        "total_slippage_cny": round(total_slippage_cny, 4),
    }
    return trades, open_leg, equity_curve, summary


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
    opens_oos: np.ndarray,
    closes_oos: np.ndarray,
    preds: np.ndarray,
    *,
    max_trades: int,
    initial_cash: float,
    lot_size: int,
    commission_bps: float,
    sell_tax_bps: float,
    slippage_bps: float,
    min_commission_cny: float,
) -> dict[str, Any]:
    all_trades, open_leg, equity_curve, summary = _trades_from_predictions(
        dates_oos,
        opens_oos,
        closes_oos,
        preds,
        initial_cash=initial_cash,
        lot_size=lot_size,
        commission_bps=commission_bps,
        sell_tax_bps=sell_tax_bps,
        slippage_bps=slippage_bps,
        min_commission_cny=min_commission_cny,
    )
    tail = all_trades[-max_trades:] if len(all_trades) > max_trades else all_trades
    out = {**base, "trade_summary": summary, "trades": tail, "open_leg": open_leg, "equity_curve_tail": equity_curve[-20:]}
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
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    sell_tax_bps: float = DEFAULT_SELL_TAX_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    lot_size: int = DEFAULT_LOT_SIZE,
    min_commission_cny: float = DEFAULT_MIN_COMMISSION_CNY,
    oos_from: str | None = None,
    oos_to: str | None = None,
    methods: list[str] | None = None,
    live_bars: bool = False,
    live_persist: bool = True,
    data_source: str | None = None,
    live_as_of: str | None = None,
) -> dict[str, Any]:
    """
    对单标的做 walk-forward OOS 评估；默认数据来自本地 bars。

    horizon：预测未来 H 个交易日累计涨跌方向。
    min_train_rows：从该样本索引起进入 OOS（前段仅用于训练逻辑回归）。
    retrain_every：每隔多少根 OOS 步长重训一次 logistic（中间沿用上一权重）。
    ma_short / ma_long：双均线策略周期（短 < 长），与常见教材 5/10 类似。
    oos_from / oos_to：若指定，则仅在该闭区间内做样本外指标与成交示意（训练仍使用此前全部历史，无前视）。
    methods：要返回的方法键名子集；None 表示四种全部（dual_ma_cross / logistic_walkforward / rule_trend / majority_causal）。
    live_bars：为 True 时先联网拉取 incremental 窗口内的日线再回测；False 则仅读库、不联网。
    live_persist：live_bars 时 True=incremental_refresh 写入 SQLite 后读库；False=仅将联网数据与内存中的本地行合并，不写库。
    data_source：live_bars 时传给拉取逻辑；None 时用服务端默认 ingest 路线。
    live_as_of：live_bars 时作为 incremental 截止日期（含当日）YYYY-MM-DD；None 则用服务器当天。宜与③结束日期或样本外 oos_to 对齐。
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
    if commission_bps < 0 or sell_tax_bps < 0 or slippage_bps < 0:
        raise ValueError("commission_bps / sell_tax_bps / slippage_bps 不能为负数")
    if initial_cash < 1000:
        raise ValueError("initial_cash 过小，至少应能覆盖一手股票与手续费")
    if lot_size < 1:
        raise ValueError("lot_size 至少为 1")
    if min_commission_cny < 0:
        raise ValueError("min_commission_cny 不能为负数")
    if ma_short < 2 or ma_long < 3:
        raise ValueError("ma_short 至少 2，ma_long 至少 3")
    if ma_short >= ma_long:
        raise ValueError("双均线要求 ma_short < ma_long")
    if ma_long > 250:
        raise ValueError("ma_long 过大（上限 250）")
    need_warm = ma_long + 5
    if min_train_rows < need_warm:
        raise ValueError(f"min_train_rows 至少应为 ma_long+5 = {need_warm}，以便双均线与特征同时有效")

    o_from = _normalize_oos_bound(oos_from, "oos_from")
    o_to = _normalize_oos_bound(oos_to, "oos_to")
    if o_from is not None and o_to is not None and o_from > o_to:
        raise ValueError("oos_from 不能晚于 oos_to")

    mf = _normalize_forecast_methods(methods)

    live_as_of_d = _parse_live_as_of_date(live_as_of) if live_bars else None

    try:
        df = load_bars_for_forecast(
            sym,
            live_bars=live_bars,
            live_persist=live_persist if live_bars else False,
            data_source=data_source if live_bars else None,
            as_of_date=live_as_of_d,
        )
    except (RuntimeError, ValueError) as e:
        raise ValueError(str(e)) from e
    if df.empty:
        raise ValueError("本地无 K 线，请先 POST /ingest/update")
    bars_last_trade_date = str(df["trade_date"].iloc[-1]) if len(df) else None
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
    if o_from is not None or o_to is not None:
        d_str = dates.iloc[oos_idx].astype(str).to_numpy()
        keep = np.ones(len(oos_idx), dtype=bool)
        if o_from is not None:
            keep &= d_str >= o_from
        if o_to is not None:
            keep &= d_str <= o_to
        oos_idx = oos_idx[keep]
        if len(oos_idx) == 0:
            raise ValueError(
                "按 oos_from/oos_to 过滤后没有样本外交易日；请扩大区间、补充本地 K 线，或暂时去掉日期过滤"
            )

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
    open_series = df.loc[valid.index, "open"].astype(float)
    opens_oos = open_series.iloc[oos_idx].to_numpy(dtype=np.float64)
    close_series = df.loc[valid.index, "close"].astype(float)
    closes_oos = close_series.iloc[oos_idx].to_numpy(dtype=np.float64)
    close_all = close_series.to_numpy(dtype=np.float64)
    ma_full = _dual_ma_signal_series(close_all, ma_short, ma_long)
    ma_pred = ma_full[oos_idx]

    baseline_acc = float(max(y_oos.mean(), 1.0 - y_oos.mean()))

    how_to_read = (
        "【准确率】在样本外每个交易日，先产生一个「多空信号」（双均线 / 规则 / Logistic），"
        "再单独用同一套日期去检验「往后 H 个交易日累计涨跌是否为正」是否猜对；可与「无脑猜多数类」对比。"
        "【信号何时产生】每个交易日收盘后，使用当日及以前的数据生成下一步多空信号。"
        "【何时下单】若需要调仓，则在下一个交易日开盘挂单并成交。"
        "【成交价】买入按 next open*(1+滑点)，卖出按 next open*(1-滑点)。"
        "【买多少】默认初始资金 10 万、整手 100 股、单次满仓；若现金不足一手则不成交。"
        "【持仓更新】每日收盘按 cash + shares*close 更新净值；交易费用含佣金、卖出印花税与滑点估算。"
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

    attached: dict[str, dict[str, Any]] = {
        "dual_ma_cross": _attach_trades_to_method(
            m_ma,
            dates_oos,
            opens_oos,
            closes_oos,
            ma_pred,
            max_trades=trade_limit,
            initial_cash=initial_cash,
            lot_size=lot_size,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
            min_commission_cny=min_commission_cny,
        ),
        "logistic_walkforward": _attach_trades_to_method(
            m_log,
            dates_oos,
            opens_oos,
            closes_oos,
            log_pred,
            max_trades=trade_limit,
            initial_cash=initial_cash,
            lot_size=lot_size,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
            min_commission_cny=min_commission_cny,
        ),
        "rule_trend": _attach_trades_to_method(
            m_rule,
            dates_oos,
            opens_oos,
            closes_oos,
            rule_pred,
            max_trades=trade_limit,
            initial_cash=initial_cash,
            lot_size=lot_size,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
            min_commission_cny=min_commission_cny,
        ),
        "majority_causal": _attach_trades_to_method(
            m_maj,
            dates_oos,
            opens_oos,
            closes_oos,
            maj_pred,
            max_trades=trade_limit,
            initial_cash=initial_cash,
            lot_size=lot_size,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
            min_commission_cny=min_commission_cny,
        ),
    }
    methods = [attached[k] for k in mf]
    ui_focus = mf[0] if mf else "dual_ma_cross"

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
        "stack_note": "数据：pandas DataFrame；数值：numpy；分类模型：自实现 Logistic 梯度下降。当前版本按“收盘产生信号、次日开盘成交”近似短线回放，并计入默认佣金/印花税/滑点与整手仓位限制，但仍是日线级近似。",
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
        "first_oos_trade_date": str(dates.iloc[int(oos_idx[0])]) if len(oos_idx) else None,
        "last_oos_trade_date": str(dates.iloc[int(oos_idx[-1])]) if len(oos_idx) else None,
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
        "ui_focus_method": ui_focus,
        "pedagogy": pedagogy,
        "strategy_params": {
            "horizon": horizon,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "min_train_rows": min_train_rows,
            "retrain_every": retrain_every,
            "trade_limit": trade_limit,
            "commission_bps": commission_bps,
            "sell_tax_bps": sell_tax_bps,
            "slippage_bps": slippage_bps,
            "initial_cash": initial_cash,
            "lot_size": lot_size,
            "min_commission_cny": min_commission_cny,
            "oos_from": o_from,
            "oos_to": o_to,
            "methods_included": list(mf),
            "live_bars": bool(live_bars),
            "live_persist": bool(live_persist) if live_bars else False,
            "live_data_source": data_source,
            "live_as_of": live_as_of_d.isoformat() if live_bars and live_as_of_d is not None else None,
            "bars_last_trade_date": bars_last_trade_date,
        },
        "execution_assumptions": {
            "signal_timing": "当日收盘后",
            "order_timing": "下一交易日开盘",
            "execution_price_rule": "买入 next open*(1+slippage)，卖出 next open*(1-slippage)",
            "sizing_rule": f"默认初始资金 {initial_cash:.0f} 元，整手 {lot_size} 股，单次满仓，现金不足一手则不成交",
            "position_update_rule": "每日收盘按 cash + shares*close 更新权益与净值",
            "cost_rule": f"佣金 {commission_bps} bps，卖出印花税 {sell_tax_bps} bps，单边滑点 {slippage_bps} bps，最低佣金 {min_commission_cny:.2f} 元",
        },
        "fundamentals_backtest": {
            "merged_into_walkforward": False,
            "snapshot_cached": snapshot_cached,
            "note": fb_note,
        },
    }
