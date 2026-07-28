"""Pick symbols from top hot sectors with optional technical ranking and filters."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from .backtest import (
    consecutive_close_on_ma5_streak,
    consecutive_rising_close_streak,
    last_n_days_close_on_ma5,
    last_n_days_rising,
)
from .datasources import BaseAShareDataSource
from .market_utils import is_listed_a_share_equity, normalize_code, safe_float
from .screening import evaluate_screen


logger = logging.getLogger(__name__)

_HOT_PICK_UI_LOG_CAP = 120


def _append_hot_pick_ui_log(progress_log: list[str] | None, text: str) -> None:
    """同时写服务端 logger、可回传 progress_log，以及进程内轮询缓冲。"""
    logger.info("%s", text)
    try:
        from app.hot_sectors_job import hot_sectors_job_append_log

        hot_sectors_job_append_log(text)
    except Exception:  # noqa: BLE001
        pass
    if progress_log is None:
        return
    progress_log.append(text)
    excess = len(progress_log) - _HOT_PICK_UI_LOG_CAP
    if excess > 0:
        del progress_log[0:excess]


def _stock_pick_label(stock: dict[str, Any]) -> str:
    code = normalize_code(str(stock.get("code", "") or ""))
    if not code:
        return ""
    name = str(stock.get("name", "") or "").strip()
    return f"{code} {name}" if name else code


def _log_sector_pick_summary(
    pick_group: str,
    *,
    sector_rank: int,
    sector_name: str,
    selected: list[dict[str, Any]],
    total_scanned: int,
    progress_log: list[str] | None = None,
    extra: str = "",
) -> None:
    """每板块一条：仅输出入选标的（无入选时 DEBUG，不逐股刷屏）。"""
    labels = [x for x in (_stock_pick_label(s) for s in selected) if x]
    suffix = f" {extra}".strip()
    if labels:
        show = labels[:40]
        tail = f"…等共{len(labels)}只" if len(labels) > len(show) else ""
        stocks_txt = "、".join(show) + tail
        line = (
            f"[{pick_group}] 板块#{sector_rank}「{sector_name}」"
            f" 入选 {len(labels)}/{total_scanned}：{stocks_txt}"
        )
        if suffix:
            line += f"（{suffix}）"
        _append_hot_pick_ui_log(progress_log, line)
    else:
        logger.debug(
            "[%s] 板块#%s「%s」无入选（扫描 %s 只%s）",
            pick_group,
            sector_rank,
            sector_name,
            total_scanned,
            f"，{suffix}" if suffix else "",
        )


# *ST / ＊ST；或名称以 ST 开头（如 ST华扬，\bST\b 无法匹配 ST+中文）
_ST_PATTERN = re.compile(
    r"(\*ST|＊ST|^ST(?=[\u4e00-\u9fffA-Za-z0-9])|^ST\s|\bST\b)",
    re.IGNORECASE,
)

HOT_PICK_CONDITION_GROUPS = frozenset({"sector_hot", "ma5_capital", "rising_3d"})


def normalize_pick_condition_groups(groups: list[str] | None) -> set[str]:
    """解析并校验选股条件组；空则默认两项都选。"""
    if not groups:
        return set(HOT_PICK_CONDITION_GROUPS)
    out: set[str] = set()
    for raw in groups:
        key = str(raw).strip().lower()
        if not key:
            continue
        if key not in HOT_PICK_CONDITION_GROUPS:
            raise ValueError(
                f"不支持的选股条件: {key}，可选: {', '.join(sorted(HOT_PICK_CONDITION_GROUPS))}"
            )
        out.add(key)
    if not out:
        raise ValueError("请至少选择一种选股条件（sector_hot / ma5_capital / rising_3d）")
    return out


def is_st_stock_name(name: object) -> bool:
    """Heuristic: *ST / ST prefix / ST as word (EastMoney & mootdx style)."""
    if name is None:
        return False
    s = str(name).strip()
    if not s:
        return False
    return bool(_ST_PATTERN.search(s))


def is_st_sector_name(sector_name: object) -> bool:
    """通达信/东财常见 ST 概念或行业板块名。"""
    if sector_name is None:
        return False
    s = str(sector_name).strip().upper()
    if not s:
        return False
    return s in ("ST", "ST板块") or s.startswith("ST板块")


def is_star_board_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == 6 and (c.startswith("688") or c.startswith("689"))


def is_chinext_board_code(code: str) -> bool:
    """创业板：300/301 开头。"""
    c = normalize_code(code)
    return len(c) == 6 and (c.startswith("300") or c.startswith("301"))


def _data_source_label(datasource: BaseAShareDataSource) -> str:
    return str(getattr(datasource, "source_name", "") or "unknown")


def _eligible_constituent_rows(
    constituents: pd.DataFrame,
    *,
    exclude_st: bool,
    exclude_kcb: bool,
    exclude_cyb: bool = False,
    limit: int | None = None,
) -> list[tuple[str, str, pd.Series]]:
    """过滤 ST/科创板/创业板后，返回 (code, name, row) 列表供逐股拉取日志计数。

    ``limit`` 为正整数时：按成分表原有顺序只保留前 N 只（用于「每板块取前 N」）。
    """
    rows: list[tuple[str, str, pd.Series]] = []
    max_n = max(1, int(limit)) if limit is not None else None
    for _, crow in constituents.iterrows():
        code = normalize_code(crow.get("code", crow.get("代码", "")))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = str(crow.get("name", crow.get("名称", "")) or "").strip()
        if exclude_kcb and is_star_board_code(code):
            continue
        if exclude_cyb and is_chinext_board_code(code):
            continue
        if exclude_st and is_st_stock_name(name):
            continue
        rows.append((code, name, crow))
        if max_n is not None and len(rows) >= max_n:
            break
    return rows


def _hot_pick_stock_log(
    pick_group: str,
    *,
    sector_rank: int,
    sector_name: str,
    code: str,
    name: str,
    idx: int,
    total: int,
    message: str,
    data_source: str = "",
    force_info: bool = False,
    progress_log: list[str] | None = None,
) -> None:
    """逐股过程仅 DEBUG，不向 INFO / 控制台 progress 刷屏。"""
    del force_info, progress_log
    nm = f" {name}" if name else ""
    ds = f" 路线={data_source}" if data_source else ""
    line = (
        f"[{pick_group}] 板块#{sector_rank}「{sector_name}」 "
        f"{idx}/{total} {code}{nm}{ds} · {message}"
    )
    logger.debug("[热门选股] %s", line)


def _series_to_jsonable_dict(series: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in series.items():
        k = str(key)
        try:
            if pd.isna(val):
                out[k] = None
            elif isinstance(val, (np.integer, np.floating)):
                out[k] = val.item()
            elif isinstance(val, np.bool_):
                out[k] = bool(val)
            elif hasattr(val, "item") and not isinstance(val, (str, bytes)):
                try:
                    out[k] = val.item()
                except Exception:
                    out[k] = str(val)
            else:
                out[k] = val
        except (TypeError, ValueError):
            out[k] = str(val) if val is not None else None
    return out


def _ranking_row_to_metrics(row: pd.Series, sector_rank: int) -> dict[str, Any]:
    base = _series_to_jsonable_dict(row)
    base["sector_rank"] = sector_rank
    return base


@dataclass
class HotPickResult:
    """Ordered sector bundles + flat symbol list for watchlist upsert."""

    sectors_detail: list[dict[str, Any]]
    symbols_for_watchlist: list[str]
    warnings: list[str] = field(default_factory=list)
    ma5_capital_sectors_detail: list[dict[str, Any]] = field(default_factory=list)
    rising_3d_sectors_detail: list[dict[str, Any]] = field(default_factory=list)
    pick_condition_groups: list[str] = field(default_factory=list)
    progress_log: list[str] = field(default_factory=list)


def _pick_history_window() -> tuple[str, str]:
    """A little over 1 year of calendar days to cover 120+ trading sessions."""
    end = date.today()
    start = end - timedelta(days=420)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _avg_turnover_nd(frame: pd.DataFrame, days: int) -> float:
    if frame is None or frame.empty or "turnover" not in frame.columns:
        return 0.0
    n = max(1, int(days))
    return safe_float(frame["turnover"].tail(n).mean(), default=0.0)


def _avg_turnover_20d(frame: pd.DataFrame) -> float:
    return _avg_turnover_nd(frame, 20)


def _basic_pick_from_constituents(
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int,
    exclude_st: bool,
    exclude_kcb: bool,
    exclude_cyb: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Legacy fast path: preserve row order after basic ST / STAR / ChiNext filters."""
    stocks: list[dict[str, Any]] = []
    symbols: list[str] = []
    stock_rank = 0
    for _, crow in constituents.iterrows():
        code = normalize_code(crow.get("code", crow.get("代码", "")))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = crow.get("name", crow.get("名称", ""))
        if exclude_kcb and is_star_board_code(code):
            continue
        if exclude_cyb and is_chinext_board_code(code):
            continue
        if exclude_st and is_st_stock_name(name):
            continue
        stock_rank += 1
        if stock_rank > stocks_per_sector:
            break
        detail = _series_to_jsonable_dict(crow)
        detail["stock_rank_in_sector"] = stock_rank
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        stocks.append(detail)
        symbols.append(code)
    return stocks, symbols


def _technical_pick_from_constituents(
    datasource: BaseAShareDataSource,
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int,
    exclude_st: bool,
    exclude_kcb: bool,
    exclude_cyb: bool = False,
    sort_by_trend_strength: bool,
    require_technical_pass: bool,
    exclude_overextended: bool,
    max_return_20d_pct: float,
    enable_liquidity_filter: bool,
    min_avg_turnover_20d_100m: float,
    warnings: list[str],
    progress_log: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Advanced path: fetch price history per stock, compute screen metrics, then rank/filter.

    `min_avg_turnover_20d_100m` is expressed in 100 million CNY (亿元).
    """
    start_date, end_date = _pick_history_window()
    min_turnover_amt = max(0.0, float(min_avg_turnover_20d_100m)) * 1e8
    candidates: list[dict[str, Any]] = []
    filtered_overextended = 0
    filtered_liquidity = 0
    filtered_technical = 0
    failed_history = 0
    ds_label = _data_source_label(datasource)
    eligible = _eligible_constituent_rows(
        constituents,
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        exclude_cyb=exclude_cyb,
        limit=max(1, int(stocks_per_sector)),
    )
    total = len(eligible)
    logger.debug(
        "[热门选股][sector_hot] 板块#%s「%s」扫描前 %s 只 路线=%s",
        sector_rank,
        sector_name,
        total,
        ds_label,
    )

    for idx, (code, name, crow) in enumerate(eligible, start=1):
        _hot_pick_stock_log(
            "sector_hot",
            sector_rank=sector_rank,
            sector_name=sector_name,
            code=code,
            name=name,
            idx=idx,
            total=total,
            message="拉取日线…",
            data_source=ds_label,
        )
        try:
            hist = datasource.get_price_history(code, start_date, end_date, adjust="qfq")
            screen = evaluate_screen(hist)
        except Exception as exc:  # noqa: BLE001
            failed_history += 1
            _hot_pick_stock_log(
                "sector_hot",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"日线失败：{exc}",
                data_source=ds_label,
            )
            warnings.append(f"板块「{sector_name}」成分股 {code} 技术面计算失败：{exc}")
            continue

        avg_turnover_5d = _avg_turnover_nd(hist, 5)
        avg_turnover_10d = _avg_turnover_nd(hist, 10)
        avg_turnover_20d = _avg_turnover_20d(hist)
        if exclude_overextended and screen.return_20d > max_return_20d_pct:
            filtered_overextended += 1
            _hot_pick_stock_log(
                "sector_hot",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"跳过：20日涨幅 {screen.return_20d:.2f}% 超限",
                data_source=ds_label,
            )
            continue
        if enable_liquidity_filter and avg_turnover_20d < min_turnover_amt:
            filtered_liquidity += 1
            _hot_pick_stock_log(
                "sector_hot",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message="跳过：流动性不足",
                data_source=ds_label,
            )
            continue
        if require_technical_pass and not screen.passed:
            filtered_technical += 1
            _hot_pick_stock_log(
                "sector_hot",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message="跳过：技术面未通过",
                data_source=ds_label,
            )
            continue

        detail = _series_to_jsonable_dict(crow)
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        detail["screen_passed"] = bool(screen.passed)
        detail["trend_score"] = screen.trend_score
        detail["volume_score"] = screen.volume_score
        detail["risk_score"] = screen.risk_score
        detail["screen_score"] = screen.screen_score
        detail["return_5d"] = screen.return_5d
        detail["return_10d"] = screen.return_10d
        detail["return_20d"] = screen.return_20d
        detail["distance_to_60d_high"] = screen.distance_to_60d_high
        detail["volume_ratio_20_60"] = screen.volume_ratio_20_60
        detail["vol_ratio_last_day"] = screen.vol_ratio_last_day
        detail["drawdown_60d"] = screen.drawdown_60d
        detail["annual_volatility_20d"] = screen.annual_volatility_20d
        detail["avg_turnover_5d"] = round(avg_turnover_5d, 2)
        detail["avg_turnover_10d"] = round(avg_turnover_10d, 2)
        detail["avg_turnover_20d"] = round(avg_turnover_20d, 2)
        detail["avg_turnover_5d_100m"] = round(avg_turnover_5d / 1e8, 4)
        detail["avg_turnover_10d_100m"] = round(avg_turnover_10d / 1e8, 4)
        detail["avg_turnover_20d_100m"] = round(avg_turnover_20d / 1e8, 4)
        detail["latest_close"] = screen.latest_close
        detail["screen_reasons"] = screen.reasons
        detail["short_term_passed"] = bool(screen.short_term_passed)
        detail["short_term_score"] = screen.short_term_score
        detail["long_term_passed"] = bool(screen.long_term_passed)
        detail["long_term_score"] = screen.long_term_score
        detail["ma20_slope_pct"] = screen.ma20_slope_pct
        candidates.append(detail)
        _hot_pick_stock_log(
            "sector_hot",
            sector_rank=sector_rank,
            sector_name=sector_name,
            code=code,
            name=name,
            idx=idx,
            total=total,
            message="入选候选",
            data_source=ds_label,
        )

    if sort_by_trend_strength:
        # 主排序：技术面与趋势；同分下略偏好更低波动（常见组合构建中的风险约束近似）。
        candidates.sort(
            key=lambda x: (
                1 if x.get("screen_passed") else 0,
                safe_float(x.get("screen_score")),
                safe_float(x.get("trend_score")),
                -safe_float(x.get("annual_volatility_20d")),
                safe_float(x.get("volume_score")),
                safe_float(x.get("avg_turnover_20d")),
            ),
            reverse=True,
        )

    selected = candidates[:stocks_per_sector]
    for idx, detail in enumerate(selected, start=1):
        detail["stock_rank_in_sector"] = idx

    if filtered_overextended:
        warnings.append(
            f"板块「{sector_name}」因近 20 日涨幅过大被过滤 {filtered_overextended} 只"
        )
    if filtered_liquidity:
        warnings.append(f"板块「{sector_name}」因流动性不足被过滤 {filtered_liquidity} 只")
    if filtered_technical:
        warnings.append(f"板块「{sector_name}」因技术面未通过被过滤 {filtered_technical} 只")
    if failed_history:
        warnings.append(f"板块「{sector_name}」有 {failed_history} 只历史行情不足或计算失败")

    _log_sector_pick_summary(
        "sector_hot",
        sector_rank=sector_rank,
        sector_name=sector_name,
        selected=selected,
        total_scanned=total,
        progress_log=progress_log,
        extra=f"候选 {len(candidates)}，日线失败 {failed_history}",
    )
    return selected, [normalize_code(x.get("code", "")) for x in selected if x.get("code")]


def _main_net_inflow_from_flow_row(row: dict[str, Any]) -> float | None:
    for key in ("主力净流入-净额", "main_net_inflow"):
        if key not in row:
            continue
        raw = row[key]
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            return v
    return None


def evaluate_capital_support(
    sym: str,
    *,
    lookback_days: int = 3,
    min_positive_days: int = 2,
) -> tuple[bool, float, dict[str, Any]]:
    """
    资金承接：最近 lookback_days 个交易日中至少 min_positive_days 日主力净流入为正，且合计为正。
    资金流为东财日级（与行情数据源路线无关）。
    """
    from app.fundamentals import fetch_individual_fund_flow_latest_metrics, fetch_individual_fund_flow_recent_rows

    lb = max(1, int(lookback_days))
    need_pos = max(1, min(int(min_positive_days), lb))
    rows = fetch_individual_fund_flow_recent_rows(sym, limit_rows=lb + 2)
    meta: dict[str, Any] = {
        "fund_flow_lookback_days": lb,
        "fund_flow_positive_days": 0,
        "main_net_inflow_3d": None,
        "main_net_inflow": None,
        "fund_flow_date": None,
        "em_large_net_pct": None,
        "capital_support_score": 0.0,
    }
    if not rows:
        return False, 0.0, meta
    tail = rows[-lb:]
    inflows: list[float] = []
    for r in tail:
        v = _main_net_inflow_from_flow_row(r)
        inflows.append(v if v is not None else 0.0)
    positive = sum(1 for x in inflows if x > 0)
    total = float(sum(inflows))
    meta["fund_flow_positive_days"] = positive
    meta["main_net_inflow_3d"] = round(total, 2)
    latest = fetch_individual_fund_flow_latest_metrics(sym) or {}
    from app.fundamentals import fetch_latest_main_flow

    main_last, flow_d, _basis = fetch_latest_main_flow(sym)
    meta["main_net_inflow"] = round(main_last, 2) if main_last is not None else None
    meta["fund_flow_date"] = flow_d
    meta["em_large_net_pct"] = latest.get("em_large_net_pct")
    ok = positive >= need_pos and total > 0
    if ok and meta["em_large_net_pct"] is not None:
        try:
            if float(meta["em_large_net_pct"]) < 0:
                ok = False
        except (TypeError, ValueError):
            pass
    score = total + positive * 5e7
    if meta["em_large_net_pct"] is not None:
        try:
            score += float(meta["em_large_net_pct"]) * 1e6
        except (TypeError, ValueError):
            pass
    meta["capital_support_score"] = round(score, 2)
    return ok, score, meta


def _ma5_capital_pick_from_constituents(
    datasource: BaseAShareDataSource,
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int | None = None,
    exclude_st: bool = True,
    exclude_kcb: bool = True,
    exclude_cyb: bool = False,
    ma5_stand_min_days: int,
    capital_lookback_days: int,
    capital_min_positive_days: int,
    require_capital: bool = True,
    warnings: list[str],
    progress_log: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """连续站上 MA5；可选再叠加资金承接（与上方热门条件分开展示）。

    stocks_per_sector 为 None 时扫描全部合格成分；否则按成分顺序只取前 N 只再筛。
    require_capital=False 时跳过东财资金流，仅按 MA5 入选。
    """
    start_date, end_date = _pick_history_window()
    min_days = max(1, int(ma5_stand_min_days))
    candidates: list[dict[str, Any]] = []
    failed_history = 0
    failed_ma5 = 0
    failed_capital = 0
    failed_fund = 0
    ds_label = _data_source_label(datasource)
    mode_label = "五日MA5" if not require_capital else "五日强承接"
    scan_limit = max(1, int(stocks_per_sector)) if stocks_per_sector is not None else None
    eligible = _eligible_constituent_rows(
        constituents,
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        exclude_cyb=exclude_cyb,
        limit=scan_limit,
    )
    total = len(eligible)
    limit_hint = f"取前 {scan_limit} 只" if scan_limit is not None else "不限只数"
    capital_hint = "仅MA5" if not require_capital else "MA5+资金"
    _append_hot_pick_ui_log(
        progress_log,
        f"[{mode_label}] 板块#{sector_rank}「{sector_name}」开始扫描 {total} 只（{limit_hint}·{capital_hint}）…",
    )
    logger.debug(
        "[热门选股][ma5_capital] 板块#%s「%s」扫描 %s 只（%s·%s）日线路线=%s",
        sector_rank,
        sector_name,
        total,
        limit_hint,
        capital_hint,
        ds_label,
    )

    for idx, (code, name, crow) in enumerate(eligible, start=1):
        if idx == 1 or idx % 20 == 0 or idx == total:
            _append_hot_pick_ui_log(
                progress_log,
                f"[{mode_label}] 板块#{sector_rank}「{sector_name}」日线进度 {idx}/{total}"
                f"（当前 {code} {name}）",
            )
        _hot_pick_stock_log(
            "ma5_capital",
            sector_rank=sector_rank,
            sector_name=sector_name,
            code=code,
            name=name,
            idx=idx,
            total=total,
            message="拉取日线…",
            data_source=ds_label,
        )
        try:
            hist = datasource.get_price_history(code, start_date, end_date, adjust="qfq")
        except Exception as exc:  # noqa: BLE001
            failed_history += 1
            _hot_pick_stock_log(
                "ma5_capital",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"日线失败：{exc}",
                data_source=ds_label,
            )
            warnings.append(f"「{mode_label}」板块「{sector_name}」{code} 日线失败：{exc}")
            continue
        streak = consecutive_close_on_ma5_streak(hist)
        if not last_n_days_close_on_ma5(hist, min_days=min_days):
            failed_ma5 += 1
            _hot_pick_stock_log(
                "ma5_capital",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"跳过：MA5 连续 {streak} 日（需 ≥{min_days}）",
                data_source=ds_label,
            )
            continue

        cap_ok = True
        cap_score = float(streak)
        cap_meta: dict[str, Any] = {
            "fund_flow_lookback_days": capital_lookback_days,
            "fund_flow_positive_days": None,
            "main_net_inflow_3d": None,
            "main_net_inflow": None,
            "fund_flow_date": None,
            "em_large_net_pct": None,
            "capital_support_score": 0.0,
            "capital_check_skipped": not require_capital,
        }
        if require_capital:
            _hot_pick_stock_log(
                "ma5_capital",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"MA5 通过({streak}日)，拉取东财资金流…",
                data_source=ds_label,
            )
            try:
                cap_ok, cap_score, cap_meta = evaluate_capital_support(
                    code,
                    lookback_days=capital_lookback_days,
                    min_positive_days=capital_min_positive_days,
                )
                cap_meta["capital_check_skipped"] = False
            except Exception as exc:  # noqa: BLE001
                failed_fund += 1
                _hot_pick_stock_log(
                    "ma5_capital",
                    sector_rank=sector_rank,
                    sector_name=sector_name,
                    code=code,
                    name=name,
                    idx=idx,
                    total=total,
                    message=f"资金流失败：{exc}",
                    data_source=ds_label,
                )
                warnings.append(f"「{mode_label}」板块「{sector_name}」{code} 资金流失败：{exc}")
                continue
            if not cap_ok:
                failed_capital += 1
                pos_days = cap_meta.get("fund_flow_positive_days")
                inflow_3d = cap_meta.get("main_net_inflow_3d")
                _hot_pick_stock_log(
                    "ma5_capital",
                    sector_rank=sector_rank,
                    sector_name=sector_name,
                    code=code,
                    name=name,
                    idx=idx,
                    total=total,
                    message=f"跳过：资金承接不足（{pos_days}日正流入，3日合计 {inflow_3d}）",
                    data_source=ds_label,
                )
                continue
        else:
            _hot_pick_stock_log(
                "ma5_capital",
                sector_rank=sector_rank,
                sector_name=sector_name,
                code=code,
                name=name,
                idx=idx,
                total=total,
                message=f"MA5 通过({streak}日)，跳过资金检查",
                data_source=ds_label,
            )

        detail = _series_to_jsonable_dict(crow)
        detail["pick_group"] = "ma5_capital"
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        detail["ma5_consecutive_days"] = streak
        detail["ma5_stand_min_days"] = min_days
        detail["ma5_require_capital"] = require_capital
        detail["capital_support_score"] = cap_meta.get("capital_support_score")
        detail["fund_flow_positive_days"] = cap_meta.get("fund_flow_positive_days")
        detail["main_net_inflow_3d"] = cap_meta.get("main_net_inflow_3d")
        detail["main_net_inflow"] = cap_meta.get("main_net_inflow")
        detail["fund_flow_date"] = cap_meta.get("fund_flow_date")
        detail["em_large_net_pct"] = cap_meta.get("em_large_net_pct")
        detail["capital_check_skipped"] = bool(cap_meta.get("capital_check_skipped"))
        try:
            screen = evaluate_screen(hist)
            detail["latest_close"] = screen.latest_close
            detail["return_5d"] = screen.return_5d
            detail["return_10d"] = screen.return_10d
            detail["return_20d"] = screen.return_20d
        except Exception:
            pass
        detail["_sort_capital"] = cap_score
        detail["_sort_ma5"] = streak
        candidates.append(detail)
        pass_msg = (
            f"入选候选（MA5={streak}日，未检查资金）"
            if not require_capital
            else f"入选候选（MA5={streak}日，资金分={cap_meta.get('capital_support_score')}）"
        )
        _hot_pick_stock_log(
            "ma5_capital",
            sector_rank=sector_rank,
            sector_name=sector_name,
            code=code,
            name=name,
            idx=idx,
            total=total,
            message=pass_msg,
            data_source=ds_label,
        )

    if require_capital:
        candidates.sort(
            key=lambda x: (
                safe_float(x.get("_sort_capital")),
                safe_float(x.get("_sort_ma5")),
                safe_float(x.get("main_net_inflow_3d")),
            ),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda x: (
                safe_float(x.get("_sort_ma5")),
                safe_float(x.get("return_5d")),
            ),
            reverse=True,
        )
    selected = candidates if stocks_per_sector is None else candidates[: max(1, int(stocks_per_sector))]
    for idx, detail in enumerate(selected, start=1):
        detail["stock_rank_in_sector"] = idx
        detail.pop("_sort_capital", None)
        detail.pop("_sort_ma5", None)

    if failed_ma5:
        warnings.append(f"「{mode_label}」板块「{sector_name}」未满足连续 {min_days} 日站上 MA5：{failed_ma5} 只")
    if failed_capital:
        warnings.append(f"「{mode_label}」板块「{sector_name}」资金承接不足：{failed_capital} 只")
    if failed_fund:
        warnings.append(f"「{mode_label}」板块「{sector_name}」资金流拉取失败：{failed_fund} 只")
    if failed_history:
        warnings.append(f"「{mode_label}」板块「{sector_name}」日线不足：{failed_history} 只")

    extra = f"MA5不符 {failed_ma5}，日线失败 {failed_history}"
    if require_capital:
        extra += f"，资金不符 {failed_capital}，资金失败 {failed_fund}"
    else:
        extra += "，未检查资金"
    _log_sector_pick_summary(
        "ma5_capital",
        sector_rank=sector_rank,
        sector_name=sector_name,
        selected=selected,
        total_scanned=total,
        progress_log=progress_log,
        extra=extra,
    )
    return selected, [normalize_code(x.get("code", "")) for x in selected if x.get("code")]


def _return_over_last_n_closes(frame: pd.DataFrame, n: int) -> float | None:
    from .market_utils import standardize_price_frame

    data = standardize_price_frame(frame)
    need = max(1, int(n))
    if len(data) < need + 1:
        return None
    start = float(data.iloc[-need - 1]["close"])
    end = float(data.iloc[-1]["close"])
    if start <= 0:
        return None
    return round((end / start - 1) * 100, 4)


def _rising_3d_pick_from_constituents(
    datasource: BaseAShareDataSource,
    constituents: pd.DataFrame,
    *,
    sector_rank: int,
    sector_name: str,
    stocks_per_sector: int | None = None,
    exclude_st: bool = True,
    exclude_kcb: bool = True,
    exclude_cyb: bool = True,
    rising_min_days: int = 3,
    warnings: list[str],
    progress_log: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """连续收涨候选：末根起至少 rising_min_days 日收盘逐日抬高。

    stocks_per_sector 为 None 时扫描全部合格成分；否则按成分顺序只取前 N 只再筛。
    """
    start_date, end_date = _pick_history_window()
    min_days = max(1, int(rising_min_days))
    candidates: list[dict[str, Any]] = []
    failed_history = 0
    failed_rising = 0
    ds_label = _data_source_label(datasource)
    scan_limit = max(1, int(stocks_per_sector)) if stocks_per_sector is not None else None
    eligible = _eligible_constituent_rows(
        constituents,
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        exclude_cyb=exclude_cyb,
        limit=scan_limit,
    )
    total = len(eligible)
    limit_hint = f"取前 {scan_limit} 只" if scan_limit is not None else "不限只数"
    _append_hot_pick_ui_log(
        progress_log,
        f"[三日连涨] 板块#{sector_rank}「{sector_name}」开始扫描 {total} 只（{limit_hint}）…",
    )

    for idx, (code, name, crow) in enumerate(eligible, start=1):
        if idx == 1 or idx % 20 == 0 or idx == total:
            _append_hot_pick_ui_log(
                progress_log,
                f"[三日连涨] 板块#{sector_rank}「{sector_name}」日线进度 {idx}/{total}"
                f"（当前 {code} {name}）",
            )
        try:
            hist = datasource.get_price_history(code, start_date, end_date, adjust="qfq")
        except Exception as exc:  # noqa: BLE001
            failed_history += 1
            warnings.append(f"「三日连涨」板块「{sector_name}」{code} 日线失败：{exc}")
            continue
        streak = consecutive_rising_close_streak(hist)
        if not last_n_days_rising(hist, min_days=min_days):
            failed_rising += 1
            continue

        detail = _series_to_jsonable_dict(crow)
        detail["pick_group"] = "rising_3d"
        detail["sector_rank"] = sector_rank
        detail["sector_name"] = sector_name
        detail["rising_consecutive_days"] = streak
        detail["rising_min_days"] = min_days
        try:
            screen = evaluate_screen(hist)
            detail["latest_close"] = screen.latest_close
            detail["return_1d"] = _return_over_last_n_closes(hist, 1)
            detail["return_3d"] = _return_over_last_n_closes(hist, 3)
            detail["return_5d"] = screen.return_5d
            detail["return_10d"] = screen.return_10d
            detail["return_20d"] = screen.return_20d
        except Exception:
            pass

        # 今日下跌的不纳入候选
        r1d = safe_float(detail.get("return_1d"))
        if r1d is not None and r1d < 0:
            failed_rising += 1
            continue

        detail["_sort_rising"] = streak
        detail["_sort_return_3d"] = safe_float(detail.get("return_3d"))
        candidates.append(detail)
        _hot_pick_stock_log(
            "rising_3d",
            sector_rank=sector_rank,
            sector_name=sector_name,
            code=code,
            name=name,
            idx=idx,
            total=total,
            message=f"入选候选（连涨={streak}日）",
            data_source=ds_label,
            force_info=True,
            progress_log=progress_log,
        )

    candidates.sort(
        key=lambda x: (
            safe_float(x.get("_sort_rising")),
            safe_float(x.get("_sort_return_3d")),
            safe_float(x.get("return_5d")),
        ),
        reverse=True,
    )
    selected = candidates if stocks_per_sector is None else candidates[: max(1, int(stocks_per_sector))]
    for idx, detail in enumerate(selected, start=1):
        detail["stock_rank_in_sector"] = idx
        detail.pop("_sort_rising", None)
        detail.pop("_sort_return_3d", None)

    if failed_rising:
        warnings.append(f"「三日连涨」板块「{sector_name}」未满足连续 {min_days} 日收涨：{failed_rising} 只")
    if failed_history:
        warnings.append(f"「三日连涨」板块「{sector_name}」日线不足：{failed_history} 只")

    _log_sector_pick_summary(
        "rising_3d",
        sector_rank=sector_rank,
        sector_name=sector_name,
        selected=selected,
        total_scanned=total,
        progress_log=progress_log,
        extra=f"连涨不符 {failed_rising}，日线失败 {failed_history}",
    )
    return selected, [normalize_code(x.get("code", "")) for x in selected if x.get("code")]


def pick_from_hot_sectors(
    datasource: BaseAShareDataSource,
    *,
    top_sectors: int = 5,
    stocks_per_sector: int = 5,
    board_type: str = "all",
    exclude_st: bool = True,
    exclude_kcb: bool = True,
    exclude_cyb: bool = True,
    rankings_override: pd.DataFrame | None = None,
    sector_names: list[str] | None = None,
    sort_by_trend_strength: bool = True,
    require_technical_pass: bool = False,
    exclude_overextended: bool = False,
    max_return_20d_pct: float = 22.0,
    enable_liquidity_filter: bool = False,
    min_avg_turnover_20d_100m: float = 2.5,
    should_cancel: Callable[[], bool] | None = None,
    pick_condition_groups: list[str] | None = None,
    ma5_stand_min_days: int = 3,
    ma5_require_capital: bool = False,
    capital_flow_lookback_days: int = 3,
    capital_min_positive_days: int = 2,
    ma5_exclude_st: bool = True,
    ma5_exclude_kcb: bool = True,
    ma5_exclude_cyb: bool = True,
    rising_3d_exclude_st: bool = True,
    rising_3d_exclude_kcb: bool = True,
    rising_3d_exclude_cyb: bool = True,
    progress_log: list[str] | None = None,
) -> HotPickResult:
    """
    Walk top hot sectors and choose up to M symbols per sector.

    Fast path: if all advanced technical toggles are off, preserve original constituent row order.
    Advanced path: fetch price history, compute `evaluate_screen()`, optionally filter overextended /
    illiquid / technical-fail names, then rank by trend strength inside each sector.

    If ``sector_names`` is provided, only those sectors are scanned (order follows rankings hot score);
    ``top_sectors`` truncation is skipped.
    """
    warnings: list[str] = []
    cond_groups = normalize_pick_condition_groups(pick_condition_groups)
    want_sector_hot = "sector_hot" in cond_groups
    want_ma5_capital = "ma5_capital" in cond_groups
    want_rising_3d = "rising_3d" in cond_groups
    ds_label = _data_source_label(datasource)
    rankings = rankings_override.copy() if rankings_override is not None else datasource.get_sector_rankings(board_type)
    if rankings is None or rankings.empty:
        logger.warning("[热门选股] 板块列表为空 路线=%s board=%s", ds_label, board_type)
        return HotPickResult(sectors_detail=[], symbols_for_watchlist=[], warnings=["热门板块列表为空"])

    wanted_names: list[str] = []
    if sector_names:
        seen_names: set[str] = set()
        for raw in sector_names:
            name = str(raw or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            wanted_names.append(name)
        if not wanted_names:
            return HotPickResult(
                sectors_detail=[],
                symbols_for_watchlist=[],
                warnings=["未指定有效板块名称"],
                pick_condition_groups=sorted(cond_groups),
            )
        name_col = rankings["sector_name"].astype(str)
        rankings = rankings.loc[name_col.isin(seen_names)].reset_index(drop=True)
        found = set(rankings["sector_name"].astype(str))
        missing = [n for n in wanted_names if n not in found]
        if missing:
            show = "、".join(missing[:15])
            more = f" 等共 {len(missing)} 个" if len(missing) > 15 else ""
            warnings.append(f"以下板块未在排名表中找到：{show}{more}")
        if rankings.empty:
            return HotPickResult(
                sectors_detail=[],
                symbols_for_watchlist=[],
                warnings=warnings + ["指定板块均不在热门排名表中"],
                pick_condition_groups=sorted(cond_groups),
            )

    rankings_len = len(rankings)
    rankings_src = "override" if rankings_override is not None else "live"
    if wanted_names:
        # 用户已勾选板块：全部扫描，不再用 top_sectors 截断
        loop_n = rankings_len
        top_n_sector_hot = rankings_len if want_sector_hot else 0
        _append_hot_pick_ui_log(
            progress_log,
            f"按指定板块筛选：请求 {len(wanted_names)} 个，命中 {rankings_len} 个 · 路线={ds_label}",
        )
        if want_ma5_capital:
            _append_hot_pick_ui_log(
                progress_log,
                f"[五日强承接] 扫描指定 {rankings_len} 个板块 · 路线={ds_label} "
                f"排除ST={ma5_exclude_st} 科创板={ma5_exclude_kcb}",
            )
        if want_rising_3d:
            _append_hot_pick_ui_log(
                progress_log,
                f"[三日连涨] 扫描指定 {rankings_len} 个板块 · 路线={ds_label} "
                f"排除ST={rising_3d_exclude_st} 科创板={rising_3d_exclude_kcb} "
                f"创业板={rising_3d_exclude_cyb}",
            )
    else:
        top_n_sector_hot = min(max(1, top_sectors), rankings_len) if want_sector_hot else 0
        if want_ma5_capital or want_rising_3d:
            loop_n = rankings_len
            if want_ma5_capital:
                _append_hot_pick_ui_log(
                    progress_log,
                    f"[五日强承接] 扫描全部 {rankings_len} 个板块 · 路线={ds_label} "
                    f"排除ST={ma5_exclude_st} 科创板={ma5_exclude_kcb}",
                )
            if want_rising_3d:
                _append_hot_pick_ui_log(
                    progress_log,
                    f"[三日连涨] 扫描全部 {rankings_len} 个板块 · 路线={ds_label} "
                    f"排除ST={rising_3d_exclude_st} 科创板={rising_3d_exclude_kcb} "
                    f"创业板={rising_3d_exclude_cyb}",
                )
        elif want_sector_hot:
            loop_n = top_n_sector_hot
        else:
            loop_n = 0
    _append_hot_pick_ui_log(
        progress_log,
        f"开始筛选 首选路线={ds_label} 板块表={rankings_len} 行({rankings_src}) "
        f"将扫描 {loop_n} 个板块 条件={','.join(sorted(cond_groups))}",
    )
    sectors_detail: list[dict[str, Any]] = []
    ma5_capital_sectors_detail: list[dict[str, Any]] = []
    rising_3d_sectors_detail: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    symbols_order: list[str] = []

    for i in range(loop_n):
        if should_cancel and should_cancel():
            warnings.append("用户已取消热门板块任务，仅返回已处理板块")
            break
        srow = rankings.iloc[i]
        sector_rank = i + 1
        sector_name = str(srow.get("sector_name", "") or "")
        bt = srow.get("board_type")
        bt_arg = str(bt) if bt is not None and str(bt) not in ("", "nan") else None

        sector_metrics = _ranking_row_to_metrics(srow, sector_rank)
        try:
            from app.hot_sectors_job import hot_sectors_job_set_progress

            hot_sectors_job_set_progress(
                done=i,
                total=loop_n,
                current_sector=sector_name,
                message=f"处理板块 {sector_rank}/{loop_n}「{sector_name}」",
            )
        except Exception:  # noqa: BLE001
            pass
        _append_hot_pick_ui_log(
            progress_log,
            f"处理板块 {sector_rank}/{loop_n}「{sector_name}」· 拉取成分股…",
        )

        if is_st_sector_name(sector_name):
            sector_hot_needed = want_sector_hot and i < top_n_sector_hot and not exclude_st
            ma5_needed = want_ma5_capital and not ma5_exclude_st
            rising_needed = want_rising_3d and not rising_3d_exclude_st
            if not (sector_hot_needed or ma5_needed or rising_needed):
                _append_hot_pick_ui_log(
                    progress_log,
                    f"板块#{sector_rank}「{sector_name}」为 ST 板块，整板块跳过",
                )
                continue

        logger.debug(
            "[热门选股] 获取板块#%s「%s」成分股 路线=%s",
            sector_rank,
            sector_name,
            ds_label,
        )

        try:
            cons = datasource.get_sector_constituents(sector_name, bt_arg)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"板块「{sector_name}」成分股获取失败：{exc}")
            _append_hot_pick_ui_log(
                progress_log,
                f"板块#{sector_rank}「{sector_name}」成分股失败：{exc}",
            )
            empty = {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": []}
            if want_sector_hot and i < top_n_sector_hot:
                sectors_detail.append(empty)
            continue

        if cons is None or cons.empty:
            warnings.append(f"板块「{sector_name}」无成分股数据")
            empty = {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": []}
            if want_sector_hot and i < top_n_sector_hot:
                sectors_detail.append(empty)
            continue

        stocks: list[dict[str, Any]] = []
        sector_symbols: list[str] = []
        if want_sector_hot and i < top_n_sector_hot:
            advanced_enabled = (
                sort_by_trend_strength
                or require_technical_pass
                or exclude_overextended
                or enable_liquidity_filter
            )
            if advanced_enabled:
                stocks, sector_symbols = _technical_pick_from_constituents(
                    datasource,
                    cons,
                    sector_rank=sector_rank,
                    sector_name=sector_name,
                    stocks_per_sector=stocks_per_sector,
                    exclude_st=exclude_st,
                    exclude_kcb=exclude_kcb,
                    exclude_cyb=exclude_cyb,
                    sort_by_trend_strength=sort_by_trend_strength,
                    require_technical_pass=require_technical_pass,
                    exclude_overextended=exclude_overextended,
                    max_return_20d_pct=max_return_20d_pct,
                    enable_liquidity_filter=enable_liquidity_filter,
                    min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
                    warnings=warnings,
                    progress_log=progress_log,
                )
            else:
                stocks, sector_symbols = _basic_pick_from_constituents(
                    cons,
                    sector_rank=sector_rank,
                    sector_name=sector_name,
                    stocks_per_sector=stocks_per_sector,
                    exclude_st=exclude_st,
                    exclude_kcb=exclude_kcb,
                    exclude_cyb=exclude_cyb,
                )
                _log_sector_pick_summary(
                    "sector_hot",
                    sector_rank=sector_rank,
                    sector_name=sector_name,
                    selected=stocks,
                    total_scanned=len(cons) if cons is not None else 0,
                    progress_log=progress_log,
                    extra="快速路径（成分顺序）",
                )
            for st in stocks:
                if isinstance(st, dict):
                    st["pick_group"] = "sector_hot"
            for code in sector_symbols:
                if code not in seen_symbols:
                    seen_symbols.add(code)
                    symbols_order.append(code)
            if len(stocks) < stocks_per_sector:
                warnings.append(
                    f"板块「{sector_name}」[热门筛选] 仅 {len(stocks)} 只（目标 {stocks_per_sector}）"
                )
            sectors_detail.append(
                {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": stocks}
            )

        stocks_mc: list[dict[str, Any]] = []
        if want_ma5_capital:
            stocks_mc, mc_syms = _ma5_capital_pick_from_constituents(
                datasource,
                cons,
                sector_rank=sector_rank,
                sector_name=sector_name,
                stocks_per_sector=stocks_per_sector,
                exclude_st=ma5_exclude_st,
                exclude_kcb=ma5_exclude_kcb,
                exclude_cyb=ma5_exclude_cyb,
                ma5_stand_min_days=ma5_stand_min_days,
                capital_lookback_days=capital_flow_lookback_days,
                capital_min_positive_days=capital_min_positive_days,
                require_capital=ma5_require_capital,
                warnings=warnings,
                progress_log=progress_log,
            )
            for code in mc_syms:
                if code not in seen_symbols:
                    seen_symbols.add(code)
                    symbols_order.append(code)
            if stocks_mc:
                ma5_capital_sectors_detail.append(
                    {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": stocks_mc}
                )

        if want_rising_3d:
            stocks_r3, r3_syms = _rising_3d_pick_from_constituents(
                datasource,
                cons,
                sector_rank=sector_rank,
                sector_name=sector_name,
                stocks_per_sector=stocks_per_sector,
                exclude_st=rising_3d_exclude_st,
                exclude_kcb=rising_3d_exclude_kcb,
                exclude_cyb=rising_3d_exclude_cyb,
                rising_min_days=3,
                warnings=warnings,
                progress_log=progress_log,
            )
            for code in r3_syms:
                if code not in seen_symbols:
                    seen_symbols.add(code)
                    symbols_order.append(code)
            if stocks_r3:
                rising_3d_sectors_detail.append(
                    {"sector_rank": sector_rank, "sector_metrics": sector_metrics, "stocks": stocks_r3}
                )

    def _count_stocks(blocks: list[dict[str, Any]]) -> int:
        return sum(len(b.get("stocks") or []) for b in blocks)

    _append_hot_pick_ui_log(
        progress_log,
        f"任务完成：热门筛选 {_count_stocks(sectors_detail)} 只/{len(sectors_detail)} 板块，"
        f"{'五日MA5' if not ma5_require_capital else '五日+资金'} "
        f"{_count_stocks(ma5_capital_sectors_detail)} 只/{len(ma5_capital_sectors_detail)} 板块，"
        f"三日连涨 {_count_stocks(rising_3d_sectors_detail)} 只/{len(rising_3d_sectors_detail)} 板块",
    )

    return HotPickResult(
        sectors_detail=sectors_detail,
        symbols_for_watchlist=symbols_order,
        warnings=warnings,
        ma5_capital_sectors_detail=ma5_capital_sectors_detail,
        rising_3d_sectors_detail=rising_3d_sectors_detail,
        pick_condition_groups=sorted(cond_groups),
        progress_log=list(progress_log or []),
    )
