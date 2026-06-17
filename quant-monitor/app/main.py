"""
FastAPI 应用入口：HTTP API、图形控制台静态页与 OpenAPI 文档。

## 功能作用

`main.py` 是 quant-monitor 的 **Web 服务层**：将各业务模块（ingest、signals、holdings、
quant_stock_selector 等）暴露为 REST 接口，并托管 **[/ui](/ui)** 图形控制台。

- 启动时 `lifespan` → `init_db()` 建表；
- 约 **60+** 个路由，按控制台步骤分为 OpenAPI **tags ①～⑩**；
- 敏感读写默认 `Depends(optional_api_key)`（配置了 `API_KEY` 时须 `X-API-Key`）；
- 限流：`slowapi` + `get_remote_address`，多数接口 `@limiter.limit(...)`。

## 控制台步骤与路由分组

| Tag | 主题 | 代表接口 |
|-----|------|----------|
| ① 入门必读 | 健康、鉴权、批量取消/进度、热门快照 meta | `/health`, `/meta/*` |
| ② 管理自选 | 自选 CRUD、现价刷新、热门股导入 | `/watchlist`, `/watchlist/*` |
| ③ 更新行情 | 日线 ingest、扩展因子、连通性测试 | `/ingest/update`, `/ingest/fundamentals` |
| ④ 查看信号 | K 线查询、批量/单只信号 | `/signals`, `/quotes/{symbol}/bars` |
| ⑤ 个股咨询 | 行情、新闻、概念、业务与营收 | `/research/stock-brief/{symbol}` |
| ⑥ 金融从零学起 | 系统课程、免责说明 | `/meta/stock-knowledge`, `/meta/disclaimer` |
| ⑦ AI算法 | 汇总②③④数据、AI 潜力测算 | `/research/ai-potential` |
| ⑧ 研究 | walk-forward 预测验证、打分分档验证 | `/research/forecast-validate`, `/research/score-bucket-validate` |
| ⑨ 量化选股 | 板块选股 pipeline | `/research/sector-screen` |
| ⑩ 持仓记录 | 持仓 CRUD、进离场与目标测算 | `/holdings/*` |

## 文件结构（阅读顺序）

1. **导入** — 聚合 `app.*` 子模块；
2. **OPENAPI_DESCRIPTION / OPENAPI_TAGS** — Swagger 小白说明（人类读 `/docs`）；
3. **FastAPI 实例** — CORS、limiter、lifespan；
4. **`_*` 辅助函数** — 自选 enrich、热门板块、sector-screen 编排等（非路由）；
5. **`@app.get/post` 路由** — 按 tag 分段；
6. **静态资源** — `/ui`, `/static`。

## 非路由入口

- `GET /`, `/health` — 无需 Key（health 探活）；
- `GET /ui`, `/ui/web-crawler` — `include_in_schema=False`，不在 Swagger 列表。

业务逻辑应放在对应 `app/*.py` 模块；本文件以 **编排与 HTTP 边界** 为主，避免继续膨胀。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import delete, or_, select

from app.batch_cancel import KNOWN_SCOPES, cancel_many, clear, clear_many, is_cancelled
from app.ingest_batch_job import (
    ingest_batch_enter_finalize,
    ingest_batch_finish,
    ingest_batch_set_current,
    ingest_batch_should_cancel,
    ingest_batch_start,
    ingest_batch_status,
    ingest_batch_tick,
)
from app.symbols_batch_job import (
    symbols_batch_finish,
    symbols_batch_partial_results,
    symbols_batch_push_result,
    symbols_batch_set_current,
    symbols_batch_start,
    symbols_batch_status,
    symbols_batch_tick,
)
from app.config import get_settings
from app.db import (
    WATCHLIST_ORIGIN_AUTO_HOT,
    WATCHLIST_ORIGIN_AUTO_QUANT,
    WATCHLIST_ORIGIN_MANUAL,
    DecisionJournalRow,
    ForwardOutlookRow,
    HoldingRow,
    WatchlistAddLogRow,
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
    _live_row_has_price,
    fetch_stock_name,
    fetch_stock_names_map,
    incremental_refresh,
    ingest_symbol_range,
    list_bars_from_db,
    local_ingest_result_row,
    normalize_symbol,
    resolve_data_source,
    strength_snapshot_for_symbol,
    test_akshare_connectivity,
    enrich_ingest_results_with_spot,
    enrich_ingest_results_with_spot_progress,
    enrich_one_ingest_result_spot,
    live_quote_fields_for_codes,
    live_quote_fields_for_codes_enhanced,
    backfill_today_bar_from_live,
    parse_watchlist_spot_reuse_map,
    watchlist_bar_fields_for_session,
)
from app.watchlist_add_log import (
    entries_added_in_range,
    entries_added_on_date,
    latest_added_at_for_symbols,
    list_watchlist_add_dates,
    log_row_snapshot_fields,
    parse_added_date_param,
    record_watchlist_add,
    record_watchlist_adds_with_snapshot,
    shanghai_today_ymd,
)
from app.watchlist_spot_job import (
    watchlist_spot_job_finish,
    watchlist_spot_job_set_current,
    watchlist_spot_job_should_cancel,
    watchlist_spot_job_start,
    watchlist_spot_job_status,
    watchlist_spot_job_tick,
)
from app.quant_stock_selector import DataSourceError, get_data_source, pick_from_hot_sectors
from app.quant_stock_selector.models import StockEvaluation
from app.quant_stock_selector.hot_pick import is_star_board_code, is_st_stock_name
from app.quant_stock_selector.market_utils import is_listed_a_share_equity, normalize_code
from app.quant_stock_selector.datasources import (
    default_sector_snapshot_path,
    load_sector_rankings_snapshot,
    save_sector_rankings_snapshot,
)
from app.holdings import (
    HOLDING_STATUS_CLOSED,
    HOLDING_STATUS_HOLDING,
    apply_holding_defaults,
    build_holdings_list,
    compute_holding_entry_advice,
    compute_holding_exit_advice,
    compute_holdings_review_summary,
    create_closed_holding_record,
    normalize_holdings_notify_url,
    post_holdings_refresh_webhook,
    validate_holding_sell_price,
)
from app.holdings_goal import (
    check_goal_plan_live_readiness,
    compute_goal_progress,
    compute_holding_goal_plan,
)
from app.quant_stock_selector.cli import validate_args
from app.quant_stock_selector.pipeline import run_analysis
from app.schemas import (
    AiPotentialIn,
    AiPotentialOut,
    AiPotentialContextOut,
    AiDefaultsOut,
    CancelBatchIn,
    DailyBarOut,
    DisclaimerOut,
    FillHotSectorsIn,
    FillHotSectorsOut,
    FillHotSectorsSummary,
    ForecastValidateOut,
    ScoreBucketValidateOut,
    HotMarketSnapshotFileOut,
    HotMarketSnapshotOut,
    HotMarketSnapshotRefreshIn,
    HotMarketSnapshotRefreshOut,
    IngestDataSource,
    IngestFundamentalsIn,
    IngestUpdateIn,
    ForwardOutlookOut,
    ForwardOutlookSyncIn,
    ForwardOutlookSyncOut,
    HoldingCloseIn,
    HoldingClosedRecordIn,
    HoldingReviewSummaryOut,
    HoldingEntryAdviceOut,
    HoldingExitAdviceOut,
    HoldingGoalPlanIn,
    HoldingGoalPlanLivePreflightOut,
    HoldingGoalPlanOut,
    HoldingGoalProgressOut,
    HoldingIn,
    HoldingsNotifyIn,
    HoldingsNotifyOut,
    HoldingOut,
    HoldingUpdateIn,
    JournalIn,
    JournalOut,
    QuantWatchlistSyncIn,
    QuantWatchlistSyncOut,
    SectorConstituentsTopIn,
    SectorConstituentsTopOut,
    SectorScreenDataSource,
    SectorScreenIn,
    SectorScreenOut,
    SectorScreenPoolMode,
    SelectorSectorDataSource,
    SelfUseMetaOut,
    SignalOut,
    StockBriefOut,
    StockKnowledgeOut,
    WatchlistBatchAddIn,
    WatchlistBatchAddOut,
    WatchlistBatchDeleteIn,
    WatchlistBatchDeleteOut,
    WatchlistDeleteAllIn,
    WatchlistDeleteAllOut,
    QuantWatchlistStockRowIn,
    WatchlistReplaceAllIn,
    WatchlistReplaceAllOut,
    WatchlistAddDatesOut,
    WatchlistIn,
    WatchlistItem,
    WatchlistTodayCloseBackfillIn,
    WatchlistTodayCloseBackfillOut,
    WatchlistTodayCloseBackfillRow,
    WatchlistRefetchKlineIn,
    WatchlistRefetchKlineOut,
    WatchlistRefetchKlineResultRow,
    WebDataPreviewIn,
)
from app.forecast_validate import run_forecast_validate
from app.score_validate import run_score_bucket_validate
from app.stock_knowledge import stock_knowledge_payload
from app.forward_outlook import (
    DEFAULT_HORIZON,
    _stock_names_for_symbols,
    row_to_dict,
    settle_all_pending,
    sync_after_ingest,
    sync_symbol_outlook,
)
from app.hot_market_snapshot import (
    default_hot_market_snapshot_path,
    fetch_hot_market_snapshot,
    load_hot_market_snapshot,
    save_hot_market_snapshot,
)
from app.signals import compute_signal
from app.ai_potential import (
    ai_defaults_payload,
    gather_symbol_context,
    resolve_symbols_for_ai,
    run_ai_potential,
)
from app.stock_brief import build_stock_brief

STATIC_DIR = Path(__file__).resolve().parent / "static"

# --- 日志 ---

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- OpenAPI 文档（Swagger /docs 展示用） ---

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

5. **（推荐）自用定位**  
   `GET /meta/self-use` 查看工具定位与风控检查项。

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
        "name": "⑤ 个股咨询",
        "description": "输入 6 位代码，联网聚合东财公开数据：现价行情、近期新闻、核心概念/题材、公司业务与主营构成、营收与盈利指标。非投资建议。",
    },
    {
        "name": "⑥ 金融从零学起",
        "description": "系统金融课程（约 30+ 节、8 阶段）：金融本质、货币宏观、市场与公司财务、估值、组合理论、A 股实务；含练手与自测。底部含免责全文。",
    },
    {
        "name": "⑦ AI算法",
        "description": "汇总②自选、③行情、④信号等本地测算结果，调用 OpenAI 兼容大模型做「潜力」Demo 解读（API Key 可在⑦控制台填写或服务端 .env）。非投资建议。",
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


# --- 内部辅助函数（路由编排，非业务核心） ---


def _resolve_ingest_route(data_source: IngestDataSource | None) -> str:
    """与 ingest 一致的小写路线关键字。"""
    if data_source is not None:
        return data_source.value
    return str(get_settings().ingest_data_source or "auto").strip().lower()


def _pre_refresh_symbols(
    symbols: list[str],
    *,
    route: str,
    pre_refresh: bool,
    cancel_scope: str = "pre_refresh",
) -> None:
    """按路线对各标的 incremental_refresh；失败仅打日志。cancel_scope 供批量中断检查。"""
    if not pre_refresh or not symbols:
        return
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    for i, sym in enumerate(symbols):
        if is_cancelled(cancel_scope):
            logger.info("pre_refresh cancelled (scope=%s) before %s", cancel_scope, sym)
            return
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


def _codes_from_hot_sectors_detail(sectors_detail: list[dict[str, Any]]) -> list[str]:
    """热门板块明细中全部成分股代码（去重、保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for bundle in sectors_detail:
        for st in bundle.get("stocks") or []:
            if not isinstance(st, dict):
                continue
            nk = normalize_code(str(st.get("code") or st.get("代码") or ""))
            if len(nk) != 6 or nk in seen:
                continue
            seen.add(nk)
            out.append(nk)
    return out


def _live_quote_map_for_hot_sector_codes(
    codes: list[str],
    *,
    selector_data_source: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """
    为热门板块明细合并「最新价」：mootdx 用通达信批量快照，其余用东财全 A 列表；
    列表未命中且数量较少时再逐股东财 push2 报价。
    """
    warnings: list[str] = []
    qmap: dict[str, dict[str, Any]] = {}
    if not codes:
        return qmap, warnings

    ds_key = (selector_data_source or "akshare").strip().lower()
    if ds_key == "mootdx":
        try:
            ds_mx = get_data_source("mootdx")
            fn = getattr(ds_mx, "quote_snapshot_for_codes", None)
            if callable(fn):
                raw = fn(codes)
                for k, row in (raw or {}).items():
                    nk = normalize_code(str(k))
                    if len(nk) != 6 or not isinstance(row, dict):
                        continue
                    p = row.get("tdx_last_price")
                    if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                        continue
                    chg = row.get("tdx_change_pct")
                    chg_f = None
                    if chg is not None and math.isfinite(float(chg)):
                        chg_f = round(float(chg), 2)
                    qd = row.get("tdx_quote_date")
                    qmap[nk] = {
                        "price": float(p),
                        "change_pct": chg_f,
                        "quote_date": qd if isinstance(qd, str) else None,
                        "source": "mootdx_snapshot",
                    }
        except Exception as e:
            logger.warning("hot sectors mootdx quote overlay skipped: %s", e)
            warnings.append(f"热门板块「最新价」：通达信快照失败，仍展示日线末根收盘价（{e}）")
    else:
        try:
            spot_by = spot_liquidity_fields_for_codes(codes, force_refresh=True)
            for sym in codes:
                row = spot_by.get(sym) or {}
                p = row.get("spot_last_price")
                if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                    continue
                chg = row.get("spot_change_pct")
                chg_f = None
                if chg is not None and math.isfinite(float(chg)):
                    chg_f = round(float(chg), 2)
                qmap[sym] = {
                    "price": float(p),
                    "change_pct": chg_f,
                    "quote_date": row.get("spot_quote_date"),
                    "source": "eastmoney_spot",
                }
        except Exception as e:
            logger.warning("hot sectors spot overlay skipped: %s", e)
            warnings.append(f"热门板块「最新价」：东财全 A 列表快照失败，仍展示日线末根收盘价（{e}）")

    missing = [c for c in codes if c not in qmap]
    if missing and len(missing) <= 40:
        try:
            live_by = live_quote_fields_for_codes(missing)
            for sym in missing:
                row = live_by.get(sym) or {}
                p = row.get("live_last_price")
                if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                    continue
                chg = row.get("live_change_pct")
                chg_f = None
                if chg is not None and math.isfinite(float(chg)):
                    chg_f = round(float(chg), 2)
                qmap[sym] = {
                    "price": float(p),
                    "change_pct": chg_f,
                    "quote_date": row.get("live_quote_date"),
                    "source": "eastmoney_bid_ask",
                }
        except Exception as e:
            logger.debug("hot sectors live_quote fallback skipped: %s", e)

    return qmap, warnings


def _overlay_live_prices_on_hot_sectors_detail(
    sectors_detail: list[dict[str, Any]],
    *,
    selector_data_source: str,
) -> list[str]:
    """用实时/盘口快照覆盖技术面里的 latest_close（日线末根），返回追加 warnings。"""
    codes = _codes_from_hot_sectors_detail(sectors_detail)
    if not codes:
        return []
    qmap, warnings = _live_quote_map_for_hot_sector_codes(
        codes, selector_data_source=selector_data_source
    )
    sh_today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    updated = 0
    for bundle in sectors_detail:
        for st in bundle.get("stocks") or []:
            if not isinstance(st, dict):
                continue
            nk = normalize_code(str(st.get("code") or st.get("代码") or ""))
            if len(nk) != 6:
                continue
            row = qmap.get(nk)
            if not row:
                continue
            p = row.get("price")
            if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                continue
            bar_close = st.get("latest_close")
            if bar_close is not None:
                try:
                    st["latest_close_bar"] = round(float(bar_close), 4)
                except (TypeError, ValueError):
                    st["latest_close_bar"] = bar_close
            st["latest_close"] = round(float(p), 2)
            st["latest_price_source"] = row.get("source")
            qd = row.get("quote_date")
            if isinstance(qd, str) and len(qd) >= 10 and qd[4] == "-" and qd[7] == "-":
                st["latest_trade_date"] = qd[:10]
            else:
                st["latest_trade_date"] = sh_today
            chg = row.get("change_pct")
            if chg is not None and math.isfinite(float(chg)):
                st["spot_change_pct"] = round(float(chg), 2)
            updated += 1
    if updated < len(codes):
        n_miss = len(codes) - updated
        if not any("最新价" in w for w in warnings):
            warnings.append(
                f"热门板块「最新价」：{n_miss} 只未能合并实时快照，仍展示技术面计算用的日线末根收盘价。"
            )
    return warnings


_HOT_SECTOR_DS_LABELS: dict[str, str] = {
    "akshare": "AkShare（东财）",
    "mootdx": "mootdx（通达信）",
    "tushare": "TuShare",
    "hot_chain": "热门链",
    "baostock": "BaoStock",
}


def _raise_if_hot_sectors_cancelled() -> None:
    if is_cancelled("hot_sectors"):
        raise HTTPException(status_code=499, detail="热门板块任务已取消")


def _run_hot_pick_common(
    *,
    top_sectors: int,
    stocks_per_sector: int,
    board_type: str,
    exclude_st: bool,
    exclude_kcb: bool,
    exclude_cyb: bool = True,
    selector_data_source: str,
    use_sector_snapshot: bool,
    tushare_token: str | None = None,
    sort_by_trend_strength: bool = True,
    require_technical_pass: bool = False,
    exclude_overextended: bool = False,
    max_return_20d_pct: float = 22.0,
    enable_liquidity_filter: bool = False,
    min_avg_turnover_20d_100m: float = 2.5,
    pick_condition_groups: list[str] | None = None,
    ma5_stand_min_days: int = 3,
    capital_flow_lookback_days: int = 3,
    capital_min_positive_days: int = 2,
    ma5_exclude_st: bool = True,
    ma5_exclude_kcb: bool = True,
    ma5_exclude_cyb: bool = True,
    rising_3d_exclude_st: bool = True,
    rising_3d_exclude_kcb: bool = True,
    rising_3d_exclude_cyb: bool = True,
):
    clear("hot_sectors")
    cond_set = None
    try:
        from app.quant_stock_selector.hot_pick import normalize_pick_condition_groups

        cond_set = normalize_pick_condition_groups(pick_condition_groups)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    ds_key = (selector_data_source or "akshare").strip().lower()
    ds_label = _HOT_SECTOR_DS_LABELS.get(ds_key, ds_key)
    progress_log: list[str] = []
    logger.info(
        "hot_sectors pick start: route=%s (%s) conditions=%s",
        ds_key,
        ds_label,
        sorted(cond_set or []),
    )
    progress_log.append(
        f"热门板块筛选启动 路线={ds_key}（{ds_label}）条件={','.join(sorted(cond_set or []))}"
    )
    _raise_if_hot_sectors_cancelled()
    ds = _resolve_sector_datasource(selector_data_source, tushare_token=tushare_token)
    impl_name = type(ds).__name__
    logger.info(
        "hot_sectors: 首选数据源已就绪 route=%s impl=%s",
        ds_key,
        impl_name,
    )
    progress_log.append(f"首选数据源已就绪 路线={ds_key} 实现={impl_name}")
    board_key = (board_type or "all").strip().lower() or "all"
    snapshot_path = default_sector_snapshot_path(
        get_settings().data_dir, selector_data_source, board_key
    )
    rankings_override = None
    if use_sector_snapshot and snapshot_path.exists():
        try:
            rankings_override = load_sector_rankings_snapshot(snapshot_path)
            logger.info("hot_sectors: loaded sector snapshot %s", snapshot_path)
            progress_log.append(
                f"板块列表：读取本地快照 {snapshot_path.name}（{len(rankings_override)} 行）"
            )
        except Exception as exc:
            logger.warning("sector snapshot load failed, fallback to live fetch: %s", exc)
            progress_log.append(f"板块快照读取失败，改走实时拉取：{exc}")
    if rankings_override is None:
        logger.info("hot_sectors: live fetch sector rankings via %s (board=%s)", ds_key, board_key)
        progress_log.append(f"板块列表：实时拉取 路线={ds_key} board={board_key}")
        _raise_if_hot_sectors_cancelled()
        try:
            rankings_override = ds.get_sector_rankings(board_key)
            n_rows = len(rankings_override) if rankings_override is not None else 0
            logger.info(
                "hot_sectors: 板块列表已拉取 route=%s board=%s rows=%s",
                ds_key,
                board_key,
                n_rows,
            )
            progress_log.append(f"板块列表已拉取 {n_rows} 行")
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"热门板块列表拉取失败（当前路线：{ds_label}）：{exc}",
            ) from exc
        try:
            save_sector_rankings_snapshot(rankings_override, snapshot_path)
        except Exception as exc:
            logger.warning("sector snapshot save failed: %s", exc)
    _raise_if_hot_sectors_cancelled()
    hot = pick_from_hot_sectors(
        ds,
        top_sectors=top_sectors,
        stocks_per_sector=stocks_per_sector,
        board_type=board_key,
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        exclude_cyb=exclude_cyb,
        rankings_override=rankings_override,
        sort_by_trend_strength=sort_by_trend_strength,
        require_technical_pass=require_technical_pass,
        exclude_overextended=exclude_overextended,
        max_return_20d_pct=max_return_20d_pct,
        enable_liquidity_filter=enable_liquidity_filter,
        min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
        should_cancel=lambda: is_cancelled("hot_sectors"),
        pick_condition_groups=sorted(cond_set or []),
        ma5_stand_min_days=ma5_stand_min_days,
        capital_flow_lookback_days=capital_flow_lookback_days,
        capital_min_positive_days=capital_min_positive_days,
        ma5_exclude_st=ma5_exclude_st,
        ma5_exclude_kcb=ma5_exclude_kcb,
        ma5_exclude_cyb=ma5_exclude_cyb,
        rising_3d_exclude_st=rising_3d_exclude_st,
        rising_3d_exclude_kcb=rising_3d_exclude_kcb,
        rising_3d_exclude_cyb=rising_3d_exclude_cyb,
        progress_log=progress_log,
    )
    if hot.progress_log:
        progress_log = hot.progress_log
    _raise_if_hot_sectors_cancelled()
    if "sector_hot" in cond_set:
        price_warnings = _overlay_live_prices_on_hot_sectors_detail(
            hot.sectors_detail,
            selector_data_source=selector_data_source,
        )
        if price_warnings:
            hot.warnings.extend(price_warnings)
    if "ma5_capital" in cond_set and hot.ma5_capital_sectors_detail:
        mc_warnings = _overlay_live_prices_on_hot_sectors_detail(
            hot.ma5_capital_sectors_detail,
            selector_data_source=selector_data_source,
        )
        if mc_warnings:
            hot.warnings.extend(mc_warnings)
    if "rising_3d" in cond_set and hot.rising_3d_sectors_detail:
        r3_warnings = _overlay_live_prices_on_hot_sectors_detail(
            hot.rising_3d_sectors_detail,
            selector_data_source=selector_data_source,
        )
        if r3_warnings:
            hot.warnings.extend(r3_warnings)
    hot.progress_log = progress_log
    return hot


def _hot_sectors_preview_payload(
    *,
    top_sectors: int,
    stocks_per_sector: int,
    board_type: str,
    exclude_st: bool,
    exclude_kcb: bool,
    exclude_cyb: bool = True,
    selector_data_source: str,
    use_sector_snapshot: bool,
    tushare_token: str | None,
    sort_by_trend_strength: bool,
    require_technical_pass: bool,
    exclude_overextended: bool,
    max_return_20d_pct: float,
    enable_liquidity_filter: bool,
    min_avg_turnover_20d_100m: float,
    pick_condition_groups: list[str] | None = None,
    ma5_stand_min_days: int = 3,
    capital_flow_lookback_days: int = 3,
    capital_min_positive_days: int = 2,
    ma5_exclude_st: bool = True,
    ma5_exclude_kcb: bool = True,
    ma5_exclude_cyb: bool = True,
    rising_3d_exclude_st: bool = True,
    rising_3d_exclude_kcb: bool = True,
    rising_3d_exclude_cyb: bool = True,
) -> FillHotSectorsOut:
    hot = _run_hot_pick_common(
        top_sectors=top_sectors,
        stocks_per_sector=stocks_per_sector,
        board_type=(board_type or "all").strip().lower(),
        exclude_st=exclude_st,
        exclude_kcb=exclude_kcb,
        exclude_cyb=exclude_cyb,
        selector_data_source=selector_data_source,
        use_sector_snapshot=use_sector_snapshot,
        tushare_token=tushare_token,
        sort_by_trend_strength=sort_by_trend_strength,
        require_technical_pass=require_technical_pass,
        exclude_overextended=exclude_overextended,
        max_return_20d_pct=max_return_20d_pct,
        enable_liquidity_filter=enable_liquidity_filter,
        min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
        pick_condition_groups=pick_condition_groups,
        ma5_stand_min_days=ma5_stand_min_days,
        capital_flow_lookback_days=capital_flow_lookback_days,
        capital_min_positive_days=capital_min_positive_days,
        ma5_exclude_st=ma5_exclude_st,
        ma5_exclude_kcb=ma5_exclude_kcb,
        ma5_exclude_cyb=ma5_exclude_cyb,
        rising_3d_exclude_st=rising_3d_exclude_st,
        rising_3d_exclude_kcb=rising_3d_exclude_kcb,
        rising_3d_exclude_cyb=rising_3d_exclude_cyb,
    )
    return FillHotSectorsOut(
        sectors_detail=hot.sectors_detail,
        ma5_capital_sectors_detail=hot.ma5_capital_sectors_detail,
        rising_3d_sectors_detail=hot.rising_3d_sectors_detail,
        pick_condition_groups=list(hot.pick_condition_groups),
        summary=FillHotSectorsSummary(
            added=0,
            skipped_existing_manual=0,
            removed_auto=0,
            warnings=list(hot.warnings),
            progress_log=list(hot.progress_log or []),
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


# --- ① 入门必读 / meta（健康、鉴权、批量任务、热门快照） ---


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
    "/meta/ai-defaults",
    response_model=AiDefaultsOut,
    tags=["⑦ AI算法"],
    summary="大模型默认参数（不含密钥）",
    description="返回服务端 .env 中的 AI_API_BASE / AI_MODEL 等默认值，供⑦控制台预填；**不**返回密钥。",
)
def meta_ai_defaults():
    return AiDefaultsOut.model_validate(ai_defaults_payload())


@app.post(
    "/meta/cancel-batch",
    tags=["① 入门必读"],
    summary="中断进行中的批量任务",
    description="图形控制台「取消请求」调用。scopes 可为 ingest、signals、fundamentals、pre_refresh、hot_sectors、sector_screen 或 all。",
)
@limiter.limit("60/minute")
def meta_cancel_batch(
    request: Request,
    body: CancelBatchIn = Body(default_factory=CancelBatchIn),
    _: None = Depends(optional_api_key),
):
    touched = cancel_many(body.scopes or ["all"])
    return {
        "ok": True,
        "cancelled_scopes": touched,
        "known_scopes": sorted(KNOWN_SCOPES),
    }


@app.post(
    "/meta/clear-batch",
    tags=["① 入门必读"],
    summary="清除批量任务的中断标记",
    description="新一批请求开始前由控制台调用，避免上次「取消」标记影响本次任务。scopes 与 cancel-batch 相同。",
)
@limiter.limit("60/minute")
def meta_clear_batch(
    request: Request,
    body: CancelBatchIn = Body(default_factory=CancelBatchIn),
    _: None = Depends(optional_api_key),
):
    clear_many(body.scopes or list(KNOWN_SCOPES))
    return {"ok": True, "cleared_scopes": body.scopes or list(KNOWN_SCOPES)}


@app.get(
    "/meta/ingest-batch-status",
    tags=["① 入门必读"],
    summary="批量拉取日线任务是否进行中",
    description="② 控制台在刷新页面后据此恢复「正在拉取」按钮状态；`active=true` 表示服务端仍在处理 `/ingest/update` 批量任务。",
)
def meta_ingest_batch_status(_: None = Depends(optional_api_key)):
    return ingest_batch_status()


@app.get(
    "/meta/watchlist-spot-refresh-status",
    tags=["① 入门必读"],
    summary="刷新列表（现价）任务进度",
    description="② 控制台「刷新列表」轮询本接口，在按钮上显示成功数/总数（如 3/20）。",
)
def meta_watchlist_spot_refresh_status(_: None = Depends(optional_api_key)):
    return watchlist_spot_job_status()


@app.get(
    "/meta/watchlist-add-dates",
    response_model=WatchlistAddDatesOut,
    tags=["① 入门必读"],
    summary="有自选加入记录的日期列表",
    description="返回东八区今日及 watchlist_add_log 中出现过记录的日期（新→旧），供②日期筛选。",
)
def meta_watchlist_add_dates(_: None = Depends(optional_api_key)):
    with session_scope() as s:
        dates = list_watchlist_add_dates(s)
    return WatchlistAddDatesOut(shanghai_today=shanghai_today_ymd(), dates=dates)


@app.get(
    "/meta/symbols-batch-status",
    tags=["① 入门必读"],
    summary="按只批量任务进度（③④/扩展因子等）",
    description=(
        "scope 为 ingest（③ 更新现价/扩展因子，skip_bars）、signals、"
        "fundamentals、backfill_close；active=true 时 done/total 供按钮显示进度。"
        "② 拉日线进度请用 GET /meta/ingest-batch-status。"
    ),
)
def meta_symbols_batch_status(
    scope: str = Query(
        ...,
        description="ingest | signals | fundamentals | backfill_close",
    ),
    _: None = Depends(optional_api_key),
):
    sc = str(scope or "").strip().lower()
    if sc not in ("ingest", "signals", "fundamentals", "backfill_close"):
        raise HTTPException(
            status_code=400,
            detail="scope 须为 ingest、signals、fundamentals 或 backfill_close",
        )
    return symbols_batch_status(sc)


@app.get(
    "/meta/symbols-batch-partial-results",
    tags=["① 入门必读"],
    summary="批量任务已完成结果的增量拉取",
    description=(
        "供控制台在批量任务进行中增量渲染结果表。"
        "传入 offset（已拉取条数），返回从该位置起的新结果。"
    ),
)
def meta_symbols_batch_partial_results(
    scope: str = Query(
        ...,
        description="ingest | signals | fundamentals | backfill_close",
    ),
    offset: int = Query(0, ge=0, description="从第几条开始取（已拉取条数）"),
    _: None = Depends(optional_api_key),
):
    sc = str(scope or "").strip().lower()
    if sc not in ("ingest", "signals", "fundamentals", "backfill_close"):
        raise HTTPException(
            status_code=400,
            detail="scope 须为 ingest、signals、fundamentals 或 backfill_close",
        )
    return symbols_batch_partial_results(sc, offset)


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
    "/meta/stock-knowledge",
    response_model=StockKnowledgeOut,
    tags=["⑥ 金融从零学起"],
    summary="金融从零学起（分阶段系统课程）",
    description="""
返回 **8 阶段、30+ 节** 结构化金融课程：金融本质、时间价值、货币银行、宏观经济学、
金融市场、公司财务三表与估值、投资组合与行为金融、A 股实务及与本工具配合。

每节含摘要、正文、关键词、练手建议与自测题；`roadmap` 字段为推荐学习路线。

**无需** API Key。阅读进度由浏览器 localStorage 记录，服务端不存储。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def meta_stock_knowledge(request: Request):
    """⑥ 股票知识学习静态内容。"""
    return stock_knowledge_payload()


@app.get(
    "/meta/disclaimer",
    response_model=DisclaimerOut,
    tags=["⑥ 金融从零学起"],
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
            "若实盘：在持仓记录中标注计划仓位 % 与是否按计划执行，便于复盘",
        ],
        related_doc_files=["docs/SELF_USE_GUIDE.md"],
        journal_api="/journal",
        holdings_api="/holdings",
        example_risk_policy_file="examples/risk_policy.example.json",
    )


@app.get(
    "/meta/hot-market-snapshot",
    response_model=HotMarketSnapshotFileOut,
    tags=["③ 更新行情数据"],
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

- **个股热门**：各源在按涨幅/人气排序后，**仅保留沪、深主板**（60/000–003 段，不含科创 688/689、创业板 300/301、北交所等），再取前 **top_stocks**（默认 100）。
- **新浪成功时**：板块与个股均来自新浪公开接口（个股为上述主板口径）。
- **腾讯步**：个股为腾讯财经 A 股排行（客户端按 `zdf` 排序）；因腾讯无对等板块全表，板块由东财补充（响应内 `sector_source` / `notes` 会说明）。
- **东财 / akshare 步**：板块为东财；个股为人气榜筛主板（与「涨幅序」不同，见 `notes`）。

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


def _build_watchlist_items(
    s,
    rows: list[WatchlistRow],
    *,
    force_spot_refresh: bool = False,
) -> list[WatchlistItem]:
    """批量附带本地 bars 摘要；refresh_spot 时联网拉现价，否则读现价缓存（与上次刷新列表一致）。"""
    symbols = [r.symbol for r in rows]
    route = get_settings().ingest_data_source
    bar_by = watchlist_bar_fields_for_session(s, symbols, data_source=route)
    live_by: dict[str, dict] = {}
    if symbols:
        if force_spot_refresh:
            if watchlist_spot_job_status().get("active"):
                logger.info(
                    "watchlist spot refresh already active (%s/%s); list without duplicate job",
                    watchlist_spot_job_status().get("done"),
                    watchlist_spot_job_status().get("total"),
                )
                live_by = live_quote_fields_for_codes_enhanced(
                    symbols, data_source=route, force_spot_refresh=False
                )
            else:
                live_by = _live_spot_refresh_with_progress(symbols, route)
        else:
            live_by = live_quote_fields_for_codes_enhanced(
                symbols, data_source=route, force_spot_refresh=False
            )
    latest_added_by = latest_added_at_for_symbols(s, symbols) if symbols else {}
    return _watchlist_items_from_parts(
        rows,
        bar_by,
        live_by,
        latest_added_at_by_symbol=latest_added_by,
    )


def _watchlist_item_with_add_meta(
    item: WatchlistItem, *, added_at: str, in_pool: bool
) -> WatchlistItem:
    d = item.model_dump()
    d["watchlist_added_at"] = added_at
    d["in_watchlist_pool"] = in_pool
    return WatchlistItem(**d)


def _build_watchlist_items_for_first_ingest_range(
    s,
    *,
    range_start: str | None = None,
    range_end: str | None = None,
    symbols_filter: list[str] | None = None,
    force_spot_refresh: bool = False,
) -> list[WatchlistItem]:
    """按东八区「首次入库」日期闭区间筛选自选池；起止皆空则返回全池。"""
    from app.ingest import first_ingest_ymd_in_range, normalize_ingest_date_range

    start, end = normalize_ingest_date_range(range_start, range_end)
    q = select(WatchlistRow)
    if symbols_filter:
        q = q.where(WatchlistRow.symbol.in_(symbols_filter))
    rows = list(s.execute(q.order_by(WatchlistRow.id.asc())).scalars().all())
    if symbols_filter:
        by_sym = {r.symbol: r for r in rows}
        rows = [by_sym[sym] for sym in symbols_filter if sym in by_sym]
    if not rows:
        return []
    items = _build_watchlist_items(s, rows, force_spot_refresh=force_spot_refresh)
    if not start and not end:
        items.sort(key=lambda x: (x.bars_first_ingested_at or "", x.symbol))
        return items
    out = [
        it
        for it in items
        if first_ingest_ymd_in_range(
            it.bars_first_ingested_at, range_start=start, range_end=end
        )
    ]
    out.sort(key=lambda x: (x.bars_first_ingested_at or "", x.symbol))
    return out


def _watchlist_item_from_add_log_row(
    log_row,
    pool_row: WatchlistRow | None = None,
) -> WatchlistItem:
    """按加入日志行（含写入时快照）构建列表项，不联网拉现价。"""
    sym = str(log_row.symbol or "").strip()
    added_at = (log_row.added_at or "").strip()
    snap = log_row_snapshot_fields(log_row)
    if pool_row is not None:
        name = (pool_row.name or "").strip() or (log_row.name or "").strip()
        origin = pool_row.origin or log_row.origin or WATCHLIST_ORIGIN_MANUAL
        in_pool = True
    else:
        name = (log_row.name or "").strip()
        origin = log_row.origin or WATCHLIST_ORIGIN_MANUAL
        in_pool = False
    return WatchlistItem(
        symbol=sym,
        name=name,
        origin=origin,
        watchlist_added_at=added_at,
        in_watchlist_pool=in_pool,
        **snap,
    )


def _watchlist_items_from_add_log_rows(
    s,
    log_rows: list,
    *,
    force_spot_refresh: bool = False,
) -> list[WatchlistItem]:
    """按加入日志行筛顺序；展示字段用当前 SQLite bars + 现价（查询用缓存，刷新列表联网）。"""
    if not log_rows:
        return []
    syms = [str(r.symbol or "").strip() for r in log_rows]
    pool_rows = {
        r.symbol: r
        for r in s.execute(select(WatchlistRow).where(WatchlistRow.symbol.in_(syms))).scalars().all()
    }
    pool_list = [pool_rows[sym] for sym in syms if sym in pool_rows]
    built: dict[str, WatchlistItem] = {}
    if pool_list:
        for item in _build_watchlist_items(s, pool_list, force_spot_refresh=force_spot_refresh):
            built[item.symbol] = item
    out: list[WatchlistItem] = []
    for log_row in log_rows:
        sym = str(log_row.symbol or "").strip()
        added_at = (log_row.added_at or "").strip()
        if sym in built:
            out.append(_watchlist_item_with_add_meta(built[sym], added_at=added_at, in_pool=True))
        else:
            out.append(_watchlist_item_from_add_log_row(log_row, pool_row=None))
    return out


def _build_watchlist_items_for_added_date(
    s,
    added_date: str,
    *,
    force_spot_refresh: bool = False,
) -> list[WatchlistItem]:
    """按加入日志某日筛选；顺序为当日首次加入时间。"""
    return _watchlist_items_from_add_log_rows(
        s,
        entries_added_on_date(s, added_date),
        force_spot_refresh=force_spot_refresh,
    )


def _build_watchlist_items_for_added_range(
    s,
    *,
    range_start: str | None = None,
    range_end: str | None = None,
    force_spot_refresh: bool = False,
) -> list[WatchlistItem]:
    """按东八区「加入自选」日期闭区间筛选；各日历史互不影响。"""
    return _watchlist_items_from_add_log_rows(
        s,
        entries_added_in_range(s, range_start=range_start, range_end=range_end),
        force_spot_refresh=force_spot_refresh,
    )


def _watchlist_items_from_parts(
    rows: list[WatchlistRow],
    bar_by: dict[str, dict],
    live_by: dict[str, dict],
    *,
    latest_added_at_by_symbol: dict[str, str] | None = None,
) -> list[WatchlistItem]:
    out: list[WatchlistItem] = []
    latest_map = latest_added_at_by_symbol or {}
    for r in rows:
        meta = bar_by.get(r.symbol, {})
        live = live_by.get(r.symbol) or {}
        out.append(
            WatchlistItem(
                symbol=r.symbol,
                name=(r.name or "").strip(),
                origin=r.origin or WATCHLIST_ORIGIN_MANUAL,
                spot_last_price=live.get("live_last_price"),
                spot_change_pct=live.get("live_change_pct"),
                spot_quote_date=live.get("live_quote_date"),
                watchlist_added_at=latest_map.get(r.symbol),
                **meta,
            )
        )
    return out


def _live_spot_refresh_with_progress(
    symbols: list[str],
    route: str,
    *,
    chunk_size: int = 8,
) -> dict[str, dict]:
    """分块拉现价并更新 watchlist_spot_job（成功=拿到有效现价）。"""
    if watchlist_spot_job_status().get("active"):
        raise HTTPException(
            status_code=409,
            detail="已有「刷新列表」任务进行中，请稍候或点「取消请求」",
        )
    clear("watchlist_spot")
    batch_gen = watchlist_spot_job_start(len(symbols))
    live_by: dict[str, dict] = {}
    cancelled = False
    try:
        for i in range(0, len(symbols), chunk_size):
            if watchlist_spot_job_should_cancel(batch_gen):
                cancelled = True
                logger.info("watchlist spot refresh cancelled at chunk %s", i)
                break
            chunk = symbols[i : i + chunk_size]
            for sym in chunk:
                watchlist_spot_job_set_current(sym)
            try:
                chunk_live = live_quote_fields_for_codes_enhanced(
                    chunk, data_source=route, force_spot_refresh=True
                )
            except Exception as e:
                logger.warning("watchlist spot chunk failed: %s", e)
                chunk_live = {}
            live_by.update(chunk_live)
            for sym in chunk:
                got = _live_row_has_price(chunk_live.get(sym))
                watchlist_spot_job_tick(sym, got_spot=got)
    finally:
        watchlist_spot_job_finish(
            cancelled=cancelled or watchlist_spot_job_should_cancel(batch_gen)
        )
    return live_by


def _watchlist_item_with_bars(s, r: WatchlistRow) -> WatchlistItem:
    items = _build_watchlist_items(s, [r])
    return items[0]


def _auto_ingest_watchlist_kline(
    sym: str,
    *,
    ingest_days: int,
    data_source: str | None,
) -> dict[str, Any]:
    """添加自选后拉取近若干日历日的日线并 upsert 到 bars。"""
    days = max(7, min(120, int(ingest_days)))
    today = date.today()
    start = today - timedelta(days=days)
    primary = resolve_data_source(data_source)
    routes: list[str] = [primary]
    if primary != "auto":
        routes.append("auto")
    errors: list[str] = []
    for route in routes:
        try:
            rec = ingest_symbol_range(
                sym,
                range_start=start,
                range_end=today,
                data_source=route,
            )
            n = int(rec.get("rows_upserted") or 0)
            if n <= 0:
                msg = f"路线 {route} 未收到日线（返回 0 条）"
                errors.append(msg)
                logger.warning("watchlist auto ingest %s: %s", sym, msg)
                continue
            return {
                "ok": True,
                "rows_upserted": n,
                "start": rec.get("start"),
                "end": rec.get("end"),
                "data_source": rec.get("data_source"),
                "provider": rec.get("provider"),
                "ingest_rec": rec,
                "route_used": route,
            }
        except Exception as e:
            err = str(e)
            errors.append(f"{route}: {err}")
            logger.warning("watchlist auto ingest %s via %s: %s", sym, route, e)
    return {"ok": False, "error": "；".join(errors) if errors else "拉取失败"}


def _watchlist_item_after_ingest(
    s,
    r: WatchlistRow,
    ingest: dict[str, Any] | None,
) -> WatchlistItem:
    item = _watchlist_item_with_bars(s, r)
    if ingest is None:
        return item
    d = item.model_dump()
    d["kline_ingest_ok"] = bool(ingest.get("ok"))
    d["kline_ingest_error"] = None if ingest.get("ok") else str(ingest.get("error") or "拉取失败")
    rows = ingest.get("rows_upserted")
    d["kline_ingest_rows"] = int(rows) if ingest.get("ok") and rows is not None else None
    return WatchlistItem(**d)


def _watchlist_kline_refetch_row(
    session,
    sym: str,
    ingest_rec: dict[str, Any],
    *,
    data_source: str | None,
) -> dict[str, Any]:
    """将 ingest 摘要与本地 bars 展示字段合并，供②列表更新（不拉现价、不同步前向展望）。"""
    bar = watchlist_bar_fields_for_session(session, [sym], data_source=data_source).get(sym, {})
    out: dict[str, Any] = {
        "symbol": sym,
        "rows_upserted": ingest_rec.get("rows_upserted"),
        "today_bar_backfill": ingest_rec.get("today_bar_backfill"),
    }
    out.update(bar)
    last_td = bar.get("bars_last_trade_date") or ingest_rec.get("last_trade_date")
    if last_td:
        out["last_trade_date"] = last_td
    if bar.get("last_close") is None and ingest_rec.get("last_close") is not None:
        out["last_close"] = ingest_rec.get("last_close")
    return out


# --- ② 管理自选股票 ---


@app.get(
    "/watchlist",
    response_model=list[WatchlistItem],
    tags=["② 管理自选股票"],
    summary="列出当前已添加的股票",
    description="""
返回自选池里**所有**股票代码列表；每项附带本地 **bars** 摘要：**bars_first_ingested_at** / **bars_last_ingested_at**（首次/最近入库 UTC 时间）、**last_close**（最新日线收盘价）、**last_daily_close_label**（最后交易日收盘说明），以及 **spot_last_price** / **spot_change_pct**（东财单股/列表快照；东财失败时用通达信批量行情兜底，非交易所 tick）。

- Query **`refresh_spot=true`**（控制台「刷新列表」会带上）：跳过 spot 内存缓存并重新拉现价。
- Query **`first_ingest_from` / `first_ingest_to`**（东八区 YYYY-MM-DD，闭区间）：按**首次入库**日期筛选；可只填起始或结束。皆省略且 `pool=false` 时返回**自选池全部**标的。
- Query **`added_from` / `added_to`**（东八区 YYYY-MM-DD，闭区间）：按**加入自选**日志日期筛选；各日历史互不影响，返回该区间内对应日的加入时间（非最近一次）。
- Query **`added_date=YYYY-MM-DD`**：兼容旧参数，等价于 `first_ingest_from=to=该日`（首次入库，非加入自选）。
- Query **`symbols=600519&symbols=000001`**（可重复）：仅返回并刷新指定代码子集（须在自选池）。
- Query **`pool=true`**：返回完整自选池（等同不按日期筛选）。
- 若配置了 `API_KEY`，请先点右上角 **Authorize**。
- 若列表为空，下一步请用 `POST /watchlist` 添加。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_list(
    request: Request,
    response: Response,
    refresh_spot: bool = Query(
        False,
        description="为 true 时强制重新拉现价（东财→通达信兜底），控制台「刷新列表」应传 true",
    ),
    added_date: str | None = Query(
        None,
        description="（兼容）东八区 YYYY-MM-DD；等价 first_ingest_from=to=该日",
    ),
    first_ingest_from: str | None = Query(
        None,
        description="东八区起始日期 YYYY-MM-DD（首次入库 ≥ 该日）",
    ),
    first_ingest_to: str | None = Query(
        None,
        description="东八区结束日期 YYYY-MM-DD（首次入库 ≤ 该日）",
    ),
    added_from: str | None = Query(
        None,
        description="东八区起始日期 YYYY-MM-DD（加入自选 ≥ 该日）",
    ),
    added_to: str | None = Query(
        None,
        description="东八区结束日期 YYYY-MM-DD（加入自选 ≤ 该日）",
    ),
    symbols: list[str] | None = Query(
        None,
        description="仅处理所列 6 位代码（须在自选池）；可重复传参",
    ),
    pool: bool = Query(
        False,
        description="为 true 时返回完整自选池，不按首次入库日期筛选",
    ),
    _: None = Depends(optional_api_key),
):
    """列出自选；可按首次入库日期区间筛选或返回全池。"""
    with session_scope() as s:
        rows_all = list(s.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all())
        rows_by_sym = {r.symbol: r for r in rows_all}
        subset_syms: list[str] | None = None
        if symbols:
            subset_syms = []
            seen_sub: set[str] = set()
            for raw in symbols:
                try:
                    sym = normalize_symbol(str(raw))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                if sym in seen_sub:
                    continue
                seen_sub.add(sym)
                if sym in rows_by_sym:
                    subset_syms.append(sym)
            if not subset_syms:
                response.headers["X-Quant-Watchlist-View"] = "subset_empty"
                return []
        if not pool:
            added_start_raw = (added_from or "").strip()
            added_end_raw = (added_to or "").strip()
            if added_start_raw or added_end_raw:
                try:
                    from app.ingest import normalize_ingest_date_range

                    added_start_n, added_end_n = normalize_ingest_date_range(
                        added_start_raw, added_end_raw
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                items = _build_watchlist_items_for_added_range(
                    s,
                    range_start=added_start_n,
                    range_end=added_end_n,
                    force_spot_refresh=refresh_spot,
                )
                if subset_syms:
                    allow = set(subset_syms)
                    items = [it for it in items if it.symbol in allow]
                if added_start_n:
                    response.headers["X-Quant-Watchlist-Added-From"] = added_start_n
                if added_end_n:
                    response.headers["X-Quant-Watchlist-Added-To"] = added_end_n
                response.headers["X-Quant-Watchlist-Add-Count"] = str(len(items))
                response.headers["X-Quant-Watchlist-View"] = "added_range"
                return items
            ad = parse_added_date_param(added_date) if added_date else None
            start_raw = (first_ingest_from or "").strip() or (ad if ad else None)
            end_raw = (first_ingest_to or "").strip() or (ad if ad else None)
            if ad and not (first_ingest_from or first_ingest_to):
                start_raw = end_raw = ad
            if start_raw or end_raw:
                try:
                    from app.ingest import normalize_ingest_date_range

                    start_n, end_n = normalize_ingest_date_range(start_raw, end_raw)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                items = _build_watchlist_items_for_first_ingest_range(
                    s,
                    range_start=start_n,
                    range_end=end_n,
                    symbols_filter=subset_syms,
                    force_spot_refresh=refresh_spot,
                )
                if start_n:
                    response.headers["X-Quant-Watchlist-First-Ingest-From"] = start_n
                if end_n:
                    response.headers["X-Quant-Watchlist-First-Ingest-To"] = end_n
                response.headers["X-Quant-Watchlist-Add-Count"] = str(len(items))
                response.headers["X-Quant-Watchlist-View"] = "first_ingest_range"
                return items
        if subset_syms:
            rows = [rows_by_sym[sym] for sym in subset_syms if sym in rows_by_sym]
        else:
            rows = rows_all
        missing = [r.symbol for r in rows if not (r.name or "").strip()]
        if missing:
            by_sym = {r.symbol: r for r in rows}
            for sym, nm in fetch_stock_names_map(missing).items():
                r = by_sym.get(sym)
                if r is not None and not (r.name or "").strip():
                    r.name = nm
        response.headers["X-Quant-Watchlist-View"] = "pool"
        return _build_watchlist_items(s, rows, force_spot_refresh=refresh_spot)


@app.post(
    "/watchlist/refetch-kline",
    response_model=WatchlistRefetchKlineOut,
    tags=["② 管理自选股票"],
    summary="按日期区间重新拉取自选日线（仅入库 + 更新列表展示字段）",
    description="""
② 控制台「**按日期拉取日线**」专用：严格按 Body **`start_date` + `end_date`** 闭区间联网拉 K 线写入本地 **bars**。

- **`symbols`** 省略或空：处理**自选池全部**；传列表则仅处理其中且在池内的代码。
- **`start_date` / `end_date`**：须同时传入；**不会**把结束日自动抬到今天，**不会**补写当日 bar 或联网补区间外 K 线。
- **`data_source`**：行情路线，与 ③ 一致。
- **不做**：逐只拉现价、扩展因子、前向展望同步等 ③ 批量更新附带操作。
- 进度与取消：与 `POST /ingest/update` 共用 `GET /meta/ingest-batch-status` 与 `POST /meta/cancel-batch`（scope=`ingest`）。
""",
)
@limiter.limit("20/minute")
def watchlist_refetch_kline(
    request: Request,
    body: WatchlistRefetchKlineIn = Body(default_factory=WatchlistRefetchKlineIn),
    _: None = Depends(optional_api_key),
):
    """按日期区间为自选拉日线；仅 upsert bars 并回传自选展示字段。"""
    with session_scope() as s:
        orm_rows = list(s.execute(select(WatchlistRow)).scalars().all())
        wl_pairs = [(r.symbol, (r.name or "").strip()) for r in orm_rows]
    if not wl_pairs:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
    symbols, _, suffix_errs = _watchlist_subset_symbols(body.symbols, wl_pairs)
    from app.ingest import shanghai_today_date

    st, en = body.start_date, body.end_date
    sh_today = shanghai_today_date()
    if not st or not en:
        raise HTTPException(
            status_code=400,
            detail="须同时传入 start_date 与 end_date（闭区间拉取 K 线）",
        )
    if st > en:
        st, en = en, st
    if en > sh_today:
        raise HTTPException(status_code=400, detail="结束日期不能晚于东八区今日")
    if st > sh_today:
        raise HTTPException(status_code=400, detail="开始日期不能晚于东八区今日")
    ds = body.data_source.value if body.data_source is not None else None
    resolved_ds = ds if ds is not None else get_settings().ingest_data_source
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    if ingest_batch_status().get("active") or symbols_batch_status("ingest").get("active"):
        raise HTTPException(
            status_code=409,
            detail="已有批量行情任务进行中，请等待结束或取消后再试",
        )
    if not symbols and not suffix_errs:
        raise HTTPException(status_code=400, detail="没有可拉取的有效自选代码")
    clear("ingest")
    batch_gen = ingest_batch_start(len(symbols) + len(suffix_errs))
    results: list[dict[str, Any]] = []
    cancelled = False

    for i, sym in enumerate(symbols):
        if ingest_batch_should_cancel(batch_gen):
            cancelled = True
            logger.info("watchlist refetch kline cancelled at %s", sym)
            break
        ingest_batch_set_current(sym)
        if i > 0 and pause > 0:
            time.sleep(pause)
            if ingest_batch_should_cancel(batch_gen):
                cancelled = True
                break
        try:
            ingest_rec = ingest_symbol_range(
                sym,
                range_start=st,
                range_end=en,
                data_source=ds,
                strict_range=True,
            )
            if ingest_batch_should_cancel(batch_gen):
                cancelled = True
                break
            with session_scope() as s:
                row_out = _watchlist_kline_refetch_row(
                    s, sym, ingest_rec, data_source=resolved_ds
                )
            results.append(row_out)
        except ValueError as e:
            results.append({"symbol": sym, "error": str(e)})
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})
        ingest_batch_tick(sym)

    for er in suffix_errs:
        if ingest_batch_should_cancel(batch_gen):
            cancelled = True
            break
        results.append(er)
        sym_e = er.get("symbol")
        if sym_e:
            ingest_batch_tick(str(sym_e))

    ingest_batch_finish(cancelled=cancelled or ingest_batch_should_cancel(batch_gen))
    out_rows = [WatchlistRefetchKlineResultRow(**r) for r in results]
    return WatchlistRefetchKlineOut(
        cancelled=cancelled,
        start_date=st,
        end_date=en,
        ingest_data_source=resolved_ds,
        results=out_rows,
    )


@app.post(
    "/watchlist/backfill-today-close",
    response_model=WatchlistTodayCloseBackfillOut,
    tags=["② 管理自选股票"],
    summary="一键用现价补写当日收盘 K 线",
    description="""
对自选标的用**联网现价**补写或更新东八区**今日**日线 bar，② 列表「当日收盘」即可显示（不再仅「待入库」）。

- 与 ingest 拉日线、③ 刷新现价、②「刷新列表」**并存**；本接口只写 bars 中今日一根，不替代完整日线拉取。
- **`allow_intraday=true`**（默认）：盘中也可用现价补写，值为参考价。
- **`force_refresh=true`**（默认）：已有今日 bar 时仍用最新现价覆盖。
- **`symbols`** 省略或空：处理当前自选池全部代码（控制台「现价补当日收盘」仅传已勾选子集）。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_backfill_today_close(
    body: WatchlistTodayCloseBackfillIn,
    request: Request,
    _: None = Depends(optional_api_key),
):
    ds = body.data_source.value if body.data_source is not None else None
    with session_scope() as s:
        if body.symbols:
            want: list[str] = []
            for raw in body.symbols:
                try:
                    want.append(normalize_symbol(raw))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            rows = (
                s.execute(select(WatchlistRow).where(WatchlistRow.symbol.in_(want)))
                .scalars()
                .all()
            )
            found = {r.symbol for r in rows}
            symbols = [sym for sym in want if sym in found]
        else:
            rows = s.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all()
            symbols = [r.symbol for r in rows]
        if not symbols:
            return WatchlistTodayCloseBackfillOut()
        clear("backfill_close")
        symbols_batch_start("backfill_close", len(symbols))
        raw_results: list[dict[str, Any]] = []
        cancelled = False
        pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
        try:
            for i, sym in enumerate(symbols):
                if is_cancelled("backfill_close"):
                    cancelled = True
                    logger.info("backfill today close cancelled before %s", sym)
                    break
                symbols_batch_set_current("backfill_close", sym)
                if i > 0 and pause > 0:
                    time.sleep(pause)
                try:
                    raw_results.append(
                        backfill_today_bar_from_live(
                            sym,
                            trade_date=body.trade_date,
                            data_source=ds,
                            allow_intraday=body.allow_intraday,
                            force_refresh=body.force_refresh,
                        )
                    )
                except Exception as e:
                    logger.warning("backfill today close %s: %s", sym, e)
                    raw_results.append(
                        {
                            "ok": False,
                            "symbol": sym,
                            "rows_upserted": 0,
                            "error": str(e),
                            "skipped_reason": "exception",
                        }
                    )
                symbols_batch_tick("backfill_close", sym)
        finally:
            symbols_batch_finish("backfill_close", cancelled=cancelled)
        result_rows = [WatchlistTodayCloseBackfillRow(**r) for r in raw_results]
        updated = sum(1 for r in result_rows if r.rows_upserted > 0)
        skipped = sum(
            1
            for r in result_rows
            if r.rows_upserted <= 0 and r.ok and r.skipped_reason
        )
        failed = sum(1 for r in result_rows if not r.ok and r.error)
        sym_set = set(symbols)
        pool_rows = (
            s.execute(select(WatchlistRow).where(WatchlistRow.symbol.in_(sym_set)))
            .scalars()
            .all()
        )
        items = _build_watchlist_items(s, pool_rows, force_spot_refresh=True)
        return WatchlistTodayCloseBackfillOut(
            results=result_rows,
            items=items,
            updated_count=updated,
            skipped_count=skipped,
            failed_count=failed,
        )


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

已存在相同代码时不会报错；**每次添加**（含已在池中的代码）均记一条加入日志，按东八区加入时间查询，不影响历史其它日期的加入记录。

默认 **`auto_ingest_kline=true`**：添加成功后自动联网拉取近 **30 个日历日**（可用 `ingest_days` 或环境变量 `WATCHLIST_AUTO_INGEST_DAYS` 调整）的日线写入本地 `bars`，② 列表即可显示「最近入库 / 收盘参考」。可选 **`data_source`** 与 ③ 行情路线一致。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_add(body: WatchlistIn, request: Request, _: None = Depends(optional_api_key)):
    """添加自选；代码规范化后若已存在则幂等返回该标的；每次均记加入日志；默认自动拉取近一月日线。"""
    try:
        sym = normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    days = body.ingest_days if body.ingest_days is not None else get_settings().watchlist_auto_ingest_days
    ds = body.data_source.value if body.data_source is not None else None
    ingest: dict[str, Any] | None = None
    nm_for_log = ""
    with session_scope() as s:
        existing = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
        if existing:
            if existing.origin != WATCHLIST_ORIGIN_MANUAL:
                existing.origin = WATCHLIST_ORIGIN_MANUAL
            if not (existing.name or "").strip():
                existing.name = fetch_stock_name(sym) or ""
            nm_for_log = (existing.name or "").strip() or fetch_stock_name(sym) or ""
        else:
            nm = fetch_stock_name(sym) or ""
            nm_for_log = nm
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_MANUAL, name=nm))
    if body.auto_ingest_kline:
        ingest = _auto_ingest_watchlist_kline(sym, ingest_days=days, data_source=ds)
        if ingest.get("ok"):
            try:
                meta = ingest.get("ingest_rec") or {}
                sync_after_ingest(
                    [sym],
                    horizon=DEFAULT_HORIZON,
                    ingest_meta_by_sym={sym: meta} if meta else None,
                )
            except Exception as e:
                logger.warning("forward outlook sync after watchlist add %s: %s", sym, e)
    with session_scope() as s:
        record_watchlist_adds_with_snapshot(
            s, [(sym, nm_for_log, WATCHLIST_ORIGIN_MANUAL)]
        )
        # autoflush=False：须 flush 后 latest_added_at_for_symbols 才能读到本条加入日志
        s.flush()
        row = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one()
        return _watchlist_item_after_ingest(s, row, ingest)


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
    "/watchlist/batch-add",
    response_model=WatchlistBatchAddOut,
    tags=["② 管理自选股票"],
    summary="批量加入自选（手动，不删除其它条目）",
    description="""
将多只股票一次性写入自选，**origin=manual**。已在池中的代码会标为手动（不删热门/量化自动以外的其它代码）。

每次成功写入（含已在池中、仅更新为手动的）都会在 **watchlist_add_log** 记一条加入日志，供②按日查询。

用于热门板块填充/预览结果中勾选后「加入自选」。**仅写入自选库与基础加入日志**，不联网拉 K 线、不补 bars/现价快照（请传 `log_snapshot=false`，亦为默认值）。无效代码计入 skipped 与 warnings。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_batch_add(
    request: Request,
    body: WatchlistBatchAddIn = Body(...),
    _: None = Depends(optional_api_key),
):
    added = 0
    updated = 0
    skipped = 0
    warnings: list[str] = []
    seen: set[str] = set()
    log_pairs: list[tuple[str, str, str]] = []
    with session_scope() as s:
        for row in body.stocks:
            try:
                sym = normalize_symbol(row.code)
            except ValueError:
                skipped += 1
                warnings.append(f"跳过无效代码：{row.code!r}")
                continue
            if sym in seen:
                continue
            seen.add(sym)
            nm_in = (row.name or "").strip()
            existing = s.execute(
                select(WatchlistRow).where(WatchlistRow.symbol == sym)
            ).scalar_one_or_none()
            if existing:
                if existing.origin != WATCHLIST_ORIGIN_MANUAL:
                    existing.origin = WATCHLIST_ORIGIN_MANUAL
                if nm_in and not (existing.name or "").strip():
                    existing.name = nm_in[:64]
                elif not (existing.name or "").strip():
                    existing.name = (fetch_stock_name(sym) or "")[:64]
                use_nm = nm_in or (existing.name or "").strip() or (fetch_stock_name(sym) or "")
                log_pairs.append((sym, use_nm[:64], WATCHLIST_ORIGIN_MANUAL))
                updated += 1
            else:
                use_nm = nm_in or (fetch_stock_name(sym) or "")
                s.add(
                    WatchlistRow(
                        symbol=sym,
                        origin=WATCHLIST_ORIGIN_MANUAL,
                        name=use_nm[:64],
                    )
                )
                log_pairs.append((sym, use_nm[:64], WATCHLIST_ORIGIN_MANUAL))
                added += 1
        if log_pairs:
            if body.log_snapshot:
                record_watchlist_adds_with_snapshot(s, log_pairs)
            else:
                for sym, nm, origin in log_pairs:
                    record_watchlist_add(
                        s, symbol=sym, name=nm, origin=origin, snapshot=None
                    )
    return WatchlistBatchAddOut(
        added=added,
        updated=updated,
        skipped=skipped,
        warnings=warnings,
    )


@app.post(
    "/watchlist/delete-all",
    response_model=WatchlistDeleteAllOut,
    tags=["② 管理自选股票"],
    summary="一次性清空自选（全部或仅自动池）",
    description="""
- **scope=all**：删除自选池中的**全部**记录（含手动、热门自动、量化自动）。
- **scope=auto**：仅删除 **auto_hot** 与 **auto_quant**，**保留**手动自选。

无需勾选表格行；与「删除所选」互补。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_delete_all(
    request: Request,
    body: WatchlistDeleteAllIn = Body(default_factory=WatchlistDeleteAllIn),
    _: None = Depends(optional_api_key),
):
    scope = (body.scope or "all").strip().lower()
    if scope not in ("all", "auto"):
        raise HTTPException(status_code=400, detail="scope 须为 all 或 auto")
    with session_scope() as s:
        if scope == "all":
            res = s.execute(delete(WatchlistRow))
        else:
            res = s.execute(
                delete(WatchlistRow).where(
                    WatchlistRow.origin.in_(
                        (WATCHLIST_ORIGIN_AUTO_HOT, WATCHLIST_ORIGIN_AUTO_QUANT)
                    )
                )
            )
        try:
            removed = int(res.rowcount or 0)
        except (TypeError, ValueError):
            removed = 0
    return WatchlistDeleteAllOut(removed=removed, scope=scope)


@app.post(
    "/watchlist/replace-all",
    response_model=WatchlistReplaceAllOut,
    tags=["② 管理自选股票"],
    summary="用股票列表完全替换自选池",
    description="""
删除当前自选池**全部**记录（含手动、热门自动、量化自动），再按请求体 **stocks** 顺序写入，**origin=auto_hot**。

用于控制台「快照热门股」勾选后「将所选加入自选」：② 管理列表仅保留本次选择，不与旧条目合并。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_replace_all(
    request: Request,
    body: WatchlistReplaceAllIn = Body(...),
    _: None = Depends(optional_api_key),
):
    """清空自选后按列表重建（来源 auto_hot）。"""
    warnings: list[str] = []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in body.stocks:
        try:
            sym = normalize_symbol(row.code)
        except ValueError:
            warnings.append(f"跳过无效代码：{row.code!r}")
            continue
        if sym in seen:
            continue
        seen.add(sym)
        nm = (row.name or "").strip()
        if nm.lower() == "nan":
            nm = ""
        pairs.append((sym, nm))
    if not pairs:
        raise HTTPException(status_code=400, detail="没有有效的 A 股代码可写入")
    removed = 0
    with session_scope() as s:
        res = s.execute(delete(WatchlistRow))
        try:
            removed = int(res.rowcount or 0)
        except (TypeError, ValueError):
            removed = 0
        add_pairs: list[tuple[str, str, str]] = []
        for sym, nm in pairs:
            use_nm = nm
            if not use_nm:
                use_nm = fetch_stock_name(sym) or ""
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_AUTO_HOT, name=use_nm))
            add_pairs.append((sym, use_nm, WATCHLIST_ORIGIN_AUTO_HOT))
        if add_pairs:
            record_watchlist_adds_with_snapshot(s, add_pairs)
    return WatchlistReplaceAllOut(removed=removed, added=len(pairs), warnings=warnings)


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
        add_pairs: list[tuple[str, str, str]] = []
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
            add_pairs.append((sym, nm, WATCHLIST_ORIGIN_AUTO_QUANT))
            added += 1
        if add_pairs:
            record_watchlist_adds_with_snapshot(s, add_pairs)
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
            exclude_cyb=body.exclude_cyb,
            selector_data_source=body.selector_data_source.value,
            use_sector_snapshot=body.use_sector_snapshot,
            tushare_token=body.tushare_token,
            sort_by_trend_strength=body.sort_by_trend_strength,
            require_technical_pass=body.require_technical_pass,
            exclude_overextended=body.exclude_overextended,
            max_return_20d_pct=body.max_return_20d_pct,
            enable_liquidity_filter=body.enable_liquidity_filter,
            min_avg_turnover_20d_100m=body.min_avg_turnover_20d_100m,
            pick_condition_groups=[g.value for g in body.pick_condition_groups],
            ma5_stand_min_days=body.ma5_stand_min_days,
            capital_flow_lookback_days=body.capital_flow_lookback_days,
            capital_min_positive_days=body.capital_min_positive_days,
            ma5_exclude_st=body.ma5_exclude_st,
            ma5_exclude_kcb=body.ma5_exclude_kcb,
            ma5_exclude_cyb=body.ma5_exclude_cyb,
            rising_3d_exclude_st=body.rising_3d_exclude_st,
            rising_3d_exclude_kcb=body.rising_3d_exclude_kcb,
            rising_3d_exclude_cyb=body.rising_3d_exclude_cyb,
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
    hot_names.update(_names_from_hot_sectors_detail(hot.ma5_capital_sectors_detail))
    hot_names.update(_names_from_hot_sectors_detail(hot.rising_3d_sectors_detail))
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
        add_pairs: list[tuple[str, str, str]] = []
        for sym in hot.symbols_for_watchlist:
            row = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
            if row is not None:
                if row.origin == WATCHLIST_ORIGIN_MANUAL:
                    skipped_existing_manual += 1
                continue
            nm = (hot_names.get(sym) or "").strip()
            s.add(WatchlistRow(symbol=sym, origin=WATCHLIST_ORIGIN_AUTO_HOT, name=nm))
            add_pairs.append((sym, nm, WATCHLIST_ORIGIN_AUTO_HOT))
            added += 1
        if add_pairs:
            record_watchlist_adds_with_snapshot(s, add_pairs)

    summary = FillHotSectorsSummary(
        added=added,
        skipped_existing_manual=skipped_existing_manual,
        removed_auto=removed_auto,
        warnings=warnings,
        progress_log=list(hot.progress_log or []),
    )
    return FillHotSectorsOut(
        sectors_detail=hot.sectors_detail,
        ma5_capital_sectors_detail=hot.ma5_capital_sectors_detail,
        rising_3d_sectors_detail=hot.rising_3d_sectors_detail,
        pick_condition_groups=list(hot.pick_condition_groups),
        summary=summary,
    )


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
    exclude_cyb: bool = Query(True, description="sector_hot：排除创业板 300/301"),
    selector_data_source: SelectorSectorDataSource = Query(..., description="akshare、mootdx 或 tushare"),
    use_sector_snapshot: bool = Query(True, description="true=优先使用本地板块快照；false=强制请求最新板块数据"),
    tushare_token: str | None = Query(None, description="TuShare 时可选；优先于服务端环境变量"),
    sort_by_trend_strength: bool = Query(True),
    require_technical_pass: bool = Query(False),
    exclude_overextended: bool = Query(False),
    max_return_20d_pct: float = Query(22.0, ge=0, le=500),
    enable_liquidity_filter: bool = Query(False),
    min_avg_turnover_20d_100m: float = Query(2.5, ge=0, le=10000),
    pick_condition_groups: list[str] = Query(
        default=["sector_hot", "ma5_capital"],
        description="可多选：sector_hot、ma5_capital",
    ),
    ma5_stand_min_days: int = Query(3, ge=2, le=10),
    capital_flow_lookback_days: int = Query(3, ge=1, le=10),
    capital_min_positive_days: int = Query(2, ge=1, le=10),
    ma5_exclude_st: bool = Query(True),
    ma5_exclude_kcb: bool = Query(True),
    ma5_exclude_cyb: bool = Query(True, description="ma5_capital：排除创业板 300/301"),
    rising_3d_exclude_st: bool = Query(True),
    rising_3d_exclude_kcb: bool = Query(True),
    rising_3d_exclude_cyb: bool = Query(True),
    _: None = Depends(optional_api_key),
):
    try:
        return _hot_sectors_preview_payload(
            top_sectors=top_sectors,
            stocks_per_sector=stocks_per_sector,
            board_type=board_type,
            exclude_st=exclude_st,
            exclude_kcb=exclude_kcb,
            exclude_cyb=exclude_cyb,
            selector_data_source=selector_data_source.value,
            use_sector_snapshot=use_sector_snapshot,
            tushare_token=tushare_token,
            sort_by_trend_strength=sort_by_trend_strength,
            require_technical_pass=require_technical_pass,
            exclude_overextended=exclude_overextended,
            max_return_20d_pct=max_return_20d_pct,
            enable_liquidity_filter=enable_liquidity_filter,
            min_avg_turnover_20d_100m=min_avg_turnover_20d_100m,
            pick_condition_groups=pick_condition_groups,
            ma5_stand_min_days=ma5_stand_min_days,
            capital_flow_lookback_days=capital_flow_lookback_days,
            capital_min_positive_days=capital_min_positive_days,
            ma5_exclude_st=ma5_exclude_st,
            ma5_exclude_kcb=ma5_exclude_kcb,
            ma5_exclude_cyb=ma5_exclude_cyb,
            rising_3d_exclude_st=rising_3d_exclude_st,
            rising_3d_exclude_kcb=rising_3d_exclude_kcb,
            rising_3d_exclude_cyb=rising_3d_exclude_cyb,
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
    route = body.selector_data_source.value
    t0 = time.monotonic()
    logger.info(
        "hot_sectors preview POST start: route=%s top=%s per_sector=%s board=%s snapshot=%s "
        "trend_sort=%s tech_pass=%s liq_filter=%s conditions=%s",
        route,
        body.top_sectors,
        body.stocks_per_sector,
        body.board_type,
        body.use_sector_snapshot,
        body.sort_by_trend_strength,
        body.require_technical_pass,
        body.enable_liquidity_filter,
        [g.value for g in body.pick_condition_groups],
    )
    try:
        out = _hot_sectors_preview_payload(
            top_sectors=body.top_sectors,
            stocks_per_sector=body.stocks_per_sector,
            board_type=body.board_type,
            exclude_st=body.exclude_st,
            exclude_kcb=body.exclude_kcb,
            exclude_cyb=body.exclude_cyb,
            selector_data_source=route,
            use_sector_snapshot=body.use_sector_snapshot,
            tushare_token=body.tushare_token,
            sort_by_trend_strength=body.sort_by_trend_strength,
            require_technical_pass=body.require_technical_pass,
            exclude_overextended=body.exclude_overextended,
            max_return_20d_pct=body.max_return_20d_pct,
            enable_liquidity_filter=body.enable_liquidity_filter,
            min_avg_turnover_20d_100m=body.min_avg_turnover_20d_100m,
            pick_condition_groups=[g.value for g in body.pick_condition_groups],
            ma5_stand_min_days=body.ma5_stand_min_days,
            capital_flow_lookback_days=body.capital_flow_lookback_days,
            capital_min_positive_days=body.capital_min_positive_days,
            ma5_exclude_st=body.ma5_exclude_st,
            ma5_exclude_kcb=body.ma5_exclude_kcb,
            ma5_exclude_cyb=body.ma5_exclude_cyb,
            rising_3d_exclude_st=body.rising_3d_exclude_st,
            rising_3d_exclude_kcb=body.rising_3d_exclude_kcb,
            rising_3d_exclude_cyb=body.rising_3d_exclude_cyb,
        )
        n_stocks = sum(len(b.get("stocks") or []) for b in out.sectors_detail)
        n_mc = sum(len(b.get("stocks") or []) for b in out.ma5_capital_sectors_detail)
        n_r3 = sum(len(b.get("stocks") or []) for b in out.rising_3d_sectors_detail)
        logger.info(
            "hot_sectors preview POST done: %.1fs route=%s hot_sectors=%s hot_stocks=%s "
            "ma5_sectors=%s ma5_stocks=%s rising_3d_sectors=%s rising_3d_stocks=%s warnings=%s",
            time.monotonic() - t0,
            route,
            len(out.sectors_detail),
            n_stocks,
            len(out.ma5_capital_sectors_detail),
            n_mc,
            len(out.rising_3d_sectors_detail),
            n_r3,
            len(out.summary.warnings),
        )
        return out
    except HTTPException as e:
        logger.warning(
            "hot_sectors preview POST aborted: %.1fs route=%s status=%s detail=%s",
            time.monotonic() - t0,
            route,
            e.status_code,
            e.detail,
        )
        raise
    except Exception as e:
        logger.exception(
            "hot_pick preview post failed: %.1fs route=%s err=%s",
            time.monotonic() - t0,
            route,
            e,
        )
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


# --- ③ 更新行情数据 ---


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
- **`include_fundamentals`**：为 `true` 时，日线完成后对**同一批**标的再拉扩展因子（估值/财报同比/主力净流入等），写入 `fundamental_snapshots`；响应含 **`fundamentals_results`**（与 `POST /ingest/fundamentals` 单条结构相同）。
- **`skip_bars`**：为 `true` 时**不联网拉取/写入日线**，仅用本地 bars 构造结果并刷新现价/强弱；可与 `include_fundamentals` 组合。K 线请在②控制台或其它显式拉取路径更新。
- **`reuse_watchlist_spot`** + **`watchlist_spot_by_symbol`**：为 `true` 时优先用②「刷新列表」传入的快照现价，**不对已有快照的标的重复联网拉现价**；缺快照的标的用本地 bars 回退。
- **`GET /ingest/test-connection`**：探测本机能否访问数据源（短区间测试，需 API Key 时同上）；可带 Query **`data_source`**。
- 需要能访问外网（通过 AkShare 拉公开数据）。
- 自选为空时会返回错误，请先用 `POST /watchlist` 添加股票。
- 某一只股票拉取失败时，结果里该条会带 `error`，其它股票仍会继续。
- 成功条目中附带 **`watchlist_name`**、**`last_trade_date` / `last_close`**（入库日线末根）、**`strength`**、**`live_last_price` / `live_change_pct`**（优先东财单股 push2，失败则东财全 A 列表快照；仍失败时标为 `daily_close_not_realtime` 的收盘参考，非 tick 实时）。

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
    对自选池每个标的执行 ingest_symbol_range（按 Body 日期规则拉取日线并 upsert），
    或 skip_bars=true 时仅用本地 bars 构造结果（不联网拉 K 线）。

    自选为空返回 400；单个标的失败时该条结果带 error 字段，不整批失败。
    """
    with session_scope() as s:
        orm_rows = list(s.execute(select(WatchlistRow)).scalars().all())
        wl_pairs = [(r.symbol, (r.name or "").strip()) for r in orm_rows]
    if not wl_pairs:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
    symbols, wl_name_by_sym, suffix_errs = _watchlist_subset_symbols(body.symbols, wl_pairs)
    from app.ingest import shanghai_today_date

    st, en = body.start_date, body.end_date
    sh_today = shanghai_today_date()
    if en and en > sh_today:
        raise HTTPException(status_code=400, detail="结束日期不能晚于东八区今日")
    if st and st > sh_today:
        raise HTTPException(status_code=400, detail="开始日期不能晚于东八区今日")
    if en and en < sh_today and (sh_today - en).days <= 7:
        en = sh_today
    ds = body.data_source.value if body.data_source is not None else None
    resolved_ds = ds if ds is not None else get_settings().ingest_data_source
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    spot_reuse_map = parse_watchlist_spot_reuse_map(
        body.reuse_watchlist_spot,
        (
            {k: v.model_dump(exclude_none=True) for k, v in body.watchlist_spot_by_symbol.items()}
            if body.watchlist_spot_by_symbol
            else None
        ),
    )

    def _ingest_symbol_needs_rate_limit_pause(sym: str) -> bool:
        if not body.skip_bars:
            return True
        if body.include_fundamentals:
            return True
        if not body.reuse_watchlist_spot:
            return True
        return sym not in spot_reuse_map

    if ingest_batch_status().get("active") or symbols_batch_status("ingest").get("active"):
        raise HTTPException(
            status_code=409,
            detail="已有批量行情任务进行中，请等待结束或取消后再试",
        )
    clear("ingest")
    batch_total = len(symbols)
    use_kline_job = not body.skip_bars
    batch_gen: int | None = None
    if use_kline_job:
        batch_gen = ingest_batch_start(batch_total)
    else:
        symbols_batch_start("ingest", batch_total, meta={
            "data_source": resolved_ds,
            "skip_bars": body.skip_bars,
            "include_fundamentals": bool(body.include_fundamentals),
            "reuse_watchlist_spot": bool(body.reuse_watchlist_spot),
        })

    def _ingest_update_should_cancel() -> bool:
        if use_kline_job:
            assert batch_gen is not None
            return ingest_batch_should_cancel(batch_gen)
        return is_cancelled("ingest")

    def _ingest_update_set_current(symbol: str | None) -> None:
        if use_kline_job:
            ingest_batch_set_current(symbol)
        else:
            symbols_batch_set_current("ingest", symbol)

    def _ingest_update_tick(symbol: str | None = None) -> None:
        if use_kline_job:
            ingest_batch_tick(symbol)
        else:
            symbols_batch_tick("ingest", symbol)

    results: list[dict[str, Any]] = []
    cancelled = False
    fundamentals_results: list[dict[str, Any]] | None = (
        [] if body.include_fundamentals else None
    )
    fundamentals_cancelled = False
    for i, sym in enumerate(symbols):
            if _ingest_update_should_cancel():
                cancelled = True
                logger.info("ingest/update cancelled by user at %s", sym)
                break
            _ingest_update_set_current(sym)
            if i > 0 and pause > 0 and _ingest_symbol_needs_rate_limit_pause(symbols[i - 1]):
                time.sleep(pause)
                if _ingest_update_should_cancel():
                    cancelled = True
                    logger.info("ingest/update cancelled by user after pause at %s", sym)
                    break
            nm = wl_name_by_sym.get(sym, "").strip() or None
            row_out: dict[str, Any] | None = None
            try:
                if body.skip_bars:
                    row_out = local_ingest_result_row(sym, data_source=ds)
                else:
                    row_out = ingest_symbol_range(
                        sym, range_start=st, range_end=en, data_source=ds
                    )
                if _ingest_update_should_cancel():
                    cancelled = True
                    logger.info("ingest/update cancelled by user after %s fetch", sym)
                    break
                row_out["watchlist_name"] = nm
                snap = strength_snapshot_for_symbol(sym)
                if snap is not None:
                    row_out["strength"] = snap
                enrich_one_ingest_result_spot(
                    row_out,
                    data_source=resolved_ds,
                    skip_bar_fetch=body.skip_bars,
                    prefetched_live=spot_reuse_map.get(sym),
                    skip_spot_network=bool(body.reuse_watchlist_spot),
                )
                if _ingest_update_should_cancel():
                    cancelled = True
                    break
                if body.include_fundamentals and fundamentals_results is not None:
                    if is_cancelled("fundamentals"):
                        fundamentals_cancelled = True
                        cancelled = True
                        break
                    fundamentals_results.append(
                        upsert_fundamental_snapshot(sym, ingest_row=row_out)
                    )
                results.append(row_out)
                symbols_batch_push_result("ingest", row_out)
            except ValueError as e:
                err_row = {"symbol": sym, "watchlist_name": nm, "error": str(e)}
                results.append(err_row)
                symbols_batch_push_result("ingest", err_row)
            except Exception as e:
                err_row = {"symbol": sym, "watchlist_name": nm, "error": str(e)}
                results.append(err_row)
                symbols_batch_push_result("ingest", err_row)
            _ingest_update_tick(sym)
    for er in suffix_errs:
        results.append(er)
        sym_e = er.get("symbol")
        if sym_e:
            _ingest_update_tick(str(sym_e))
    row_by_sym = {
        str(r["symbol"]): r for r in results if r.get("symbol") and "error" not in r
    }
    ok_syms = [str(r["symbol"]) for r in results if r.get("symbol") and "error" not in r]
    meta_by_sym: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.get("symbol") and "error" not in r:
            meta_by_sym[str(r["symbol"])] = r
    # 先把 batch 状态置为 finished：避免前端按钮在“前向展望同步”期间仍显示 N/N 转圈。
    # ③ 控制台只关心 ingest 进度本身，前向展望是后置的“自动收尾”，不应阻塞按钮归位。
    finish_cancelled = (
        cancelled or fundamentals_cancelled or _ingest_update_should_cancel()
    )
    if use_kline_job:
        ingest_batch_finish(cancelled=finish_cancelled)
    else:
        symbols_batch_finish("ingest", cancelled=finish_cancelled)
    outlook_sync: dict[str, Any] | None = None
    if not cancelled and ok_syms:
        try:
            outlook_sync = sync_after_ingest(
                ok_syms,
                horizon=DEFAULT_HORIZON,
                ingest_meta_by_sym=meta_by_sym,
            )
        except Exception as e:
            logger.warning("forward outlook auto-sync after ingest: %s", e)
    out: dict[str, Any] = {
        "results": results,
        "ingest_data_source": resolved_ds,
        "skip_bars": body.skip_bars,
        "reuse_watchlist_spot": body.reuse_watchlist_spot,
        "watchlist_spot_reused_count": (
            sum(1 for s in ok_syms if s in spot_reuse_map) if body.reuse_watchlist_spot else 0
        ),
        "cancelled": cancelled,
        "disclaimer": _disclaimer_payload().model_dump(),
        "forward_outlook_sync": outlook_sync,
    }
    if fundamentals_results is not None:
        out["fundamentals_results"] = fundamentals_results
        out["fundamentals_cancelled"] = fundamentals_cancelled
        out["fundamentals_note"] = (
            "扩展因子为 Demo 合成规则；数据源为东财/AkShare。"
            "PE/PB 来自东财全 A 列表（拉取时刷新，近实时）；主力净流入为日级资金表（非 tick）；"
            "盘中若无「当日」资金行则下行标「末收」；财报指标为最近一期，上下行相同。"
        )
    return out


@app.get(
    "/ingest/live-quotes",
    tags=["③ 更新行情数据"],
    summary="刷新自选标的单股实时报价（③ 表格下行现价）",
    description="""
对 `symbols` 拉现价：东财单股 push2 → 东财全 A 列表快照 → **通达信批量行情** → 新浪/腾讯日线 → 本地 bars（与日线 `data_source` 无关；末级回退可能为昨收）。

供控制台 ③ 拉取结果表 **定时刷新下行「现价」**；上行「昨收」仍来自入库前一根日线。
""",
)
@limiter.limit("60/minute")
def ingest_live_quotes(
    request: Request,
    symbols: str = Query(..., description="逗号分隔的 6 位代码，如 600619,600519"),
    _: None = Depends(optional_api_key),
):
    codes: list[str] = []
    seen: set[str] = set()
    for part in symbols.split(","):
        try:
            nc = normalize_symbol(part.strip())
        except ValueError:
            continue
        if nc not in seen:
            seen.add(nc)
            codes.append(nc)
    if not codes:
        raise HTTPException(status_code=400, detail="请提供至少一个有效 6 位代码")
    if len(codes) > 50:
        raise HTTPException(status_code=400, detail="单次最多 50 只")
    route = get_settings().ingest_data_source
    by_sym = live_quote_fields_for_codes_enhanced(
        codes, data_source=route, force_spot_refresh=True
    )
    quotes = []
    for sym in codes:
        row = by_sym.get(sym) or {}
        quotes.append({"symbol": sym, **row})
    return {"quotes": quotes, "disclaimer": _disclaimer_payload().model_dump()}


@app.post(
    "/ingest/fundamentals",
    tags=["③ 更新行情数据"],
    summary="拉取扩展因子并写入本地（Demo）",
    description="""
对自选池拉取并入库（默认**每一只**；也可传 **`symbols`** 只处理子集，规则与 `POST /ingest/update` 相同）。

- **估值**：市盈率(动)、市净率（东财全 A 列表，短 TTL 缓存）。
- **成长**：营业收入/归属净利润**同比 %**、财报报告期。
- **质量**：ROE、ROA、销售毛利率、销售净利率。
- **杠杆与偿债**：资产负债率、流动比率、速动比率。
- **现金流**：每股经营活动现金流量净额。
- **资金流**：最近交易日**主力净流入净额**及对应日期（东财日级）。

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
    clear("fundamentals")
    symbols_batch_start("fundamentals", len(symbols) + len(suffix_errs))
    results: list[dict] = []
    cancelled = False
    try:
        for i, sym in enumerate(symbols):
            if is_cancelled("fundamentals"):
                cancelled = True
                logger.info("ingest/fundamentals cancelled by user at %s", sym)
                break
            symbols_batch_set_current("fundamentals", sym)
            if i > 0 and pause > 0:
                time.sleep(pause)
            ctx_row: dict[str, Any] | None = None
            try:
                bars = list_bars_from_db(sym, limit=3)
                if bars:
                    lb = bars[-1]
                    ctx_row = {
                        "symbol": sym,
                        "last_trade_date": lb["trade_date"],
                        "last_close": lb["close"],
                    }
                    if len(bars) >= 2:
                        pb = bars[-2]
                        ctx_row["prev_trade_date"] = pb["trade_date"]
                        ctx_row["prev_close"] = pb["close"]
            except ValueError:
                ctx_row = None
            results.append(upsert_fundamental_snapshot(sym, ingest_row=ctx_row))
            symbols_batch_tick("fundamentals", sym)
        for er in suffix_errs:
            results.append(er)
            sym_e = er.get("symbol")
            if sym_e:
                symbols_batch_tick("fundamentals", str(sym_e))
    finally:
        symbols_batch_finish("fundamentals", cancelled=cancelled)
    return {
        "results": results,
        "cancelled": cancelled,
        "disclaimer": _disclaimer_payload().model_dump(),
        "note": (
            "扩展因子为 Demo 合成规则；数据源为东财/AkShare。"
            "今行优先执行日资金表，无则回退末根日；昨行为其上一日。PE/PB 为拉取时东财列表快照。"
        ),
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
    from app.ingest import shanghai_today_date

    sh_today = shanghai_today_date().isoformat()
    refresh: dict[str, object] = {"attempted": body.refresh_kline, "ok": None, "detail": None, "result": None}
    if body.refresh_kline:
        try:
            refresh["result"] = incremental_refresh(
                sym, data_source=route, as_of_date=shanghai_today_date()
            )
            refresh["ok"] = True
        except Exception as e:
            refresh["ok"] = False
            refresh["detail"] = f"{type(e).__name__}: {e}"
            logger.warning("web_data_preview refresh %s: %s", sym, e)
    try:
        bars = list_bars_from_db(sym, limit=body.bar_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    last_td: str | None = None
    if bars:
        last_td = str(bars[-1].get("trade_date") or "")[:10] or None
    lag_note = ""
    if last_td and last_td < sh_today:
        lag_note = (
            f"数据源最新完整日线为 {last_td}（东八区今日 {sh_today}）。"
            "盘中或收盘后源站未出当日 K 线时，末根会少一日；弹窗顶部「最新价」为盘口报价（若拉取成功）。"
        )
    fund_rows = fetch_individual_fund_flow_recent_rows(sym, limit_rows=body.fund_flow_recent_days)
    fund_latest = fund_rows[-1] if fund_rows else None
    return {
        "symbol": sym,
        "data_source": route,
        "shanghai_today": sh_today,
        "bars_last_trade_date": last_td,
        "kline_refresh": refresh,
        "bars": bars,
        "fund_flow_recent": fund_rows,
        "fund_flow_latest": fund_latest,
        "disclaimer": _disclaimer_payload().model_dump(),
        "note": (
            "实现上通过 AkShare 聚合公开页面接口，非浏览器自动化爬虫；"
            "资金流为日级汇总，「最新一行」对应数据源最近交易日。"
            + (f" {lag_note}" if lag_note else "")
        ),
    }


@app.get(
    "/quotes/{symbol}/bars",
    response_model=list[DailyBarOut],
    tags=["④ 查看信号"],
    summary="本地日线行情（OHLCV）",
    description="""
读取 **已写入 SQLite** 的日线（前复权），按交易日**从旧到新**排列。

- **limit**：未传 `from_date`/`to_date` 时表示最近多少根（1～500，默认 30）。**若传了任一日期界**，在区间内至多返回 **500** 根（`limit` 参数被忽略）。
- **from_date** / **to_date**：可选，含当日，按 `trade_date` 闭区间筛选；仅传下界或上界亦可。
- **change_pct**：相对**上一交易日收盘**的涨跌幅（%）；返回区间内第一根若前一交易日不在结果中则相对库内上一交易日收盘，无则 `null`。
- 若库里尚无该代码数据，返回空列表 `[]`（请先 `POST /ingest/update`）。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def quotes_daily_bars(
    symbol: str,
    request: Request,
    limit: int = Query(30, ge=1, le=500, description="未传日期界时：最近几根日线；传了 from_date/to_date 时该参数被忽略（区间内至多 500 根）"),
    from_date: date | None = Query(None, description="筛选起始日（含），YYYY-MM-DD"),
    to_date: date | None = Query(None, description="筛选结束日（含），YYYY-MM-DD"),
    _: None = Depends(optional_api_key),
):
    """规范化代码后读库；无行则返回空列表。"""
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        if from_date is not None or to_date is not None:
            d_from = from_date.isoformat() if from_date is not None else None
            d_to = to_date.isoformat() if to_date is not None else None
            rows = list_bars_from_db(
                sym,
                limit=500,
                trade_date_from=d_from,
                trade_date_to=d_to,
            )
        else:
            rows = list_bars_from_db(sym, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rows


# --- ④ 查看信号 ---


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
- **symbols**：可重复传参（如 `?symbols=600519&symbols=000001`），仅计算所列且**须在自选池**的代码；省略则处理全部自选。
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
    symbols: list[str] | None = Query(
        None,
        description="仅计算所列 6 位代码（须在自选池）；可重复传参。省略则处理全部自选",
    ),
    _: None = Depends(optional_api_key),
):
    """对自选池逐个 compute_signal；失败标的打日志并跳过，不中断其它标的。"""
    route = _resolve_ingest_route(data_source)
    response.headers["X-Quant-Data-Source"] = route
    response.headers["X-Quant-Pre-Refresh"] = "1" if pre_refresh else "0"
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow)).scalars().all()
        wl_pairs = [(r.symbol, (r.name or "")) for r in rows]
    symbols, _, subset_errs = _watchlist_subset_symbols(symbols, wl_pairs)
    failed_syms: list[str] = []
    for err in subset_errs:
        sym = err.get("symbol")
        if sym and str(sym) not in failed_syms:
            failed_syms.append(str(sym))
    if not symbols:
        response.headers["X-Quant-Signals-Success-Count"] = "0"
        response.headers["X-Quant-Signals-Failed-Count"] = str(len(failed_syms))
        if failed_syms:
            joined = ",".join(failed_syms[:120])
            if len(failed_syms) > 120:
                joined += ",..."
            response.headers["X-Quant-Signals-Failed-Symbols"] = joined[:1800]
        return []
    clear("signals")
    symbols_batch_start(
        "signals",
        len(symbols),
        meta={"data_source": route, "pre_refresh": bool(pre_refresh)},
    )
    out: list[SignalOut] = []
    cancelled = False
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    try:
        for i, sym in enumerate(symbols):
            if is_cancelled("signals"):
                cancelled = True
                logger.info("signals batch cancelled by user before %s", sym)
                break
            symbols_batch_set_current("signals", sym)
            if i > 0 and pause > 0:
                time.sleep(pause)
            if pre_refresh:
                try:
                    incremental_refresh(sym, data_source=route)
                except Exception as e:
                    logger.debug("pre_refresh skipped %s route=%s: %s", sym, route, e)
            try:
                sig = compute_signal(sym, data_source=route)
                out.append(sig)
                # ④ 增量推送：成功一只就让前端轮询拿到，及时上屏
                try:
                    symbols_batch_push_result("signals", sig.model_dump())
                except Exception as e:
                    logger.debug("signals partial push failed %s: %s", sym, e)
            except Exception as e:
                failed_syms.append(sym)
                logger.debug("signal skipped %s: %s", sym, e)
                # 失败也推一条占位，便于前端知道该只已处理
                try:
                    symbols_batch_push_result(
                        "signals", {"symbol": sym, "error": str(e)}
                    )
                except Exception:
                    pass
            symbols_batch_tick("signals", sym)
    finally:
        symbols_batch_finish("signals", cancelled=cancelled)
    if cancelled:
        response.headers["X-Quant-Signals-Cancelled"] = "1"
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


# --- ⑤ 个股咨询 ---


@app.get(
    "/research/stock-brief/{symbol}",
    response_model=StockBriefOut,
    tags=["⑤ 个股咨询"],
    summary="个股速览：行情、新闻、概念、业务与营收",
    description="""
联网聚合东财 / AkShare 公开数据，便于快速了解单只 A 股：

- **行情**：现价、涨跌幅、换手等（优先盘口快照，可回退本地日线末根）。
- **新闻面**：近期个股新闻标题与摘要（东财搜索接口）。
- **相关概念**：东财 F10 核心题材 / 概念板块列表。
- **公司业务**：公司介绍、行业、经营范围；主营构成（按产品，含收入占比与毛利率）。
- **营收能力**：最近报告期营收/净利润同比、ROE、毛利率、PE/PB 等。
- **财报质量**：每股收益 vs 经营现金流、资产负债率及 Demo 解读。
- **股东结构**：十大流通股东、股权质押比例。
- **估值对比**：同行业 PE/PB 中位数、历史分位（约3年样本）。
- **风险提示**：ST、高负债、质押、业绩变脸等 Demo 规则标签。
- **当日涨跌解读**：涨停/跌停股池、龙虎榜、当日公告与新闻、盘口异动、题材线索；**涨跌归因（Demo）** 对比大盘/行业；**主力资金流**、**近3日事件时间线**。

各块独立拉取，局部失败时对应字段为空并在 `warnings` 中说明。**非投资建议**；有频率限制，请勿连续狂点。
""",
)
@limiter.limit("12/minute")
def research_stock_brief(
    request: Request,
    symbol: str,
    news_limit: int = Query(8, ge=1, le=20, description="返回新闻条数上限"),
    _: None = Depends(optional_api_key),
):
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        raw = build_stock_brief(sym, news_limit=news_limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("stock-brief %s failed", sym)
        raise HTTPException(status_code=502, detail=f"拉取个股资料失败：{e}") from e
    return StockBriefOut.model_validate(raw)


# --- ⑦ AI 潜力测算 ---


@app.get(
    "/research/ai-potential/context",
    response_model=AiPotentialContextOut,
    tags=["⑦ AI算法"],
    summary="预览单只②③④汇总数据（不调用 AI）",
    description="""
读取本地库内数据，汇总②自选、③ K 线质量、④ 信号、本地打分与前向展望，供⑦ AI 测算前核对。

**不联网**增量；若 K 线不足请先在③更新。在⑦控制台填写 AI 配置或配置服务端 `AI_API_KEY` 后可测算。
""",
)
@limiter.limit("30/minute")
def research_ai_potential_context(
    request: Request,
    symbol: str = Query(..., description="6 位 A 股代码", examples=["600519"]),
    _: None = Depends(optional_api_key),
):
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    raw = gather_symbol_context(sym)
    return AiPotentialContextOut.model_validate(raw)


@app.post(
    "/research/ai-potential",
    response_model=AiPotentialOut,
    tags=["⑦ AI算法"],
    summary="AI 潜力测算（基于②③④本地数据）",
    description="""
在②③④已完成本地测算的前提下，汇总各标的信号、K 线质量、本地打分与前向展望，交由 **OpenAI 兼容** 大模型输出潜力 Demo 解读。

- **preview_only=true**：仅返回 `contexts`，不调用 AI（无需密钥）。
- **use_watchlist=true**：包含自选池全部代码（与 `symbols` 合并去重）；单次最多 8 只。
- **ai**：⑦ 控制台传入的 `api_key` / `api_base` / `model` 等，优先于服务端 `.env`。

**非投资建议**；有频率与 token 成本，请勿连续狂点。
""",
)
@limiter.limit("8/minute")
def research_ai_potential(
    request: Request,
    body: AiPotentialIn,
    _: None = Depends(optional_api_key),
):
    try:
        syms = resolve_symbols_for_ai(body.symbols, use_watchlist=body.use_watchlist)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not syms:
        raise HTTPException(status_code=400, detail="请指定 symbols 或勾选 use_watchlist")
    try:
        raw = run_ai_potential(
            syms,
            horizon_days=body.horizon_days,
            user_note=body.note,
            question=body.question,
            preview_only=body.preview_only,
            ai=body.ai,
        )
        return AiPotentialOut.model_validate(raw)
    except ValueError as e:
        msg = str(e)
        if "AI_API_KEY" in msg or "AI API Key" in msg or "AI 接口" in msg:
            raise HTTPException(status_code=503, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except Exception as e:
        logger.exception("ai-potential failed")
        raise HTTPException(status_code=502, detail=f"AI 测算失败：{e}") from e


# --- ⑧ 研究：预测验证 ---


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


@app.get(
    "/research/score-bucket-validate",
    response_model=ScoreBucketValidateOut,
    tags=["⑧ 研究：预测验证"],
    summary="本地打分分档 vs 前向展望 H 日实际收益",
    description="""
读取 **forward_outlook 已结算（settled）** 记录，在每条 `signal_trade_date` 因果截断 K 线后复算：

- **short_term_score**、**final_score_v2_short**（板块热度默认中性 50）、**final_score_v2_trade**（交易向，无样本内回测分）、**signal_technical_score**

再按分档汇总 `actual_return_pct`，并给出 Spearman 相关与「初筛通过」子样本摘要。

**过滤**：`require_screen_pass=true` 仅保留短线初筛通过；`min_turnover_amt` 过滤 20 日均成交额（元）。

样本过少时结论不可靠，**不构成投资建议**。
""",
)
@limiter.limit("20/minute")
def research_score_bucket_validate(
    request: Request,
    symbol: str | None = Query(None, description="可选：仅统计单标的"),
    horizon: int | None = Query(None, ge=1, le=60, description="可选：仅统计指定 H 日展望"),
    sector_hot_score: float = Query(
        50.0,
        ge=0,
        le=100,
        description="复算综合分时假设的板块热度（历史板块热度未入库时用中性值）",
    ),
    min_turnover_amt: float = Query(
        0.0,
        ge=0,
        description="20 日均成交额下限（元）；0 表示不过滤",
    ),
    require_screen_pass: bool = Query(
        False,
        description="为 true 时仅统计 short_term_passed 的样本（交易向硬门槛）",
    ),
    fast_period: int = Query(10, ge=2, le=120),
    slow_period: int = Query(30, ge=3, le=250),
    settle_pending: bool = Query(True, description="统计前先尝试结算 pending 展望"),
    _: None = Depends(optional_api_key),
):
    try:
        raw = run_score_bucket_validate(
            symbol=symbol,
            horizon=horizon,
            sector_hot_score=sector_hot_score,
            min_turnover_amt=min_turnover_amt,
            require_screen_pass=require_screen_pass,
            fast_period=fast_period,
            slow_period=slow_period,
            settle_pending=settle_pending,
        )
        return ScoreBucketValidateOut.model_validate(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


_SECTOR_SCREEN_DUAL_MA_KEYS = frozenset(
    {
        "dual_ma_total_return_pct",
        "dual_ma_annual_return_pct",
        "dual_ma_max_drawdown_pct",
        "dual_ma_sharpe_ratio",
        "dual_ma_trade_count",
        "dual_ma_win_rate_pct",
    }
)
_SECTOR_SCREEN_TRIPLE_MA_KEYS = frozenset(
    {
        "triple_ma_total_return_pct",
        "triple_ma_annual_return_pct",
        "triple_ma_max_drawdown_pct",
        "triple_ma_sharpe_ratio",
        "triple_ma_trade_count",
        "triple_ma_win_rate_pct",
    }
)


def _sector_screen_stock_row_dict(
    ev: StockEvaluation,
    *,
    show_dual: bool,
    show_triple: bool,
    show_ma5: bool,
    show_ma5_3d: bool,
) -> dict[str, Any]:
    row = asdict(ev)
    if not show_dual:
        for k in _SECTOR_SCREEN_DUAL_MA_KEYS:
            row.pop(k, None)
    if not show_triple:
        for k in _SECTOR_SCREEN_TRIPLE_MA_KEYS:
            row.pop(k, None)
    if not show_ma5:
        row.pop("ma5_stand_count", None)
    if not show_ma5_3d:
        row.pop("ma5_consecutive_stand_days", None)
    return row


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

        flat_universe = False
        flat_universe_top = max(1, min(500, body.universe_max_stocks))
        flat_universe_scan = 0
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
        elif body.pool_mode == SectorScreenPoolMode.universe:
            hot_sectors = False
            sector_name = None
            codes_arg = None
            flat_universe = True
            if (
                body.show_dual_ma_strategy
                or body.show_triple_ma_strategy
                or body.show_ma5_stand_strategy
                or body.show_ma5_stand_3d_strategy
            ):
                flat_universe_scan = max(
                    flat_universe_top, min(8000, int(body.universe_scan_cap))
                )
            else:
                flat_universe_scan = 0
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
            screen_mode=body.screen_mode,
            only_passed=body.only_passed,
            top_stocks=body.top_stocks_limit,
            output=None,
            hot_chain_prefer_cache=body.hot_chain_prefer_snapshot,
            hot_chain_force_refresh=body.hot_chain_refresh_snapshot,
            include_hot_snapshot_stocks=body.include_hot_snapshot_stocks,
            hot_snapshot_stocks_cap=body.hot_snapshot_stocks_cap,
            flat_universe=flat_universe,
            flat_universe_top=flat_universe_top,
            flat_universe_scan=flat_universe_scan,
            universe_segments=list(body.universe_segments),
            universe_exclude_st=body.universe_exclude_st,
            show_dual_ma_strategy=body.show_dual_ma_strategy,
            show_triple_ma_strategy=body.show_triple_ma_strategy,
            show_ma5_stand_strategy=body.show_ma5_stand_strategy,
            ma5_stand_lookback=body.ma5_stand_lookback,
            show_ma5_stand_3d_strategy=body.show_ma5_stand_3d_strategy,
            ma5_stand_3d_min_days=body.ma5_stand_3d_min_days,
        )
        validate_args(ns)
        sectors, stocks = run_analysis(ns)
        logger.info(
            "sector-screen pipeline done: data_source=%s stocks=%d sectors=%d",
            body.data_source.value,
            len(stocks),
            len(sectors),
        )
        if not stocks:
            logger.warning(
                "sector-screen returned zero stocks; per-symbol fetch/eval skips are logged above"
            )
    except HTTPException:
        raise
    except DataSourceError as e:
        logger.warning("sector-screen aborted (data source): %s", e)
        raise _http_exception_from_datasource(e) from e
    except Exception as e:
        logger.exception("sector-screen failed: %s", e)
        raise HTTPException(status_code=502, detail=f"选股流水线失败：{e}") from e
    finally:
        if tmp_codes is not None and tmp_codes.is_file():
            tmp_codes.unlink(missing_ok=True)

    d = _disclaimer_payload()
    lim = max(1, body.top_stocks_limit)
    slice_stocks = list(stocks[:lim])
    codes_req = [normalize_code(s.code) for s in slice_stocks]
    sh_today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    merged = []
    if body.data_source == SectorScreenDataSource.mootdx:
        qmap: dict[str, dict[str, Any]] = {}
        try:
            ds_mx = get_data_source("mootdx")
            fn = getattr(ds_mx, "quote_snapshot_for_codes", None)
            if callable(fn):
                qmap = fn(codes_req)
        except Exception as e:
            logger.warning("sector-screen: mootdx quote snapshot skipped: %s", e)
            qmap = {}
        for ev in slice_stocks:
            nk = normalize_code(ev.code)
            row = qmap.get(nk) or {}
            p = row.get("tdx_last_price")
            qd = row.get("tdx_quote_date")
            dt_show = sh_today
            if isinstance(qd, str) and len(qd) >= 10 and qd[4] == "-" and qd[7] == "-":
                dt_show = qd[:10]
            chg = row.get("tdx_change_pct")
            chg_f = None
            if chg is not None and math.isfinite(float(chg)):
                chg_f = round(float(chg), 2)
            if p is not None and math.isfinite(float(p)) and float(p) > 0:
                merged.append(
                    replace(
                        ev,
                        latest_close=round(float(p), 2),
                        latest_trade_date=dt_show,
                        spot_change_pct=chg_f,
                    )
                )
            else:
                merged.append(ev)
        note_tail = (
            "当前数据源为 mootdx：股票池为通达信列表顺序截取；"
            "「最新价」「最近交易日」在能拉到通达信批量行情时，用快照现价与行情日期列（若无日期列则用东八区自然日）覆盖日线末根展示；"
            "「涨幅%」为快照 (现价−昨收)/昨收，与最新价同源；"
            "初筛与回测仍基于区间内日线。"
        )
    else:
        try:
            spot_by = spot_liquidity_fields_for_codes(codes_req, force_refresh=True)
        except Exception as e:
            logger.warning("sector-screen: spot merge for latest price skipped: %s", e)
            spot_by = {}
        for ev in slice_stocks:
            nk = normalize_code(ev.code)
            row = spot_by.get(nk) or {}
            p = row.get("spot_last_price")
            qd = row.get("spot_quote_date")
            trade_show = sh_today
            if isinstance(qd, str) and len(qd) >= 10 and qd[4] == "-" and qd[7] == "-":
                trade_show = qd[:10]
            chg = row.get("spot_change_pct")
            chg_f = None
            if chg is not None and math.isfinite(float(chg)):
                chg_f = round(float(chg), 2)
            if p is not None and math.isfinite(float(p)) and float(p) > 0:
                merged.append(
                    replace(
                        ev,
                        latest_close=round(float(p), 2),
                        latest_trade_date=trade_show,
                        spot_change_pct=chg_f,
                    )
                )
            else:
                merged.append(ev)
        note_tail = (
            "返回中「最新价」在能拉到东财全 A 列表快照时，用快照现价覆盖日线末根收盘价；"
            "「涨幅%」为东财列表「涨跌幅」列（与最新价同源）；"
            "「最近交易日」优先用快照表内日期列，否则用东八区当前自然日。"
            "每次选股会跳过 spot 内存 TTL 尽量拉新表；拉取失败时仍可能回退到缓存表。"
            "初筛与回测仍基于区间内日线。"
        )

    stock_rows = [
        _sector_screen_stock_row_dict(
            s,
            show_dual=body.show_dual_ma_strategy,
            show_triple=body.show_triple_ma_strategy,
            show_ma5=body.show_ma5_stand_strategy,
            show_ma5_3d=body.show_ma5_stand_3d_strategy,
        )
        for s in merged
    ]
    missing_names: list[str] = []
    for row in stock_rows:
        c = normalize_symbol(str(row.get("code") or ""))
        nm = str(row.get("name") or "").strip()
        if c and (not nm or nm.lower() == "nan"):
            missing_names.append(c)
    if missing_names:
        try:
            nm_by = fetch_stock_names_map(list(dict.fromkeys(missing_names)))
            for row in stock_rows:
                c = normalize_symbol(str(row.get("code") or ""))
                if not (str(row.get("name") or "").strip()) and nm_by.get(c):
                    row["name"] = str(nm_by[c]).strip()
        except Exception as e:
            logger.debug("sector-screen: fetch_stock_names_map skipped: %s", e)

    return SectorScreenOut(
        sectors=[asdict(s) for s in sectors],
        stocks=stock_rows,
        stocks_total=len(stocks),
        start_date=body.start_date,
        end_date=body.end_date,
        disclaimer=d.disclaimer,
        show_dual_ma_strategy=body.show_dual_ma_strategy,
        show_triple_ma_strategy=body.show_triple_ma_strategy,
        show_ma5_stand_strategy=body.show_ma5_stand_strategy,
        ma5_stand_lookback=body.ma5_stand_lookback,
        show_ma5_stand_3d_strategy=body.show_ma5_stand_3d_strategy,
        ma5_stand_3d_min_days=body.ma5_stand_3d_min_days,
        note=(
            "与根目录 `quant_stock_selector.py` 及包 `app.quant_stock_selector` 流水线一致（技术面初筛 + 双均线回测 + 综合分）。"
            + note_tail
            + "请求会大量拉取行情，请勿频繁触发。"
        ),
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


# --- ⑨ 量化选股 ---


@app.post(
    "/research/sector-screen",
    response_model=SectorScreenOut,
    tags=["⑨ 量化选股（脚本）"],
    summary="板块或全市场股票池选股（脚本同款流水线）",
    description="""
对应命令行 **`quant_stock_selector.py`** / 包 **`app.quant_stock_selector`**：

1. 构建股票池：**pool_mode=universe** 且未指定 **sector**、**symbols** 时，从当前 `data_source` 的全市场列表顺序截取 `universe_max_stocks` 只；否则拉取热门板块（或指定 **sector**、或 **symbols** 自定义列表）；
2. 取成分股，按 `start_date`～`end_date` 拉日线（优先本地 `data_dir` 在 CLI 中有，API 固定仅走网络数据源）；
3. 技术面初筛（`evaluate_screen`，默认 **screen_mode=short_term** 短线强化）+ 双均线回测（`run_sma_backtest`）合成 **final_score**（短线模式用 **v2_short** 权重：板块 20% + 短线技术分 50% + 回测 30%）。

**注意**：会对多只股票依次请求行情，**耗时长**、易受数据源限流；全市场截取模式请将 `universe_max_stocks`、`top_stocks_limit` 控制在合理范围。

**`data_source=hot_chain`**：热门板块表与 `POST /meta/hot-market-snapshot/refresh` 相同（**新浪优先** → 回退等），可用 `hot_chain_prefer_snapshot` / `hot_chain_refresh_snapshot` 控制是否读本地 `hot_market_snapshot.json`；**成分股与日线**仍经东财拉取。详见 `app/hot_market_snapshot.py`。

**`include_hot_snapshot_stocks=true`**（仅 **pool_mode=hot_sectors** 且未传 **sector**、**symbols**）：在板块成分之外，再按 `hot_snapshot_stocks_cap` 从本地 `hot_market_snapshot.json` 的 **stocks** 并入热门股，与已有代码**去重**后，与各板块股**同一套** K 线窗口与双均线回测流程。

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


def _holding_one_out(s, row: HoldingRow) -> HoldingOut:
    return build_holdings_list(s, [row])[0]


# --- ⑩ 持仓记录（自用） ---


@app.get(
    "/holdings",
    response_model=list[HoldingOut],
    tags=["⑩ 持仓记录（自用）"],
    summary="列出持仓记录",
    description="""
返回本机 SQLite 中的持仓列表（新记录在前）。可选 `status`：`holding` 仅持仓中、`closed` 仅已平仓；不传为全部。

每条附带**估算**浮动/已实现盈亏：参考价为盘口现价（若有）或本地最新日线收盘；**非**券商成交回报，不构成投资建议。

**Query `sync`**（默认 `true`）：联网刷新现价估算，并将 `mark_price` / `mark_price_at` / `updated_at` 写回本机 `holdings` 表；设为 `false` 则只读库、不联网、不回写。

**Query `ids`**（可重复）：仅刷新/返回所列持仓记录 id（控制台⑩定时刷新勾选行）。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def holdings_list(
    request: Request,
    status: str | None = Query(
        None,
        description="holding=仅持仓中；closed=仅已平仓；省略=全部",
    ),
    limit: int = Query(200, ge=1, le=500),
    sync: bool = Query(
        True,
        description="为 true 时联网拉现价并写回 holdings 快照列（mark_price 等）",
    ),
    ids: list[int] | None = Query(
        None,
        description="仅处理所列持仓记录 id；可重复传参",
    ),
    _: None = Depends(optional_api_key),
):
    st = (status or "").strip().lower() or None
    if st is not None and st not in (HOLDING_STATUS_HOLDING, HOLDING_STATUS_CLOSED):
        raise HTTPException(status_code=400, detail="status 须为 holding 或 closed")
    subset_ids: list[int] | None = None
    if ids:
        subset_ids = []
        seen_ids: set[int] = set()
        for raw in ids:
            try:
                hid = int(raw)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail="ids 须为整数") from e
            if hid <= 0 or hid in seen_ids:
                continue
            seen_ids.add(hid)
            subset_ids.append(hid)
        if not subset_ids:
            return []
    with session_scope() as s:
        q = select(HoldingRow).order_by(HoldingRow.id.desc()).limit(limit)
        if st:
            q = q.where(HoldingRow.status == st)
        if subset_ids:
            q = q.where(HoldingRow.id.in_(subset_ids))
        rows = list(s.execute(q).scalars().all())
        if subset_ids:
            order = {hid: i for i, hid in enumerate(subset_ids)}
            rows.sort(key=lambda r: order.get(r.id, 10**9))
        return build_holdings_list(
            s, rows, force_spot_refresh=sync, persist_snapshots=sync
        )


@app.post(
    "/holdings/notify",
    response_model=HoldingsNotifyOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="推送定时刷新结果到通知地址",
    description="""
将本次刷新得到的持仓 JSON **POST** 到控制台填写的通知地址（Webhook）。

前端可按每只股票「推送价」过滤后再调用本接口；`alert_triggers` 会写入 markdown 正文（如「触发≥15.50」）。

请求体示例字段：`event=holdings_spot_refresh`、`refreshed_at`、`picked_ids`、`items`（与列表行一致）。
服务端代发，避免浏览器 CORS 限制。
""",
)
@limiter.limit("30/minute")
def holdings_notify(
    body: HoldingsNotifyIn,
    request: Request,
    _: None = Depends(optional_api_key),
):
    try:
        url = normalize_holdings_notify_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    ok, detail, meta = post_holdings_refresh_webhook(
        url,
        items=body.items,
        picked_ids=body.picked_ids,
        refreshed_at=body.refreshed_at,
        alert_triggers=body.alert_triggers,
    )
    if not ok:
        raise HTTPException(status_code=502, detail=detail or "通知发送失败")
    return HoldingsNotifyOut(
        ok=True,
        detail=detail,
        channel=str(meta.get("channel") or ""),
        preview=str(meta.get("preview") or ""),
        remote_reply=str(meta.get("remote_reply") or ""),
    )


@app.get(
    "/holdings/review-summary",
    response_model=HoldingReviewSummaryOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="已平仓复盘汇总",
    description="统计本机全部「已平仓」记录的笔数、盈亏合计、胜率与平均持仓天数（非券商回报）。",
)
@limiter.limit(get_settings().rate_limit_default)
def holdings_review_summary(request: Request, _: None = Depends(optional_api_key)):
    with session_scope() as s:
        return HoldingReviewSummaryOut(**compute_holdings_review_summary(s))


@app.get(
    "/holdings/goal-progress",
    response_model=HoldingGoalProgressOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="距目标进度（持仓盈亏汇总）",
    description="""
按本机**全部**持仓记录（含已平仓）汇总浮动/已实现盈亏，估算：

`当前权益 ≈ 起始资金 + 盈亏合计`，并计算距目标还差多少元、完成度 %。

需已录入持仓；现价优先盘口（与列表一致）。**非**券商资产证明。
""",
)
@limiter.limit("60/minute")
def holdings_goal_progress(
    request: Request,
    start_capital: float = Query(..., gt=0, description="起始资金（元）"),
    target_capital: float = Query(..., gt=0, description="目标资金（元）"),
    _: None = Depends(optional_api_key),
):
    if target_capital <= start_capital:
        raise HTTPException(status_code=400, detail="目标资金须大于起始资金")
    with session_scope() as s:
        try:
            return compute_goal_progress(s, start_capital=start_capital, target_capital=target_capital)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/holdings",
    response_model=HoldingOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="新增一条持仓",
    description="记录买入代码、股数、成本价、买入日期（均必填）；备注可选。数据仅存本机，非投资建议。",
)
@limiter.limit("30/minute")
def holdings_create(body: HoldingIn, request: Request, _: None = Depends(optional_api_key)):
    try:
        sym = normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    buy_d = body.buy_date.isoformat()
    row = HoldingRow(
        symbol=sym,
        name=(body.name or "").strip(),
        status=HOLDING_STATUS_HOLDING,
        shares=float(body.shares),
        cost_price=float(body.cost_price),
        buy_date=buy_d,
        notes=body.notes.strip() if body.notes else None,
        created_at="",
        updated_at="",
    )
    with session_scope() as s:
        apply_holding_defaults(row, sym=sym)
        s.add(row)
        s.flush()
        s.refresh(row)
        return _holding_one_out(s, row)


@app.post(
    "/holdings/closed-record",
    response_model=HoldingOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="补录一条已平仓记录（复盘）",
    description="""
直接写入**已平仓**状态（含买入/卖出价与日期），用于补录券商历史成交，便于在「仅已平仓」筛选中复盘。

**勿用「删除」清理误录**；已平仓记录应保留。数据仅存本机，非投资建议。
""",
)
@limiter.limit("30/minute")
def holdings_create_closed_record(
    body: HoldingClosedRecordIn, request: Request, _: None = Depends(optional_api_key)
):
    try:
        sym = normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with session_scope() as s:
        row = create_closed_holding_record(
            s,
            sym=sym,
            shares=float(body.shares),
            cost_price=float(body.cost_price),
            buy_date=body.buy_date.isoformat(),
            sell_price=float(body.sell_price),
            sell_date=body.sell_date.isoformat(),
            notes=body.notes,
            name=body.name,
        )
        return _holding_one_out(s, row)


@app.get(
    "/holdings/{holding_id}/exit-advice",
    response_model=HoldingExitAdviceOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="平仓建议（成本 + ④ 信号规则）",
    description="""
对**持仓中**记录计算是否倾向减仓/平仓（0–100 分）。因子包括：相对成本的浮盈亏、是否跌破 MA20、趋势/强度、④ 仓位提示、Demo 止损线等。

**非**卖出指令；需本地已有 K 线（③ 拉取）。可选 Query：`data_source`（与 ③/④ 一致）、`current_price`（与列表「当前价格」列一致，用于浮盈亏与 MA20 比较）。
""",
)
@limiter.limit("40/minute")
def holdings_exit_advice(
    holding_id: int,
    request: Request,
    data_source: IngestDataSource | None = Query(None),
    current_price: float | None = Query(
        None,
        gt=0,
        description="表格「当前价格」列取值；传入则按该价计算浮盈亏与 MA20 对比（非 tick 实时）",
    ),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            return compute_holding_exit_advice(
                row, session=s, data_source=ds, current_price=current_price
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/holdings/{holding_id}/entry-advice",
    response_model=HoldingEntryAdviceOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="可否建仓（最新价 + ④ 信号规则）",
    description="""
对**任意**持仓记录（持仓中 / 已平仓）评估「此刻是否适合新开仓或加仓」（0–100 分）。
因子包括：合成适合度、趋势/强度、④ 仓位提示、现价相对 MA20、风险标签等。

**非**买入指令；需本地已有 K 线（③ 拉取）。可选 Query：`data_source`、`current_price`（与列表现价列一致；已平仓请传盘口现价而非卖出价）。
""",
)
@limiter.limit("40/minute")
def holdings_entry_advice(
    holding_id: int,
    request: Request,
    data_source: IngestDataSource | None = Query(None),
    current_price: float | None = Query(
        None,
        gt=0,
        description="表格「现价」取值；已平仓行请传第二行现价，勿传卖出价",
    ),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            return compute_holding_entry_advice(
                row, session=s, data_source=ds, current_price=current_price
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/holdings/{holding_id}/goal-plan",
    response_model=HoldingGoalPlanOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="目标资金测算（留仓 vs 换自选）",
    description="""
设定**起始资金**与**目标资金**（如 5000 → 10000），结合当前持仓的平仓压力分、④ 适合度，
并在 **② 自选池**中扫描更强候选，给出「继续持有 / 观察 / 清仓换仓」的分步 Demo 路径。

**非**交易指令；需本地 K 线与自选数据（③ 拉取、② 维护自选）。
""",
)
@limiter.limit("20/minute")
def holdings_goal_plan(
    holding_id: int,
    body: HoldingGoalPlanIn,
    request: Request,
    data_source: IngestDataSource | None = Query(None),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            return compute_holding_goal_plan(
                row,
                session=s,
                start_capital=float(body.start_capital),
                target_capital=float(body.target_capital),
                data_source=ds,
                current_price=body.current_price,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/holdings/{holding_id}/goal-plan-live/preflight",
    response_model=HoldingGoalPlanLivePreflightOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="当日测算预检（缺什么数据）",
    description="""
测算**当日最新**前调用：检查本地 K 线是否足够、能否拉到联网现价等。
`ready=false` 时按 `missing[].action` 先去 ③ 拉日线或点「刷新列表」，再调 `POST …/goal-plan-live`。
""",
)
@limiter.limit("40/minute")
def holdings_goal_plan_live_preflight(
    holding_id: int,
    request: Request,
    data_source: IngestDataSource | None = Query(None),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            return check_goal_plan_live_readiness(row, session=s, data_source=ds)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/holdings/{holding_id}/goal-plan-live",
    response_model=HoldingGoalPlanOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="目标资金测算（当日联网现价）",
    description="""
与 `goal-plan` 相同逻辑；测算前会用联网现价**补写/刷新今日日线**，趋势/MA20/适合度均基于**含当日**的 K 线（盘中为参考价）。
建议先 `GET …/goal-plan-live/preflight` 确认 `ready=true`。
""",
)
@limiter.limit("20/minute")
def holdings_goal_plan_live(
    holding_id: int,
    body: HoldingGoalPlanIn,
    request: Request,
    data_source: IngestDataSource | None = Query(None),
    _: None = Depends(optional_api_key),
):
    ds = data_source.value if data_source is not None else None
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            pre = check_goal_plan_live_readiness(row, session=s, data_source=ds)
            if not pre.ready:
                detail = "；".join(m.message for m in pre.missing) or pre.summary_zh
                raise HTTPException(status_code=400, detail=detail)
            return compute_holding_goal_plan(
                row,
                session=s,
                start_capital=float(body.start_capital),
                target_capital=float(body.target_capital),
                data_source=ds,
                live_mode=True,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.patch(
    "/holdings/{holding_id}",
    response_model=HoldingOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="更新持仓（数量/成本/备注等）",
)
@limiter.limit("30/minute")
def holdings_update(
    holding_id: int,
    body: HoldingUpdateIn,
    request: Request,
    _: None = Depends(optional_api_key),
):
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.status == HOLDING_STATUS_CLOSED:
            if body.shares is not None or body.cost_price is not None or body.buy_date is not None:
                raise HTTPException(
                    status_code=400,
                    detail="已平仓记录不可改股数/成本/买入日；可 PATCH sell_price、sell_date 或删除后重录",
                )
            if body.sell_price is not None:
                try:
                    row.sell_price = validate_holding_sell_price(
                        float(body.sell_price),
                        shares=float(row.shares),
                        cost_price=float(row.cost_price),
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            if body.sell_date is not None:
                row.sell_date = body.sell_date.isoformat()
            if body.notes is not None:
                row.notes = body.notes.strip() or None
            if body.name is not None:
                row.name = body.name.strip()
            row.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            s.flush()
            s.refresh(row)
            return _holding_one_out(s, row)
        if body.sell_price is not None or body.sell_date is not None:
            raise HTTPException(status_code=400, detail="仅已平仓记录可修改卖出价/卖出日")
        if body.shares is not None:
            row.shares = float(body.shares)
        if body.cost_price is not None:
            row.cost_price = float(body.cost_price)
        if body.buy_date is not None:
            row.buy_date = body.buy_date.isoformat()
        if body.notes is not None:
            row.notes = body.notes.strip() or None
        if body.name is not None:
            row.name = body.name.strip()
        row.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        s.flush()
        s.refresh(row)
        return _holding_one_out(s, row)


@app.post(
    "/holdings/{holding_id}/close",
    response_model=HoldingOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="标记为已平仓",
)
@limiter.limit("30/minute")
def holdings_close(
    holding_id: int,
    body: HoldingCloseIn,
    request: Request,
    _: None = Depends(optional_api_key),
):
    sell_d = (body.sell_date or date.today()).isoformat()
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.status == HOLDING_STATUS_CLOSED:
            raise HTTPException(status_code=400, detail="已是平仓状态")
        try:
            sell_px = validate_holding_sell_price(
                float(body.sell_price),
                shares=float(row.shares),
                cost_price=float(row.cost_price),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        row.status = HOLDING_STATUS_CLOSED
        row.sell_price = sell_px
        row.sell_date = sell_d
        if body.notes and body.notes.strip():
            extra = body.notes.strip()
            row.notes = (row.notes + "\n" if row.notes else "") + f"[平仓] {extra}"
        row.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        s.flush()
        s.refresh(row)
        return _holding_one_out(s, row)


@app.post(
    "/holdings/{holding_id}/reopen",
    response_model=HoldingOut,
    tags=["⑩ 持仓记录（自用）"],
    summary="恢复为持仓中（误点平仓时用）",
)
@limiter.limit("30/minute")
def holdings_reopen(holding_id: int, request: Request, _: None = Depends(optional_api_key)):
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        row.status = HOLDING_STATUS_HOLDING
        row.sell_price = None
        row.sell_date = None
        row.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        s.flush()
        s.refresh(row)
        return _holding_one_out(s, row)


@app.delete(
    "/holdings/{holding_id}",
    tags=["⑩ 持仓记录（自用）"],
    summary="删除一条持仓记录",
)
@limiter.limit("30/minute")
def holdings_delete(holding_id: int, request: Request, _: None = Depends(optional_api_key)):
    with session_scope() as s:
        row = s.execute(select(HoldingRow).where(HoldingRow.id == holding_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        s.delete(row)
    return {"ok": True, "id": holding_id}


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


_JOURNAL_API_TAG = "API · 复盘记录"


# --- 复盘 API（journal / 前向展望，无控制台入口） ---


@app.post(
    "/journal",
    response_model=JournalOut,
    tags=[_JOURNAL_API_TAG],
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
    tags=[_JOURNAL_API_TAG],
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
    tags=[_JOURNAL_API_TAG],
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
    tags=[_JOURNAL_API_TAG],
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
    "/forward-outlook",
    response_model=list[ForwardOutlookOut],
    tags=[_JOURNAL_API_TAG],
    summary="自动前向展望列表（③ 更新后自动登记）",
    description="""
③ `POST /ingest/update` 成功后会对成功拉取的标的**自动**登记：

- **数据质量**：根数、末根收盘、与③返回是否一致等；
- **pending**：基于末根 K 线的 H 日方向演示预测（默认 H=3）；
- **settled**：库内已有 signal 日 + H 个交易日收盘后自动结算实际涨跌。

可通过本接口查询 pending / settled 列表（控制台无独立展示页）。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def forward_outlook_list(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    symbol: str | None = Query(None, description="仅看某 6 位代码"),
    status: str | None = Query(None, description="pending | settled"),
    _: None = Depends(optional_api_key),
):
    settle_all_pending()
    sym: str | None = None
    if symbol and symbol.strip():
        try:
            sym = normalize_symbol(symbol)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    with session_scope() as s:
        q = select(ForwardOutlookRow).order_by(ForwardOutlookRow.id.desc()).limit(limit)
        if sym is not None:
            q = q.where(ForwardOutlookRow.symbol == sym)
        if status and status.strip():
            q = q.where(ForwardOutlookRow.status == status.strip().lower())
        rows = s.execute(q).scalars().all()
        name_map = _stock_names_for_symbols([r.symbol for r in rows], s)
        out_rows: list[ForwardOutlookOut] = []
        for r in rows:
            nm = (r.stock_name or "").strip() or name_map.get(r.symbol, "")
            if nm and not (r.stock_name or "").strip():
                r.stock_name = nm[:64]
            out_rows.append(ForwardOutlookOut(**row_to_dict(r, stock_name=nm)))
        return out_rows


@app.post(
    "/forward-outlook/sync",
    response_model=ForwardOutlookSyncOut,
    tags=[_JOURNAL_API_TAG],
    summary="手动触发前向展望同步（一般不必，③ 已自动）",
)
@limiter.limit("20/minute")
def forward_outlook_sync(
    request: Request,
    body: ForwardOutlookSyncIn = Body(default_factory=ForwardOutlookSyncIn),
    _: None = Depends(optional_api_key),
):
    with session_scope() as s:
        wl = [r.symbol for r in s.execute(select(WatchlistRow)).scalars().all()]
    syms = body.symbols
    if syms:
        out_syms: list[str] = []
        for raw in syms:
            try:
                out_syms.append(normalize_symbol(raw))
            except ValueError:
                continue
        syms = out_syms
    else:
        syms = wl
    if not syms:
        raise HTTPException(status_code=400, detail="自选为空且未传 symbols")
    meta: dict[str, dict[str, Any]] = {}
    with session_scope() as s:
        wl_rows = s.execute(select(WatchlistRow).where(WatchlistRow.symbol.in_(syms))).scalars().all()
        wl_name = {r.symbol: (r.name or "").strip() for r in wl_rows}
    for sym in syms:
        meta[sym] = {"watchlist_name": wl_name.get(sym, "")}
    result = sync_after_ingest(syms, horizon=body.horizon, ingest_meta_by_sym=meta)
    return ForwardOutlookSyncOut(**result)


# --- 根路径与静态控制台 ---


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
            "holdings": "/holdings",
            "research_forecast_validate": "/research/forecast-validate",
            "research_score_bucket_validate": "/research/score-bucket-validate",
            "research_sector_screen": "/research/sector-screen",
            "research_stock_brief": "/research/stock-brief/{symbol}",
            "research_ai_potential": "/research/ai-potential",
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
