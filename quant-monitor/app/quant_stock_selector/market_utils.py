"""Price normalization, file IO, and small math helpers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .constants import PRICE_COLUMN_ALIASES
from .exceptions import DataSourceError


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

    standardized = frame.rename(
        columns={column: PRICE_COLUMN_ALIASES.get(str(column).strip().lower(), column) for column in frame.columns}
    )
    standardized = standardized.rename(
        columns={column: PRICE_COLUMN_ALIASES.get(str(column).strip(), column) for column in standardized.columns}
    )
    if "code" not in standardized.columns:
        raise DataSourceError("股票列表文件缺少 code/代码 列")

    standardized["code"] = standardized["code"].map(normalize_code)
    if "name" not in standardized.columns:
        standardized["name"] = ""
    standardized = standardized[standardized["code"] != ""].drop_duplicates("code")
    return standardized[["code", "name"]]


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

    for candidate in deduped_candidates:
        try:
            if candidate.suffix.lower() in {".xlsx", ".xls"}:
                frame = pd.read_excel(candidate)
            else:
                frame = pd.read_csv(candidate)
            standardized = standardize_price_frame(frame)
            standardized["code"] = normalized_code
            return standardized
        except Exception:
            continue
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
