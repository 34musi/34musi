"""
FastAPI 应用入口：健康检查、自选池、行情摄取、信号查询、告警预览与元信息。

启动时 lifespan 内 init_db；多数写操作与敏感读依赖 optional_api_key（配置 API_KEY 时生效）。
限流使用 slowapi，按客户端 IP（get_remote_address）计桶。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from datetime import date, datetime, timezone

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import delete, or_, select

from app.config import get_settings
from app.db import (
    WATCHLIST_ORIGIN_AUTO_HOT,
    WATCHLIST_ORIGIN_AUTO_QUANT,
    WATCHLIST_ORIGIN_MANUAL,
    DecisionJournalRow,
    SignalCacheRow,
    WatchlistRow,
    init_db,
    session_scope,
)
from app.fundamentals import (
    fetch_individual_fund_flow_recent_rows,
    fetch_individual_fund_flow_latest_metrics,
    spot_liquidity_fields_for_codes,
    upsert_fundamental_snapshot,
)
from app.ingest import (
    fetch_stock_name,
    fetch_stock_names_map,
    incremental_refresh,
    ingest_symbol_range,
    list_bars_from_db,
    normalize_symbol,
    strength_snapshot_for_symbol,
    test_akshare_connectivity,
    watchlist_bar_fields_for_session,
)
from app.quant_stock_selector import DataSourceError, get_data_source, pick_from_hot_sectors
from app.quant_stock_selector.hot_pick import is_star_board_code, is_st_stock_name
from app.quant_stock_selector.market_utils import is_listed_a_share_equity, normalize_code
from app.quant_stock_selector.datasources import (
    default_sector_snapshot_path,
    load_sector_rankings_snapshot,
    save_sector_rankings_snapshot,
)
from app.quant_stock_selector.cli import validate_args
from app.quant_stock_selector.pipeline import run_analysis
from app.schemas import (
    AlertsPreviewIn,
    DailyBarOut,
    DisclaimerOut,
    FillHotSectorsIn,
    FillHotSectorsOut,
    FillHotSectorsSummary,
    ForecastValidateOut,
    HotMarketSnapshotFileOut,
    HotMarketSnapshotOut,
    HotMarketSnapshotRefreshIn,
    HotMarketSnapshotRefreshOut,
    IngestDataSource,
    IngestFundamentalsIn,
    IngestUpdateIn,
    JournalIn,
    JournalOut,
    QuantWatchlistSyncIn,
    QuantWatchlistSyncOut,
    SectorConstituentsTopIn,
    SectorConstituentsTopOut,
    SectorScreenDataSource,
    SectorScreenIn,
    SectorScreenOut,
    SelectorSectorDataSource,
    SelfUseMetaOut,
    SignalOut,
    WatchlistBatchDeleteIn,
    WatchlistBatchDeleteOut,
    WatchlistHotSnapshotImportIn,
    WatchlistHotSnapshotImportOut,
    WatchlistIn,
    WatchlistItem,
    WebDataPreviewIn,
)
from app.forecast_validate import run_forecast_validate
from app.hot_market_snapshot import (
    default_hot_market_snapshot_path,
    fetch_hot_market_snapshot,
    load_hot_market_snapshot,
    save_hot_market_snapshot,
)
from app.signals import compute_signal
from app.alerts import detect_changes, signal_to_snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- OpenAPI / Swagger：面向小白的说明与分组 ----------
OPENAPI_DESCRIPTION = """
## 这个页面是干什么的？

**普通用户**请优先打开图形控制台：**[/ui](/ui)**（分步表单，不必懂接口细节）。

这是 **在线试用说明**：不用写代码，在浏览器里就能「点一点」调用接口，看返回的 JSON。

**数据说明**：行情来自公开渠道的 **日线（前复权）**，**不是** 实时盘口；结果仅供技术向参考，**不构成投资建议**。

---

### 第一次用？按下面顺序操作即可

1. **看要不要登录钥**  
   若管理员在服务器上配置了环境变量 `API_KEY`，请先点页面右上角的 **Authorize**，在 `X-API-Key` 里填入**完全相同**的一串字符；没配置则不用填。

2. **添加要关注的股票**  
   打开分组 **「② 管理自选股票」** → `POST /watchlist` → 点 **Try it out**，在请求体里填 `600519` 这类 **6 位代码** → **Execute**。

3. **把行情下载到本机数据库**  
   打开 **「③ 更新行情数据」** → `POST /ingest/update` → **Execute**（会按自选列表逐个拉取，需联网，可能要等几秒）。

4. **看系统算出的信号**  
   打开 **「④ 查看信号」** → `GET /signals` 或 `GET /signals/{symbol}` → **Execute**。

4b. **（可选 Demo）拉扩展因子**  
   `POST /ingest/fundamentals`：把估值、财报同比、ROE/ROA、毛利率与净利率、资产负债率、流动与速动比、每股经营现金流、主力净流入等写入本地，再查信号时会与技术面分数合成（有界调整）。

4c. **（研究向）可验证的方向预测**  
   `GET /research/forecast-validate?symbol=600519`：仅用**已入库日线**做 walk-forward，含**双均线（周期可配）**、Logistic、趋势规则、多数类基线；响应中带 `pedagogy` 学习路线说明与参考阅读链接（非投资建议）。

5. **（可选）看和上次比有没有变化**  
   打开 **「⑤ 变动预览」** → `POST /alerts/preview`。

6. **（推荐）自用定位与决策日志**  
   `GET /meta/self-use` 查看工具定位与风控检查项；`POST /journal` 记录本周结论与实盘复盘（仓位、是否按计划）。

---

### 小技巧

- 顶部 **Filter** 搜索框可输入 `watchlist`、`signals` 快速定位接口。
- 若返回 **401**：说明服务端启用了 API Key，请检查 **Authorize** 是否已填对。
- 若 **400 / 数据不足**：请先完成第 2、3 步。
"""

OPENAPI_TAGS = [
    {
        "name": "① 入门必读",
        "description": "先看服务是否正常；本页最上面的说明也值得读一遍。",
    },
    {
        "name": "② 管理自选股票",
        "description": "告诉系统你要关注哪些 6 位 A 股代码（先加自选，后面拉数据、算信号都基于自选列表）。",
    },
    {
        "name": "③ 更新行情数据",
        "description": "从网络拉日线写入本机数据库；自选里有多少只股票，就会更新多少只。含扩展因子（估值/财报同比/资金流）入库接口。",
    },
    {
        "name": "④ 查看信号",
        "description": "根据已有日线计算趋势、强度、评分等；若已执行扩展因子入库，会展示 fundamentals 并与技术面分合成总分。",
    },
    {
        "name": "⑤ 变动预览",
        "description": "对比「上一次缓存」与「当前计算结果」，看趋势或评分档位是否变化。",
    },
    {
        "name": "⑥ 说明与免责",
        "description": "数据源与免责声明全文。",
    },
    {
        "name": "⑦ 决策日志（自用）",
        "description": "记录判断依据、计划仓位与执行一致性；辅助一周趋势复盘，非投资建议。",
    },
    {
        "name": "⑧ 研究：预测验证",
        "description": "本地日线 walk-forward：双均线 + Logistic + 规则 + 基线；附学习路线说明（pandas/numpy 回测叙事）。非投资建议。",
    },
    {
        "name": "⑨ 量化选股（脚本）",
        "description": "与仓库 `quant_stock_selector.py` / `app.quant_stock_selector` 同构：热门板块或指定板块/代码 → 拉行情 → 技术面 + 双均线回测 → 综合分。联网多、耗时长。非投资建议。",
    },
]

limiter = Limiter(key_func=get_remote_address)

# 供 Swagger「Authorize」填写；与 optional_api_key 使用同一请求头名
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def optional_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    """
    依赖注入：若 Settings.api_key 非空，则要求请求头 X-API-Key 与其相等，否则 401。

    api_key 为空字符串时不校验（本地开发常用）。使用 Security 后，文档页会显示「Authorize」按钮。
    """
    settings = get_settings()
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时建库/建表；关闭时无额外清理（SQLite 文件保留）。"""
    init_db()
    yield


app = FastAPI(
    title="A 股趋势监控 · 可点着用的接口说明",
    description=OPENAPI_DESCRIPTION,
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
    swagger_ui_parameters={
        "docExpansion": "list",
        "filter": True,
        "tryItOutEnabled": True,
        "displayRequestDuration": True,
    },
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_ingest_route(data_source: IngestDataSource | None) -> str:
    """与 ingest 一致的小写路线关键字。"""
    if data_source is not None:
        return data_source.value
    return str(get_settings().ingest_data_source or "auto").strip().lower()


def _pre_refresh_symbols(symbols: list[str], *, route: str, pre_refresh: bool) -> None:
    """按路线对各标的 incremental_refresh；失败仅打日志。"""
    if not pre_refresh or not symbols:
        return
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    for i, sym in enumerate(symbols):
        if i > 0 and pause > 0:
            time.sleep(pause)
        try:
            incremental_refresh(sym, data_source=route)
        except Exception as e:
            logger.debug("pre_refresh skipped %s route=%s: %s", sym, route, e)


def _http_exception_from_datasource(e: DataSourceError) -> HTTPException:
    """TuShare 等同花顺接口返回「无权限」时改用 403，便于控制台与客户端区分参数错误与权限不足。"""
    msg = str(e)
    if "没有接口" in msg and "权限" in msg:
        return HTTPException(status_code=403, detail=msg)
    return HTTPException(status_code=400, detail=msg)


def _hot_tushare_token(explicit: str | None) -> str | None:
    """热门接口用 TuShare token：请求体/Query 优先，其次服务端配置与环境变量。"""
    raw = (explicit or "").strip()
    if raw:
        return raw
    s = get_settings()
    cfg = (getattr(s, "tushare_token", None) or "").strip()
    if cfg:
        return cfg
    return (os.getenv("TUSHARE_TOKEN") or "").strip() or None


def _resolve_sector_datasource(name: str, *, tushare_token: str | None = None):
    """热门板块：akshare / mootdx / tushare（同花顺指数，需 token 与积分）。"""
    key = (name or "").strip().lower()
    if key == "tushare":
        token = _hot_tushare_token(tushare_token)
        if not token:
            raise HTTPException(
                status_code=400,
                detail="使用 TuShare 热门板块请在请求体中传入 tushare_token，或配置环境变量 TUSHARE_TOKEN / 服务端 tushare_token。",
            )
        try:
            return get_data_source("tushare", tushare_token=token)
        except DataSourceError as e:
            raise _http_exception_from_datasource(e) from e
    if key not in ("akshare", "mootdx"):
        raise HTTPException(
            status_code=400,
            detail="热门板块 selector_data_source 须为 akshare、mootdx 或 tushare。",
        )
    try:
        return get_data_source(key)
    except DataSourceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _names_from_hot_sectors_detail(sectors_detail: list[dict]) -> dict[str, str]:
    """从热门选股明细里汇总 code→name（同代码保留首次）。"""
    out: dict[str, str] = {}
    for bundle in sectors_detail:
        for st in bundle.get("stocks") or []:
            if not isinstance(st, dict):
                continue
            raw = st.get("code") if st.get("code") is not None else st.get("代码")
            if raw is None:
                continue
            try:
                code = normalize_symbol(str(raw))
            except ValueError:
                continue
            nm_raw = st.get("name") if st.get("name") is not None else st.get("名称")
            nm = str(nm_raw).strip() if nm_raw is not None else ""
            if nm.lower() == "nan":
                nm = ""
            if code not in out and nm:
                out[code] = nm
    return out


def _run_hot_pick_common(
    *,
    top_sectors: int,
    stocks_per_sector: int,
    board_type: str,
    exclude_st: bool,
    exclude_kcb: bool,
    selector_data_source: str,
    use_sector_snapshot: bool,
    tushare_token: str | None = None,
    sort_by_trend_strength: bool = True,
    require_technical_pass: bool = False,
    exclude_overextended: bool = False,
    max_return_20d_pct: float = 25.0,
    enable_liquidity_filter: bool = False,
    min_avg_turnover_20d_100m: float = 1.0,
):
    ds = _resolve_sector_datasource(selector_data_source, tushare_token=tushare_token)
    board_key = (board_type or "all").strip().lower() or "all"
    snapshot_path = default_sector_snapshot_path(
        get_settings().data_dir, selector_data_source, board_key
    )
    rankings_override = None
    if use_sector_snapshot and snapshot_path.exists():
        try:
            rankings_override = load_sector_rankings_snapshot(snapshot_path)
        except Exception as exc:
            logger.warning("sector snapshot load failed, fallback to live fetch: %s", exc)
    if rankings_override is None:
        rankings_override = ds.get_sector_rankings(board_key)
        try:
            save_sector_rankings_snapshot(rankings_override, snapshot_path)
        except Exception as exc:
            logger.warning("sector snapshot save failed: %s", exc)
    return pick_from_hot_sectors(
        ds,
        top_sectors=top_sectors,
        stocks_per_sector=stocks_per_sector,
        board_type=board_key,
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        rankings_override=rankings_override,
        sort_by_trend_strength=sort_by_trend_strength,
        require_technical_pass=require_technical_pass,
        exclude_overextended=exclude_overextended,
        max_return_20d_pct=max_return_20d_pct,
        enable_liquidity_filter=enable_liquidity_filter,
        min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
    )


def _hot_sectors_preview_payload(
    *,
    top_sectors: int,
    stocks_per_sector: int,
    board_type: str,
    exclude_st: bool,
    exclude_kcb: bool,
    selector_data_source: str,
    use_sector_snapshot: bool,
    tushare_token: str | None,
    sort_by_trend_strength: bool,
    require_technical_pass: bool,
    exclude_overextended: bool,
    max_return_20d_pct: float,
    enable_liquidity_filter: bool,
    min_avg_turnover_20d_100m: float,
) -> FillHotSectorsOut:
    hot = _run_hot_pick_common(
        top_sectors=top_sectors,
        stocks_per_sector=stocks_per_sector,
        board_type=(board_type or "all").strip().lower(),
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        selector_data_source=selector_data_source,
        use_sector_snapshot=use_sector_snapshot,
        tushare_token=tushare_token,
        sort_by_trend_strength=sort_by_trend_strength,
        require_technical_pass=require_technical_pass,
        exclude_overextended=exclude_overextended,
        max_return_20d_pct=max_return_20d_pct,
        enable_liquidity_filter=enable_liquidity_filter,
        min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
    )
    return FillHotSectorsOut(
        sectors_detail=hot.sectors_detail,
        summary=FillHotSectorsSummary(
            added=0,
            skipped_existing_manual=0,
            removed_auto=0,
            warnings=list(hot.warnings),
        ),
    )


def _disclaimer_payload() -> DisclaimerOut:
    """组装免责与数据源说明（部分接口与根路径 JSON 复用）。"""
    s = get_settings()
    return DisclaimerOut(
        disclaimer=s.disclaimer_short,
        data_source_note=s.data_source_note,
        data_delay_note="展示数据基于公开来源拉取的日线（前复权），非实时行情，不能用于对时效要求极高的交易决策。",
    )


@app.get(
    "/health",
    tags=["① 入门必读"],
    summary="检查服务是否在运行",
    description="**不用填任何参数。** 返回 `ok` 表示程序已启动。部署、运维常用；与股票数据无关。",
)
def health():
    """负载均衡/探活用，无鉴权。"""
    return {"status": "ok"}


@app.get(
    "/meta/auth-status",
    tags=["① 入门必读"],
    summary="是否需要 API Key（给控制台探测用）",
    description="返回 `api_key_required`，**不**暴露密钥本身。图形控制台 `/ui` 会调用本接口。",
)
def meta_auth_status():
    """公开：告知客户端服务端是否配置了 API_KEY（布尔值，不泄露密钥）。"""
    return {"api_key_required": bool(get_settings().api_key)}


@app.get(
    "/meta/data-sources",
    tags=["① 入门必读"],
    summary="行情拉取路线列表（控制台下拉用）",
    description="返回服务端默认 `server_default` 与各选项的 `value`/`label`；不需 API Key。",
)
def meta_data_sources():
    return {
        "server_default": get_settings().ingest_data_source,
        "options": [
            {"value": "auto", "label": "自动（新浪 → 腾讯 → Baostock）"},
            {
                "value": "eastmoney",
                "label": "仅东方财富日线（AkShare，请求间隔约 3–5 秒防限流）",
            },
            {
                "value": "akshare",
                "label": "AkShare 东财日线（与 eastmoney 等价，与选股脚本 --data-source akshare 对齐）",
            },
            {"value": "sina", "label": "仅新浪财经（AkShare）"},
            {"value": "tencent", "label": "仅腾讯（AkShare，无成交量则记 0）"},
            {"value": "baostock", "label": "仅 Baostock（开源证券数据）"},
            {"value": "mootdx", "label": "mootdx 通达信协议日线（需 pip install mootdx）"},
            {
                "value": "tushare",
                "label": "TuShare 日线（需 tushare 包与 TUSHARE_TOKEN / 配置 tushare_token）",
            },
        ],
    }


@app.get(
    "/meta/disclaimer",
    response_model=DisclaimerOut,
    tags=["⑥ 说明与免责"],
    summary="查看免责说明与数据来源",
    description="返回完整的免责声明、数据来源说明、延时说明。**不需要先加自选。**",
)
@limiter.limit(get_settings().rate_limit_default)
def meta_disclaimer(request: Request):
    """返回免责与数据源说明全文。"""
    return _disclaimer_payload()


@app.get(
    "/meta/self-use",
    response_model=SelfUseMetaOut,
    tags=["① 入门必读"],
    summary="自用工具定位与风控检查摘要",
    description="""
返回本工具**默认定位**（辅助决策、不支持自动下单）、风控检查项列表，以及仓库内说明文件路径提示。

**无需** API Key；不含任何真实资金数据。详细说明见仓库 `docs/SELF_USE_GUIDE.md`。
""",
)
def meta_self_use():
    """与「小白自用量化决策」计划对齐的静态摘要。"""
    return SelfUseMetaOut(
        tool_mode="assist_only",
        automatic_trading_supported=False,
        risk_checklist=[
            "明确单笔最大亏损占账户比例上限",
            "明确单标的与总仓位上限",
            "写明停机条件（连续亏损笔数、单日回撤、信号背离周数等）",
            "固定复盘节奏（如以一周为窗口则每周固定时间更新日线并记录 3 条以内依据）",
            "若实盘：在决策日志中记录计划仓位 % 与是否按计划执行，便于复盘",
        ],
        related_doc_files=["docs/SELF_USE_GUIDE.md"],
        journal_api="/journal",
        example_risk_policy_file="examples/risk_policy.example.json",
    )


@app.get(
    "/meta/hot-market-snapshot",
    response_model=HotMarketSnapshotFileOut,
    tags=["⑥ 说明与免责"],
    summary="读取已保存的热门板块+热门股快照",
    description="""
从本地 `data/hot_market_snapshot.json` 读取上次 **POST /meta/hot-market-snapshot/refresh** 落盘内容。

未刷新过时 `snapshot` 为 null。数据为公开源截面，**非投资建议**。
""",
)
def meta_hot_market_snapshot_get():
    p = default_hot_market_snapshot_path()
    raw = load_hot_market_snapshot()
    if raw is None:
        return HotMarketSnapshotFileOut(path=str(p), snapshot=None)
    return HotMarketSnapshotFileOut(path=str(p), snapshot=HotMarketSnapshotOut(**asdict(raw)))


@app.post(
    "/meta/hot-market-snapshot/refresh",
    response_model=HotMarketSnapshotRefreshOut,
    tags=["③ 更新行情数据"],
    summary="拉取热门板块+热门股并保存快照",
    description="""
按固定顺序 **新浪 → 腾讯 → Baostock → 东财 → akshare** 尝试，直到成功（与 `app/hot_market_snapshot.py` 内说明一致）。

- **新浪成功时**：板块与个股均来自新浪公开接口（个股为沪深 A 按涨跌幅降序取前 N）。
- **腾讯步**：个股为腾讯财经 A 股排行（客户端按 `zdf` 排序）；因腾讯无对等板块全表，板块由东财补充（响应内 `sector_source` / `notes` 会说明）。
- **东财 / akshare 步**：板块为东财；个股为人气榜（与「涨幅序」不同，见 `notes`）。

**公开数据，非实时撮合；不构成投资建议。**
""",
)
@limiter.limit(get_settings().rate_limit_default)
def meta_hot_market_snapshot_refresh(
    request: Request,
    body: HotMarketSnapshotRefreshIn | None = None,
    _: None = Depends(optional_api_key),
):
    """联网拉取并覆盖写入 `data/hot_market_snapshot.json`。"""
    b = body or HotMarketSnapshotRefreshIn()
    try:
        snap = fetch_hot_market_snapshot(top_stocks=b.top_stocks, chain=b.chain)
        path = save_hot_market_snapshot(snap)
    except DataSourceError as e:
        raise _http_exception_from_datasource(e) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return HotMarketSnapshotRefreshOut(
        saved_to=str(path),
        snapshot=HotMarketSnapshotOut(**asdict(snap)),
    )


def _watchlist_item_with_bars(s, r: WatchlistRow) -> WatchlistItem:
    meta = watchlist_bar_fields_for_session(s, [r.symbol]).get(r.symbol, {})
    return WatchlistItem(
        symbol=r.symbol,
        name=(r.name or "").strip(),
        origin=r.origin or WATCHLIST_ORIGIN_MANUAL,
        **meta,
    )


@app.get(
    "/watchlist",
    response_model=list[WatchlistItem],
    tags=["② 管理自选股票"],
    summary="列出当前已添加的股票",
    description="""
返回自选池里**所有**股票代码列表；每项附带本地 **bars** 摘要：**bars_last_ingested_at**（最近入库 UTC 时间）、**last_close**（最新日线收盘价）、**last_daily_close_label**（最后交易日收盘说明）。

- 若配置了 `API_KEY`，请先点右上角 **Authorize**。
- 若列表为空，下一步请用 `POST /watchlist` 添加。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_list(request: Request, _: None = Depends(optional_api_key)):
    """列出自选池全部标的（按 id 升序）。"""
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all()
        missing = [r.symbol for r in rows if not (r.name or "").strip()]
        if missing:
            by_sym = {r.symbol: r for r in rows}
            for sym, nm in fetch_stock_names_map(missing).items():
                r = by_sym.get(sym)
                if r is not None and not (r.name or "").strip():
                    r.name = nm
        return [_watchlist_item_with_bars(s, r) for r in rows]


@app.post(
    "/watchlist",
    response_model=WatchlistItem,
    tags=["② 管理自选股票"],
    summary="添加一只自选股票",
    description="""
**最常用的第一步。**

1. 点 **Try it out**
2. 在 Request body 里把 `symbol` 改成你要的 6 位代码（示例已填 `600519`）
3. 点 **Execute**

已存在相同代码时不会报错，会原样返回该代码。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_add(body: WatchlistIn, request: Request, _: None = Depends(optional_api_key)):
    """添加自选；代码规范化后若已存在则幂等返回该标的。"""
    try:
        sym = normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with session_scope() as s:
        existing = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
        if existing:
            if existing.origin != WATCHLIST_ORIGIN_MANUAL:
                existing.origin = WATCHLIST_ORIGIN_MANUAL
            if not (existing.name or "").strip():
                existing.name = fetch_stock_name(sym) or ""
            return _watchlist_item_with_bars(s, existing)
        nm = fetch_stock_name(sym) or ""
        s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_MANUAL, name=nm))
        s.flush()
        row = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one()
        return _watchlist_item_with_bars(s, row)


@app.delete(
    "/watchlist/{symbol}",
    tags=["② 管理自选股票"],
    summary="从自选里删掉一只股票",
    description="""
在 **symbol** 路径里填 6 位代码（如 `600519`），执行后即可从自选池移除。

与「添加」一样，代码里的字母、空格会被自动去掉，只保留数字。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_delete(symbol: str, request: Request, _: None = Depends(optional_api_key)):
    """按路径中的代码删除自选（规范化后匹配）。"""
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with session_scope() as s:
        s.execute(delete(WatchlistRow).where(WatchlistRow.symbol == sym))
    return {"ok": True, "symbol": sym}


@app.post(
    "/watchlist/batch-delete",
    response_model=WatchlistBatchDeleteOut,
    tags=["② 管理自选股票"],
    summary="批量从自选删除多只股票",
    description="""
请求体传入 **symbols** 字符串列表；每项会经 `normalize_symbol` 规范为 6 位，**去重**后一次性从库中删除。

无效代码（无法规范为 6 位 A 股）会被跳过；若去重后列表为空则返回 400。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_batch_delete(
    request: Request,
    body: WatchlistBatchDeleteIn = Body(...),
    _: None = Depends(optional_api_key),
):
    seen: set[str] = set()
    syms: list[str] = []
    for raw in body.symbols:
        try:
            sym = normalize_symbol(str(raw))
        except ValueError:
            continue
        if sym not in seen:
            seen.add(sym)
            syms.append(sym)
    if not syms:
        raise HTTPException(status_code=400, detail="没有有效的 A 股代码可删除")
    with session_scope() as s:
        res = s.execute(delete(WatchlistRow).where(WatchlistRow.symbol.in_(syms)))
        try:
            removed = int(res.rowcount or 0)
        except (TypeError, ValueError):
            removed = 0
    return WatchlistBatchDeleteOut(removed=removed, requested_unique=len(syms))


@app.post(
    "/watchlist/import-hot-market-snapshot",
    response_model=WatchlistHotSnapshotImportOut,
    tags=["② 管理自选股票"],
    summary="从热门市场快照 JSON 导入热门股到自选",
    description="""
读取服务端 **`data/hot_market_snapshot.json`**（与⑨「热门市场快照」、**`GET /meta/hot-market-snapshot`** 同源），将其 **stocks** 列表写入自选，**origin=auto_hot**。

- **`replace_auto_pool=true`（默认）**：先删除当前全部 **auto_hot** 与 **auto_quant**，再按快照顺序写入；**手动**（manual）不删，若代码已存在且为手动则**跳过**。
- **`replace_auto_pool=false`**：不删池子，仅对尚未出现在自选的代码追加为 auto_hot；已存在任意来源的代码则跳过（不覆盖名称）。

快照文件不存在或 **stocks** 为空时返回 404 / 400。无需先打开⑨页面，只要服务端已有落盘文件即可。
""",
)
@limiter.limit("6/minute")
def watchlist_import_hot_market_snapshot(
    request: Request,
    body: WatchlistHotSnapshotImportIn = Body(default_factory=WatchlistHotSnapshotImportIn),
    _: None = Depends(optional_api_key),
):
    b = body
    snap = load_hot_market_snapshot()
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail="未找到本地 hot_market_snapshot.json，请先在⑨点击「刷新热门快照（联网）」或执行 POST /meta/hot-market-snapshot/refresh。",
        )
    stocks = list(snap.stocks or [])
    if not stocks:
        raise HTTPException(status_code=400, detail="快照中热门股列表 stocks 为空")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for st in stocks:
        if not isinstance(st, dict):
            continue
        raw_code = st.get("code")
        if raw_code is None:
            continue
        try:
            sym = normalize_symbol(str(raw_code))
        except ValueError:
            warnings.append(f"跳过无效代码：{raw_code!r}")
            continue
        if sym in seen:
            continue
        seen.add(sym)
        nm_raw = st.get("name")
        nm = str(nm_raw).strip() if nm_raw is not None else ""
        if nm.lower() == "nan":
            nm = ""
        pairs.append((sym, nm))
    if not pairs:
        raise HTTPException(status_code=400, detail="快照中无任何有效 A 股代码可导入")
    skipped_existing_manual = 0
    added = 0
    removed_auto = 0
    with session_scope() as s:
        if b.replace_auto_pool:
            res = s.execute(
                delete(WatchlistRow).where(
                    or_(
                        WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_HOT,
                        WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_QUANT,
                    )
                )
            )
            try:
                removed_auto = int(res.rowcount or 0)
            except (TypeError, ValueError):
                removed_auto = 0
        for sym, nm in pairs:
            row = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
            if row is not None:
                if row.origin == WATCHLIST_ORIGIN_MANUAL:
                    skipped_existing_manual += 1
                continue
            use_nm = nm
            if not use_nm:
                use_nm = fetch_stock_name(sym) or ""
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_AUTO_HOT, name=use_nm))
            added += 1
    return WatchlistHotSnapshotImportOut(
        added=added,
        skipped_existing_manual=skipped_existing_manual,
        removed_auto=removed_auto,
        snapshot_stock_rows=len(stocks),
        candidates=len(pairs),
        warnings=warnings,
    )


@app.post(
    "/watchlist/sync-from-quant-screen",
    response_model=QuantWatchlistSyncOut,
    tags=["② 管理自选股票"],
    summary="将⑨量化选股结果写入自选（origin=auto_quant）",
    description="""
将 **`POST /research/sector-screen`** 返回的 **stocks** 列表写入自选：**origin=auto_quant**。

- **会先删除** 当前所有 `auto_hot` 与 `auto_quant` 记录，再写入本次结果；**手动**（`manual`）**不会**被删。
- 若某代码已在自选且为手动，本次列表**跳过**（不覆盖）。
- 控制台在「使用量化选股结果」模式下，⑨ 运行成功后会自动调用本接口；也可在②手动点「同步缓存的量化结果」。
""",
)
@limiter.limit("6/minute")
def watchlist_sync_from_quant_screen(
    request: Request,
    body: QuantWatchlistSyncIn = Body(...),
    _: None = Depends(optional_api_key),
):
    warnings: list[str] = []
    skipped_existing_manual = 0
    added = 0
    removed_auto = 0
    seen_codes: set[str] = set()
    with session_scope() as s:
        res = s.execute(
            delete(WatchlistRow).where(
                or_(
                    WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_HOT,
                    WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_QUANT,
                )
            )
        )
        try:
            removed_auto = int(res.rowcount or 0)
        except (TypeError, ValueError):
            removed_auto = 0
        for row in body.stocks:
            try:
                sym = normalize_symbol(row.code)
            except ValueError:
                warnings.append(f"跳过无效代码：{row.code!r}")
                continue
            if sym in seen_codes:
                continue
            seen_codes.add(sym)
            existing = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
            if existing is not None:
                if existing.origin == WATCHLIST_ORIGIN_MANUAL:
                    skipped_existing_manual += 1
                continue
            nm = (row.name or "").strip()
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_AUTO_QUANT, name=nm))
            added += 1
    return QuantWatchlistSyncOut(
        added=added,
        skipped_existing_manual=skipped_existing_manual,
        removed_auto=removed_auto,
        warnings=warnings,
    )


@app.post(
    "/watchlist/fill-hot-sectors",
    response_model=FillHotSectorsOut,
    tags=["② 管理自选股票"],
    summary="按热门板块自动填充自选（保留手动）",
    description="""
按 **get_sector_rankings** 的热度顺序取前 N 个板块；可选对板块内成分股拉日线并按**趋势强度**重排，再叠加“过热涨幅”“流动性”“技术面通过”过滤后，写入自选 **origin=auto_hot**。

- **会先删除** 当前所有 `auto_hot` 与 `auto_quant` 记录，再写入本次结果；**手动添加**（`manual`）**不会**被删。
- 若某代码已在自选且为手动，本次自动列表**跳过**（不覆盖、不重复）。
- **selector_data_source**：**akshare**（东财板块较全）、**mootdx**（通达信板块较少）、**tushare**（同花顺 ths_index / ths_daily / ths_member，通常需 TuShare **6000 积分**）。选 **tushare** 时请在 Body 传 **tushare_token**（或服务端配置 `TUSHARE_TOKEN` / `tushare_token`）。
- **use_sector_snapshot**：为 true 时优先读取本地板块热度快照；为 false 时强制重新请求最新板块排名，并刷新快照文件。

响应含 **sectors_detail**（板块指标 + 每只股票成分表全列字典），便于核对。
""",
)
@limiter.limit("6/minute")
def watchlist_fill_hot_sectors(
    request: Request,
    body: FillHotSectorsIn = Body(...),
    _: None = Depends(optional_api_key),
):
    bt = (body.board_type or "all").strip().lower()
    try:
        hot = _run_hot_pick_common(
            top_sectors=body.top_sectors,
            stocks_per_sector=body.stocks_per_sector,
            board_type=bt,
            exclude_st=body.exclude_st,
            exclude_kcb=body.exclude_kcb,
            selector_data_source=body.selector_data_source.value,
            use_sector_snapshot=body.use_sector_snapshot,
            tushare_token=body.tushare_token,
            sort_by_trend_strength=body.sort_by_trend_strength,
            require_technical_pass=body.require_technical_pass,
            exclude_overextended=body.exclude_overextended,
            max_return_20d_pct=body.max_return_20d_pct,
            enable_liquidity_filter=body.enable_liquidity_filter,
            min_avg_turnover_20d_100m=body.min_avg_turnover_20d_100m,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hot_pick failed: %s", e)
        raise HTTPException(status_code=502, detail=f"热门板块选股失败：{e}") from e

    warnings = list(hot.warnings)
    skipped_existing_manual = 0
    added = 0
    removed_auto = 0
    hot_names = _names_from_hot_sectors_detail(hot.sectors_detail)
    with session_scope() as s:
        res = s.execute(
            delete(WatchlistRow).where(
                or_(
                    WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_HOT,
                    WatchlistRow.origin == WATCHLIST_ORIGIN_AUTO_QUANT,
                )
            )
        )
        try:
            removed_auto = int(res.rowcount or 0)
        except (TypeError, ValueError):
            removed_auto = 0
        for sym in hot.symbols_for_watchlist:
            row = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
            if row is not None:
                if row.origin == WATCHLIST_ORIGIN_MANUAL:
                    skipped_existing_manual += 1
                continue
            nm = (hot_names.get(sym) or "").strip()
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_AUTO_HOT, name=nm))
            added += 1

    summary = FillHotSectorsSummary(
        added=added,
        skipped_existing_manual=skipped_existing_manual,
        removed_auto=removed_auto,
        warnings=warnings,
    )
    return FillHotSectorsOut(sectors_detail=hot.sectors_detail, summary=summary)


@app.get(
    "/watchlist/hot-sectors/preview",
    response_model=FillHotSectorsOut,
    tags=["② 管理自选股票"],
    summary="预览热门板块选股（不写库，Query）",
    description="参数与 POST `/watchlist/fill-hot-sectors` 一致（Query）。**TuShare** 时建议改用 **POST** `/watchlist/hot-sectors/preview` 在 Body 传 `tushare_token`，避免 token 出现在 URL。",
)
@limiter.limit("12/minute")
def watchlist_hot_sectors_preview(
    request: Request,
    top_sectors: int = Query(5, ge=1, le=200),
    stocks_per_sector: int = Query(5, ge=1, le=50),
    board_type: str = Query("all"),
    exclude_st: bool = Query(True),
    exclude_kcb: bool = Query(True),
    selector_data_source: SelectorSectorDataSource = Query(..., description="akshare、mootdx 或 tushare"),
    use_sector_snapshot: bool = Query(True, description="true=优先使用本地板块快照；false=强制请求最新板块数据"),
    tushare_token: str | None = Query(None, description="TuShare 时可选；优先于服务端环境变量"),
    sort_by_trend_strength: bool = Query(True),
    require_technical_pass: bool = Query(False),
    exclude_overextended: bool = Query(False),
    max_return_20d_pct: float = Query(25.0, ge=0, le=500),
    enable_liquidity_filter: bool = Query(False),
    min_avg_turnover_20d_100m: float = Query(1.0, ge=0, le=10000),
    _: None = Depends(optional_api_key),
):
    try:
        return _hot_sectors_preview_payload(
            top_sectors=top_sectors,
            stocks_per_sector=stocks_per_sector,
            board_type=board_type,
            exclude_st=exclude_st,
            exclude_kcb=exclude_kcb,
            selector_data_source=selector_data_source.value,
            use_sector_snapshot=use_sector_snapshot,
            tushare_token=tushare_token,
            sort_by_trend_strength=sort_by_trend_strength,
            require_technical_pass=require_technical_pass,
            exclude_overextended=exclude_overextended,
            max_return_20d_pct=max_return_20d_pct,
            enable_liquidity_filter=enable_liquidity_filter,
            min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hot_pick preview failed: %s", e)
        raise HTTPException(status_code=502, detail=f"热门板块预览失败：{e}") from e


@app.post(
    "/watchlist/hot-sectors/preview",
    response_model=FillHotSectorsOut,
    tags=["② 管理自选股票"],
    summary="预览热门板块选股（不写库，Body）",
    description="请求体与 `POST /watchlist/fill-hot-sectors` 相同字段；**不写库**。控制台与 TuShare 推荐走本接口以便安全传递 `tushare_token`。",
)
@limiter.limit("12/minute")
def watchlist_hot_sectors_preview_post(
    request: Request,
    body: FillHotSectorsIn = Body(...),
    _: None = Depends(optional_api_key),
):
    try:
        return _hot_sectors_preview_payload(
            top_sectors=body.top_sectors,
            stocks_per_sector=body.stocks_per_sector,
            board_type=body.board_type,
            exclude_st=body.exclude_st,
            exclude_kcb=body.exclude_kcb,
            selector_data_source=body.selector_data_source.value,
            use_sector_snapshot=body.use_sector_snapshot,
            tushare_token=body.tushare_token,
            sort_by_trend_strength=body.sort_by_trend_strength,
            require_technical_pass=body.require_technical_pass,
            exclude_overextended=body.exclude_overextended,
            max_return_20d_pct=body.max_return_20d_pct,
            enable_liquidity_filter=body.enable_liquidity_filter,
            min_avg_turnover_20d_100m=body.min_avg_turnover_20d_100m,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("hot_pick preview post failed: %s", e)
        raise HTTPException(status_code=502, detail=f"热门板块预览失败：{e}") from e


def _watchlist_subset_symbols(
    subset: list[str] | None,
    wl_pairs: list[tuple[str, str]],
) -> tuple[list[str], dict[str, str], list[dict]]:
    """从 (symbol, name) 列表解析待处理代码顺序；须在 session 内先展开 ORM 行再传入，避免 DetachedInstanceError。"""
    wl_name_by_sym = {sym: nm for sym, nm in wl_pairs}
    ordered_all = [sym for sym, _ in wl_pairs]
    wl_set = set(ordered_all)
    suffix_errs: list[dict] = []

    if not subset:
        return list(ordered_all), wl_name_by_sym, suffix_errs

    ordered: list[str] = []
    seen: set[str] = set()
    for raw in subset:
        try:
            s = normalize_symbol(str(raw))
        except ValueError:
            token = str(raw).strip()[:20] if raw is not None else ""
            suffix_errs.append(
                {
                    "symbol": token or "?",
                    "watchlist_name": None,
                    "error": "代码格式无效（需 6 位 A 股数字）",
                }
            )
            continue
        if s in seen:
            continue
        seen.add(s)
        if s not in wl_set:
            suffix_errs.append(
                {
                    "symbol": s,
                    "watchlist_name": None,
                    "error": "不在当前自选池",
                }
            )
            continue
        ordered.append(s)
    return ordered, wl_name_by_sym, suffix_errs


@app.post(
    "/ingest/update",
    tags=["③ 更新行情数据"],
    summary="下载/更新自选股票的日线行情",
    description="""
**添加自选后必做的一步**（否则后面算信号会提示数据不够）。

- **Body 可留空**（增量更新到今天）。
- 传 **`start_date` + `end_date`**：按该闭区间拉取；仅 **`start_date`**：从该日拉到今日；仅 **`end_date`**：增量更新到该日。
- **`symbols`**（可选）：只拉取列表中的代码（**须在自选池**）；不传或空表示**自选全部**。不在池内的代码会在 `results` 里单独返回 `error`，不阻塞其余标的。
- **`data_source`**：行情路线（`auto` / `eastmoney` / `akshare` / `sina` / `tencent` / `baostock` / `mootdx` / `tushare`）；不传则用 **`INGEST_DATA_SOURCE`**（默认 `auto`）。`eastmoney` 与 `akshare` 均为东财日线（后者与选股脚本命名对齐），东财路线有 **3–5 秒随机间隔**；`mootdx` / `tushare` 经 `quant_stock_selector` 核心拉取（需依赖与 TuShare token）。
- **`GET /ingest/test-connection`**：探测本机能否访问数据源（短区间测试，需 API Key 时同上）；可带 Query **`data_source`**。
- 需要能访问外网（通过 AkShare 拉公开数据）。
- 自选为空时会返回错误，请先用 `POST /watchlist` 添加股票。
- 某一只股票拉取失败时，结果里该条会带 `error`，其它股票仍会继续。
- 成功条目中附带 **`watchlist_name`**（自选里存的简称，可能为空）、**`last_trade_date` / `last_close`**（拉取后本地库中**最新一根**日线交易日与收盘价，前复权）、**`strength`**（基于拉取后本地 K 线的简要强弱摘要：约 5/20 日涨跌与相对 MA20，仅供自览）。

**注意**：接口有「每分钟次数」限制，不要连续狂点。
""",
)
@limiter.limit("20/minute")
def ingest_update(
    request: Request,
    body: IngestUpdateIn = Body(default_factory=IngestUpdateIn),
    _: None = Depends(optional_api_key),
):
    """
    对自选池每个标的执行 ingest_symbol_range（按 Body 日期规则拉取日线并 upsert）。

    自选为空返回 400；单个标的失败时该条结果带 error 字段，不整批失败。
    """
    with session_scope() as s:
        orm_rows = list(s.execute(select(WatchlistRow)).scalars().all())
        wl_pairs = [(r.symbol, (r.name or "").strip()) for r in orm_rows]
    if not wl_pairs:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
    symbols, wl_name_by_sym, suffix_errs = _watchlist_subset_symbols(body.symbols, wl_pairs)
    st, en = body.start_date, body.end_date
    if en and en > date.today():
        raise HTTPException(status_code=400, detail="结束日期不能晚于今天")
    if st and st > date.today():
        raise HTTPException(status_code=400, detail="开始日期不能晚于今天")
    ds = body.data_source.value if body.data_source is not None else None
    resolved_ds = ds if ds is not None else get_settings().ingest_data_source
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    results = []
    for i, sym in enumerate(symbols):
        if i > 0 and pause > 0:
            time.sleep(pause)
        nm = wl_name_by_sym.get(sym, "").strip() or None
        try:
            rec = ingest_symbol_range(sym, range_start=st, range_end=en, data_source=ds)
            rec["watchlist_name"] = nm
            snap = strength_snapshot_for_symbol(sym)
            if snap is not None:
                rec["strength"] = snap
            results.append(rec)
        except ValueError as e:
            results.append({"symbol": sym, "watchlist_name": nm, "error": str(e)})
        except Exception as e:
            results.append({"symbol": sym, "watchlist_name": nm, "error": str(e)})
    results.extend(suffix_errs)
    return {
        "results": results,
        "ingest_data_source": resolved_ds,
        "disclaimer": _disclaimer_payload().model_dump(),
    }


@app.post(
    "/ingest/fundamentals",
    tags=["③ 更新行情数据"],
    summary="拉取扩展因子并写入本地（Demo）",
    description="""
对自选池拉取并入库（默认**每一只**；也可传 **`symbols`** 只处理子集，规则与 `POST /ingest/update` 相同）。

- **估值**：东财沪深京 A 股列表中的市盈率(动)、市净率（全表有短 TTL 内存缓存，减轻限流）。
- **成长**：最近一期财报的营业收入/归属净利润**同比 %**（`stock_financial_analysis_indicator_em`）。
- **资金流**：最近交易日**主力净流入净额**（东财日级）。

完成后 `GET /signals` 会读取本地快照，在技术面得分上做 **有界** 合成（通常 ±15 分）。**非投资建议**；接口有频率限制，勿连续狂点。
""",
)
@limiter.limit("12/minute")
def ingest_fundamentals(
    request: Request,
    body: IngestFundamentalsIn = Body(default_factory=IngestFundamentalsIn),
    _: None = Depends(optional_api_key),
):
    """自选批量扩展因子 upsert；单条失败体现在该条 `error` 字段。"""
    with session_scope() as s:
        orm_rows = list(s.execute(select(WatchlistRow)).scalars().all())
        wl_pairs = [(r.symbol, (r.name or "").strip()) for r in orm_rows]
    if not wl_pairs:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
    symbols, _, suffix_errs = _watchlist_subset_symbols(body.symbols, wl_pairs)
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    results: list[dict] = []
    for i, sym in enumerate(symbols):
        if i > 0 and pause > 0:
            time.sleep(pause)
        results.append(upsert_fundamental_snapshot(sym))
    results.extend(suffix_errs)
    return {
        "results": results,
        "disclaimer": _disclaimer_payload().model_dump(),
        "note": "扩展因子为 Demo 合成规则；数据源为东财/AkShare 聚合接口，可能存在延时或缺项。",
    }


@app.get(
    "/ingest/test-connection",
    tags=["③ 更新行情数据"],
    summary="测试与行情数据源的连接",
    description="""
用极短日期区间请求一只探测股票（默认 000001，可用环境变量 `AKSHARE_TEST_SYMBOL` 修改），**不依赖自选列表**。

返回 `ok`、`user_message`、`latency_ms`、`data_source`、`provider` 等；若 `ok` 为 false，`user_message` 为中文原因说明。

**Query `data_source`**：与 `POST /ingest/update` 相同枚举；不传则用 `INGEST_DATA_SOURCE`。
""",
)
@limiter.limit("30/minute")
def ingest_test_connection(
    request: Request,
    data_source: IngestDataSource | None = Query(
        None,
        description="探测使用的路线；不传则使用服务端 INGEST_DATA_SOURCE",
    ),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    return test_akshare_connectivity(data_source=ds)


@app.post(
    "/ingest/web-data-preview",
    tags=["③ 更新行情数据"],
    summary="单标的：K 线 + 日级资金流（网页数据预览）",
    description="""
针对**单只**标的（**不必**在自选池中）：

- **K 线**：默认先 `incremental_refresh` 联网写入本地 `bars`，再返回最近 `bar_limit` 根日线（与 `GET /quotes/{symbol}/bars` 同结构）。
- **资金流**：东财个股日级表（AkShare `stock_individual_fund_flow`），返回最近 `fund_flow_recent_days` 行；**非** tick 级「实时主力」，盘中请以交易所与券商行情为准。

用于 `/ui/web-crawler` 配置页预览；有频率限制，请勿连续狂点。**非投资建议**。
""",
)
@limiter.limit("20/minute")
def ingest_web_data_preview(
    request: Request,
    body: WebDataPreviewIn,
    _: None = Depends(optional_api_key),
):
    try:
        sym = normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    route = _resolve_ingest_route(body.data_source)
    refresh: dict[str, object] = {"attempted": body.refresh_kline, "ok": None, "detail": None, "result": None}
    if body.refresh_kline:
        try:
            refresh["result"] = incremental_refresh(sym, data_source=route)
            refresh["ok"] = True
        except Exception as e:
            refresh["ok"] = False
            refresh["detail"] = f"{type(e).__name__}: {e}"
            logger.warning("web_data_preview refresh %s: %s", sym, e)
    try:
        bars = list_bars_from_db(sym, limit=body.bar_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    fund_rows = fetch_individual_fund_flow_recent_rows(sym, limit_rows=body.fund_flow_recent_days)
    fund_latest = fund_rows[-1] if fund_rows else None
    return {
        "symbol": sym,
        "data_source": route,
        "kline_refresh": refresh,
        "bars": bars,
        "fund_flow_recent": fund_rows,
        "fund_flow_latest": fund_latest,
        "disclaimer": _disclaimer_payload().model_dump(),
        "note": (
            "实现上通过 AkShare 聚合公开页面接口，非浏览器自动化爬虫；"
            "资金流为日级汇总，「最新一行」对应数据源最近交易日。"
        ),
    }


@app.get(
    "/quotes/{symbol}/bars",
    response_model=list[DailyBarOut],
    tags=["④ 查看信号"],
    summary="本地日线行情（OHLCV）",
    description="""
读取 **已写入 SQLite** 的日线（前复权），按交易日**从旧到新**排列。

- **limit**：最近多少根 K 线（1～500，默认 30）。
- **change_pct**：相对**上一交易日收盘**的涨跌幅（%）；返回区间内第一根为 `null`。
- 若库里尚无该代码数据，返回空列表 `[]`（请先 `POST /ingest/update`）。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def quotes_daily_bars(
    symbol: str,
    request: Request,
    limit: int = Query(30, ge=1, le=500, description="最近几根日线"),
    _: None = Depends(optional_api_key),
):
    """规范化代码后读库；无行则返回空列表。"""
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        rows = list_bars_from_db(sym, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rows


@app.get(
    "/signals",
    response_model=list[SignalOut],
    tags=["④ 查看信号"],
    summary="一次看完自选里所有股票的信号",
    description="""
对自选池里**每一只**股票计算信号（趋势、强度、评分、风险提示等）。

- 自选为空时返回空列表 `[]`。
- 若某只股票从未成功拉取过 K 线，该只会被跳过（默认不在服务日志打 WARNING，可用 DEBUG 查看）；响应头 **`X-Quant-Signals-Success-Count`** / **`X-Quant-Signals-Failed-Count`** / **`X-Quant-Signals-Failed-Symbols`** 汇总跳过代码。
- **建议先执行** `POST /ingest/update`。
- **pre_refresh**：为 `true` 时**按只**增量拉取再算信号；某只拉取失败**仅跳过该只**，不影响其它标的。与③所选路线一致时需传相同 `data_source`。响应头 `X-Quant-Data-Source` / `X-Quant-Pre-Refresh` 及跳过汇总头见上文。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def signals_batch(
    request: Request,
    response: Response,
    pre_refresh: bool = Query(
        False,
        description="为 true 时先按 data_source 增量更新各标的日线再计算信号",
    ),
    data_source: IngestDataSource | None = Query(
        None,
        description="行情路线；不传则用 INGEST_DATA_SOURCE。与 pre_refresh 配合使用",
    ),
    _: None = Depends(optional_api_key),
):
    """对自选池逐个 compute_signal；失败标的打日志并跳过，不中断其它标的。"""
    route = _resolve_ingest_route(data_source)
    response.headers["X-Quant-Data-Source"] = route
    response.headers["X-Quant-Pre-Refresh"] = "1" if pre_refresh else "0"
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow)).scalars().all()
        symbols = [r.symbol for r in rows]
    if not symbols:
        return []
    _pre_refresh_symbols(symbols, route=route, pre_refresh=pre_refresh)
    out: list[SignalOut] = []
    failed_syms: list[str] = []
    for sym in symbols:
        try:
            out.append(compute_signal(sym, data_source=route))
        except Exception as e:
            failed_syms.append(sym)
            logger.debug("signal skipped %s: %s", sym, e)
    response.headers["X-Quant-Signals-Success-Count"] = str(len(out))
    response.headers["X-Quant-Signals-Failed-Count"] = str(len(failed_syms))
    if failed_syms:
        joined = ",".join(failed_syms[:120])
        if len(failed_syms) > 120:
            joined += ",..."
        response.headers["X-Quant-Signals-Failed-Symbols"] = joined[:1800]
    return out


@app.get(
    "/signals/{symbol}",
    response_model=SignalOut,
    tags=["④ 查看信号"],
    summary="只查一只股票的信号",
    description="""
在路径参数 **symbol** 里填 6 位代码（如 `600519`）。

- 代码格式不对、或本地 K 线太少时，会返回 **400**，请先 `POST /ingest/update`。
- 一般需先在自选里添加并拉取过行情；若库里已有该代码日线，也可直接查。
- **pre_refresh** / **data_source**：与 `GET /signals` 相同，可先按所选路线增量更新该标的再计算。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def signals_one(
    symbol: str,
    request: Request,
    response: Response,
    pre_refresh: bool = Query(False, description="为 true 时先按路线增量更新该标的日线"),
    data_source: IngestDataSource | None = Query(None, description="行情路线；不传则用服务端默认"),
    _: None = Depends(optional_api_key),
):
    """单标的信号；代码非法或数据不足时 400。"""
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    route = _resolve_ingest_route(data_source)
    response.headers["X-Quant-Data-Source"] = route
    response.headers["X-Quant-Pre-Refresh"] = "1" if pre_refresh else "0"
    _pre_refresh_symbols([sym], route=route, pre_refresh=pre_refresh)
    try:
        return compute_signal(sym, data_source=route)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/research/forecast-validate",
    response_model=ForecastValidateOut,
    tags=["⑧ 研究：预测验证"],
    summary="walk-forward：未来 H 日涨跌方向（样本外指标）",
    description="""
仅用 **SQLite 已存日线**（不触发联网增量），按时间顺序做 **walk-forward** 样本外评估（与常见「pandas 算信号 → 回测验证」入门路径一致）：

- **标签**：t 日收盘至 t+H 日收盘累计收益是否大于 0（H 默认 5 个交易日）。
- **策略**：**双均线**（短/长周期可配，默认 5/10）、趋势规则、周期性重训 **Logistic**（NumPy）、因果多数类基线；可用查询参数 **methods** 逗号分隔子集，仅计算所选。
- **行情**：默认仅读 SQLite；**live_bars=true** 时先联网拉 incremental 窗口（**data_source** 与 ingest 一致）；**live_as_of** 为截止日期 YYYY-MM-DD（省略用服务器当天）；**live_persist** 控制是否写库。响应 **bars_last_trade_date** 为实际用到的最后一根 K 线，若仍早于预期多为数据源未更新到该日。
- **特征**（Logistic 用）：ret5/ret20、MA20 斜率、量比、距 60 日高回撤、20 日实现波动（均仅用 t 及以前数据）。
- **买卖示意**：信号由空转多记买入收盘、由多转空记卖出收盘；返回最近若干笔区间涨跌与汇总（默认已计入佣金/印花税/滑点估算，仍非逐笔撮合）。
- **pedagogy**：响应内附学习路线说明与参考阅读链接（博客园公开笔记，非商业背书）。
- **fundamentals_backtest**：说明扩展因子快照是否已缓存，以及**为何未**并入历史 walk-forward（避免用「最新财务」冒充历史每一天的可知信息）。

若返回 400，多为 K 线太短或未 ingest。结果受单标的噪声影响大，**不构成投资建议**。
""",
)
@limiter.limit("20/minute")
def research_forecast_validate(
    request: Request,
    symbol: str = Query(..., description="6 位 A 股代码", examples=["600519"]),
    horizon: int = Query(5, ge=1, le=60, description="未来多少个交易日的累计收益方向"),
    min_train_rows: int = Query(
        120,
        ge=80,
        le=2500,
        description="进入样本外前至少保留的训练行数（越大越稳、OOS 越短）",
    ),
    retrain_every: int = Query(
        20,
        ge=1,
        le=500,
        description="Logistic 每隔多少根 OOS 步长用扩展窗重训一次",
    ),
    trade_limit: int = Query(
        25,
        ge=1,
        le=200,
        description="返回最近多少笔完整买卖（示意）；汇总统计仍基于全部 OOS 成交笔",
    ),
    ma_short: int = Query(
        5,
        ge=2,
        le=120,
        description="双均线：短周期（交易日）",
    ),
    ma_long: int = Query(
        10,
        ge=3,
        le=250,
        description="双均线：长周期（须大于 ma_short）",
    ),
    commission_bps: float = Query(
        3.0,
        ge=0,
        le=100,
        description="单边佣金/过户等估算成本，单位 bps；默认 3",
    ),
    sell_tax_bps: float = Query(
        5.0,
        ge=0,
        le=100,
        description="卖出印花税估算，单位 bps；默认 5",
    ),
    slippage_bps: float = Query(
        2.0,
        ge=0,
        le=100,
        description="单边滑点估算，单位 bps；默认 2",
    ),
    oos_from: str | None = Query(
        None,
        description="样本外区间起始日（含）YYYY-MM-DD；与 oos_to 联用可只看某段（如 2025 起），训练仍用此前全部历史",
    ),
    oos_to: str | None = Query(
        None,
        description="样本外区间结束日（含）YYYY-MM-DD",
    ),
    methods: str | None = Query(
        None,
        description="逗号分隔方法键名，仅计算并返回所选：dual_ma_cross,logistic_walkforward,rule_trend,majority_causal；省略则四种全开",
    ),
    live_bars: bool = Query(
        False,
        description="为 true 时先对该标的联网 incremental_refresh 再回测（写入本地库，与③同源 data_source）",
    ),
    data_source: str | None = Query(
        None,
        description="仅在 live_bars=true 时生效：行情路线，与 POST /ingest/update 的 data_source 一致；省略用服务端默认",
    ),
    live_persist: bool = Query(
        True,
        description="live_bars=true 时：true=拉取后写入 SQLite 再读库；false=仅内存合并（不写库）",
    ),
    live_as_of: str | None = Query(
        None,
        description="live_bars=true 时：联网增量截止日期（含）YYYY-MM-DD，与③结束日期对齐；省略用服务器当天",
    ),
    _: None = Depends(optional_api_key),
):
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    method_list: list[str] | None = None
    if methods and methods.strip():
        method_list = [p.strip() for p in methods.split(",") if p.strip()]
    try:
        raw = run_forecast_validate(
            sym,
            horizon=horizon,
            min_train_rows=min_train_rows,
            retrain_every=retrain_every,
            trade_limit=trade_limit,
            ma_short=ma_short,
            ma_long=ma_long,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
            oos_from=oos_from,
            oos_to=oos_to,
            methods=method_list,
            live_bars=live_bars,
            live_persist=live_persist,
            data_source=data_source if live_bars else None,
            live_as_of=live_as_of if live_bars else None,
        )
        return ForecastValidateOut.model_validate(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _run_sector_screen(body: SectorScreenIn) -> SectorScreenOut:
    """执行选股流水线；symbols 模式写临时 CSV，等价命令行 --codes。"""
    tmp_codes: Path | None = None
    try:
        if body.data_source == SectorScreenDataSource.tushare:
            tok = _hot_tushare_token(body.tushare_token)
            if not tok:
                raise HTTPException(
                    status_code=400,
                    detail="data_source=tushare 时请传 tushare_token，或配置 TUSHARE_TOKEN / 服务端 tushare_token。",
                )
            tushare_token_resolved = tok
        else:
            tushare_token_resolved = body.tushare_token

        if body.symbols:
            norm: list[str] = []
            seen: set[str] = set()
            for raw in body.symbols:
                if raw is None or not str(raw).strip():
                    continue
                try:
                    c = normalize_symbol(str(raw))
                except ValueError:
                    continue
                if c not in seen:
                    seen.add(c)
                    norm.append(c)
            if not norm:
                raise HTTPException(status_code=400, detail="symbols 中无有效 A 股代码")
            fd, tpath = tempfile.mkstemp(suffix=".csv", text=True)
            os.close(fd)
            tmp_codes = Path(tpath)
            with tmp_codes.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["code"])
                for c in norm:
                    w.writerow([c])
            hot_sectors = False
            sector_name = None
            codes_arg = tmp_codes
        elif body.sector and body.sector.strip():
            hot_sectors = False
            sector_name = body.sector.strip()
            codes_arg = None
        else:
            hot_sectors = True
            sector_name = None
            codes_arg = None

        ns = argparse.Namespace(
            data_source=body.data_source.value,
            tushare_token=tushare_token_resolved,
            hot_sectors=hot_sectors,
            sector=sector_name,
            codes=codes_arg,
            data_dir=None,
            board_type=body.board_type,
            top_sectors=body.top_sectors,
            max_stocks_per_sector=body.max_stocks_per_sector,
            start_date=body.start_date,
            end_date=body.end_date,
            adjust=body.adjust,
            fast_period=body.fast_period,
            slow_period=body.slow_period,
            initial_cash=body.initial_cash,
            commission=body.commission,
            stop_loss=body.stop_loss,
            scoring_strategy=body.scoring_strategy,
            only_passed=body.only_passed,
            top_stocks=body.top_stocks_limit,
            output=None,
            hot_chain_prefer_cache=body.hot_chain_prefer_snapshot,
            hot_chain_force_refresh=body.hot_chain_refresh_snapshot,
        )
        validate_args(ns)
        sectors, stocks = run_analysis(ns)
    except HTTPException:
        raise
    except DataSourceError as e:
        raise _http_exception_from_datasource(e) from e
    except Exception as e:
        logger.exception("sector-screen failed: %s", e)
        raise HTTPException(status_code=502, detail=f"选股流水线失败：{e}") from e
    finally:
        if tmp_codes is not None and tmp_codes.is_file():
            tmp_codes.unlink(missing_ok=True)

    d = _disclaimer_payload()
    lim = max(1, body.top_stocks_limit)
    stock_rows = [asdict(s) for s in stocks[:lim]]
    return SectorScreenOut(
        sectors=[asdict(s) for s in sectors],
        stocks=stock_rows,
        stocks_total=len(stocks),
        start_date=body.start_date,
        end_date=body.end_date,
        disclaimer=d.disclaimer,
        note="与根目录 `quant_stock_selector.py` 及包 `app.quant_stock_selector` 流水线一致（技术面初筛 + 双均线回测 + 综合分）。请求会大量拉取行情，请勿频繁触发。",
    )


def _run_sector_constituents_top(body: SectorConstituentsTopIn) -> SectorConstituentsTopOut:
    """按板块名拉成分股，过滤后取前 limit 条，并合并东财 spot 与个股日级资金流向末行。"""
    if body.data_source == SectorScreenDataSource.tushare:
        tok = _hot_tushare_token(body.tushare_token)
        if not tok:
            raise HTTPException(
                status_code=400,
                detail="data_source=tushare 时请传 tushare_token，或配置 TUSHARE_TOKEN / 服务端 tushare_token。",
            )
        tushare_token_resolved = tok
    else:
        tushare_token_resolved = body.tushare_token

    bt_arg = None if body.board_type == "all" else body.board_type
    sector_nm = body.sector_name.strip()
    constituents_note_suffix = ""

    try:
        ds = get_data_source(
            body.data_source.value,
            tushare_token=tushare_token_resolved,
            hot_chain_prefer_cache=body.hot_chain_prefer_snapshot,
            hot_chain_force_refresh=body.hot_chain_refresh_snapshot,
        )
        cons = ds.get_sector_constituents(sector_nm, bt_arg)
    except HTTPException:
        raise
    except DataSourceError as e:
        # mootdx 仅覆盖通达信板块文件中的块名；新浪/东财快照里的概念名常对不上，成分改走东财解析。
        if body.data_source == SectorScreenDataSource.mootdx and "未找到板块" in str(e):
            try:
                em = get_data_source("akshare")
                cons = em.get_sector_constituents(sector_nm, bt_arg)
                constituents_note_suffix = (
                    " 该板块名在 mootdx 通达信板块表中未命中（与新浪/东财板块名不完全一致），"
                    "成分列表已自动改用东财(AkShare)拉取。"
                )
                logger.info("sector-constituents-top: mootdx miss sector=%r, fell back to akshare", sector_nm)
            except DataSourceError as e2:
                raise _http_exception_from_datasource(e2) from e
        else:
            raise _http_exception_from_datasource(e) from e
    except Exception as e:
        logger.exception("sector-constituents-top failed: %s", e)
        raise HTTPException(status_code=502, detail=f"拉取板块成分失败：{e}") from e

    if cons is None or cons.empty:
        d = _disclaimer_payload()
        return SectorConstituentsTopOut(
            sector_name=body.sector_name.strip(),
            board_type=None,
            stocks=[],
            stocks_total=0,
            constituents_total_after_filter=0,
            disclaimer=d.disclaimer,
            note="未返回任何成分行。",
        )

    lim = max(1, min(int(body.limit), 50))
    passing: list[dict[str, Any]] = []
    resolved_bt: str | None = None
    for _, crow in cons.iterrows():
        code = normalize_code(str(crow.get("code", "") or ""))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        name = crow.get("name", crow.get("名称", ""))
        if body.exclude_kcb and is_star_board_code(code):
            continue
        if body.exclude_st and is_st_stock_name(name):
            continue
        row_bt = crow.get("board_type")
        bt_str: str | None = None
        if row_bt is not None and str(row_bt).strip():
            bt_str = str(row_bt).strip().lower()
            if resolved_bt is None:
                resolved_bt = bt_str
        passing.append(
            {
                "code": code,
                "name": str(name).strip() if name is not None else "",
                "sector_name": str(crow.get("sector_name", body.sector_name)).strip(),
                "board_type": bt_str,
            }
        )

    slice_rows = passing[:lim]
    codes_for_spot = [r["code"] for r in slice_rows]
    spot_by_code = spot_liquidity_fields_for_codes(codes_for_spot)
    stocks_out: list[dict[str, Any]] = []
    for i, row in enumerate(slice_rows, start=1):
        code = row["code"]
        merged: dict[str, Any] = {**row, "rank": i}
        merged.update(spot_by_code.get(code, {}))
        try:
            ff = fetch_individual_fund_flow_latest_metrics(code)
            if ff:
                merged.update(ff)
        except Exception as e:
            logger.debug("sector-constituents-top fund_flow %s: %s", code, e)
        stocks_out.append(merged)
        if i < len(slice_rows):
            time.sleep(0.06)

    d = _disclaimer_payload()
    return SectorConstituentsTopOut(
        sector_name=body.sector_name.strip(),
        board_type=resolved_bt,
        stocks=stocks_out,
        stocks_total=len(stocks_out),
        constituents_total_after_filter=len(passing),
        disclaimer=d.disclaimer,
        note=(
            "成分后已尝试以东财全 A 列表补「现价/昨收、成交量、成交额」，并以个股日级资金流向表末行补"
            "「收盘价与对应交易日、大单(含超大单)与小单净流入净占比」；成交额/量为列表截面（盘中为当日累积），"
            "大单小单占比为东财日级口径而非逐笔拆单。远端失败时对应字段为空。不构成投资建议。"
            + constituents_note_suffix
        ),
    )


@app.post(
    "/research/sector-screen",
    response_model=SectorScreenOut,
    tags=["⑨ 量化选股（脚本）"],
    summary="热门板块选股（脚本同款流水线）",
    description="""
对应命令行 **`quant_stock_selector.py`** / 包 **`app.quant_stock_selector`**：

1. 拉取热门板块（或指定 **sector**、或 **symbols** 自定义列表）；
2. 取成分股，按 `start_date`～`end_date` 拉日线（优先本地 `data_dir` 在 CLI 中有，API 固定仅走网络数据源）；
3. 技术面初筛（`evaluate_screen`）+ 双均线回测（`run_sma_backtest`）合成 **final_score**。

**注意**：会对多只股票依次请求行情，**耗时长**、易受数据源限流；请将 `max_stocks_per_sector`、`top_sectors` 控制在合理范围。

**`data_source=hot_chain`**：热门板块表与 `POST /meta/hot-market-snapshot/refresh` 相同（**新浪优先** → 回退等），可用 `hot_chain_prefer_snapshot` / `hot_chain_refresh_snapshot` 控制是否读本地 `hot_market_snapshot.json`；**成分股与日线**仍经东财拉取。详见 `app/hot_market_snapshot.py`。

**`data_source=baostock`**：**日 K** 经 Baostock（与③行情 `baostock` 路线一致，通常较爬东财页稳）；**板块排名与成分股**仍用东财（AkShare），因 Baostock 无同形态热门板块表。

**TuShare** 须 `tushare_token` 或服务端 token 配置。**不构成投资建议**。
""",
)
@limiter.limit("3/minute")
def research_sector_screen(
    request: Request,
    body: SectorScreenIn = Body(...),
    _: None = Depends(optional_api_key),
):
    return _run_sector_screen(body)


@app.post(
    "/research/sector-constituents-top",
    response_model=SectorConstituentsTopOut,
    tags=["⑨ 量化选股（脚本）"],
    summary="热门板块成分前 N（仅列表，不跑回测）",
    description="""
按 **sector_name**（与板块列表「板块」列一致）调用当前 **data_source** 的 `get_sector_constituents`，
在服务端按选项过滤 **ST** / **科创板** 后取前 **limit** 条（默认 10）。

**`data_source=mootdx`**：成分名依赖通达信板块文件；若板块名与新浪/东财快照不一致导致「未找到板块」，
服务端会**自动改以东财(AkShare)** 再拉一次成分（响应 `note` 会说明）。

不写库、不跑选股 K 线与回测；返回每条成分时会**尽量**以东财全 A 列表补成交量/成交额/现价与昨收，
并以个股**日级资金流向**末行补收盘价、对应交易日、大单(含超大单)与小单净流入净占比（详见响应 `note`）。
**TuShare** 规则与 `/research/sector-screen` 相同。**不构成投资建议**。
""",
)
@limiter.limit("20/minute")
def research_sector_constituents_top(
    request: Request,
    body: SectorConstituentsTopIn = Body(...),
    _: None = Depends(optional_api_key),
):
    return _run_sector_constituents_top(body)


@app.post(
    "/alerts/preview",
    tags=["⑤ 变动预览"],
    summary="看看和「上一次」比，信号变了吗",
    description="""
会把**当前算出来的信号**和**服务器里上次记住的结果**做对比：

- **new**：第一次记录这只股票；
- **shift**：趋势变了，或评分从「十位档」上跳了一档（例如 50→59 不算，59→61 算）。

执行成功后，会用当前结果覆盖缓存，**下次再点**就会和这次比。

**Body** 可传 `pre_refresh`、`data_source`（与 `GET /signals` 一致）：先按③所选路线增量更新自选日线，再对比信号。

**Body 留空** 等价于二者默认，行为与旧版一致（仅用库内已有数据）。
""",
)
@limiter.limit("30/minute")
def alerts_preview(
    request: Request,
    body: AlertsPreviewIn = Body(default_factory=AlertsPreviewIn),
    _: None = Depends(optional_api_key),
):
    """
    对比 signal_cache 中上一版快照与当前 compute_signal 结果，返回 new/shift 事件。

    随后用当前结果刷新缓存（upsert SignalCacheRow），供下次对比使用。
    """
    route = _resolve_ingest_route(body.data_source)
    with session_scope() as s:
        cached = s.execute(select(SignalCacheRow)).scalars().all()
        watch = s.execute(select(WatchlistRow)).scalars().all()
        prev_map = {row.symbol: json.loads(row.payload_json) for row in cached}
        watch_symbols = [w.symbol for w in watch]
    _pre_refresh_symbols(watch_symbols, route=route, pre_refresh=body.pre_refresh)
    current: dict[str, SignalOut] = {}
    for sym in watch_symbols:
        try:
            current[sym] = compute_signal(sym, data_source=route)
        except Exception:
            continue
    events = detect_changes(prev_map, current)
    from datetime import datetime

    now = datetime.utcnow().isoformat() + "Z"
    with session_scope() as s:
        for sym, sig in current.items():
            payload = json.dumps(signal_to_snapshot(sig), ensure_ascii=False)
            row = s.execute(select(SignalCacheRow).where(SignalCacheRow.symbol == sym)).scalar_one_or_none()
            if row:
                row.payload_json = payload
                row.updated_at = now
            else:
                s.add(SignalCacheRow(symbol=sym, payload_json=payload, updated_at=now))
    return {
        "events": events,
        "disclaimer": _disclaimer_payload().model_dump(),
        "request": {
            "pre_refresh": body.pre_refresh,
            "data_source": route,
        },
    }


def _journal_row_to_out(row: DecisionJournalRow) -> JournalOut:
    return JournalOut(
        id=row.id,
        created_at=row.created_at,
        symbol=row.symbol,
        title=row.title,
        body=row.body,
        signal_snapshot_json=row.signal_snapshot_json,
        planned_action=row.planned_action,
        planned_position_pct=row.planned_position_pct,
        executed_as_planned=row.executed_as_planned,
        actual_action=row.actual_action,
    )


@app.post(
    "/journal",
    response_model=JournalOut,
    tags=["⑦ 决策日志（自用）"],
    summary="新增一条决策/复盘记录",
    description="""
记录本周趋势判断、依据与（若实盘）计划仓位、事后是否按计划执行。

- `attach_current_signal=true` 且 `symbol` 有效时，会尝试把当前 `GET /signals/{symbol}` 的结果 JSON 附在 `signal_snapshot_json`（失败则该字段为空）。
- **非投资建议**；数据仅存本机 SQLite。
""",
)
@limiter.limit("30/minute")
def journal_create(body: JournalIn, request: Request, _: None = Depends(optional_api_key)):
    sym: str | None = None
    if body.symbol and str(body.symbol).strip():
        try:
            sym = normalize_symbol(body.symbol)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    snap: str | None = None
    if body.attach_current_signal and sym:
        try:
            sig = compute_signal(sym)
            snap = json.dumps(sig.model_dump(), ensure_ascii=False)
        except Exception as e:
            logger.debug("journal attach signal skipped: %s", e)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    row = DecisionJournalRow(
        created_at=now,
        symbol=sym,
        title=body.title.strip(),
        body=body.body.strip(),
        signal_snapshot_json=snap,
        planned_action=body.planned_action.strip() if body.planned_action else None,
        planned_position_pct=body.planned_position_pct,
        executed_as_planned=body.executed_as_planned,
        actual_action=body.actual_action.strip() if body.actual_action else None,
    )
    with session_scope() as s:
        s.add(row)
        s.flush()
        s.refresh(row)
        out = _journal_row_to_out(row)
    return out


@app.get(
    "/journal",
    response_model=list[JournalOut],
    tags=["⑦ 决策日志（自用）"],
    summary="列出决策日志（新在前）",
    description="可选 `symbol` 过滤；`limit` 默认 30，最大 100。",
)
@limiter.limit(get_settings().rate_limit_default)
def journal_list(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    symbol: str | None = Query(None, description="仅看某 6 位代码相关记录"),
    _: None = Depends(optional_api_key),
):
    sym: str | None = None
    if symbol and symbol.strip():
        try:
            sym = normalize_symbol(symbol)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    with session_scope() as s:
        q = select(DecisionJournalRow).order_by(DecisionJournalRow.id.desc()).limit(limit)
        if sym is not None:
            q = q.where(DecisionJournalRow.symbol == sym)
        rows = s.execute(q).scalars().all()
        return [_journal_row_to_out(r) for r in rows]


@app.get(
    "/journal/{entry_id}",
    response_model=JournalOut,
    tags=["⑦ 决策日志（自用）"],
    summary="按 id 取单条日志",
)
@limiter.limit(get_settings().rate_limit_default)
def journal_one(entry_id: int, request: Request, _: None = Depends(optional_api_key)):
    with session_scope() as s:
        row = s.execute(select(DecisionJournalRow).where(DecisionJournalRow.id == entry_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return _journal_row_to_out(row)


@app.delete(
    "/journal/{entry_id}",
    tags=["⑦ 决策日志（自用）"],
    summary="删除一条日志",
)
@limiter.limit("30/minute")
def journal_delete(entry_id: int, request: Request, _: None = Depends(optional_api_key)):
    with session_scope() as s:
        row = s.execute(select(DecisionJournalRow).where(DecisionJournalRow.id == entry_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        s.delete(row)
    return {"ok": True, "id": entry_id}


@app.get(
    "/",
    tags=["① 入门必读"],
    summary="服务信息（给程序看的 JSON）",
    description="""
返回服务名称、文档路径、一句免责摘要。**人类用户**请用 **[/ui](/ui)** 图形控制台；开发者用 **/docs**。

无需 API Key。
""",
)
def root():
    """服务标识、文档入口与简要免责（JSON）；不需要 API Key。"""
    d = _disclaimer_payload()
    return JSONResponse(
        {
            "service": "quant-monitor",
            "ui": "/ui",
            "web_data_preview_ui": "/ui/web-crawler",
            "docs": "/docs",
            "self_use": "/meta/self-use",
            "journal": "/journal",
            "research_forecast_validate": "/research/forecast-validate",
            "research_sector_screen": "/research/sector-screen",
            "disclaimer": d.disclaimer,
            "data_source_note": d.data_source_note,
        }
    )


@app.get("/ui", include_in_schema=False)
def ui_console():
    """图形控制台（静态页）；不参与 OpenAPI，避免与 Swagger 重复。"""
    path = STATIC_DIR / "console.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="控制台页面未找到")
    return FileResponse(path)


@app.get("/ui/web-crawler", include_in_schema=False)
def ui_web_crawler():
    """K 线与资金流网页数据预览（静态页）；与 POST /ingest/web-data-preview 配套。"""
    path = STATIC_DIR / "web-crawler.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="页面未找到")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
