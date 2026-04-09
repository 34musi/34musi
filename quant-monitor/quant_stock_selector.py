#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A-share hot sector selector and stock evaluator.

This script supports:
1. Ranking hot sectors from AkShare board snapshots.
2. Selecting candidate stocks inside hot or user-selected sectors.
3. Running a simple trend-following backtest for each stock.
4. Exporting ranked candidates for manual review.
"""

from __future__ import annotations

import abc
import argparse
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar

_T = TypeVar("_T")

import numpy as np
import pandas as pd
import requests


TRADING_DAYS_PER_YEAR = 252
DEFAULT_START_DATE = "20230101"
DEFAULT_END_DATE = pd.Timestamp.today().strftime("%Y%m%d")
NEXT_DAY_PRIOR_WIN_RATE = 0.50
NEXT_DAY_PRIOR_SAMPLE_SIZE = 12
NEXT_DAY_FULL_CONFIDENCE_SAMPLE_SIZE = 30
RECENT_TREND_LOOKBACK_DAYS = 22
RECENT_TREND_SPARK_POINTS = 12

PRICE_COLUMN_ALIASES = {
    "date": "date",
    "datetime": "date",
    "日期": "date",
    "交易日期": "date",
    "open": "open",
    "开盘": "open",
    "high": "high",
    "最高": "high",
    "low": "low",
    "最低": "low",
    "close": "close",
    "收盘": "close",
    "latest": "close",
    "最新价": "close",
    "volume": "volume",
    "vol": "volume",
    "成交量": "volume",
    "成交额": "turnover",
    "amount": "turnover",
    "turnover": "turnover",
    "code": "code",
    "symbol": "code",
    "代码": "code",
    "stock_code": "code",
    "name": "name",
    "名称": "name",
    "股票名称": "name",
}


class DataSourceError(RuntimeError):
    """Raised when a market data source cannot serve a request."""


@dataclass
class SectorRecord:
    sector_name: str
    board_type: str
    change_pct: float
    advancers_ratio: float
    leader_change_pct: float
    liquidity_metric: float
    hot_score: float
    source: str


@dataclass
class ScreenMetrics:
    passed: bool
    trend_score: float
    volume_score: float
    risk_score: float
    liquidity_score: float
    overheat_penalty: float
    screen_score: float
    latest_close: float
    ma20: float
    ma60: float
    ma120: float
    return_5d: float
    return_20d: float
    distance_to_60d_high: float
    distance_to_20d_ma: float
    volume_ratio_20_60: float
    avg_turnover_20d: float
    drawdown_60d: float
    annual_volatility_20d: float
    reasons: str


@dataclass
class BacktestMetrics:
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trade_count: int
    win_rate_pct: float
    final_value: float
    backtest_score: float


@dataclass
class NextDayStats:
    pattern_count: int
    next_day_up_prob_pct: float
    next_day_avg_return_pct: float
    next_day_target_hit_pct: float
    next_day_stop_hit_pct: float
    suggested_exit_pct: float
    suggested_exit_rule: str


@dataclass
class StockEvaluation:
    sector_name: str
    board_type: str
    code: str
    name: str
    sector_hot_score: float
    screen_passed: bool
    trend_score: float
    volume_score: float
    risk_score: float
    liquidity_score: float
    overheat_penalty: float
    screen_score: float
    latest_close: float
    return_5d: float
    distance_to_60d_high: float
    distance_to_20d_ma: float
    volume_ratio_20_60: float
    avg_turnover_20d: float
    drawdown_60d: float
    annual_volatility_20d: float
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trade_count: int
    win_rate_pct: float
    next_day_pattern_count: int
    next_day_up_prob_pct: float
    next_day_avg_return_pct: float
    next_day_target_hit_pct: float
    next_day_stop_hit_pct: float
    suggested_exit_pct: float
    suggested_exit_rule: str
    recent_month_return_pct: float
    recent_month_trend: str
    short_term_sample_level: str
    should_reference_next_day_stats: str
    short_term_confidence_hint: str
    final_score: float
    reasons: str


def normalize_code(raw_code: object) -> str:
    digits = "".join(ch for ch in str(raw_code).strip().upper() if ch.isdigit())
    if not digits:
        return ""
    if len(digits) >= 6:
        return digits[-6:]
    return digits.zfill(6)


def to_tushare_code(code: str) -> str:
    if code.startswith(("8", "4")):
        suffix = "BJ"
    elif code.startswith(("6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{normalize_code(code)}.{suffix}"


def safe_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("%", "")
        if not value:
            return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def standardize_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataSourceError("行情数据为空")

    renamed: Dict[str, str] = {}
    for column in frame.columns:
        normalized = PRICE_COLUMN_ALIASES.get(str(column).strip().lower())
        if normalized:
            renamed[column] = normalized
            continue
        normalized = PRICE_COLUMN_ALIASES.get(str(column).strip())
        if normalized:
            renamed[column] = normalized

    standardized = frame.rename(columns=renamed).copy()

    if "date" not in standardized.columns:
        if standardized.index.name:
            standardized = standardized.reset_index()
            standardized = standardized.rename(columns={standardized.columns[0]: "date"})
        else:
            raise DataSourceError("无法识别日期列，请提供 date/日期 列")

    required = {"open", "high", "low", "close"}
    missing = required.difference(standardized.columns)
    if missing:
        raise DataSourceError(f"缺少必要行情列: {', '.join(sorted(missing))}")

    standardized["date"] = pd.to_datetime(standardized["date"], errors="coerce")
    standardized = standardized.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")

    for column in ("open", "high", "low", "close", "volume", "turnover"):
        if column in standardized.columns:
            standardized[column] = pd.to_numeric(standardized[column], errors="coerce")

    if "volume" not in standardized.columns:
        standardized["volume"] = 0.0
    if "turnover" not in standardized.columns:
        standardized["turnover"] = 0.0

    standardized = standardized.dropna(subset=["open", "high", "low", "close"])
    standardized["code"] = standardized.get("code", "")
    standardized["name"] = standardized.get("name", "")
    return standardized[["date", "open", "high", "low", "close", "volume", "turnover", "code", "name"]]


def read_codes_file(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"股票列表文件不存在: {path}")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path)
    else:
        frame = pd.read_csv(path)

    standardized = frame.rename(columns={column: PRICE_COLUMN_ALIASES.get(str(column).strip().lower(), column)
                                         for column in frame.columns})
    standardized = standardized.rename(columns={column: PRICE_COLUMN_ALIASES.get(str(column).strip(), column)
                                                for column in standardized.columns})
    if "code" not in standardized.columns:
        raise DataSourceError("股票列表文件缺少 code/代码 列")

    standardized["code"] = standardized["code"].map(normalize_code)
    if "name" not in standardized.columns:
        standardized["name"] = ""
    standardized = standardized[standardized["code"] != ""].drop_duplicates("code")
    return standardized[["code", "name"]]


def local_history_match_priority(path: Path, normalized_code: str) -> int:
    stem = path.stem.strip().lower()
    code = normalized_code.lower()
    if stem == code:
        return 0
    if (
        stem.startswith(f"{code}_")
        or stem.startswith(f"{code}-")
        or stem.startswith(f"{code}.")
        or code in [token for token in re.split(r"[\W_]+", stem) if token]
    ):
        return 1
    if code in stem:
        return 2
    return 99


def read_local_history_file(path: Path, normalized_code: str) -> pd.DataFrame:
    try:
        if path.suffix.lower() in {".xlsx", ".xls"}:
            frame = pd.read_excel(path)
        else:
            frame = pd.read_csv(path)
        standardized = standardize_price_frame(frame)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(f"本地行情文件不可用: {path} ({exc})") from exc
    standardized["code"] = normalized_code
    return standardized


def load_local_history(code: str, data_dir: Optional[Path]) -> Optional[pd.DataFrame]:
    if data_dir is None or not data_dir.exists():
        return None

    normalized_code = normalize_code(code)
    patterns = [
        f"{normalized_code}.csv",
        f"{normalized_code}.xlsx",
        f"{normalized_code}_*.csv",
        f"*{normalized_code}*.csv",
        f"*{normalized_code}*.xlsx",
    ]

    candidates: List[Path] = []
    for pattern in patterns:
        candidates.extend(data_dir.rglob(pattern))

    seen = set()
    deduped_candidates = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped_candidates.append(candidate)

    prioritized_candidates: Dict[int, List[Path]] = {}
    for candidate in deduped_candidates:
        priority = local_history_match_priority(candidate, normalized_code)
        if priority >= 99:
            continue
        prioritized_candidates.setdefault(priority, []).append(candidate)

    for priority in sorted(prioritized_candidates):
        same_priority_candidates = sorted(prioritized_candidates[priority], key=lambda item: str(item).lower())
        if len(same_priority_candidates) > 1:
            readable = ", ".join(str(item) for item in same_priority_candidates[:5])
            suffix = " ..." if len(same_priority_candidates) > 5 else ""
            raise DataSourceError(
                f"股票 {normalized_code} 的本地行情存在多个同优先级候选文件，请只保留一个最明确的文件: "
                f"{readable}{suffix}"
            )
        return read_local_history_file(same_priority_candidates[0], normalized_code)
    return None


def compute_max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    cummax = series.cummax()
    drawdowns = series / cummax - 1.0
    return abs(float(drawdowns.min()))


def normalize_score(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=float)
    if series.nunique() == 1:
        return pd.Series([50.0] * len(series), index=series.index)
    return (series.rank(pct=True) * 100.0).round(2)


def sample_confidence_weight(sample_count: int, full_confidence_samples: int = NEXT_DAY_FULL_CONFIDENCE_SAMPLE_SIZE) -> float:
    if sample_count <= 0:
        return 0.0
    return min(sample_count / max(full_confidence_samples, 1), 1.0)


def bayesian_success_rate(
    successes: int,
    sample_count: int,
    prior_rate: float = NEXT_DAY_PRIOR_WIN_RATE,
    prior_sample_size: int = NEXT_DAY_PRIOR_SAMPLE_SIZE,
) -> float:
    if sample_count <= 0:
        return prior_rate
    prior_successes = prior_rate * max(prior_sample_size, 0)
    total = sample_count + max(prior_sample_size, 0)
    if total <= 0:
        return prior_rate
    return (successes + prior_successes) / total


def shrink_towards_zero(value: float, sample_count: int) -> float:
    return safe_float(value) * sample_confidence_weight(sample_count)


def build_next_day_signal_score(up_prob_pct: float, sample_count: int) -> float:
    confidence = sample_confidence_weight(sample_count)
    centered = safe_float(up_prob_pct) - 50.0
    return round(float(np.clip(50.0 + centered * confidence, 0.0, 100.0)), 2)


def sample_series_points(values: Sequence[float], target_points: int) -> List[float]:
    if not values:
        return []
    if len(values) <= target_points:
        return list(values)
    positions = np.linspace(0, len(values) - 1, target_points)
    return [float(values[int(round(position))]) for position in positions]


def build_ascii_sparkline(values: Sequence[float], max_points: int = RECENT_TREND_SPARK_POINTS) -> str:
    sampled = sample_series_points(list(values), max_points)
    if len(sampled) < 2:
        return "--"
    low = min(sampled)
    high = max(sampled)
    if math.isclose(low, high):
        return "-" * len(sampled)
    charset = "._-~=^*#"
    spread = high - low
    chars: List[str] = []
    for value in sampled:
        ratio = (value - low) / spread if spread else 0.0
        index = min(int(round(ratio * (len(charset) - 1))), len(charset) - 1)
        chars.append(charset[index])
    return "".join(chars)


def build_recent_month_trend(history: pd.DataFrame) -> Tuple[float, str]:
    data = standardize_price_frame(history)
    closes = data["close"].tail(RECENT_TREND_LOOKBACK_DAYS).dropna()
    if len(closes) < 2:
        return 0.0, "样本不足"
    start_price = float(closes.iloc[0])
    end_price = float(closes.iloc[-1])
    if start_price <= 0:
        return 0.0, "样本不足"
    recent_return = end_price / start_price - 1.0
    if recent_return >= 0.15:
        trend_label = "强势上行"
    elif recent_return >= 0.05:
        trend_label = "震荡上行"
    elif recent_return > -0.05:
        trend_label = "震荡整理"
    elif recent_return > -0.15:
        trend_label = "震荡走弱"
    else:
        trend_label = "明显下行"
    sparkline = build_ascii_sparkline(closes.tolist())
    return recent_return, f"{trend_label} {recent_return * 100:+.2f}% {sparkline}"


def classify_short_term_sample_level(sample_count: int) -> str:
    if sample_count < 10:
        return "很低"
    if sample_count < 20:
        return "偏低"
    if sample_count < 50:
        return "中等"
    if sample_count < 80:
        return "较高"
    return "高"


def should_reference_next_day_stats_label(sample_count: int) -> str:
    if sample_count < 20:
        return "否"
    if sample_count < 30:
        return "谨慎参考"
    return "是"


def build_short_term_confidence_hint(sample_count: int) -> str:
    if sample_count < 10:
        return "样本过少，次日统计仅作观察"
    if sample_count < 20:
        return "样本偏少，建议以技术面和回测为主"
    if sample_count < 30:
        return "样本一般，可辅助参考次日统计"
    if sample_count < 50:
        return "样本尚可，次日统计已有一定参考价值"
    return "样本较充足，次日统计参考价值较高"


def estimate_trade_amount(data: pd.DataFrame) -> pd.Series:
    turnover = data["turnover"].fillna(0.0)
    fallback = data["close"].fillna(0.0) * data["volume"].fillna(0.0)
    if not turnover.gt(0).any():
        return fallback
    estimated = turnover.copy()
    missing = estimated <= 0
    if missing.any():
        estimated.loc[missing] = fallback.loc[missing]
    return estimated


def is_special_treatment_name(name: object) -> bool:
    text = str(name or "").strip()
    compact = text.replace(" ", "").upper()
    return (
        compact.startswith(("ST", "*ST", "S*ST", "ST*"))
        or "退" in text
    )


def filter_tradeable_constituents(constituents: pd.DataFrame, allow_st: bool = False) -> pd.DataFrame:
    if allow_st or constituents.empty or "name" not in constituents.columns:
        return constituents
    names = constituents["name"].fillna("").astype(str)
    filtered = constituents[~names.map(is_special_treatment_name)].copy()
    return filtered.reset_index(drop=True)


EXCLUDED_SECTOR_KEYWORDS = (
    "创业板指",
    "创业板综",
    "创业板50",
    "创业成长",
    "创业成份",
    "科创50",
)


def is_excluded_sector_name(name: object) -> bool:
    text = str(name or "").strip()
    return any(keyword in text for keyword in EXCLUDED_SECTOR_KEYWORDS)


def filter_ranked_sectors(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "sector_name" not in frame.columns:
        return frame
    filtered = frame[~frame["sector_name"].fillna("").astype(str).map(is_excluded_sector_name)].copy()
    return filtered.reset_index(drop=True)


SECTOR_SNAPSHOT_COLUMNS = [
    "sector_name", "board_type", "change_pct", "advancers_ratio",
    "leader_change_pct", "liquidity_metric", "hot_score", "source",
]


def default_sector_snapshot_path(data_source: str, board_type: str) -> Path:
    return Path(f"sector_rankings_snapshot_{data_source}_{board_type}.csv")


def resolve_sector_snapshot_path(args: argparse.Namespace) -> Path:
    if args.sector_snapshot_path:
        return args.sector_snapshot_path
    return default_sector_snapshot_path(args.data_source, args.board_type)


def load_sector_rankings_snapshot(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "liquidity_metric" not in frame.columns and "turnover_rate" in frame.columns:
        frame = frame.rename(columns={"turnover_rate": "liquidity_metric"})
    missing = [column for column in SECTOR_SNAPSHOT_COLUMNS if column not in frame.columns]
    if missing:
        raise DataSourceError(f"板块热度快照缺少必要列: {', '.join(missing)}")
    return frame[SECTOR_SNAPSHOT_COLUMNS].copy()


def save_sector_rankings_snapshot(frame: pd.DataFrame, path: Path) -> None:
    if frame.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frame[SECTOR_SNAPSHOT_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


def get_sector_rankings_with_snapshot(datasource: BaseAShareDataSource, args: argparse.Namespace) -> pd.DataFrame:
    snapshot_path = resolve_sector_snapshot_path(args)
    if args.use_sector_snapshot and snapshot_path.exists():
        try:
            return load_sector_rankings_snapshot(snapshot_path)
        except Exception as exc:
            print(f"读取板块热度快照失败，改为拉取最新数据: {exc}")

    rankings = datasource.get_sector_rankings(args.board_type)
    save_sector_rankings_snapshot(rankings, snapshot_path)
    return rankings


class BaseAShareDataSource(abc.ABC):
    """Abstracts data access for A-share universes, boards and histories."""

    source_name = "base"

    @abc.abstractmethod
    def get_stock_universe(self) -> pd.DataFrame:
        raise NotImplementedError

    @abc.abstractmethod
    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        raise NotImplementedError

    @abc.abstractmethod
    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        raise NotImplementedError

    @abc.abstractmethod
    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        raise NotImplementedError


class AkShareDataSource(BaseAShareDataSource):
    source_name = "akshare"

    _RETRY_ATTEMPTS = 3
    _RETRY_BACKOFF = 2.0

    def __init__(self) -> None:
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise DataSourceError("未安装 akshare，请先安装该依赖后再运行脚本") from exc
        self.ak = ak

    def _retry_call(self, fn: Callable[[], _T], label: str) -> _T:
        """Call *fn* up to _RETRY_ATTEMPTS times with exponential backoff on network errors."""
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._RETRY_ATTEMPTS:
                    wait = self._RETRY_BACKOFF ** attempt
                    print(f"[AkShare] {label} 第 {attempt} 次请求失败，{wait:.0f}s 后重试… ({exc})")
                    time.sleep(wait)
        raise last_exc

    def get_stock_universe(self) -> pd.DataFrame:
        try:
            frame = self._retry_call(self.ak.stock_zh_a_spot_em, "获取 A 股股票池")
        except Exception as exc:
            raise DataSourceError(f"AkShare 获取 A 股股票池失败: {exc}") from exc
        if frame.empty:
            raise DataSourceError("AkShare 未返回 A 股股票池")
        result = frame.rename(columns={"代码": "code", "名称": "name"}).copy()
        result["code"] = result["code"].map(normalize_code)
        return result[["code", "name"]].drop_duplicates("code")

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        frames: List[pd.DataFrame] = []
        try:
            if board_types in {"all", "concept"}:
                frames.append(self._prepare_board_frame(
                    self._retry_call(self.ak.stock_board_concept_name_em, "获取概念板块列表"), "concept"
                ))
            if board_types in {"all", "industry"}:
                frames.append(self._prepare_board_frame(
                    self._retry_call(self.ak.stock_board_industry_name_em, "获取行业板块列表"), "industry"
                ))
        except Exception as exc:
            raise DataSourceError(f"AkShare 获取热门板块失败，请检查网络或稍后重试: {exc}") from exc
        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")

        rankings = filter_ranked_sectors(pd.concat(frames, ignore_index=True))
        rankings["hot_score"] = (
            normalize_score(rankings["change_pct"]) * 0.45
            + normalize_score(rankings["advancers_ratio"]) * 0.20
            + normalize_score(rankings["leader_change_pct"]) * 0.20
            + normalize_score(rankings["liquidity_metric"]) * 0.15
        ).round(2)
        rankings = rankings.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)
        return rankings

    def _prepare_board_frame(self, frame: pd.DataFrame, board_type: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=[
                "sector_name", "board_type", "change_pct", "advancers_ratio",
                "leader_change_pct", "liquidity_metric", "source"
            ])

        columns = {str(column).strip(): column for column in frame.columns}
        sector_col = columns.get("板块名称") or columns.get("名称")
        change_col = columns.get("涨跌幅")
        up_col = columns.get("上涨家数")
        down_col = columns.get("下跌家数")
        leader_col = columns.get("领涨股票-涨跌幅") or columns.get("领涨股票涨跌幅")
        turnover_col = columns.get("换手率")

        prepared = pd.DataFrame({
            "sector_name": frame[sector_col] if sector_col else "",
            "change_pct": frame[change_col].map(safe_float) if change_col else 0.0,
            "up_count": frame[up_col].map(safe_float) if up_col else 0.0,
            "down_count": frame[down_col].map(safe_float) if down_col else 0.0,
            "leader_change_pct": frame[leader_col].map(safe_float) if leader_col else 0.0,
            # Use a source-agnostic liquidity metric here; AkShare provides turnover rate.
            "liquidity_metric": frame[turnover_col].map(safe_float) if turnover_col else 0.0,
        })
        total = (prepared["up_count"] + prepared["down_count"]).replace(0, np.nan)
        prepared["advancers_ratio"] = (prepared["up_count"] / total).fillna(0.5)
        prepared["board_type"] = board_type
        prepared["source"] = self.source_name
        return prepared[[
            "sector_name", "board_type", "change_pct", "advancers_ratio",
            "leader_change_pct", "liquidity_metric", "source"
        ]]

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        board = self._resolve_sector(sector_name, board_type)
        try:
            if board["board_type"] == "concept":
                frame = self._retry_call(
                    lambda: self.ak.stock_board_concept_cons_em(symbol=board["sector_name"]),
                    f"获取概念板块成分股 {board['sector_name']}",
                )
            else:
                frame = self._retry_call(
                    lambda: self.ak.stock_board_industry_cons_em(symbol=board["sector_name"]),
                    f"获取行业板块成分股 {board['sector_name']}",
                )
        except Exception as exc:
            raise DataSourceError(f"AkShare 获取板块成分股失败: {board['sector_name']} - {exc}") from exc
        if frame.empty:
            raise DataSourceError(f"未获取到板块成分股: {board['sector_name']}")
        result = frame.rename(columns={"代码": "code", "名称": "name"}).copy()
        result["code"] = result["code"].map(normalize_code)
        result["sector_name"] = board["sector_name"]
        result["board_type"] = board["board_type"]
        return result[["code", "name", "sector_name", "board_type"]].drop_duplicates("code")

    def _resolve_sector(self, sector_name: str, board_type: Optional[str]) -> pd.Series:
        rankings = self.get_sector_rankings(board_type or "all")
        exact = rankings[rankings["sector_name"].str.lower() == sector_name.lower()]
        if not exact.empty:
            return exact.iloc[0]
        fuzzy = rankings[rankings["sector_name"].str.contains(sector_name, case=False, na=False)]
        if fuzzy.empty:
            raise DataSourceError(f"未找到板块: {sector_name}")
        return fuzzy.sort_values("hot_score", ascending=False).iloc[0]

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        _sym = normalize_code(code)
        try:
            frame = self._retry_call(
                lambda: self.ak.stock_zh_a_hist(
                    symbol=_sym,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                ),
                f"获取股票 {_sym} 历史行情",
            )
        except Exception as exc:
            raise DataSourceError(f"AkShare 获取股票 {_sym} 历史行情失败: {exc}") from exc
        standardized = standardize_price_frame(frame)
        standardized["code"] = normalize_code(code)
        return standardized


class TushareDataSource(BaseAShareDataSource):
    source_name = "tushare"

    def __init__(self, token: Optional[str] = None) -> None:
        token = token or os.getenv("TUSHARE_TOKEN")
        if not token:
            raise DataSourceError("使用 tushare 需要传入 --tushare-token 或设置 TUSHARE_TOKEN")
        try:
            import tushare as ts  # type: ignore
        except ImportError as exc:
            raise DataSourceError("未安装 tushare，请先安装该依赖后再运行脚本") from exc
        ts.set_token(token)
        self.pro = ts.pro_api(token)

    def get_stock_universe(self) -> pd.DataFrame:
        try:
            frame = self.pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name")
        except Exception as exc:
            raise DataSourceError(f"TuShare 获取股票池失败: {exc}") from exc
        if frame.empty:
            raise DataSourceError("TuShare 未返回股票池")
        result = frame.rename(columns={"symbol": "code"})
        result["code"] = result["code"].map(normalize_code)
        return result[["code", "name"]].drop_duplicates("code")

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        raise DataSourceError("第一版暂未通过 TuShare 实现热门板块排序，请优先使用 akshare")

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        raise DataSourceError("第一版暂未通过 TuShare 实现板块成分查询，请优先使用 akshare")

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        ts_code = to_tushare_code(code)
        try:
            frame = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as exc:
            raise DataSourceError(f"TuShare 获取股票 {ts_code} 历史行情失败: {exc}") from exc
        if frame.empty:
            raise DataSourceError(f"TuShare 未返回股票 {ts_code} 的日线数据")
        frame = frame.rename(columns={
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "turnover",
        })
        standardized = standardize_price_frame(frame)
        standardized["code"] = normalize_code(code)
        return standardized


class MootdxDataSource(BaseAShareDataSource):
    """A-share data via Tongdaxin (TDX) protocol using mootdx.

    Price history and real-time quotes come directly from TDX servers —
    more stable than EastMoney HTTP endpoints.

    Sector data is limited to the concept blocks distributed by TDX
    (~20 named concept groups, e.g. 一带一路, 5G技术, 碳中和).  For full
    EastMoney sector coverage use AkShareDataSource instead.
    """

    source_name = "mootdx"

    # TDX protocol max stocks per get_security_quotes call
    _QUOTE_BATCH = 80

    def __init__(self) -> None:
        try:
            from mootdx.quotes import Quotes  # type: ignore
        except ImportError as exc:
            raise DataSourceError("未安装 mootdx，请先执行: pip install mootdx") from exc
        self._Quotes = Quotes
        self._client: Any = None
        self._stock_universe_cache: Optional[pd.DataFrame] = None

    def _get_client(self) -> Any:
        if self._client is None or getattr(self._client, "closed", False):
            self._client = self._Quotes.factory(market="std")
        return self._client

    def _batch_quotes(self, codes: List[str]) -> pd.DataFrame:
        """Fetch real-time quotes for *codes*, batching at _QUOTE_BATCH per call."""
        client = self._get_client()
        frames: List[pd.DataFrame] = []
        for i in range(0, len(codes), self._QUOTE_BATCH):
            batch = codes[i: i + self._QUOTE_BATCH]
            result = client.quotes(symbol=batch)
            if result is not None and not result.empty:
                frames.append(result)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # Valid A-share stock codes: SZ 000/002/003/300/301, SH 600/601/603/605/688,
    # Beijing 430/831/832/833.  Index codes (399xxx, 395xxx, 880xxx…) are excluded.
    _STOCK_CODE_RE = re.compile(
        r"^(00[0-3]\d{3}|300\d{3}|301\d{3}|60[0-9]\d{3}|688\d{3}|430\d{3}|83[0-9]\d{3})$"
    )

    def _load_block_frame(self, block_file: str) -> pd.DataFrame:
        """Download block definition file from TDX server and return a clean frame."""
        client = self._get_client()
        frame = client.block(tofile=block_file)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["blockname", "code"])
        # Keep only rows whose blockname contains at least one CJK character
        # (garbled/numeric entries from the server are skipped this way)
        mask = frame["blockname"].apply(
            lambda n: any("\u4e00" <= ch <= "\u9fff" for ch in str(n))
        )
        clean = frame[mask][["blockname", "code"]].copy()
        clean["code"] = clean["code"].astype(str).str.zfill(6)
        # Drop index / custom-block codes (e.g. 399001, 395xxx, 880xxx)
        clean = clean[clean["code"].apply(lambda c: bool(self._STOCK_CODE_RE.match(c)))]
        return clean

    def get_stock_universe(self) -> pd.DataFrame:
        if self._stock_universe_cache is not None and not self._stock_universe_cache.empty:
            return self._stock_universe_cache.copy()
        client = self._get_client()
        try:
            sz = client.stocks(market=0)
            sh = client.stocks(market=1)
        except Exception as exc:
            raise DataSourceError(f"mootdx 获取 A 股股票池失败: {exc}") from exc
        combined = pd.concat([sz, sh], ignore_index=True)
        if combined.empty:
            raise DataSourceError("mootdx 未返回 A 股股票池")
        combined["code"] = combined["code"].astype(str).str.zfill(6)
        # Keep only real A-share stock codes (start with 0/3/6/4/8, length 6 digits)
        # Exclude index codes like 399xxx, 000xxx > 5999
        combined = combined[combined["code"].str.match(r"^(0[0-9]|3[0-9]|6[0-9]|8[0-8]|4[0-9])\d{4}$")]
        if "name" not in combined.columns:
            combined["name"] = ""
        self._stock_universe_cache = combined[["code", "name"]].drop_duplicates("code").copy()
        return self._stock_universe_cache.copy()

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        frames: List[pd.DataFrame] = []

        if board_types in {"all", "concept"}:
            frames.append(self._build_sector_frame(self._load_block_frame("block_gn.dat"), "concept"))
        if board_types in {"all", "industry"}:
            # TDX block.dat contains style/index groups; use as industry proxy
            frames.append(self._build_sector_frame(self._load_block_frame("block.dat"), "industry"))

        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")

        combined = filter_ranked_sectors(pd.concat([f for f in frames if not f.empty], ignore_index=True))
        if combined.empty:
            raise DataSourceError("mootdx 未返回任何板块数据，请检查网络连接")

        combined["hot_score"] = (
            normalize_score(combined["change_pct"]) * 0.45
            + normalize_score(combined["advancers_ratio"]) * 0.20
            + normalize_score(combined["leader_change_pct"]) * 0.20
            + normalize_score(combined["liquidity_metric"]) * 0.15
        ).round(2)
        return combined.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)

    def _build_sector_frame(self, block_df: pd.DataFrame, board_type: str) -> pd.DataFrame:
        """Given a (blockname, code) frame, fetch quotes and aggregate sector metrics."""
        if block_df.empty:
            return pd.DataFrame()

        unique_codes = block_df["code"].unique().tolist()
        if not unique_codes:
            return pd.DataFrame()

        quotes = self._batch_quotes(unique_codes)
        if quotes.empty:
            return pd.DataFrame()

        # Compute per-stock change_pct and amount from raw quote fields
        quotes = quotes.copy()
        quotes["code"] = quotes["code"].astype(str).str.zfill(6)
        last_close = quotes.get("last_close", quotes.get("pre_close", pd.Series(dtype=float)))
        price = quotes.get("price", pd.Series(dtype=float))
        quotes["_chg"] = (price - last_close) / last_close.replace(0, float("nan")) * 100.0
        quotes["_chg"] = quotes["_chg"].fillna(0.0)
        quotes["_amount"] = quotes.get("amount", pd.Series(0.0, index=quotes.index)).fillna(0.0)

        quote_map = quotes.drop_duplicates("code").set_index("code")[["_chg", "_amount"]].to_dict("index")

        rows: List[Dict] = []
        for sector_name, group in block_df.groupby("blockname"):
            codes = group["code"].tolist()
            chg_vals = [quote_map[c]["_chg"] for c in codes if c in quote_map]
            amt_vals = [quote_map[c]["_amount"] for c in codes if c in quote_map]
            if not chg_vals:
                continue
            chg_arr = np.array(chg_vals, dtype=float)
            up = int((chg_arr > 0).sum())
            down = int((chg_arr < 0).sum())
            total = up + down or 1
            rows.append({
                "sector_name": sector_name,
                "board_type": board_type,
                "change_pct": float(np.mean(chg_arr)),
                "advancers_ratio": up / total,
                "leader_change_pct": float(np.max(chg_arr)),
                # mootdx lacks board-level turnover rate; keep this as a generic liquidity metric.
                "liquidity_metric": float(np.mean(amt_vals)) if amt_vals else 0.0,
                "source": self.source_name,
            })

        return pd.DataFrame(rows)

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        files = []
        if board_type == "concept" or board_type is None:
            files.append(("block_gn.dat", "concept"))
        if board_type == "industry" or board_type is None:
            files.append(("block.dat", "industry"))

        for fname, btype in files:
            block_df = self._load_block_frame(fname)
            exact = block_df[block_df["blockname"].str.lower() == sector_name.lower()]
            if exact.empty:
                exact = block_df[block_df["blockname"].str.contains(sector_name, case=False, na=False)]
            if not exact.empty:
                result = exact[["blockname", "code"]].copy()
                result = result.rename(columns={"blockname": "sector_name"})
                name_map = self.get_stock_universe().set_index("code")["name"].to_dict()
                result["name"] = result["code"].map(name_map).fillna("")
                result["board_type"] = btype
                return result[["code", "name", "sector_name", "board_type"]].drop_duplicates("code")

        raise DataSourceError(f"mootdx 未找到板块: {sector_name}")

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        sym = normalize_code(code)
        # bars() always fetches the LAST N bars from today.
        # Calculate how many calendar days from start_date to today and convert
        # to trading days (~73%) to ensure the full requested range is covered.
        try:
            days_from_start = (pd.Timestamp.today() - pd.Timestamp(start_date)).days
        except Exception:
            days_from_start = 1000
        offset = min(int(days_from_start * 0.75) + 60, 2500)

        client = self._get_client()
        try:
            frame = client.bars(symbol=sym, frequency=9, offset=offset)
        except Exception as exc:
            raise DataSourceError(f"mootdx 获取股票 {sym} 历史行情失败: {exc}") from exc

        if frame is None or frame.empty:
            raise DataSourceError(f"mootdx 未返回股票 {sym} 的历史行情")

        # bars() sets datetime as the index (proper Timestamps) AND also has a
        # "datetime" data column (truncated string like "2026-04-02 15:00"), plus
        # both "vol" and "volume" columns (the same data). Drop the duplicates first,
        # then reset_index to surface the Timestamps from the index.
        drop_cols = [c for c in ["datetime", "volume"] if c in frame.columns]
        if drop_cols:
            frame = frame.drop(columns=drop_cols)
        frame = frame.reset_index()  # index name is "datetime"
        frame = frame.rename(columns={
            "datetime": "date",
            "vol": "volume",
            "amount": "turnover",
        })
        # Normalize date and filter by requested range
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[
            (frame["date"] >= pd.Timestamp(start_date))
            & (frame["date"] <= pd.Timestamp(end_date))
        ].copy()
        standardized = standardize_price_frame(frame)
        standardized["code"] = sym
        return standardized


def get_data_source(name: str, tushare_token: Optional[str] = None) -> BaseAShareDataSource:
    if name == "akshare":
        return AkShareDataSource()
    if name == "tushare":
        return TushareDataSource(token=tushare_token)
    if name == "mootdx":
        return MootdxDataSource()
    raise DataSourceError(f"不支持的数据源: {name}")


def evaluate_screen(
    frame: pd.DataFrame,
    min_avg_turnover_20d: float = 20_000_000.0,
    max_5d_return: float = 0.18,
    max_close_above_ma20: float = 0.12,
) -> ScreenMetrics:
    data = standardize_price_frame(frame).copy()
    if len(data) < 120:
        raise DataSourceError("历史数据少于 120 个交易日，无法做完整的趋势筛选")

    close = data["close"]
    volume = data["volume"].fillna(0)
    trade_amount = estimate_trade_amount(data)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    ret5 = close.pct_change(5).iloc[-1]
    ret20 = close.pct_change(20).iloc[-1]
    rolling_high60 = close.rolling(60).max().iloc[-1]
    distance_to_high = max((rolling_high60 - close.iloc[-1]) / rolling_high60, 0.0)
    avg_volume_20 = volume.rolling(20).mean().iloc[-1]
    avg_volume_60 = volume.rolling(60).mean().iloc[-1]
    volume_ratio = avg_volume_20 / avg_volume_60 if avg_volume_60 else 0.0
    avg_turnover_20 = trade_amount.rolling(20).mean().iloc[-1]
    annual_volatility = close.pct_change().tail(20).std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown_60d = compute_max_drawdown(close.tail(60))

    latest_close = float(close.iloc[-1])
    latest_ma20 = float(ma20.iloc[-1])
    latest_ma60 = float(ma60.iloc[-1])
    latest_ma120 = float(ma120.iloc[-1])
    latest_ret5 = float(ret5)
    latest_ret20 = float(ret20)
    annual_volatility = safe_float(annual_volatility)
    avg_turnover_20 = safe_float(avg_turnover_20)
    distance_to_ma20 = max(latest_close / latest_ma20 - 1.0, 0.0) if latest_ma20 else 0.0

    trend_score = 0.0
    if latest_close > latest_ma20:
        trend_score += 20.0
    if latest_ma20 > latest_ma60:
        trend_score += 20.0
    if latest_ma60 > latest_ma120:
        trend_score += 15.0
    if latest_ret20 > 0:
        trend_score += 15.0

    volume_score = 0.0
    if volume_ratio >= 1.2:
        volume_score += 20.0
    elif volume_ratio >= 0.95:
        volume_score += 12.0
    elif volume_ratio >= 0.8:
        volume_score += 6.0
    if distance_to_high <= 0.05:
        volume_score += 15.0
    elif distance_to_high <= 0.10:
        volume_score += 8.0

    risk_score = 0.0
    if drawdown_60d <= 0.10:
        risk_score += 20.0
    elif drawdown_60d <= 0.18:
        risk_score += 10.0
    if annual_volatility <= 0.25:
        risk_score += 15.0
    elif annual_volatility <= 0.35:
        risk_score += 8.0

    liquidity_score = 0.0
    if avg_turnover_20 >= min_avg_turnover_20d * 3.0:
        liquidity_score += 12.0
    elif avg_turnover_20 >= min_avg_turnover_20d * 1.5:
        liquidity_score += 8.0
    elif avg_turnover_20 >= min_avg_turnover_20d:
        liquidity_score += 5.0

    overheat_penalty = 0.0
    if latest_ret5 > max_5d_return:
        overheat_penalty += 12.0
    elif latest_ret5 > max_5d_return * 0.8:
        overheat_penalty += 6.0
    if distance_to_ma20 > max_close_above_ma20:
        overheat_penalty += 10.0
    elif distance_to_ma20 > max_close_above_ma20 * 0.8:
        overheat_penalty += 5.0

    reasons = []
    if latest_close <= latest_ma20 or latest_ma20 <= latest_ma60:
        reasons.append("均线趋势不够强")
    if distance_to_high > 0.12:
        reasons.append("离 60 日新高较远")
    if drawdown_60d > 0.18:
        reasons.append("近 60 日回撤偏大")
    if annual_volatility > 0.35:
        reasons.append("近 20 日波动偏大")
    if volume_ratio < 0.8:
        reasons.append("量能偏弱")
    if avg_turnover_20 < min_avg_turnover_20d:
        reasons.append("近 20 日成交额不足")
    if latest_ret5 > max_5d_return:
        reasons.append("近 5 日涨幅过快")
    if distance_to_ma20 > max_close_above_ma20:
        reasons.append("离 20 日线过远")

    essential_pass = (
        latest_close > latest_ma20 > latest_ma60
        and latest_ret20 > 0
        and distance_to_high <= 0.12
        and drawdown_60d <= 0.18
        and avg_turnover_20 >= min_avg_turnover_20d
        and latest_ret5 <= max_5d_return * 1.25
        and distance_to_ma20 <= max_close_above_ma20 * 1.25
    )

    screen_score = max(trend_score + volume_score + risk_score + liquidity_score - overheat_penalty, 0.0)

    return ScreenMetrics(
        passed=bool(essential_pass),
        trend_score=round(trend_score, 2),
        volume_score=round(volume_score, 2),
        risk_score=round(risk_score, 2),
        liquidity_score=round(liquidity_score, 2),
        overheat_penalty=round(overheat_penalty, 2),
        screen_score=round(screen_score, 2),
        latest_close=round(latest_close, 2),
        ma20=round(latest_ma20, 2),
        ma60=round(latest_ma60, 2),
        ma120=round(latest_ma120, 2),
        return_5d=round(latest_ret5 * 100.0, 2),
        return_20d=round(latest_ret20 * 100.0, 2),
        distance_to_60d_high=round(distance_to_high * 100.0, 2),
        distance_to_20d_ma=round(distance_to_ma20 * 100.0, 2),
        volume_ratio_20_60=round(volume_ratio, 2),
        avg_turnover_20d=round(avg_turnover_20, 2),
        drawdown_60d=round(drawdown_60d * 100.0, 2),
        annual_volatility_20d=round(annual_volatility * 100.0, 2),
        reasons="、".join(reasons) if reasons else "趋势、量能和风险指标均达标",
    )


def run_sma_backtest(
    frame: pd.DataFrame,
    fast_period: int = 10,
    slow_period: int = 30,
    initial_cash: float = 100000.0,
    commission: float = 0.001,
    stop_loss: float = 0.08,
    slippage: float = 0.001,
) -> BacktestMetrics:
    data = standardize_price_frame(frame).copy()
    if len(data) < slow_period + 5:
        raise DataSourceError("历史数据不足，无法运行双均线回测")

    data["ma_fast"] = data["close"].rolling(fast_period).mean()
    data["ma_slow"] = data["close"].rolling(slow_period).mean()
    data["ma20"] = data["close"].rolling(20).mean()
    data["ma60"] = data["close"].rolling(60).mean()
    data["ma20_slope"] = data["ma20"] - data["ma20"].shift(5)

    cash = initial_cash
    shares = 0.0
    entry_price = 0.0
    entry_value = 0.0
    equity_curve: List[float] = []
    trades: List[float] = []
    below_ma20_streak = 0

    for row in data.itertuples():
        close_price = float(row.close)
        current_value = cash + shares * close_price
        can_trade = not any(pd.isna(value) for value in (row.ma_fast, row.ma_slow, row.ma20, row.ma60, row.ma20_slope))

        if can_trade and close_price < row.ma20:
            below_ma20_streak += 1
        elif can_trade:
            below_ma20_streak = 0

        if can_trade and shares == 0.0:
            if row.ma_fast > row.ma_slow and close_price > row.ma20 > row.ma60 and row.ma20_slope > 0:
                entry_price = close_price * (1.0 + slippage)
                shares = (cash * (1.0 - commission)) / entry_price
                entry_value = cash
                cash = 0.0
                current_value = shares * close_price
        elif can_trade and shares > 0.0:
            should_exit = (
                row.ma_fast < row.ma_slow
                or below_ma20_streak >= 2
                or close_price < row.ma60
                or close_price <= entry_price * (1.0 - stop_loss)
            )
            if should_exit:
                exit_price = close_price * (1.0 - slippage)
                cash = shares * exit_price * (1.0 - commission)
                if entry_value:
                    trades.append(cash / entry_value - 1.0)
                shares = 0.0
                entry_price = 0.0
                entry_value = 0.0
                current_value = cash

        equity_curve.append(current_value)

    if shares > 0.0:
        final_close = float(data["close"].iloc[-1]) * (1.0 - slippage)
        cash = shares * final_close * (1.0 - commission)
        if entry_value:
            trades.append(cash / entry_value - 1.0)
        shares = 0.0
        equity_curve[-1] = cash

    equity = pd.Series(equity_curve, index=data["date"])
    daily_returns = equity.pct_change().fillna(0.0)
    total_return = cash / initial_cash - 1.0
    years = max(len(data) / TRADING_DAYS_PER_YEAR, 1.0 / TRADING_DAYS_PER_YEAR)
    annual_return = (cash / initial_cash) ** (1.0 / years) - 1.0
    max_drawdown = compute_max_drawdown(equity)
    sharpe_ratio = 0.0
    if daily_returns.std(ddof=0) > 0:
        sharpe_ratio = daily_returns.mean() / daily_returns.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)
    trade_count = len(trades)
    win_rate = sum(1 for trade in trades if trade > 0) / trade_count if trade_count else 0.0

    return_score = np.clip(annual_return * 150.0 + 50.0, 0.0, 100.0)
    sharpe_score = np.clip(sharpe_ratio * 20.0 + 50.0, 0.0, 100.0)
    drawdown_score = np.clip(100.0 - max_drawdown * 200.0, 0.0, 100.0)
    backtest_score = round(return_score * 0.45 + sharpe_score * 0.25 + drawdown_score * 0.30, 2)

    return BacktestMetrics(
        total_return_pct=round(total_return * 100.0, 2),
        annual_return_pct=round(annual_return * 100.0, 2),
        max_drawdown_pct=round(max_drawdown * 100.0, 2),
        sharpe_ratio=round(sharpe_ratio, 3),
        trade_count=trade_count,
        win_rate_pct=round(win_rate * 100.0, 2),
        final_value=round(cash, 2),
        backtest_score=backtest_score,
    )


def analyze_next_day_edge(
    frame: pd.DataFrame,
    commission: float = 0.001,
    slippage: float = 0.001,
    next_day_stop_loss: float = 0.02,
    next_day_target_pct: float = 0.03,
) -> NextDayStats:
    data = standardize_price_frame(frame).copy()
    if len(data) < 90:
        raise DataSourceError("历史数据不足，无法统计次日上涨概率")

    close = data["close"]
    open_ = data["open"]
    high = data["high"]
    low = data["low"]
    volume = data["volume"].fillna(0.0)
    data["ma20"] = close.rolling(20).mean()
    data["ma60"] = close.rolling(60).mean()
    data["ret1"] = close.pct_change()
    data["ret3"] = close.pct_change(3)
    data["ret5"] = close.pct_change(5)
    data["ret20"] = close.pct_change(20)
    data["high60"] = close.rolling(60).max()
    data["dist_to_high60"] = (data["high60"] - close) / data["high60"].replace(0, np.nan)
    data["avg_vol20"] = volume.rolling(20).mean()
    data["vol_ratio_day"] = volume / data["avg_vol20"].replace(0, np.nan)
    intraday_range = (high - low).replace(0, np.nan)
    data["close_strength"] = (close - low) / intraday_range
    data["body_pct"] = (close - open_) / open_.replace(0, np.nan)
    data["high_breakout"] = high / data["high60"].replace(0, np.nan) - 1.0

    next_day_returns: List[float] = []
    next_day_target_hits: List[bool] = []
    next_day_stop_hits: List[bool] = []
    loser_drawdowns: List[float] = []
    gap_up_weak_close_hits: List[bool] = []
    spike_and_fade_hits: List[bool] = []
    close_not_at_high_hits: List[bool] = []

    for idx in range(60, len(data) - 1):
        row = data.iloc[idx]
        prev = data.iloc[idx - 1]
        nxt = data.iloc[idx + 1]
        values = (
            row["close"], row["ma20"], row["ma60"], row["ret1"], row["ret3"], row["ret5"], row["ret20"],
            row["dist_to_high60"], row["vol_ratio_day"], row["close_strength"], row["body_pct"], row["high_breakout"],
            prev["ret1"], nxt["open"], nxt["close"], nxt["high"], nxt["low"],
        )
        if any(pd.isna(v) for v in values):
            continue

        # 更龙头版：偏抓涨停、接近涨停、爆量题材股。
        # 典型特征：
        # 1. 多头趋势非常明确
        # 2. 最近 3/5/20 天都处于主升段
        # 3. 当天大阳或接近涨停，且量能显著放大
        # 4. 收盘靠近最高，说明封单/承接/情绪都更强
        # 5. 前一日已经有强势表现，模拟连板或主升加速
        signal_day = (
            row["close"] > row["ma20"] > row["ma60"]
            and row["ret20"] >= 0.12
            and row["ret5"] >= 0.08
            and row["ret3"] >= 0.05
            and 0.04 <= row["ret1"] <= 0.098
            and row["dist_to_high60"] <= 0.02
            and row["vol_ratio_day"] >= 2.0
            and row["close_strength"] >= 0.78
            and row["body_pct"] >= 0.025
            and (prev["ret1"] >= 0.02 or row["high_breakout"] >= 0.0)
        )
        if not signal_day:
            continue

        signal_close = float(row["close"])
        next_open = float(nxt["open"])
        if next_open <= 0:
            continue
        # Signal is only known after the close, so evaluate the trade from next day's open.
        entry_price = next_open * (1.0 + commission + slippage)
        exit_cost_multiplier = max(1.0 - commission - slippage, 0.0)
        next_open_gap = next_open / signal_close - 1.0
        next_close_ret = float(nxt["close"]) * exit_cost_multiplier / entry_price - 1.0
        next_high_ret = float(nxt["high"]) * exit_cost_multiplier / entry_price - 1.0
        next_low_ret = float(nxt["low"]) * exit_cost_multiplier / entry_price - 1.0
        close_from_open_ret = float(nxt["close"]) / next_open - 1.0
        next_intraday_range = max(float(nxt["high"]) - float(nxt["low"]), 1e-9)
        next_close_strength = (float(nxt["close"]) - float(nxt["low"])) / next_intraday_range

        next_day_returns.append(next_close_ret)
        next_day_target_hits.append(next_high_ret >= next_day_target_pct)
        next_day_stop_hits.append(next_low_ret <= -next_day_stop_loss)
        gap_up_weak_close_hits.append(next_open_gap >= 0.015 and close_from_open_ret <= 0.0)
        spike_and_fade_hits.append(next_high_ret >= next_day_target_pct and next_close_strength <= 0.35)
        close_not_at_high_hits.append(next_high_ret >= 0.015 and (next_high_ret - next_close_ret) >= 0.02)
        if next_close_ret <= 0:
            loser_drawdowns.append(abs(min(next_low_ret, 0.0)))

    pattern_count = len(next_day_returns)
    if pattern_count == 0:
        return NextDayStats(
            pattern_count=0,
            next_day_up_prob_pct=0.0,
            next_day_avg_return_pct=0.0,
            next_day_target_hit_pct=0.0,
            next_day_stop_hit_pct=0.0,
            suggested_exit_pct=round(next_day_stop_loss * 100.0, 2),
            suggested_exit_rule=f"样本不足，先按次日跌破 {next_day_stop_loss * 100:.2f}% 止损出票",
        )

    confidence = sample_confidence_weight(pattern_count)
    up_count = sum(1 for item in next_day_returns if item > 0)
    next_day_up_prob = bayesian_success_rate(up_count, pattern_count)
    next_day_avg_return = shrink_towards_zero(float(np.mean(next_day_returns)), pattern_count)
    next_day_target_hit = shrink_towards_zero(sum(next_day_target_hits) / pattern_count, pattern_count)
    next_day_stop_hit = shrink_towards_zero(sum(next_day_stop_hits) / pattern_count, pattern_count)
    gap_up_weak_close_rate = shrink_towards_zero(sum(gap_up_weak_close_hits) / pattern_count, pattern_count)
    spike_and_fade_rate = shrink_towards_zero(sum(spike_and_fade_hits) / pattern_count, pattern_count)
    close_not_at_high_rate = shrink_towards_zero(sum(close_not_at_high_hits) / pattern_count, pattern_count)

    if loser_drawdowns:
        adaptive_exit = float(np.percentile(loser_drawdowns, 70)) * 100.0
        adaptive_exit = min(max(adaptive_exit, 1.2), next_day_stop_loss * 100.0)
        suggested_exit_pct = (next_day_stop_loss * 100.0) * (1.0 - confidence) + adaptive_exit * confidence
    else:
        suggested_exit_pct = next_day_stop_loss * 100.0

    exit_clauses = [
        f"盘中跌破开仓价 {suggested_exit_pct:.2f}% 附近出票",
    ]
    if gap_up_weak_close_rate >= 0.25:
        exit_clauses.append("次日若高开超过 1.5% 但快速走弱，优先减仓")
    if spike_and_fade_rate >= 0.2:
        exit_clauses.append("若盘中冲高达到目标位后明显回落，按冲高回落处理")
    if close_not_at_high_rate >= 0.3:
        exit_clauses.append("若收盘明显离最高点较远，不再恋战")

    return NextDayStats(
        pattern_count=pattern_count,
        next_day_up_prob_pct=round(next_day_up_prob * 100.0, 2),
        next_day_avg_return_pct=round(next_day_avg_return * 100.0, 2),
        next_day_target_hit_pct=round(next_day_target_hit * 100.0, 2),
        next_day_stop_hit_pct=round(next_day_stop_hit * 100.0, 2),
        suggested_exit_pct=round(suggested_exit_pct, 2),
        suggested_exit_rule=f"强势龙头打法（样本数={pattern_count}，已做样本折扣）：" + "；".join(exit_clauses),
    )


def compose_final_score(sector_hot_score: float, screen_score: float, backtest_score: float) -> float:
    return round(sector_hot_score * 0.15 + screen_score * 0.45 + backtest_score * 0.40, 2)


def evaluate_stock(
    code: str,
    name: str,
    sector: SectorRecord,
    history: pd.DataFrame,
    args: argparse.Namespace,
) -> StockEvaluation:
    screen = evaluate_screen(
        history,
        min_avg_turnover_20d=args.min_avg_turnover_20d,
        max_5d_return=args.max_5d_return,
        max_close_above_ma20=args.max_close_above_ma20,
    )
    backtest = run_sma_backtest(
        history,
        fast_period=args.fast_period,
        slow_period=args.slow_period,
        initial_cash=args.initial_cash,
        commission=args.commission,
        stop_loss=args.stop_loss,
        slippage=args.slippage,
    )
    next_day = analyze_next_day_edge(
        history,
        commission=args.commission,
        slippage=args.slippage,
        next_day_stop_loss=args.next_day_stop_loss,
        next_day_target_pct=args.next_day_target_pct,
    )
    final_score = compose_final_score(sector.hot_score, screen.screen_score, backtest.backtest_score)
    if not screen.passed:
        final_score = round(final_score * 0.75, 2)
    if next_day.pattern_count > 0:
        next_day_signal_score = build_next_day_signal_score(next_day.next_day_up_prob_pct, next_day.pattern_count)
        final_score = round(final_score * 0.8 + next_day_signal_score * 0.2, 2)
    recent_month_return, recent_month_trend = build_recent_month_trend(history)
    short_term_sample_level = classify_short_term_sample_level(next_day.pattern_count)
    should_reference_next_day_stats = should_reference_next_day_stats_label(next_day.pattern_count)
    short_term_confidence_hint = build_short_term_confidence_hint(next_day.pattern_count)

    return StockEvaluation(
        sector_name=sector.sector_name,
        board_type=sector.board_type,
        code=code,
        name=name,
        sector_hot_score=sector.hot_score,
        screen_passed=screen.passed,
        trend_score=screen.trend_score,
        volume_score=screen.volume_score,
        risk_score=screen.risk_score,
        liquidity_score=screen.liquidity_score,
        overheat_penalty=screen.overheat_penalty,
        screen_score=screen.screen_score,
        latest_close=screen.latest_close,
        return_5d=screen.return_5d,
        distance_to_60d_high=screen.distance_to_60d_high,
        distance_to_20d_ma=screen.distance_to_20d_ma,
        volume_ratio_20_60=screen.volume_ratio_20_60,
        avg_turnover_20d=screen.avg_turnover_20d,
        drawdown_60d=screen.drawdown_60d,
        annual_volatility_20d=screen.annual_volatility_20d,
        total_return_pct=backtest.total_return_pct,
        annual_return_pct=backtest.annual_return_pct,
        max_drawdown_pct=backtest.max_drawdown_pct,
        sharpe_ratio=backtest.sharpe_ratio,
        trade_count=backtest.trade_count,
        win_rate_pct=backtest.win_rate_pct,
        next_day_pattern_count=next_day.pattern_count,
        next_day_up_prob_pct=next_day.next_day_up_prob_pct,
        next_day_avg_return_pct=next_day.next_day_avg_return_pct,
        next_day_target_hit_pct=next_day.next_day_target_hit_pct,
        next_day_stop_hit_pct=next_day.next_day_stop_hit_pct,
        suggested_exit_pct=next_day.suggested_exit_pct,
        suggested_exit_rule=next_day.suggested_exit_rule,
        recent_month_return_pct=round(recent_month_return * 100.0, 2),
        recent_month_trend=recent_month_trend,
        short_term_sample_level=short_term_sample_level,
        should_reference_next_day_stats=should_reference_next_day_stats,
        short_term_confidence_hint=short_term_confidence_hint,
        final_score=final_score,
        reasons=screen.reasons,
    )


def build_sector_records(frame: pd.DataFrame) -> List[SectorRecord]:
    records: List[SectorRecord] = []
    for row in frame.itertuples():
        records.append(SectorRecord(
            sector_name=row.sector_name,
            board_type=row.board_type,
            change_pct=round(safe_float(row.change_pct), 2),
            advancers_ratio=round(safe_float(row.advancers_ratio) * 100.0, 2),
            leader_change_pct=round(safe_float(row.leader_change_pct), 2),
            liquidity_metric=round(safe_float(row.liquidity_metric), 2),
            hot_score=round(safe_float(row.hot_score), 2),
            source=getattr(row, "source", "unknown"),
        ))
    return records


SECTOR_DISPLAY_COLUMNS = [
    "sector_name", "board_type", "hot_score", "change_pct",
    "advancers_ratio", "leader_change_pct", "liquidity_metric",
]

SECTOR_DISPLAY_NAMES = {
    "sector_name": "板块名称",
    "board_type": "板块类型",
    "hot_score": "热度得分",
    "change_pct": "涨跌幅(%)",
    "advancers_ratio": "上涨家数占比(%)",
    "leader_change_pct": "龙头涨幅(%)",
    "liquidity_metric": "流动性指标",
}

STOCK_DISPLAY_COLUMNS = [
    "sector_name", "code", "name", "screen_passed", "sector_hot_score",
    "screen_score", "next_day_pattern_count", "short_term_sample_level",
    "should_reference_next_day_stats", "recent_month_trend", "next_day_up_prob_pct", "next_day_avg_return_pct",
    "suggested_exit_pct", "annual_return_pct", "max_drawdown_pct", "sharpe_ratio", "final_score",
]

STOCK_DISPLAY_NAMES = {
    "sector_name": "所属板块",
    "code": "代码",
    "name": "名称",
    "screen_passed": "初筛通过",
    "sector_hot_score": "板块热度分",
    "screen_score": "技术面得分",
    "next_day_pattern_count": "次日样本数",
    "short_term_sample_level": "短线样本等级",
    "should_reference_next_day_stats": "是否建议参考次日统计",
    "recent_month_trend": "近一月走势",
    "next_day_up_prob_pct": "次日上涨概率(折扣后%)",
    "next_day_avg_return_pct": "次日平均涨跌幅(折扣后%)",
    "suggested_exit_pct": "建议出票阈值(%)",
    "annual_return_pct": "年化收益率(%)",
    "max_drawdown_pct": "最大回撤(%)",
    "sharpe_ratio": "夏普比率",
    "final_score": "综合得分",
}


BOARD_TYPE_LABELS = {
    "industry": "行业板块",
    "concept": "概念板块",
    "custom": "自定义股票池",
    "all": "全部板块",
}


def localize_board_type(value: object) -> str:
    text = str(value or "").strip().lower()
    return BOARD_TYPE_LABELS.get(text, str(value or ""))


def localize_display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    localized = frame.copy()
    if "board_type" in localized.columns:
        localized["board_type"] = localized["board_type"].map(localize_board_type)
    return localized


def format_table_value(value: object) -> str:
    if isinstance(value, (bool, np.bool_)):
        return "是" if value else "否"
    if value is None:
        return "-"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "-"
        if abs(value) >= 1000:
            return f"{value:.0f}"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def display_text_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def truncate_display_text(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if display_text_width(text) <= max_width:
        return text
    ellipsis = "..."
    ellipsis_width = display_text_width(ellipsis)
    if max_width <= ellipsis_width:
        return "." * max_width
    trimmed = ""
    current_width = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if current_width + char_width + ellipsis_width > max_width:
            break
        trimmed += char
        current_width += char_width
    return trimmed + ellipsis


def pad_display_text(text: str, width: int, align: str = "left") -> str:
    normalized = truncate_display_text(text, width)
    padding = max(width - display_text_width(normalized), 0)
    if align == "right":
        return " " * padding + normalized
    if align == "center":
        left = padding // 2
        right = padding - left
        return " " * left + normalized + " " * right
    return normalized + " " * padding


def print_pretty_table(
    frame: pd.DataFrame,
    column_names: Dict[str, str],
    alignments: Optional[Dict[str, str]] = None,
    preferred_widths: Optional[Dict[str, int]] = None,
    min_widths: Optional[Dict[str, int]] = None,
) -> None:
    if frame.empty:
        print("(无数据)")
        return

    alignments = alignments or {}
    preferred_widths = preferred_widths or {}
    min_widths = min_widths or {}

    columns = list(column_names.keys())
    headers = [column_names[column] for column in columns]
    rows = [
        [format_table_value(row[column]) for column in columns]
        for row in frame.to_dict("records")
    ]

    widths: List[int] = []
    minimums: List[int] = []
    for column, header in zip(columns, headers):
        content_width = max(display_text_width(header), *(display_text_width(row[columns.index(column)]) for row in rows))
        preferred = preferred_widths.get(column, content_width)
        minimum = min_widths.get(column, min(display_text_width(header), max(preferred, 4)))
        widths.append(max(min(content_width, preferred), minimum))
        minimums.append(minimum)

    terminal_width = max(shutil.get_terminal_size((140, 20)).columns - 2, 60)
    total_width = sum(widths) + 3 * (len(columns) - 1)
    while total_width > terminal_width:
        shrinkable = [
            index for index, width in enumerate(widths)
            if width > minimums[index]
        ]
        if not shrinkable:
            break
        shrink_index = max(shrinkable, key=lambda index: widths[index] - minimums[index])
        widths[shrink_index] -= 1
        total_width -= 1

    header_line = " | ".join(
        pad_display_text(header, widths[index], "center")
        for index, header in enumerate(headers)
    )
    separator_line = "-+-".join("-" * width for width in widths)
    print(header_line)
    print(separator_line)
    for row in rows:
        print(
            " | ".join(
                pad_display_text(
                    row[index],
                    widths[index],
                    alignments.get(columns[index], "left"),
                )
                for index in range(len(columns))
            )
        )


def filter_display_stocks(stocks: Sequence[StockEvaluation], only_passed: bool) -> List[StockEvaluation]:
    if not only_passed:
        return list(stocks)
    return [stock for stock in stocks if stock.screen_passed]


def to_itick_region(code: str) -> str:
    normalized = normalize_code(code)
    if normalized.startswith(("6", "9")):
        return "SH"
    if normalized.startswith(("0", "2", "3")):
        return "SZ"
    return ""


def to_itick_symbol(code: str) -> str:
    normalized = normalize_code(code)
    region = to_itick_region(normalized)
    if not normalized or not region:
        return ""
    return f"{normalized}${region}"


def select_itick_candidates(stocks: Sequence[StockEvaluation], max_symbols: int) -> List[StockEvaluation]:
    passed = sorted((stock for stock in stocks if stock.screen_passed), key=lambda item: item.final_score, reverse=True)
    selected: List[StockEvaluation] = []
    seen_codes = set()
    for stock in passed:
        symbol = to_itick_symbol(stock.code)
        if not symbol or stock.code in seen_codes:
            continue
        seen_codes.add(stock.code)
        selected.append(stock)
        if len(selected) >= max_symbols:
            break
    return selected


def build_analysis_summary(
    sectors: Sequence[SectorRecord],
    stocks: Sequence[StockEvaluation],
    only_passed: bool,
    top_stocks: int,
) -> str:
    display_stocks = filter_display_stocks(stocks, only_passed)
    lines = [
        "量化选股结果通知",
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"热门板块数量: {len(sectors)}",
        f"候选股票数量: {len(display_stocks)}",
        "",
        "热门板块:",
    ]
    if sectors:
        for sector in sectors[:5]:
            lines.append(
                f"- {sector.sector_name}({localize_board_type(sector.board_type)}), "
                f"热度={sector.hot_score:.2f}, 涨跌幅={sector.change_pct:.2f}%, "
                f"流动性指标={sector.liquidity_metric:.2f}"
            )
    else:
        lines.append("- 无")

    lines.append("")
    lines.append("候选股票:")
    if display_stocks:
        for stock in display_stocks[:top_stocks]:
            lines.append(
                f"- {stock.code} {stock.name}, 板块={stock.sector_name}, "
                f"综合分={stock.final_score:.2f}, 次日样本数={stock.next_day_pattern_count}, "
                f"短线样本等级={stock.short_term_sample_level}, "
                f"建议参考次日统计={stock.should_reference_next_day_stats}, "
                f"近一月走势={stock.recent_month_trend}, "
                f"折扣后次日上涨概率={stock.next_day_up_prob_pct:.2f}%, "
                f"出票阈值={stock.suggested_exit_pct:.2f}%, 初筛={'通过' if stock.screen_passed else '未通过'}"
            )
    else:
        lines.append("- 无")
    return "\n".join(lines)


def send_wecom_webhook(webhook_url: str, content: str, timeout: int = 15) -> None:
    response = requests.post(
        webhook_url,
        json={
            "msgtype": "text",
            "text": {"content": content[:3500]},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errcode") != 0:
        raise DataSourceError(f"企业微信 webhook 发送失败: {payload}")


def subscribe_itick_ticks(stocks: Sequence[StockEvaluation], args: argparse.Namespace) -> List[str]:
    token = (args.itick_token or os.getenv("ITICK_TOKEN", "")).strip()
    if not token:
        raise DataSourceError("启用 iTick 订阅时必须提供 --itick-token 或设置 ITICK_TOKEN 环境变量")

    candidates = select_itick_candidates(stocks, args.itick_max_symbols)
    if not candidates:
        print("\n没有可用于 iTick 订阅的初筛通过股票")
        return []

    symbols = [to_itick_symbol(stock.code) for stock in candidates]
    subscribed_types = ",".join(sorted({item.strip() for item in args.itick_types.split(",") if item.strip()})) or "tick"
    latest_ticks: Dict[str, str] = {}

    print("\niTick 订阅股票:")
    for stock, symbol in zip(candidates, symbols):
        print(f"  {stock.code} {stock.name} -> {symbol}")

    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise DataSourceError("缺少 websocket-client 依赖，无法进行 iTick WebSocket 订阅") from exc

    stop_event = threading.Event()
    timer: Optional[threading.Timer] = None

    def send_ping(ws_app: Any) -> None:
        while not stop_event.wait(args.itick_ping_interval):
            if getattr(ws_app, "sock", None) and ws_app.sock and ws_app.sock.connected:
                timestamp = str(int(time.time() * 1000))
                ws_app.send(json.dumps({"ac": "ping", "params": timestamp}))

    def on_open(_ws_app: Any) -> None:
        print(f"\n已连接 iTick WebSocket: {args.itick_ws_url}")

    def on_message(ws_app: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            print(f"[iTick] 原始消息: {message}")
            return

        if payload.get("msg") == "Connected Successfully":
            print("[iTick] 连接成功，等待鉴权")
            return
        if payload.get("resAc") == "auth":
            if payload.get("code") == 1:
                print("[iTick] 鉴权成功，发送订阅请求")
                ws_app.send(json.dumps({
                    "ac": "subscribe",
                    "params": ",".join(symbols),
                    "types": subscribed_types,
                }))
            else:
                print(f"[iTick] 鉴权失败: {payload.get('msg')}")
                ws_app.close()
            return
        if payload.get("resAc") == "subscribe":
            print(f"[iTick] 订阅结果: {payload.get('msg')}")
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            return
        data_type = str(data.get("type") or "").strip().lower()
        if data_type == "tick":
            symbol = data.get("s", "")
            latest = data.get("ld", "")
            volume = data.get("v", "")
            timestamp = data.get("t", "")
            tick_line = f"{symbol} 最新价={latest} 成交量={volume} 时间={timestamp}"
            latest_ticks[str(symbol)] = tick_line
            print(f"[Tick] {tick_line}")
        elif args.itick_print_raw:
            print(f"[iTick] {json.dumps(payload, ensure_ascii=False)}")

    def on_error(_ws_app: Any, error: Any) -> None:
        print(f"[iTick] WebSocket 错误: {error}")

    def on_close(_ws_app: Any, close_status_code: Any, close_msg: Any) -> None:
        stop_event.set()
        print(f"[iTick] 连接关闭: code={close_status_code}, msg={close_msg}")

    ws_app = websocket.WebSocketApp(
        args.itick_ws_url,
        header={"token": token},
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    ping_thread = threading.Thread(target=send_ping, args=(ws_app,), daemon=True)
    ping_thread.start()

    if args.itick_duration > 0:
        timer = threading.Timer(args.itick_duration, ws_app.close)
        timer.daemon = True
        timer.start()
        print(f"[iTick] 将在 {args.itick_duration} 秒后自动断开")
    else:
        print("[iTick] 未设置自动断开时间，按 Ctrl+C 停止")

    try:
        ws_app.run_forever()
    except KeyboardInterrupt:
        print("\n[iTick] 收到中断，正在关闭连接")
        ws_app.close()
    finally:
        stop_event.set()
        if timer is not None:
            timer.cancel()
    return [latest_ticks[key] for key in sorted(latest_ticks)]


def fetch_itick_ticks_rest(stocks: Sequence[StockEvaluation], args: argparse.Namespace) -> List[str]:
    token = (args.itick_token or os.getenv("ITICK_TOKEN", "")).strip()
    if not token:
        raise DataSourceError("使用 iTick REST tick 补抓时必须提供 token")

    candidates = select_itick_candidates(stocks, args.itick_max_symbols)
    if not candidates:
        return []

    headers = {
        "token": token,
        "accept": "application/json",
    }
    tick_lines: List[str] = []
    for stock in candidates:
        region = to_itick_region(stock.code)
        if not region:
            continue
        url = f"https://api.itick.org/stock/tick?region={region}&code={normalize_code(stock.code)}"
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            print(f"[iTick REST] 获取 {stock.code} 失败: {exc}")
            continue

        data = payload.get("data")
        if not isinstance(data, dict):
            print(f"[iTick REST] {stock.code} 未返回有效 tick 数据: {payload}")
            continue
        tick_lines.append(
            f"{data.get('s', stock.code)}${region} 最新价={data.get('ld', '')} "
            f"成交量={data.get('v', '')} 时间={data.get('t', '')}"
        )
    return tick_lines


def print_sector_rankings(sectors: Sequence[SectorRecord]) -> None:
    if not sectors:
        print("没有可展示的热门板块")
        return
    frame = localize_display_frame(pd.DataFrame([asdict(record) for record in sectors]))
    display = frame[SECTOR_DISPLAY_COLUMNS].copy()
    display.insert(0, "rank", range(1, len(display) + 1))
    print("\n热门板块排名:")
    print_pretty_table(
        display,
        {
            "rank": "序号",
            "sector_name": "板块名称",
            "board_type": "板块类型",
            "hot_score": "热度",
            "change_pct": "涨跌%",
            "advancers_ratio": "上涨占比%",
            "leader_change_pct": "龙头涨幅%",
            "liquidity_metric": "流动性",
        },
        alignments={
            "rank": "right",
            "hot_score": "right",
            "change_pct": "right",
            "advancers_ratio": "right",
            "leader_change_pct": "right",
            "liquidity_metric": "right",
        },
        preferred_widths={
            "sector_name": 18,
            "board_type": 10,
        },
        min_widths={
            "sector_name": 10,
            "board_type": 8,
        },
    )


def print_stock_rankings(stocks: Sequence[StockEvaluation], top_n: int, only_passed: bool = False) -> None:
    display_stocks = filter_display_stocks(stocks, only_passed)
    if not display_stocks:
        print("\n没有筛选出符合条件的股票")
        return
    frame = pd.DataFrame([asdict(stock) for stock in display_stocks]).sort_values("final_score", ascending=False)
    display = frame.head(top_n).copy()
    display.insert(0, "rank", range(1, len(display) + 1))
    print("\n候选股票排名:")
    print_pretty_table(
        display,
        {
            "rank": "序号",
            "sector_name": "板块",
            "code": "代码",
            "name": "名称",
            "screen_passed": "初筛",
            "screen_score": "技术分",
            "next_day_pattern_count": "样本",
            "short_term_sample_level": "样本级",
            "should_reference_next_day_stats": "参考次日",
            "recent_month_trend": "近一月走势",
            "next_day_up_prob_pct": "次日上涨%",
            "suggested_exit_pct": "出票%",
            "sharpe_ratio": "夏普",
            "final_score": "总分",
        },
        alignments={
            "rank": "right",
            "screen_passed": "center",
            "screen_score": "right",
            "next_day_pattern_count": "right",
            "next_day_up_prob_pct": "right",
            "suggested_exit_pct": "right",
            "sharpe_ratio": "right",
            "final_score": "right",
        },
        preferred_widths={
            "sector_name": 12,
            "name": 14,
            "should_reference_next_day_stats": 8,
            "recent_month_trend": 28,
        },
        min_widths={
            "sector_name": 8,
            "name": 8,
            "should_reference_next_day_stats": 6,
            "recent_month_trend": 16,
        },
    )


def print_sector_leader_analysis(stocks: Sequence[StockEvaluation], leaders_per_sector: int) -> None:
    if not stocks:
        return
    frame = pd.DataFrame([asdict(stock) for stock in stocks])
    print("\n板块龙头与次日胜率分析:")
    for sector_name, group in frame.groupby("sector_name", sort=False):
        display = group.sort_values(
            ["next_day_up_prob_pct", "next_day_pattern_count", "final_score", "screen_score"],
            ascending=[False, False, False, False],
        ).head(leaders_per_sector)
        print(f"\n[{sector_name}]")
        leader_table = display.copy()
        leader_table.insert(0, "rank", range(1, len(leader_table) + 1))
        print_pretty_table(
            leader_table,
            {
                "rank": "序号",
                "code": "代码",
                "name": "名称",
                "next_day_pattern_count": "样本",
                "short_term_sample_level": "样本级",
                "should_reference_next_day_stats": "参考次日",
                "recent_month_trend": "近一月走势",
                "next_day_up_prob_pct": "次日上涨%",
                "next_day_avg_return_pct": "次日均涨%",
                "suggested_exit_pct": "出票%",
                "final_score": "总分",
            },
            alignments={
                "rank": "right",
                "next_day_pattern_count": "right",
                "next_day_up_prob_pct": "right",
                "next_day_avg_return_pct": "right",
                "suggested_exit_pct": "right",
                "final_score": "right",
            },
            preferred_widths={
                "name": 14,
                "should_reference_next_day_stats": 8,
                "recent_month_trend": 26,
            },
            min_widths={
                "name": 8,
                "should_reference_next_day_stats": 6,
                "recent_month_trend": 16,
            },
        )
        for row in display.itertuples(index=False):
            print(f"  - {row.code} {row.name}: 近一月走势 {row.recent_month_trend}")
            print(f"  - {row.code} {row.name}: {row.short_term_confidence_hint}")
            print(f"    出票规则: {row.suggested_exit_rule}")


def export_results(
    sectors: Sequence[SectorRecord],
    stocks: Sequence[StockEvaluation],
    output_path: Optional[Path],
    only_passed: bool = False,
) -> None:
    if output_path is None:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path) as writer:
        if sectors:
            (
                localize_display_frame(pd.DataFrame([asdict(record) for record in sectors]))[SECTOR_DISPLAY_COLUMNS]
                .rename(columns=SECTOR_DISPLAY_NAMES)
                .to_excel(writer, sheet_name="热门板块", index=False)
            )
        display_stocks = filter_display_stocks(stocks, only_passed)
        if display_stocks:
            (
                pd.DataFrame([asdict(stock) for stock in display_stocks])
                .sort_values("final_score", ascending=False)[STOCK_DISPLAY_COLUMNS]
                .rename(columns=STOCK_DISPLAY_NAMES)
                .to_excel(
                    writer,
                    sheet_name="候选股票",
                    index=False,
                )
            )
            (
                pd.DataFrame([asdict(stock) for stock in display_stocks])
                .sort_values(
                    ["sector_name", "next_day_up_prob_pct", "next_day_pattern_count", "final_score"],
                    ascending=[True, False, False, False],
                )
                .to_excel(
                    writer,
                    sheet_name="板块龙头次日分析",
                    index=False,
                )
            )
    print(f"\n结果已导出到: {output_path}")


def collect_histories(
    datasource: BaseAShareDataSource,
    codes: Iterable[Tuple[str, str]],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    histories: Dict[str, pd.DataFrame] = {}
    for code, name in codes:
        try:
            use_local_data = args.history_source in {"auto", "local"}
            if use_local_data:
                local_history = load_local_history(code, args.data_dir)
                if local_history is not None:
                    local_history["name"] = name
                    histories[code] = local_history
                    continue
                if args.history_source == "local":
                    raise DataSourceError(
                        f"已设置仅使用本地行情，但未找到 {normalize_code(code)} 的可用本地文件"
                    )

            history = datasource.get_price_history(code, args.start_date, args.end_date, adjust=args.adjust)
            history["name"] = name
            histories[code] = history
        except DataSourceError as exc:
            print(f"跳过 {code} {name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"加载 {code} {name} 的历史行情时出现未预期错误") from exc
    return histories


def select_target_sectors(datasource: BaseAShareDataSource, args: argparse.Namespace) -> List[SectorRecord]:
    if args.sector:
        constituents = datasource.get_sector_constituents(args.sector, args.board_type if args.board_type != "all" else None)
        return [SectorRecord(
            sector_name=constituents["sector_name"].iloc[0],
            board_type=constituents["board_type"].iloc[0],
            change_pct=0.0,
            advancers_ratio=0.0,
            leader_change_pct=0.0,
            liquidity_metric=0.0,
            hot_score=60.0,
            source=args.data_source,
        )]

    if args.hot_sectors:
        rankings = get_sector_rankings_with_snapshot(datasource, args)
        return build_sector_records(rankings.head(args.top_sectors))

    return [SectorRecord(
        sector_name="自定义股票池",
        board_type="custom",
        change_pct=0.0,
        advancers_ratio=0.0,
        leader_change_pct=0.0,
        liquidity_metric=0.0,
        hot_score=50.0,
        source="custom",
    )]


def load_sector_constituents(
    datasource: BaseAShareDataSource,
    sectors: Sequence[SectorRecord],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    sector_map: Dict[str, pd.DataFrame] = {}
    if args.codes:
        codes = read_codes_file(args.codes)
        sector_map["自定义股票池"] = filter_tradeable_constituents(
            codes.assign(sector_name="自定义股票池", board_type="custom"),
            allow_st=args.allow_st,
        )
        return sector_map

    for sector in sectors:
        constituents = datasource.get_sector_constituents(sector.sector_name, sector.board_type if sector.board_type != "custom" else None)
        constituents = filter_tradeable_constituents(constituents, allow_st=args.allow_st)
        if args.max_stocks_per_sector:
            constituents = constituents.head(args.max_stocks_per_sector)
        sector_map[sector.sector_name] = constituents
    return sector_map


def select_sector_leaders(
    stocks: Sequence[StockEvaluation],
    leaders_per_sector: int,
) -> List[StockEvaluation]:
    grouped: Dict[str, List[StockEvaluation]] = {}
    for stock in stocks:
        grouped.setdefault(stock.sector_name, []).append(stock)

    selected: List[StockEvaluation] = []
    for _, items in grouped.items():
        ranked = sorted(
            items,
            key=lambda item: (
                item.screen_passed,
                item.next_day_up_prob_pct,
                min(item.next_day_pattern_count, NEXT_DAY_FULL_CONFIDENCE_SAMPLE_SIZE),
                item.next_day_avg_return_pct,
                item.final_score,
            ),
            reverse=True,
        )
        selected.extend(ranked[:leaders_per_sector])
    return selected


def run_analysis(args: argparse.Namespace) -> Tuple[List[SectorRecord], List[StockEvaluation]]:
    datasource = get_data_source(args.data_source, tushare_token=args.tushare_token)
    target_sectors = select_target_sectors(datasource, args)
    sector_constituents = load_sector_constituents(datasource, target_sectors, args)

    hot_score_map = {sector.sector_name: sector for sector in target_sectors}
    stock_results: List[StockEvaluation] = []

    for sector_name, constituents in sector_constituents.items():
        sector = hot_score_map[sector_name]
        codes = list(constituents[["code", "name"]].itertuples(index=False, name=None))
        histories = collect_histories(datasource, codes, args)
        for code, name in codes:
            history = histories.get(code)
            if history is None or history.empty:
                continue
            try:
                evaluation = evaluate_stock(code, name, sector, history, args)
            except DataSourceError as exc:
                print(f"跳过 {code} {name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"评估 {code} {name} 时出现未预期错误") from exc
            stock_results.append(evaluation)

    leader_stocks = select_sector_leaders(stock_results, args.leader_stocks_per_sector)
    ranked_stocks = sorted(
        leader_stocks,
        key=lambda item: (
            item.next_day_up_prob_pct,
            min(item.next_day_pattern_count, NEXT_DAY_FULL_CONFIDENCE_SAMPLE_SIZE),
            item.next_day_avg_return_pct,
            item.final_score,
        ),
        reverse=True,
    )
    return target_sectors, ranked_stocks


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股热门板块筛选与个股量化评估脚本")
    parser.add_argument("--data-source", choices=["akshare", "tushare", "mootdx"], default="mootdx", help="A 股数据源（mootdx 走通达信协议，更稳定；akshare 走东方财富，板块数据更全）")
    parser.add_argument("--tushare-token", help="TuShare token，可选")
    parser.add_argument("--hot-sectors", action="store_true", help="自动计算并分析近期热门板块；默认不传入口参数时也会启用")
    parser.add_argument("--sector", help="直接分析指定板块名称")
    parser.add_argument("--codes", type=Path, help="自定义股票列表文件，支持 csv/xlsx")
    parser.add_argument("--data-dir", type=Path, help="本地行情目录，可与接口模式混合使用")
    parser.add_argument(
        "--history-source",
        choices=["auto", "local", "api"],
        default="auto",
        help="历史行情来源：auto=优先本地失败后走 API，local=仅用本地文件，api=忽略本地文件直接拉最新数据",
    )
    parser.add_argument("--board-type", choices=["all", "concept", "industry"], default="all", help="板块类型")
    parser.add_argument("--use-sector-snapshot", action="store_true", help="使用已保存的板块热度快照；不传则拉取最新数据并更新快照")
    parser.add_argument("--sector-snapshot-path", type=Path, help="板块热度快照文件路径，默认按数据源和板块类型自动生成")
    parser.add_argument("--top-sectors", type=int, default=5, help="热门板块模式下分析前 N 个板块")
    parser.add_argument("--max-stocks-per-sector", type=int, default=20, help="每个板块最多分析的股票数")
    parser.add_argument("--leader-stocks-per-sector", type=int, default=5, help="每个热门板块保留前 N 只热门/龙头股票")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE, help="结束日期，格式 YYYYMMDD")
    parser.add_argument("--adjust", default="qfq", help="复权方式，AkShare 常用 qfq/hfq")
    parser.add_argument("--fast-period", type=int, default=10, help="快速均线周期")
    parser.add_argument("--slow-period", type=int, default=30, help="慢速均线周期")
    parser.add_argument("--initial-cash", type=float, default=100000.0, help="回测初始资金")
    parser.add_argument("--commission", type=float, default=0.001, help="单边交易手续费率")
    parser.add_argument("--slippage", type=float, default=0.001, help="滑点假设，例如 0.001 表示 0.1%%")
    parser.add_argument("--stop-loss", type=float, default=0.08, help="止损比例，例如 0.08 表示 8%%")
    parser.add_argument("--next-day-stop-loss", type=float, default=0.02, help="次日策略出票止损比例，例如 0.02 表示 2%%")
    parser.add_argument("--next-day-target-pct", type=float, default=0.03, help="次日目标涨幅，例如 0.03 表示 3%%")
    parser.add_argument("--allow-st", action="store_true", help="允许分析 ST/*ST/退市风险股票；默认会剔除")
    parser.add_argument("--min-avg-turnover-20d", type=float, default=20000000.0, help="近 20 日平均成交额下限")
    parser.add_argument("--max-5d-return", type=float, default=0.18, help="近 5 日涨幅上限，超过会触发过热惩罚")
    parser.add_argument("--max-close-above-ma20", type=float, default=0.12, help="收盘价高于 20 日线的最大容忍比例")
    parser.add_argument("--subscribe-itick", action="store_true", help="对初筛通过的股票使用 iTick WebSocket 订阅并打印 Tick")
    parser.add_argument("--itick-token", help="iTick API token；不传则尝试读取 ITICK_TOKEN 环境变量")
    parser.add_argument("--itick-max-symbols", type=int, default=10, help="iTick 最多订阅多少只初筛通过股票")
    parser.add_argument("--itick-duration", type=int, default=60, help="iTick 订阅持续秒数；0 表示不自动断开")
    parser.add_argument("--itick-types", default="tick", help="iTick 订阅类型，例如 tick 或 tick,quote")
    parser.add_argument("--itick-ping-interval", type=int, default=30, help="iTick 心跳间隔秒数")
    parser.add_argument("--itick-ws-url", default="wss://api.itick.org/stock", help="iTick 股票 WebSocket 地址")
    parser.add_argument("--itick-print-raw", action="store_true", help="打印非 tick 类型的原始 WebSocket 消息")
    parser.add_argument("--notify-webhook-url", help="企业微信机器人 webhook 地址，用于推送选股和 Tick 摘要")
    parser.add_argument("--only-passed", action="store_true", help="只展示通过技术面初筛的股票；默认展示全部结果")
    parser.add_argument("--top-stocks", type=int, default=20, help="终端展示前 N 只股票")
    parser.add_argument("--output", type=Path, help="结果导出路径，建议 xlsx")
    args = parser.parse_args(argv)
    if not any([args.hot_sectors, args.sector, args.codes]):
        args.hot_sectors = True
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.fast_period >= args.slow_period:
        raise DataSourceError("fast-period 必须小于 slow-period")
    if args.history_source == "local" and not args.data_dir:
        raise DataSourceError("history-source=local 时必须提供 --data-dir")
    if args.min_avg_turnover_20d < 0:
        raise DataSourceError("min-avg-turnover-20d 不能为负数")
    if args.leader_stocks_per_sector <= 0:
        raise DataSourceError("leader-stocks-per-sector 必须大于 0")
    if args.max_5d_return <= 0:
        raise DataSourceError("max-5d-return 必须大于 0")
    if args.max_close_above_ma20 <= 0:
        raise DataSourceError("max-close-above-ma20 必须大于 0")
    if not 0 <= args.slippage < 0.1:
        raise DataSourceError("slippage 必须在 [0, 0.1) 区间内")
    if not 0 < args.next_day_stop_loss < 0.2:
        raise DataSourceError("next-day-stop-loss 必须在 (0, 0.2) 区间内")
    if not 0 < args.next_day_target_pct < 0.3:
        raise DataSourceError("next-day-target-pct 必须在 (0, 0.3) 区间内")
    if args.sector_snapshot_path and args.sector_snapshot_path.suffix.lower() != ".csv":
        raise DataSourceError("sector-snapshot-path 必须是 csv 文件")
    if args.itick_max_symbols <= 0:
        raise DataSourceError("itick-max-symbols 必须大于 0")
    if args.itick_duration < 0:
        raise DataSourceError("itick-duration 不能为负数")
    if args.itick_ping_interval <= 0:
        raise DataSourceError("itick-ping-interval 必须大于 0")
    if args.notify_webhook_url and not args.notify_webhook_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="):
        raise DataSourceError("notify-webhook-url 需要是有效的企业微信机器人 webhook 地址")


def describe_history_source(args: argparse.Namespace) -> str:
    if args.history_source == "api":
        return "API"
    if args.history_source == "local":
        return "本地文件"
    if args.data_dir:
        return "自动(优先本地, 失败后 API)"
    return "自动(API)"


def describe_sector_source(args: argparse.Namespace) -> str:
    if args.codes:
        return "自定义股票池"
    if args.sector:
        return "指定板块接口"
    if args.use_sector_snapshot:
        return "板块快照"
    return "最新接口"


def print_runtime_context(args: argparse.Namespace) -> None:
    print("\n运行上下文:")
    print(f"历史行情来源: {describe_history_source(args)}")
    print(f"板块热度来源: {describe_sector_source(args)}")
    print(f"历史区间: {args.start_date} ~ {args.end_date}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        print_runtime_context(args)
        sectors, stocks = run_analysis(args)
    except DataSourceError as exc:
        print(f"执行失败: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败: 遇到未预期错误，请根据下面的堆栈继续排查: {exc}", file=sys.stderr)
        raise

    print_sector_rankings(sectors)
    print_sector_leader_analysis(stocks, args.leader_stocks_per_sector)
    print_stock_rankings(stocks, args.top_stocks, only_passed=args.only_passed)
    export_results(sectors, stocks, args.output, only_passed=args.only_passed)
    if args.notify_webhook_url:
        send_wecom_webhook(
            args.notify_webhook_url,
            build_analysis_summary(sectors, stocks, args.only_passed, args.top_stocks),
        )
    if args.subscribe_itick:
        tick_lines = subscribe_itick_ticks(stocks, args)
        if not tick_lines:
            print("[iTick] WebSocket 未拿到 tick，改用 REST tick 补抓")
            tick_lines = fetch_itick_ticks_rest(stocks, args)
        if args.notify_webhook_url:
            tick_summary = "\n".join(
                [
                    "iTick Tick 汇总",
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                    *(f"- {line}" for line in (tick_lines or ["本次未收到 tick 数据"])),
                ]
            )
            send_wecom_webhook(args.notify_webhook_url, tick_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
