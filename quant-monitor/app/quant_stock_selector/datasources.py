"""A-share data sources: AkShare, TuShare, mootdx."""

from __future__ import annotations

import abc
import math
import os
import random
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import numpy as np
import pandas as pd

from .exceptions import DataSourceError
from .market_utils import (
    is_listed_a_share_equity,
    normalize_code,
    normalize_score,
    safe_float,
    standardize_price_frame,
    to_tushare_code,
)

_T = TypeVar("_T")

TUSHARE_PERM_DOC = "https://tushare.pro/document/1?doc_id=108"


def _tushare_ths_error_message(what_zh: str, exc: BaseException) -> str:
    """同花顺板块链路（ths_*）失败时拼接可读说明；积分不足时指向文档。"""
    detail = str(exc).strip()
    msg = f"{what_zh}：{detail}"
    if "没有接口" in detail and "权限" in detail:
        return (
            f"{msg}\n"
            "—— 原因：当前 TuShare 账号无权调用同花顺板块接口（如 ths_index / ths_daily / ths_member），"
            "文档侧通常要求较高积分（常见说明为约 6000 分），与是否填写 Token 无关。\n"
            "—— 处理：在「⑨ 量化选股」将数据源改为 **akshare**（东财板块）或 **mootdx**（通达信）；"
            "若坚持使用 TuShare 板块数据，请登录 tushare.pro 提升积分或购买权限后重试。\n"
            f"—— 权限说明：{TUSHARE_PERM_DOC}"
        )
    low = detail.lower()
    if "积分" in detail or "permission" in low or "无权" in detail or "access" in low:
        return (
            f"{msg}\n"
            f"—— 若为积分或接口权限不足，请改用 akshare / mootdx，或查阅 {TUSHARE_PERM_DOC}。"
        )
    return f"{msg}\n—— 若持续失败，请确认积分是否覆盖 ths_index、ths_daily、ths_member（见 {TUSHARE_PERM_DOC}）。"


SECTOR_SNAPSHOT_COLUMNS = [
    "sector_name",
    "board_type",
    "change_pct",
    "advancers_ratio",
    "leader_change_pct",
    "turnover_rate",
    "liquidity_metric",
    "hot_score",
    "source",
]


def default_sector_snapshot_path(base_dir: Path, data_source: str, board_type: str) -> Path:
    safe_source = (data_source or "unknown").strip().lower() or "unknown"
    safe_board = (board_type or "all").strip().lower() or "all"
    return base_dir / f"sector_rankings_snapshot_{safe_source}_{safe_board}.csv"


def load_sector_rankings_snapshot(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "liquidity_metric" not in frame.columns and "turnover_rate" in frame.columns:
        frame = frame.rename(columns={"turnover_rate": "liquidity_metric"})
    if "turnover_rate" not in frame.columns and "liquidity_metric" in frame.columns:
        frame["turnover_rate"] = frame["liquidity_metric"]
    missing = [column for column in SECTOR_SNAPSHOT_COLUMNS if column not in frame.columns]
    if missing:
        raise DataSourceError(f"板块热度快照缺少必要列: {', '.join(missing)}")
    return frame[SECTOR_SNAPSHOT_COLUMNS].copy()


def save_sector_rankings_snapshot(frame: pd.DataFrame, path: Path) -> None:
    if frame is None or frame.empty:
        return
    out = frame.copy()
    if "liquidity_metric" not in out.columns and "turnover_rate" in out.columns:
        out["liquidity_metric"] = out["turnover_rate"]
    if "turnover_rate" not in out.columns and "liquidity_metric" in out.columns:
        out["turnover_rate"] = out["liquidity_metric"]
    missing = [column for column in SECTOR_SNAPSHOT_COLUMNS if column not in out.columns]
    if missing:
        raise DataSourceError(f"板块热度结果缺少必要列，无法保存快照: {', '.join(missing)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    out[SECTOR_SNAPSHOT_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


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
                # 概念板块：优先使用仓库内自定义东财接口（更可控/可扩展），失败再回退 AkShare 内置函数。
                def _concept_frame() -> pd.DataFrame:
                    try:
                        from .stock_board_concept_em import stock_board_concept_name_em

                        return stock_board_concept_name_em()
                    except Exception:
                        return self.ak.stock_board_concept_name_em()

                frames.append(
                    self._prepare_board_frame(
                        self._retry_call(_concept_frame, "fetch concept sector rankings"), "concept"
                    )
                )
            if board_types in {"all", "industry"}:
                # 概念+行业连拉时易触发东财/连接限流，中间间隔一小段再拉行业
                if board_types == "all" and frames:
                    time.sleep(random.uniform(1.2, 3.0))
                # 行业板块：优先使用仓库内自定义东财接口（更可控/可扩展），失败再回退 AkShare 内置函数。
                def _industry_frame() -> pd.DataFrame:
                    try:
                        from .stock_board_industry_em import stock_board_industry_name_em

                        return stock_board_industry_name_em()
                    except Exception:
                        return self.ak.stock_board_industry_name_em()

                frames.append(
                    self._prepare_board_frame(
                        self._retry_call(_industry_frame, "fetch industry sector rankings"), "industry"
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
                # 概念板块：优先走自定义东财板块成分接口；失败回退 AkShare 内置实现。
                def _concept_cons() -> pd.DataFrame:
                    try:
                        from .stock_board_concept_em import stock_board_concept_cons_em

                        return stock_board_concept_cons_em(symbol=str(board["sector_name"]))
                    except Exception:
                        return self.ak.stock_board_concept_cons_em(symbol=board["sector_name"])

                frame = self._retry_call(
                    _concept_cons,
                    f"fetch concept sector constituents {board['sector_name']}",
                )
            else:
                # 行业板块：优先走自定义东财板块成分接口；失败回退 AkShare 内置实现。
                def _industry_cons() -> pd.DataFrame:
                    try:
                        from .stock_board_industry_em import stock_board_industry_cons_em

                        return stock_board_industry_cons_em(symbol=str(board["sector_name"]))
                    except Exception:
                        return self.ak.stock_board_industry_cons_em(symbol=board["sector_name"])

                frame = self._retry_call(
                    _industry_cons,
                    f"fetch industry sector constituents {board['sector_name']}",
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
        self._sector_key_to_ts_code: Dict[Tuple[str, str], str] = {}

    def _fetch_ths_daily_latest(self) -> Tuple[pd.DataFrame, str]:
        """同花顺板块指数一日全市场截面（ths_daily 不传 ts_code 时按 trade_date 拉取）。"""
        end = date.today()
        start = end - timedelta(days=45)
        try:
            cal = self.pro.trade_cal(
                exchange="SSE",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                is_open="1",
            )
        except Exception as exc:
            raise DataSourceError(f"TuShare 获取交易日历失败: {exc}") from exc
        dates: List[str] = []
        if cal is not None and not cal.empty and "cal_date" in cal.columns:
            for x in cal["cal_date"].tolist():
                dates.append(str(x).replace("-", ""))
        if not dates:
            dates = [end.strftime("%Y%m%d")]
        last_err: Optional[Exception] = None
        for td in reversed(dates):
            try:
                df = self.pro.ths_daily(trade_date=td)
            except Exception as exc:
                last_err = exc
                continue
            if df is not None and not df.empty:
                return df, td
        msg = "TuShare 未取到同花顺板块指数日线（ths_daily）"
        if last_err:
            raise DataSourceError(_tushare_ths_error_message(msg, last_err)) from last_err
        msg += "；请检查积分（ths_index/ths_daily/ths_member 通常需 6000 分）与网络。"
        raise DataSourceError(msg)

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
        """
        同花顺概念/行业指数：ths_index + 最近交易日 ths_daily 涨跌幅排序。
        版权与积分要求见 TuShare 文档（通常 6000 积分）。
        """
        self._sector_key_to_ts_code.clear()
        frames: List[pd.DataFrame] = []
        try:
            if board_types in {"all", "concept"}:
                idx_c = self.pro.ths_index(exchange="A", type="N")
                if idx_c is not None and not idx_c.empty:
                    ic = idx_c.copy()
                    ic["board_type"] = "concept"
                    frames.append(ic)
            if board_types in {"all", "industry"}:
                idx_i = self.pro.ths_index(exchange="A", type="I")
                if idx_i is not None and not idx_i.empty:
                    ii = idx_i.copy()
                    ii["board_type"] = "industry"
                    frames.append(ii)
        except Exception as exc:
            raise DataSourceError(
                _tushare_ths_error_message("TuShare 获取同花顺板块列表失败", exc)
            ) from exc
        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")
        indices = pd.concat(frames, ignore_index=True)
        if indices.empty:
            raise DataSourceError("TuShare ths_index 返回空表")

        daily, _trade_d = self._fetch_ths_daily_latest()
        if "ts_code" not in daily.columns or "pct_change" not in daily.columns:
            raise DataSourceError("TuShare ths_daily 返回格式异常（需含 ts_code、pct_change）")
        dcols = ["ts_code", "pct_change"]
        if "turnover_rate" in daily.columns:
            dcols.append("turnover_rate")
        dsub = daily[dcols].drop_duplicates(subset=["ts_code"], keep="last")
        merged = indices.merge(dsub, on="ts_code", how="inner")
        if merged.empty:
            raise DataSourceError("TuShare 板块指数（ths_index）与当日行情（ths_daily）无交集，请换交易日重试")

        merged = merged.rename(columns={"name": "sector_name", "pct_change": "change_pct"})
        merged["change_pct"] = pd.to_numeric(merged["change_pct"], errors="coerce").fillna(0.0)
        if "turnover_rate" in merged.columns:
            merged["turnover_rate"] = pd.to_numeric(merged["turnover_rate"], errors="coerce").fillna(0.0)
        else:
            merged["turnover_rate"] = 0.0
        merged["advancers_ratio"] = 0.5
        merged["leader_change_pct"] = merged["change_pct"]
        merged["hot_score"] = (
            normalize_score(merged["change_pct"]) * 0.45
            + normalize_score(merged["advancers_ratio"]) * 0.20
            + normalize_score(merged["leader_change_pct"]) * 0.20
            + normalize_score(merged["turnover_rate"]) * 0.15
        ).round(2)
        merged["source"] = self.source_name
        merged = merged.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)

        for _, row in merged.iterrows():
            sn = str(row["sector_name"]).strip()
            bt = str(row["board_type"]).strip().lower()
            self._sector_key_to_ts_code[(sn, bt)] = str(row["ts_code"])

        out_cols = [
            "sector_name",
            "board_type",
            "change_pct",
            "advancers_ratio",
            "leader_change_pct",
            "turnover_rate",
            "hot_score",
            "source",
            "ts_code",
        ]
        if "count" in merged.columns:
            merged = merged.rename(columns={"count": "constituent_count"})
            out_cols.insert(-1, "constituent_count")
        return merged[[c for c in out_cols if c in merged.columns]].copy()

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        name = str(sector_name).strip()
        ts_code: Optional[str] = None
        if board_type:
            ts_code = self._sector_key_to_ts_code.get((name, str(board_type).lower()))
        if not ts_code:
            for bt in ("concept", "industry"):
                ts_code = self._sector_key_to_ts_code.get((name, bt))
                if ts_code:
                    break
        if not ts_code:
            raise DataSourceError(
                f"TuShare 未找到板块「{sector_name}」的指数代码，请先拉取热门排名再取成分（名称需与 ths_index 一致）"
            )
        try:
            mem = self.pro.ths_member(ts_code=ts_code)
        except Exception as exc:
            raise DataSourceError(
                _tushare_ths_error_message("TuShare 获取板块成分（ths_member）失败", exc)
            ) from exc
        if mem is None or mem.empty:
            return pd.DataFrame(columns=["code", "name"])
        out = mem.copy()
        out["code"] = out["con_code"].map(lambda x: normalize_code(str(x)))
        out["name"] = out["con_name"].astype(str)
        return out[["code", "name"]].drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)

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
    # 部分 HQ 节点能 connect 但 quotes/bars 恒为空（如缓存 BESTIP 失效）；探测失败时依次尝试。
    _FALLBACK_HQ_SERVERS: Tuple[Tuple[str, int], ...] = (
        ("180.153.18.170", 7709),
        ("218.75.126.9", 7709),
        ("119.147.212.81", 7709),
        ("60.12.136.91", 7709),
        ("61.152.107.168", 7721),
    )

    def __init__(self) -> None:
        try:
            from mootdx.quotes import Quotes  # type: ignore
        except ImportError as exc:
            raise DataSourceError("未安装 mootdx，请先执行: pip install mootdx") from exc
        self._Quotes = Quotes
        self._client: Any = None
        self._stock_universe_cache: pd.DataFrame | None = None
        self._stock_name_map_cache: Dict[str, str] | None = None

    @staticmethod
    def _quotes_probe_ok(client: Any) -> bool:
        try:
            sample = client.quotes(symbol=["000001"])
        except Exception:
            return False
        return sample is not None and not getattr(sample, "empty", True)

    @staticmethod
    def _persist_hq_bestip(server: Tuple[str, int]) -> None:
        """把可用 HQ 写回 ~/.mootdx/config.json，避免下次再命中空行情节点。"""
        try:
            from mootdx.utils import get_config_path  # type: ignore
        except Exception:
            return
        path = Path(get_config_path("config.json"))
        try:
            cfg: Dict[str, Any] = {}
            if path.exists():
                import json

                cfg = json.loads(path.read_text(encoding="utf-8"))
            best = dict(cfg.get("BESTIP") or {})
            best["HQ"] = [server[0], int(server[1])]
            cfg["BESTIP"] = best
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _get_client(self) -> Any:
        if self._client is not None and not getattr(self._client, "closed", False):
            return self._client

        tried: List[str] = []
        # 1) 默认（含 ~/.mootdx BESTIP 缓存）
        try:
            client = self._Quotes.factory(market="std")
            if self._quotes_probe_ok(client):
                self._client = client
                return self._client
            srv = getattr(client, "server", None)
            tried.append(f"default:{srv}")
            try:
                client.close()
            except Exception:
                pass
        except Exception as exc:
            tried.append(f"default:exc={exc}")

        # 2) 备用节点
        for ip, port in self._FALLBACK_HQ_SERVERS:
            label = f"{ip}:{port}"
            if any(label in t for t in tried):
                continue
            try:
                client = self._Quotes.factory(market="std", server=(ip, port))
                if self._quotes_probe_ok(client):
                    self._persist_hq_bestip((ip, port))
                    self._client = client
                    return self._client
                tried.append(label)
                try:
                    client.close()
                except Exception:
                    pass
            except Exception as exc:
                tried.append(f"{label}:exc={exc}")

        raise DataSourceError(
            "mootdx 通达信行情节点不可用（能连接但 quotes 为空或全部连不上）。"
            f" 已尝试：{', '.join(tried) or '无'}。"
            " 可勾选「使用板块快照」、改用 akshare，或删除 ~/.mootdx/config.json 后重试。"
        )

    def _batch_quotes(self, codes: List[str]) -> pd.DataFrame:
        client = self._get_client()
        frames: List[pd.DataFrame] = []
        for i in range(0, len(codes), self._QUOTE_BATCH):
            batch = codes[i : i + self._QUOTE_BATCH]
            result = client.quotes(symbol=batch)
            if result is not None and not result.empty:
                frames.append(result)
        if not frames:
            # 会话中途节点变空：清客户端以便下次重建
            self._client = None
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def quote_snapshot_for_codes(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        通达信批量行情快照：现价等，用于选股结果展示（与日线最后一根可能不同，更接近当前盘口）。
        键为 6 位 normalize_code。
        """
        uniq: List[str] = []
        seen: set[str] = set()
        for c in codes:
            nc = normalize_code(str(c))
            if len(nc) != 6 or nc in seen:
                continue
            seen.add(nc)
            uniq.append(nc)
        if not uniq:
            return {}
        qf = self._batch_quotes(uniq)
        if qf is None or qf.empty:
            return {}
        work = qf.copy()
        if "code" not in work.columns and "symbol" in work.columns:
            work = work.rename(columns={"symbol": "code"})
        if "code" not in work.columns:
            return {}
        work["code"] = work["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in work.iterrows():
            code = normalize_code(row["code"])
            if len(code) != 6:
                continue
            price: float | None = None
            for key in ("price", "last", "close", "current", "最新价", "现价"):
                if key not in row.index:
                    continue
                raw = row[key]
                if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                    continue
                try:
                    f = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(f) and f > 0:
                    price = round(f, 2)
                    break
            prev_close: float | None = None
            if "last_close" in row.index:
                raw_lc = row["last_close"]
                if raw_lc is not None and not (isinstance(raw_lc, float) and pd.isna(raw_lc)):
                    try:
                        lc = float(raw_lc)
                        if math.isfinite(lc) and lc > 0:
                            prev_close = lc
                    except (TypeError, ValueError):
                        prev_close = None
            if price is not None and prev_close is not None and prev_close > 0:
                ratio = price / prev_close
                if ratio > 50:
                    price = round(price / 100.0, 2)
                elif ratio < 0.02:
                    price = round(price * 100.0, 2)
            qdate: str | None = None
            for dcol in ("servertime", "server_time", "datetime", "time", "date"):
                if dcol not in row.index:
                    continue
                raw = row[dcol]
                if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                    continue
                try:
                    if hasattr(raw, "strftime"):
                        qdate = raw.strftime("%Y-%m-%d")[:10]
                    else:
                        s = str(raw).strip()
                        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                            qdate = s[:10]
                except (ValueError, TypeError, OSError):
                    qdate = None
                if qdate:
                    break
            chg_pct: float | None = None
            if price is not None and prev_close is not None and prev_close > 0:
                chg_pct = round((float(price) - prev_close) / prev_close * 100.0, 2)
            out[code] = {"tdx_last_price": price, "tdx_quote_date": qdate, "tdx_change_pct": chg_pct}
        return out

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
        if self._stock_universe_cache is not None:
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
        combined = combined[combined["code"].str.match(r"^(0[0-9]|3[0-9]|6[0-9]|8[0-8]|4[0-9])\d{4}$")]
        if "name" not in combined.columns:
            combined["name"] = ""
        self._stock_universe_cache = combined[["code", "name"]].drop_duplicates("code").copy()
        self._stock_name_map_cache = (
            self._stock_universe_cache.set_index("code")["name"].astype(str).to_dict()
        )
        return self._stock_universe_cache.copy()

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        frames: List[pd.DataFrame] = []

        if board_types in {"all", "concept"}:
            frames.append(self._build_sector_frame(self._load_block_frame("block_gn.dat"), "concept"))
        if board_types in {"all", "industry"}:
            frames.append(self._build_sector_frame(self._load_block_frame("block.dat"), "industry"))

        if not frames:
            raise DataSourceError(f"不支持的板块类型: {board_types}")

        non_empty = [f for f in frames if f is not None and not f.empty]
        if not non_empty:
            raise DataSourceError(
                "mootdx 未返回任何板块行情（板块文件已读到，但批量 quotes 为空）。"
                "常见原因：通达信 HQ 节点失效。请改用 akshare、勾选「使用板块快照」，"
                "或删除 ~/.mootdx/config.json 后重试。"
            )
        combined = pd.concat(non_empty, ignore_index=True)
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

        # mootdx 的 block 文件只有板块名和代码；为支持上层 ST 过滤，这里补齐证券简称。
        if self._stock_name_map_cache is None:
            try:
                self.get_stock_universe()
            except Exception:
                pass
        name_map = self._stock_name_map_cache or {}

        for fname, btype in files:
            block_df = self._load_block_frame(fname)
            exact = block_df[block_df["blockname"].str.lower() == sector_name.lower()]
            if exact.empty:
                exact = block_df[block_df["blockname"].str.contains(sector_name, case=False, na=False)]
            if not exact.empty:
                result = exact[["blockname", "code"]].copy()
                result = result.rename(columns={"blockname": "sector_name"})
                result["name"] = result["code"].map(lambda c: str(name_map.get(str(c).zfill(6), "")).strip())
                result["board_type"] = btype
                out = result[["code", "name", "sector_name", "board_type"]].drop_duplicates("code")
                out = out[out["code"].astype(str).str.zfill(6).map(is_listed_a_share_equity)].copy()
                return out

        raise DataSourceError(f"mootdx 未找到板块: {sector_name}")

    def get_price_history(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        _ = adjust
        sym = normalize_code(code)
        if not is_listed_a_share_equity(sym):
            raise DataSourceError(
                f"mootdx 日线接口仅适用于 A 股普通股等标准代码，{sym} 为指数/ETF/板块码或非个股代码，"
                "无法稳定解析；请从板块成分中排除或换用 akshare/tushare 等源。"
            )
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


class BaostockDataSource(BaseAShareDataSource):
    """
    日 K 走 Baostock 开源接口（与 ingest 的 baostock 路线一致，通常较爬东财页稳）。

    Baostock **不提供**与东财同形态的「全市场热门板块」HTTP 表，故 **板块排名与成分股**
    委托给 AkShare/东财；仅 **get_price_history** 使用 Baostock。选此源时，日线质量偏稳，
    板块仍依赖东财网络，若东财断连，热门板块模式可能失败，可改 mootdx / 指定板块+baostock 日线。
    """

    source_name = "baostock"

    def __init__(self) -> None:
        self._em = AkShareDataSource()

    def get_stock_universe(self) -> pd.DataFrame:
        return self._em.get_stock_universe()

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        return self._em.get_sector_rankings(board_types)

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        return self._em.get_sector_constituents(sector_name, board_type)

    def get_price_history(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        # Baostock query 已按前复权；adjust 仅与接口其它源对齐，此处忽略细分
        _ = adjust
        from app.ingest import _fetch_baostock_daily

        sym = normalize_code(code)
        if not is_listed_a_share_equity(sym):
            raise DataSourceError(f"Baostock 日线不适用于该代码: {sym}")
        start_y = str(start_date).replace("-", "")[:8]
        end_y = str(end_date).replace("-", "")[:8]
        try:
            raw = _fetch_baostock_daily(sym, start_y, end_y)
        except Exception as exc:
            raise DataSourceError(f"Baostock 拉取 {sym} 失败: {exc}") from exc
        if raw is None or raw.empty:
            raise DataSourceError(f"baostock 在区间内无 {sym} 日线")
        work = raw.drop(columns=[c for c in ("symbol",) if c in raw.columns], errors="ignore")
        if "trade_date" in work.columns:
            work = work.rename(columns={"trade_date": "date"})
        standardized = standardize_price_frame(work)
        standardized["code"] = sym
        return standardized


def get_data_source(
    name: str,
    tushare_token: Optional[str] = None,
    *,
    hot_chain_prefer_cache: bool = True,
    hot_chain_force_refresh: bool = False,
) -> BaseAShareDataSource:
    if name == "akshare":
        return AkShareDataSource()
    if name == "tushare":
        return TushareDataSource(token=tushare_token)
    if name == "mootdx":
        return MootdxDataSource()
    if name == "baostock":
        return BaostockDataSource()
    if name == "hot_chain":
        from .hot_chain_datasource import HotChainDataSource

        return HotChainDataSource(
            prefer_snapshot_cache=hot_chain_prefer_cache,
            force_refresh_snapshot=hot_chain_force_refresh,
        )
    raise DataSourceError(f"不支持的数据源: {name}")
