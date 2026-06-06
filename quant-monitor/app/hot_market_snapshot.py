"""
热门市场快照：板块热度 + 个股热门榜，链式拉取并落盘 JSON。

## 功能作用

从公开行情源拉取「最近一次收盘/当前截面」的**板块涨跌幅序**与**个股热门榜**，
写入 `data/hot_market_snapshot.json`，供控制台 **⑨ 热门市场快照**、
**⑨ 选股**（`data_source=hot_chain`）等模块**只读**使用，减轻重复联网与限流。

默认数据源链（与 ingest 路线命名对齐）：

```
sina → tencent → baostock → eastmoney → akshare
```

任一步成功即返回；全部失败则 `DataSourceError`。

## 各源说明（摘要）

| 源 | 板块 | 个股 | 备注 |
|----|------|------|------|
| **sina** | 新浪概念/行业 `stock_sector_spot` | 沪深 A 按涨幅降序，**仅沪深主板** Top N | 默认首选 |
| **tencent** | 无对等全表 → **东财板块补齐** | QQ `getBoardRankList` 按 zdf 重排 | `sector_source=eastmoney` |
| **baostock** | — | — | 无对等接口，链中跳过 |
| **eastmoney / akshare** | 东财 `get_sector_rankings` | 东财人气榜 `stock_hot_rank_em` | 与「涨幅序」含义不同 |

个股表附加 **`related_business`**：落盘前逐只调东财 `stock_individual_info_em` 的「行业」
（勿用 ulist 批量 f127，语义不同易误显示涨跌幅）。

## 沪深主板过滤

热门股列表统一筛 **沪 60 / 深 000–003**（排除科创 688/689、创业 300/301、北交所等），
见 `_is_hs_main_board_equity`。

## 对外接口

| 函数 / 类型 | 用途 |
|-------------|------|
| `HotMarketSnapshot` | 可序列化快照结构（sectors + stocks + metadata） |
| `fetch_hot_market_snapshot` | 按链联网拉取，成功返回 snapshot |
| `save_hot_market_snapshot` | 原子写入 JSON（`.tmp` 再 replace） |
| `load_hot_market_snapshot` | 读本地 JSON；不存在返回 None |
| `default_hot_market_snapshot_path` | 默认 `{data_dir}/hot_market_snapshot.json` |

## 调用方

- `POST /meta/hot-market-snapshot/refresh`、`GET /meta/hot-market-snapshot`
- `quant_stock_selector.hot_chain_datasource`、`sectors._constituents_from_hot_market_snapshot`
- `POST /research/sector-screen`（`data_source=hot_chain`、合并 snapshot stocks）

## 数据说明

快照为公开页/接口截面，非交易所实时推送；东财人气榜与新浪涨幅序不可直接对比。
旧快照无 `related_business` 时需重新「刷新热门快照」。
"""
from __future__ import annotations

import json
import logging
import time
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
from app.quant_stock_selector.market_utils import normalize_code, normalize_score, safe_float

logger = logging.getLogger(__name__)

DEFAULT_CHAIN: tuple[str, ...] = ("sina", "tencent", "baostock", "eastmoney", "akshare")
"""默认尝试顺序，与 ingest 公开路线命名一致。"""


# --- 沪深主板过滤与行业 enrichment ---


def _is_hs_main_board_equity(code: object) -> bool:
    """
    沪深主板 A 股：沪 60 段（排除 688/689 科创板）、深 000–003 段（含常见 002）。
    不含创业板 300/301、北交所 43/83/87/88/92 等。
    """
    c = normalize_code(str(code))
    if len(c) != 6:
        return False
    if c.startswith(("688", "689", "300", "301")):
        return False
    if c.startswith(("430", "83", "87", "88", "92")):
        return False
    if c.startswith("60"):
        return True
    if c.startswith(("000", "001", "002", "003")):
        return True
    return False


def _lookup_related_business_em(code6: str) -> str:
    """
    东财个股信息（AkShare `stock_individual_info_em` / qt/stock/get）中的「行业」文本。

    注意：``ulist.np/get`` 批量接口里的 f127 与 ``qt/stock/get`` 的 f127 语义不一致，
    前者在列表场景下常为数值行情字段，误用会显示成涨跌幅；此处必须用个股信息接口。
    """
    c = normalize_code(code6)
    if len(c) != 6:
        return ""
    try:
        df = ak.stock_individual_info_em(symbol=c, timeout=12)
    except Exception as e:
        logger.debug("stock_individual_info_em %s: %s", c, e)
        return ""
    if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
        return ""
    for label in ("行业", "所属行业"):
        hit = df.loc[df["item"] == label, "value"]
        if hit.empty:
            continue
        v = hit.iloc[0]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if s and s.lower() not in ("nan", "none", "-", "—"):
            return s[:220]
    return ""


def _enrich_stocks_related_business(stocks: pd.DataFrame) -> pd.DataFrame:
    """为快照个股表增加 related_business（东财个股信息中的行业）；失败时列为空字符串。"""
    if stocks is None or stocks.empty or "code" not in stocks.columns:
        return stocks
    try:
        vals: list[str] = []
        for raw in stocks["code"].astype(str):
            c6 = normalize_code(raw)
            vals.append(_lookup_related_business_em(c6) if len(c6) == 6 else "")
            time.sleep(0.05)
        s = stocks.copy()
        s["related_business"] = vals
        return s
    except Exception as e:
        logger.warning("related_business 批量补齐失败: %s", e)
        s = stocks.copy()
        s["related_business"] = [""] * len(s)
        return s

# --- 外部 API URL ---

SINA_HQ_DATA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
)
TENCENT_BOARD_RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"


# --- 快照数据结构 ---


@dataclass
class HotMarketSnapshot:
    """写入 hot_market_snapshot.json 的可序列化结构（sectors + stocks + 元数据）。"""
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


# --- 新浪：板块与涨幅榜 ---


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


def _sina_hot_stocks_pages(*, top_n: int, max_pages: int = 12) -> pd.DataFrame:
    """多页拉取 A 股按涨跌幅降序，筛沪深主板后取前 top_n（主板在全市场里占比有限，故多翻几页）。"""
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
    big = big.sort_values("changepercent", ascending=False).drop_duplicates("code")
    main = big[big["code"].map(_is_hs_main_board_equity)]
    return main.head(top_n)


def fetch_sina_hot_stocks(*, top_n: int) -> pd.DataFrame:
    df = _sina_hot_stocks_pages(top_n=top_n)
    if df is None or df.empty:
        raise DataSourceError("新浪涨幅榜筛沪深主板后无可用成分")
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


# --- 腾讯：个股排行（板块由东财补齐） ---


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
            "count": "800",
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
    tdf = tdf.sort_values("zdf", ascending=False)
    tdf = tdf[tdf["code"].map(_is_hs_main_board_equity)].head(top_n)
    if tdf.empty:
        raise DataSourceError("腾讯排行筛沪深主板后为空")
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


# --- 东财：板块表与人气榜 ---


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


# --- 各源 bundle（组装 HotMarketSnapshot） ---


def _bundle_sina(*, top_stocks: int) -> HotMarketSnapshot:
    sectors = fetch_sina_hot_sectors()
    stocks = fetch_sina_hot_stocks(top_n=top_stocks)
    stocks = _enrich_stocks_related_business(stocks)
    notes = [
        "快照热门股：在全市场涨幅排序中仅保留**沪、深主板**，取前 N 条（不含科创/创业/北交所）。",
        "个股「相关业务类型」为东财个股信息接口中的「行业」文字（非 ulist 数值字段）；与新浪个股源无关；旧快照请重新「刷新热门快照」后更新。",
    ]
    return _to_snapshot(
        "sina",
        list(DEFAULT_CHAIN),
        "sina",
        "sina",
        notes,
        top_stocks,
        sectors,
        stocks,
    )


def _bundle_tencent(*, top_stocks: int) -> HotMarketSnapshot:
    notes = [
        "腾讯侧无与新浪对等的全市场板块涨跌幅表；本步板块使用东方财富（概念+行业）作为补齐；个股为腾讯财经排行按涨跌幅重排。",
        "快照热门股：个股列表已筛为**沪、深主板**前 N 条（不含科创/创业/北交所）。",
    ]
    ds = AkShareDataSource()
    sectors = _em_sector_frame(ds)
    sectors = sectors.copy()
    sectors["source"] = "eastmoney"
    stocks = fetch_tencent_hot_stocks(top_n=top_stocks)
    stocks = _enrich_stocks_related_business(stocks)
    notes = notes + [
        "个股「相关业务类型」为东财个股信息接口「行业」字段（与腾讯个股源无关）。",
    ]
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
    if "股票名称" not in hot.columns or "涨跌幅" not in hot.columns:
        raise DataSourceError("东财人气榜列结构不符合预期")
    if "代码" not in hot.columns:
        raise DataSourceError("东财人气榜缺少代码列")
    code_col = "代码"
    raw_codes = hot[code_col].astype(str)
    code6 = raw_codes.str.replace(r"\D", "", regex=True).str.zfill(6).str[-6:]
    main_mask = code6.map(_is_hs_main_board_equity).fillna(False)
    hot2 = hot.loc[main_mask].head(top_stocks).copy()
    if hot2.empty:
        raise DataSourceError("东财人气榜中未筛出沪深主板成分股")
    code6_f = hot2[code_col].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6).str[-6:]
    stocks = pd.DataFrame(
        {
            "rank": range(1, len(hot2) + 1),
            "code": code6_f,
            "name": hot2["股票名称"],
            "change_pct": pd.to_numeric(hot2["涨跌幅"], errors="coerce"),
            "last_price": pd.to_numeric(hot2.get("最新价", float("nan")), errors="coerce"),
            "source": "eastmoney",
        }
    )
    stocks = _enrich_stocks_related_business(stocks)
    notes = notes + [
        "快照热门股：人气榜接口单次条数有限，已筛**沪、深主板**后取前 N 条（与「人气」排序含义不同）。",
        "个股表「相关业务类型」为东财个股信息接口中的「行业」分类（非市场概念标签如算力/AI 全量列表）。",
    ]
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


# --- DataFrame → 记录 / 快照构造 ---


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


# --- 主入口与本地 JSON 读写 ---


def fetch_hot_market_snapshot(
    *,
    top_stocks: int = 100,
    chain: Sequence[str] | None = None,
) -> HotMarketSnapshot:
    """
    按顺序尝试各数据源链，成功则返回 snapshot（由调用方 `save_hot_market_snapshot` 落盘）。

    参数:
        top_stocks: 个股表保留条数（筛主板后）。
        chain:      源名称序列；None 则用 DEFAULT_CHAIN。

    异常:
        DataSourceError — 全部路线失败。
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
    """默认落盘路径：`{data_dir}/hot_market_snapshot.json`。"""
    return get_settings().data_dir / "hot_market_snapshot.json"


def save_hot_market_snapshot(snap: HotMarketSnapshot, path: Path | None = None) -> Path:
    """原子写入 JSON（先写 .tmp 再 replace），返回最终路径。"""
    p = path or default_hot_market_snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load_hot_market_snapshot(path: Path | None = None) -> HotMarketSnapshot | None:
    """读取本地快照；文件不存在时返回 None。"""
    p = path or default_hot_market_snapshot_path()
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return HotMarketSnapshot(**{k: raw[k] for k in HotMarketSnapshot.__dataclass_fields__})


