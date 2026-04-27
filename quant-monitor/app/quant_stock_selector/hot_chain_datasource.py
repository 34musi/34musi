"""
热门链数据源：板块排序与 /meta/hot-market-snapshot 一致（新浪优先 → 回退 → 可落盘），
成分股与日线仍经 AkShare 东财接口拉取，与现有 pipeline 兼容。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from .datasources import AkShareDataSource, BaseAShareDataSource
from .exceptions import DataSourceError

logger = logging.getLogger(__name__)


def _snap_sectors_to_dataframe(snap) -> pd.DataFrame:
    """将 hot_market_snapshot 的 sectors 列表规范为与 get_sector_rankings 近似的表。"""
    if snap is None or not getattr(snap, "sectors", None):
        return pd.DataFrame()
    rows = snap.sectors
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    need = [
        "sector_name",
        "board_type",
        "change_pct",
        "advancers_ratio",
        "leader_change_pct",
        "turnover_rate",
        "hot_score",
    ]
    for col in need:
        if col not in frame.columns:
            if col == "hot_score":
                frame["hot_score"] = 0.0
            elif col == "advancers_ratio":
                frame["advancers_ratio"] = 0.5
            else:
                frame[col] = 0.0
    for c in need:
        if c in frame.columns and frame[c].dtype == object:
            frame[c] = pd.to_numeric(frame[c], errors="coerce")
    if "source" not in frame.columns:
        frame["source"] = getattr(snap, "provider", "hot_chain")
    for c in need:
        if c in frame.columns:
            frame[c] = frame[c].fillna(0.0)
    if "hot_score" in frame.columns and frame["hot_score"].abs().max() < 1e-6 and "change_pct" in frame.columns:
        frame["hot_score"] = frame["change_pct"]
    if "hot_score" in frame.columns:
        frame = frame.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)
    cols = need + (["source"] if "source" in frame.columns else [])
    return frame[cols]


class HotChainDataSource(BaseAShareDataSource):
    """
    板块表：app.hot_market_snapshot（链式拉取 + 本地 JSON 缓存）；
    其他：复用 AkShare / 东财实现。
    """

    source_name = "hot_chain"

    def __init__(
        self,
        *,
        prefer_snapshot_cache: bool = True,
        force_refresh_snapshot: bool = False,
    ) -> None:
        self._prefer_cache = prefer_snapshot_cache
        self._force = force_refresh_snapshot
        self._rankings: pd.DataFrame | None = None
        self._em = AkShareDataSource()
        self._meta_provider: str = ""

    def _try_snapshot_chain(self):
        """返回 HotMarketSnapshot 或 None（失败/空时由调用方直拉东财）。"""
        from app.hot_market_snapshot import (
            fetch_hot_market_snapshot,
            load_hot_market_snapshot,
            save_hot_market_snapshot,
        )

        if self._force:
            s = fetch_hot_market_snapshot()
            save_hot_market_snapshot(s)
            return s
        if self._prefer_cache:
            s = load_hot_market_snapshot()
            if s is not None and getattr(s, "sectors", None) and len(s.sectors) > 0:
                return s
        s = fetch_hot_market_snapshot()
        save_hot_market_snapshot(s)
        return s

    def _ensure_rankings(self) -> pd.DataFrame:
        if self._rankings is not None and not self._rankings.empty:
            return self._rankings
        chain_err: BaseException | None = None
        try:
            snap = self._try_snapshot_chain()
            if snap is not None:
                self._meta_provider = getattr(snap, "provider", "")
                self._rankings = _snap_sectors_to_dataframe(snap)
                if self._rankings is not None and not self._rankings.empty:
                    return self._rankings
        except Exception as e:
            chain_err = e
            logger.warning("hot_chain：热门链快照未就绪，将直接拉东财板块表: %s", e)
        # 回退：与 UI 中 akshare 一致，从东财拉板块；多次重试缓解 RemoteDisconnected/限流
        last: BaseException | None = None
        for attempt in range(1, 6):
            try:
                self._rankings = self._em.get_sector_rankings("all")
                if chain_err is not None:
                    self._meta_provider = f"em_direct(after_{type(chain_err).__name__})"
                else:
                    self._meta_provider = "em_direct"
                return self._rankings
            except Exception as e2:
                last = e2
                w = min(1.5 * (attempt**1.2), 14.0)
                logger.info("hot_chain 东财直拉第 %s 次失败，%.1fs 后重试: %s", attempt, w, e2)
                time.sleep(w)
        tail = f"{last}" if last is not None else "未知"
        if chain_err is not None:
            msg = f"新浪链失败: {chain_err!s}。东财板块直拉也失败: {tail}"
        else:
            msg = f"东财板块表拉取失败: {tail}"
        raise DataSourceError(
            f"{msg}。建议：隔几分钟重试、检查网络/系统代理，或于③改行情路线试「测试数据源」；"
            f"可暂时改用数据源「akshare」；hot_chain 下可取消「优先读快照」并勾选「强制重拉」后再运行。"
        ) from (last if last is not None else chain_err)

    def get_stock_universe(self) -> pd.DataFrame:
        return self._em.get_stock_universe()

    def get_sector_rankings(self, board_types: str = "all") -> pd.DataFrame:
        frame = self._ensure_rankings()
        if board_types == "concept":
            return frame[frame["board_type"].astype(str).str.lower() == "concept"].copy()
        if board_types == "industry":
            return frame[frame["board_type"].astype(str).str.lower() == "industry"].copy()
        return frame.copy()

    def get_sector_constituents(self, sector_name: str, board_type: Optional[str] = None) -> pd.DataFrame:
        return self._em.get_sector_constituents(sector_name, board_type)

    def get_price_history(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        return self._em.get_price_history(code, start_date, end_date, adjust=adjust)
