"""
热门市场快照：优先从新浪财经拉取「最近一次收盘/当前截面」的板块与个股涨跌幅序，
失败则按链继续（与 ingest 路线命名对齐）；结果写入本地 JSON 供后续读取。

说明：
- 新浪板块：`ak.stock_industry.stock_sector_spot`（概念/行业，公开行情页数据）。
- 新浪个股：沪深 A 股节点 `getHQNodeData`，`sort=changepercent` 降序，取前 N。
- 腾讯：个股用 QQ `getBoardRankList`（仅支持 sort_type=price，客户端按 `zdf` 排序取前 N）；
  腾讯无稳定公开的「板块涨跌幅全表」接口，故在该步以东方财富板块表补齐（见返回中的 sector_source）。
- Baostock：无对等的板块热度/全市场热门股接口，链中该步会失败并进入下一源。
- 东财 / akshare：板块用 `AkShareDataSource.get_sector_rankings`；个股用东财人气榜 `stock_hot_rank_em`（与新浪「涨幅序」含义不同，见 notes）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import akshare as ak
import pandas as pd
import requests
from akshare.stock import stock_industry
from akshare.utils import demjson

from app.config import get_settings
from app.quant_stock_selector.datasources import AkShareDataSource
from app.quant_stock_selector.exceptions import DataSourceError
from app.quant_stock_selector.market_utils import normalize_score, safe_float

logger = logging.getLogger(__name__)

DEFAULT_CHAIN: tuple[str, ...] = ("sina", "tencent", "baostock", "eastmoney", "akshare")

SINA_HQ_DATA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
)
TENCENT_BOARD_RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"


@dataclass
class HotMarketSnapshot:
    """写入 hot_market_snapshot.json 的可序列化结构。"""

    fetched_at: str
    provider: str
    chain_attempted: list[str]
    sector_source: str
    stock_source: str
    notes: list[str]
    top_stocks: int
    sector_rows: int
    stock_rows: int
    sectors: list[dict[str, Any]]
    stocks: list[dict[str, Any]]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_sina_sector_frames(frames: list[pd.DataFrame], source: str) -> pd.DataFrame:
    out: list[pd.DataFrame] = []
    for f in frames:
        if f is None or f.empty:
            continue
        bdf = f.copy()
        bdf.columns = [str(c).strip() for c in bdf.columns]
        col_map = {c: c for c in bdf.columns}
        sector_col = col_map.get("板块") or col_map.get("板块名称")
        chg_col = col_map.get("涨跌幅")
        if not sector_col or not chg_col:
            continue
        lead = bdf["个股-涨跌幅"].map(safe_float) if "个股-涨跌幅" in bdf.columns else 0.0
        turn = bdf["总成交额"].map(safe_float) if "总成交额" in bdf.columns else 0.0
        tmp = pd.DataFrame(
            {
                "sector_name": bdf[sector_col].astype(str),
                "board_type": bdf["board_type"] if "board_type" in bdf.columns else "concept",
                "change_pct": bdf[chg_col].map(safe_float),
                "advancers_ratio": 0.5,
                "leader_change_pct": lead,
                "turnover_rate": turn,
                "source": source,
            }
        )
        out.append(tmp)
    if not out:
        return pd.DataFrame()
    merged = pd.concat(out, ignore_index=True)
    merged["hot_score"] = (
        normalize_score(merged["change_pct"]) * 0.45
        + normalize_score(merged.get("advancers_ratio", 0.5)) * 0.20
        + normalize_score(merged.get("leader_change_pct", 0.0)) * 0.20
        + normalize_score(merged.get("turnover_rate", 0.0)) * 0.15
    ).round(2)
    return merged.sort_values(["hot_score", "change_pct"], ascending=False).reset_index(drop=True)


def fetch_sina_hot_sectors() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for ind, btype in (("概念", "concept"), ("行业", "industry")):
        raw = stock_industry.stock_sector_spot(indicator=ind)
        if raw is None or raw.empty:
            continue
        t = raw.copy()
        t["board_type"] = btype
        frames.append(t)
    df = _normalize_sina_sector_frames(frames, "sina")
    if df.empty:
        raise DataSourceError("新浪板块数据为空")
    return df


def _sina_hot_stocks_pages(*, top_n: int, max_pages: int = 4) -> pd.DataFrame:
    """多页拉取 A 股按涨跌幅降序，合并后取前 top_n（约等于收盘热门涨幅股）。"""
    rows: list[pd.DataFrame] = []
    for page in range(1, max_pages + 1):
        p = {
            "page": page,
            "num": "80",
            "sort": "changepercent",
            "asc": "0",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        }
        r = requests.get(SINA_HQ_DATA_URL, params=p, timeout=20)
        r.raise_for_status()
        chunk = demjson.decode(r.text)
        part = pd.DataFrame(chunk)
        if part.empty:
            break
        rows.append(part)
        if len(part) < 80:
            break
    if not rows:
        raise DataSourceError("新浪A股按涨跌幅排序无数据")
    big = pd.concat(rows, ignore_index=True)
    big["code"] = big["code"].astype(str).str.zfill(6)
    big["changepercent"] = pd.to_numeric(big["changepercent"], errors="coerce")
    big = big.sort_values("changepercent", ascending=False).drop_duplicates("code").head(top_n)
    return big


def fetch_sina_hot_stocks(*, top_n: int) -> pd.DataFrame:
    df = _sina_hot_stocks_pages(top_n=top_n)
    out = pd.DataFrame(
        {
            "rank": range(1, len(df) + 1),
            "code": df["code"],
            "name": df["name"],
            "change_pct": df["changepercent"],
            "last_price": pd.to_numeric(df.get("trade"), errors="coerce"),
            "source": "sina",
        }
    )
    return out


def fetch_tencent_hot_stocks(*, top_n: int) -> pd.DataFrame:
    """腾讯沪深京 A 股排行：接口仅支持按价排序，这里拉一批后按 zdf 重排。"""
    r = requests.get(
        TENCENT_BOARD_RANK_URL,
        params={
            "_appver": "11.17.0",
            "board_code": "aStock",
            "sort_type": "price",
            "direct": "down",
            "offset": "0",
            "count": "200",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    lst = (data.get("data") or {}).get("rank_list")
    if not lst:
        raise DataSourceError("腾讯 A 股排行无数据")
    tdf = pd.DataFrame(lst)
    if "zdf" not in tdf.columns or "code" not in tdf.columns:
        raise DataSourceError("腾讯排行字段缺失")
    tdf["code"] = tdf["code"].astype(str).str.zfill(6)
    tdf["zdf"] = pd.to_numeric(tdf["zdf"], errors="coerce")
    tdf = tdf.sort_values("zdf", ascending=False).head(top_n)
    return pd.DataFrame(
        {
            "rank": range(1, len(tdf) + 1),
            "code": tdf["code"],
            "name": tdf.get("name", ""),
            "change_pct": tdf["zdf"],
            "last_price": pd.to_numeric(tdf.get("zxj"), errors="coerce"),
            "source": "tencent",
        }
    )


def _em_sector_frame(ds: AkShareDataSource) -> pd.DataFrame:
    """
    东财板块表：连拉概念+行业时易受远端断连/限流，与 ingest 东财线类似，做多次重试与退避。
    """
    import time

    last: BaseException | None = None
    for attempt in range(1, 6):
        try:
            r = ds.get_sector_rankings("all")
            if r is not None and not r.empty:
                return r
        except Exception as e:
            last = e
            wait = 1.2 * (attempt**1.4)
            logger.warning("东财 get_sector_rankings 第 %s 次失败，%.1fs 后重试: %s", attempt, wait, e)
            time.sleep(min(wait, 12.0))
    if last is not None:
        raise DataSourceError(f"东财板块表多次重试后仍失败: {last}") from last
    raise DataSourceError("东财板块热度为空")


def _bundle_sina(*, top_stocks: int) -> HotMarketSnapshot:
    sectors = fetch_sina_hot_sectors()
    stocks = fetch_sina_hot_stocks(top_n=top_stocks)
    return _to_snapshot(
        "sina",
        list(DEFAULT_CHAIN),
        "sina",
        "sina",
        [],
        top_stocks,
        sectors,
        stocks,
    )


def _bundle_tencent(*, top_stocks: int) -> HotMarketSnapshot:
    notes = [
        "腾讯侧无与新浪对等的全市场板块涨跌幅表；本步板块使用东方财富（概念+行业）作为补齐；个股为腾讯财经排行按涨跌幅重排。",
    ]
    ds = AkShareDataSource()
    sectors = _em_sector_frame(ds)
    sectors = sectors.copy()
    sectors["source"] = "eastmoney"
    stocks = fetch_tencent_hot_stocks(top_n=top_stocks)
    return _to_snapshot(
        "tencent",
        list(DEFAULT_CHAIN),
        "eastmoney",
        "tencent",
        notes,
        top_stocks,
        sectors,
        stocks,
    )


def _bundle_baostock() -> None:
    raise DataSourceError("Baostock 无公开「热门板块/全市场涨幅榜」一体接口，请使用后续路线。")


def _bundle_eastmoney(*, top_stocks: int) -> HotMarketSnapshot:
    notes = [
        "东财步：板块为东财；个股为人气榜（stock_hot_rank_em），与「按涨幅排序」含义不同。",
    ]
    ds = AkShareDataSource()
    sectors = _em_sector_frame(ds)
    try:
        hot = ak.stock_hot_rank_em()
    except Exception as e:
        raise DataSourceError(f"东财人气榜失败: {e}") from e
    if hot is None or hot.empty:
        raise DataSourceError("东财人气榜为空")
    hot2 = hot.head(top_stocks).copy()
    if "股票名称" not in hot2.columns or "涨跌幅" not in hot2.columns:
        raise DataSourceError("东财人气榜列结构不符合预期")
    if "代码" not in hot2.columns:
        raise DataSourceError("东财人气榜缺少代码列")
    code_col = "代码"
    raw_codes = hot2[code_col].astype(str)
    code6 = raw_codes.str.replace(r"\D", "", regex=True)
    code6 = code6.str.zfill(6)
    code6 = code6.str[-6:]
    stocks = pd.DataFrame(
        {
            "rank": hot2.get("当前排名", range(1, len(hot2) + 1)),
            "code": code6,
            "name": hot2["股票名称"],
            "change_pct": pd.to_numeric(hot2["涨跌幅"], errors="coerce"),
            "last_price": pd.to_numeric(hot2.get("最新价", float("nan")), errors="coerce"),
            "source": "eastmoney",
        }
    )
    return _to_snapshot(
        "eastmoney",
        list(DEFAULT_CHAIN),
        "eastmoney",
        "eastmoney",
        notes,
        top_stocks,
        sectors,
        stocks,
    )


def _bundle_akshare(*, top_stocks: int) -> HotMarketSnapshot:
    """与 eastmoney 同为东财数据栈，与 _bundle_eastmoney 一致。"""
    b = _bundle_eastmoney(top_stocks=top_stocks)
    b.provider = "akshare"
    b.chain_attempted = list(DEFAULT_CHAIN)
    return b


def _df_sectors_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    d = df.copy()
    for col in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[col]):
            d[col] = d[col].astype(str)
    return d.to_dict(orient="records")


def _df_stocks_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return _df_sectors_to_records(df)


def _to_snapshot(
    provider: str,
    chain: list[str],
    sector_source: str,
    stock_source: str,
    notes: list[str],
    top_stocks: int,
    sectors: pd.DataFrame,
    stocks: pd.DataFrame,
) -> HotMarketSnapshot:
    return HotMarketSnapshot(
        fetched_at=_iso_now(),
        provider=provider,
        chain_attempted=chain,
        sector_source=sector_source,
        stock_source=stock_source,
        notes=notes,
        top_stocks=top_stocks,
        sector_rows=len(sectors),
        stock_rows=len(stocks),
        sectors=_df_sectors_to_records(sectors),
        stocks=_df_stocks_to_records(stocks),
    )


def _try_provider(name: str, top_stocks: int) -> HotMarketSnapshot:
    k = (name or "").strip().lower()
    if k == "sina":
        return _bundle_sina(top_stocks=top_stocks)
    if k == "tencent":
        return _bundle_tencent(top_stocks=top_stocks)
    if k == "baostock":
        _bundle_baostock()
    if k in ("eastmoney", "em"):
        return _bundle_eastmoney(top_stocks=top_stocks)
    if k == "akshare":
        return _bundle_akshare(top_stocks=top_stocks)
    raise DataSourceError(f"未知热门快照源: {name!r}")


def fetch_hot_market_snapshot(
    *,
    top_stocks: int = 100,
    chain: Sequence[str] | None = None,
) -> HotMarketSnapshot:
    """
    按顺序尝试各数据源，成功则返回并应由调用方持久化。

    默认链与 ingest 的公开路线命名对齐：sina → tencent → baostock → eastmoney → akshare
    """
    c = tuple(chain) if chain else DEFAULT_CHAIN
    last_err: Exception | None = None
    for name in c:
        key = (name or "").strip().lower()
        if not key:
            continue
        try:
            snap = _try_provider(key, top_stocks=top_stocks)
            return snap
        except Exception as e:
            last_err = e
            logger.info("hot snapshot route %s failed: %s", name, e)
    msg = f"热门市场快照所有路线均失败: {c!r}"
    if last_err:
        raise DataSourceError(f"{msg}；最后错误: {last_err}") from last_err
    raise DataSourceError(msg)


def default_hot_market_snapshot_path() -> Path:
    return get_settings().data_dir / "hot_market_snapshot.json"


def save_hot_market_snapshot(snap: HotMarketSnapshot, path: Path | None = None) -> Path:
    p = path or default_hot_market_snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load_hot_market_snapshot(path: Path | None = None) -> HotMarketSnapshot | None:
    p = path or default_hot_market_snapshot_path()
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return HotMarketSnapshot(**{k: raw[k] for k in HotMarketSnapshot.__dataclass_fields__})


