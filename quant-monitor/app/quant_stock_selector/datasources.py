"""A-share data sources: AkShare, TuShare, mootdx."""

from __future__ import annotations

import abc
import os
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

import numpy as np
import pandas as pd

from .exceptions import DataSourceError
from .market_utils import (
    normalize_code,
    normalize_score,
    safe_float,
    standardize_price_frame,
    to_tushare_code,
)

_T = TypeVar("_T")


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
                    wait = self._RETRY_BACKOFF**attempt
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
                frames.append(
                    self._prepare_board_frame(
                        self._retry_call(self.ak.stock_board_concept_name_em, "获取概念板块列表"), "concept"
                    )
                )
            if board_types in {"all", "industry"}:
                frames.append(
                    self._prepare_board_frame(
                        self._retry_call(self.ak.stock_board_industry_name_em, "获取行业板块列表"), "industry"
                    )
                )
        except Exception as exc:
            raise DataSourceError(f"AkShare 获取热门板块失败，请检查网络或稍后重试: {exc}") from exc
        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")

        rankings = pd.concat(frames, ignore_index=True)
        rankings["hot_score"] = (
            normalize_score(rankings["change_pct"]) * 0.45
            + normalize_score(rankings["advancers_ratio"]) * 0.20
            + normalize_score(rankings["leader_change_pct"]) * 0.20
            + normalize_score(rankings["turnover_rate"]) * 0.15
        ).round(2)
        rankings = rankings.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)
        return rankings

    def _prepare_board_frame(self, frame: pd.DataFrame, board_type: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(
                columns=[
                    "sector_name",
                    "board_type",
                    "change_pct",
                    "advancers_ratio",
                    "leader_change_pct",
                    "turnover_rate",
                    "source",
                ]
            )

        columns = {str(column).strip(): column for column in frame.columns}
        sector_col = columns.get("板块名称") or columns.get("名称")
        change_col = columns.get("涨跌幅")
        up_col = columns.get("上涨家数")
        down_col = columns.get("下跌家数")
        leader_col = columns.get("领涨股票-涨跌幅") or columns.get("领涨股票涨跌幅")
        turnover_col = columns.get("换手率")

        prepared = pd.DataFrame(
            {
                "sector_name": frame[sector_col] if sector_col else "",
                "change_pct": frame[change_col].map(safe_float) if change_col else 0.0,
                "up_count": frame[up_col].map(safe_float) if up_col else 0.0,
                "down_count": frame[down_col].map(safe_float) if down_col else 0.0,
                "leader_change_pct": frame[leader_col].map(safe_float) if leader_col else 0.0,
                "turnover_rate": frame[turnover_col].map(safe_float) if turnover_col else 0.0,
            }
        )
        total = (prepared["up_count"] + prepared["down_count"]).replace(0, np.nan)
        prepared["advancers_ratio"] = (prepared["up_count"] / total).fillna(0.5)
        prepared["board_type"] = board_type
        prepared["source"] = self.source_name
        return prepared[
            [
                "sector_name",
                "board_type",
                "change_pct",
                "advancers_ratio",
                "leader_change_pct",
                "turnover_rate",
                "source",
            ]
        ]

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
        _ = adjust
        ts_code = to_tushare_code(code)
        try:
            frame = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as exc:
            raise DataSourceError(f"TuShare 获取股票 {ts_code} 历史行情失败: {exc}") from exc
        if frame.empty:
            raise DataSourceError(f"TuShare 未返回股票 {ts_code} 的日线数据")
        frame = frame.rename(
            columns={
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "turnover",
            }
        )
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

    _QUOTE_BATCH = 80

    def __init__(self) -> None:
        try:
            from mootdx.quotes import Quotes  # type: ignore
        except ImportError as exc:
            raise DataSourceError("未安装 mootdx，请先执行: pip install mootdx") from exc
        self._Quotes = Quotes
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None or getattr(self._client, "closed", False):
            self._client = self._Quotes.factory(market="std")
        return self._client

    def _batch_quotes(self, codes: List[str]) -> pd.DataFrame:
        client = self._get_client()
        frames: List[pd.DataFrame] = []
        for i in range(0, len(codes), self._QUOTE_BATCH):
            batch = codes[i : i + self._QUOTE_BATCH]
            result = client.quotes(symbol=batch)
            if result is not None and not result.empty:
                frames.append(result)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _load_block_frame(self, block_file: str) -> pd.DataFrame:
        client = self._get_client()
        frame = client.block(tofile=block_file)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["blockname", "code"])
        mask = frame["blockname"].apply(lambda n: any("\u4e00" <= ch <= "\u9fff" for ch in str(n)))
        clean = frame[mask][["blockname", "code"]].copy()
        clean["code"] = clean["code"].astype(str).str.zfill(6)
        return clean

    def get_stock_universe(self) -> pd.DataFrame:
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
        combined = combined[combined["code"].str.match(r"^(0[0-9]|3[0-9]|6[0-9]|8[0-8]|4[0-9])\d{4}$")]
        if "name" not in combined.columns:
            combined["name"] = ""
        return combined[["code", "name"]].drop_duplicates("code")

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        frames: List[pd.DataFrame] = []

        if board_types in {"all", "concept"}:
            frames.append(self._build_sector_frame(self._load_block_frame("block_gn.dat"), "concept"))
        if board_types in {"all", "industry"}:
            frames.append(self._build_sector_frame(self._load_block_frame("block.dat"), "industry"))

        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")

        combined = pd.concat([f for f in frames if not f.empty], ignore_index=True)
        if combined.empty:
            raise DataSourceError("mootdx 未返回任何板块数据，请检查网络连接")

        combined["hot_score"] = (
            normalize_score(combined["change_pct"]) * 0.45
            + normalize_score(combined["advancers_ratio"]) * 0.20
            + normalize_score(combined["leader_change_pct"]) * 0.20
            + normalize_score(combined["turnover_rate"]) * 0.15
        ).round(2)
        return combined.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)

    def _build_sector_frame(self, block_df: pd.DataFrame, board_type: str) -> pd.DataFrame:
        if block_df.empty:
            return pd.DataFrame()

        unique_codes = block_df["code"].unique().tolist()
        if not unique_codes:
            return pd.DataFrame()

        quotes = self._batch_quotes(unique_codes)
        if quotes.empty:
            return pd.DataFrame()

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
            rows.append(
                {
                    "sector_name": sector_name,
                    "board_type": board_type,
                    "change_pct": float(np.mean(chg_arr)),
                    "advancers_ratio": up / total,
                    "leader_change_pct": float(np.max(chg_arr)),
                    "turnover_rate": float(np.mean(amt_vals)) if amt_vals else 0.0,
                    "source": self.source_name,
                }
            )

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
                result["name"] = ""
                result["board_type"] = btype
                return result[["code", "name", "sector_name", "board_type"]].drop_duplicates("code")

        raise DataSourceError(f"mootdx 未找到板块: {sector_name}")

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        _ = adjust
        sym = normalize_code(code)
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

        drop_cols = [c for c in ["datetime", "volume"] if c in frame.columns]
        if drop_cols:
            frame = frame.drop(columns=drop_cols)
        frame = frame.reset_index()
        frame = frame.rename(
            columns={
                "datetime": "date",
                "vol": "volume",
                "amount": "turnover",
            }
        )
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[
            (frame["date"] >= pd.Timestamp(start_date)) & (frame["date"] <= pd.Timestamp(end_date))
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
