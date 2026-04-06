"""
FastAPI 应用入口：健康检查、自选池、行情摄取、信号查询、告警预览与元信息。

启动时 lifespan 内 init_db；多数写操作与敏感读依赖 optional_api_key（配置 API_KEY 时生效）。
限流使用 slowapi，按客户端 IP（get_remote_address）计桶。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import date, datetime, timezone

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import DecisionJournalRow, SignalCacheRow, WatchlistRow, init_db, session_scope
from app.fundamentals import upsert_fundamental_snapshot
from app.ingest import (
    incremental_refresh,
    ingest_symbol_range,
    list_bars_from_db,
    normalize_symbol,
    test_akshare_connectivity,
)
from app.schemas import (
    AlertsPreviewIn,
    DailyBarOut,
    DisclaimerOut,
    ForecastValidateOut,
    IngestDataSource,
    IngestUpdateIn,
    JournalIn,
    JournalOut,
    SelfUseMetaOut,
    SignalOut,
    WatchlistIn,
    WatchlistItem,
)
from app.forecast_validate import run_forecast_validate
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
            logger.warning("pre_refresh before signal %s route=%s: %s", sym, route, e)


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
            {"value": "sina", "label": "仅新浪财经（AkShare）"},
            {"value": "tencent", "label": "仅腾讯（AkShare，无成交量则记 0）"},
            {"value": "baostock", "label": "仅 Baostock（开源证券数据）"},
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
    "/watchlist",
    response_model=list[WatchlistItem],
    tags=["② 管理自选股票"],
    summary="列出当前已添加的股票",
    description="""
返回自选池里**所有**股票代码列表。

- 若配置了 `API_KEY`，请先点右上角 **Authorize**。
- 若列表为空，下一步请用 `POST /watchlist` 添加。
""",
)
@limiter.limit(get_settings().rate_limit_default)
def watchlist_list(request: Request, _: None = Depends(optional_api_key)):
    """列出自选池全部标的（按 id 升序）。"""
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow).order_by(WatchlistRow.id.asc())).scalars().all()
        # 须在会话内读出标量，否则关闭 Session 后会触发 DetachedInstanceError → 500
        symbols = [r.symbol for r in rows]
    return [WatchlistItem(symbol=sym) for sym in symbols]


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
            return WatchlistItem(symbol=sym)
        s.add(WatchlistRow(symbol=sym))
    return WatchlistItem(symbol=sym)


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
    "/ingest/update",
    tags=["③ 更新行情数据"],
    summary="下载/更新自选股票的日线行情",
    description="""
**添加自选后必做的一步**（否则后面算信号会提示数据不够）。

- **Body 可留空**（增量更新到今天）。
- 传 **`start_date` + `end_date`**：按该闭区间拉取；仅 **`start_date`**：从该日拉到今日；仅 **`end_date`**：增量更新到该日。
- **`data_source`**：行情路线（`auto` / `eastmoney` / `sina` / `tencent` / `baostock`）；不传则用环境变量 **`INGEST_DATA_SOURCE`**（默认 `auto`）。`eastmoney` 为东财日线，服务端对相邻请求有 **3–5 秒随机间隔**（可用 `EASTMONEY_REQUEST_MIN_INTERVAL_SEC` / `MAX` 调整）。
- **`GET /ingest/test-connection`**：探测本机能否访问数据源（短区间测试，需 API Key 时同上）；可带 Query **`data_source`**。
- 需要能访问外网（通过 AkShare 拉公开数据）。
- 自选为空时会返回错误，请先用 `POST /watchlist` 添加股票。
- 某一只股票拉取失败时，结果里该条会带 `error`，其它股票仍会继续。

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
        rows = s.execute(select(WatchlistRow)).scalars().all()
        symbols = [r.symbol for r in rows]
    if not symbols:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
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
        try:
            results.append(
                ingest_symbol_range(sym, range_start=st, range_end=en, data_source=ds),
            )
        except ValueError as e:
            results.append({"symbol": sym, "error": str(e)})
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})
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
对自选池**每一只**拉取并入库：

- **估值**：东财沪深京 A 股列表中的市盈率(动)、市净率（全表有短 TTL 内存缓存，减轻限流）。
- **成长**：最近一期财报的营业收入/归属净利润**同比 %**（`stock_financial_analysis_indicator_em`）。
- **资金流**：最近交易日**主力净流入净额**（东财日级）。

完成后 `GET /signals` 会读取本地快照，在技术面得分上做 **有界** 合成（通常 ±15 分）。**非投资建议**；接口有频率限制，勿连续狂点。
""",
)
@limiter.limit("12/minute")
def ingest_fundamentals(request: Request, _: None = Depends(optional_api_key)):
    """自选批量扩展因子 upsert；单条失败体现在该条 `error` 字段。"""
    with session_scope() as s:
        rows = s.execute(select(WatchlistRow)).scalars().all()
        symbols = [r.symbol for r in rows]
    if not symbols:
        raise HTTPException(status_code=400, detail="自选池为空，请先 POST /watchlist 添加标的")
    pause = max(0.0, float(get_settings().akshare_pause_between_symbols_sec))
    results: list[dict] = []
    for i, sym in enumerate(symbols):
        if i > 0 and pause > 0:
            time.sleep(pause)
        results.append(upsert_fundamental_snapshot(sym))
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
- 若某只股票从未成功拉取过 K 线，该只会被跳过（可去看服务日志）。
- **建议先执行** `POST /ingest/update`。
- **pre_refresh**：为 `true` 时先按 **data_source**（或服务端默认路线）对各标的做**增量拉取**再算信号（与③所选路线一致时需传相同 `data_source`）。响应头 `X-Quant-Data-Source` / `X-Quant-Pre-Refresh` 供控制台展示。
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
    for sym in symbols:
        try:
            out.append(compute_signal(sym, data_source=route))
        except Exception as e:
            logger.warning("signal failed %s: %s", sym, e)
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
- **策略**：**双均线**（短/长周期可配，默认 5/10）、趋势规则、周期性重训 **Logistic**（NumPy）、因果多数类基线。
- **特征**（Logistic 用）：ret5/ret20、MA20 斜率、量比、距 60 日高回撤、20 日实现波动（均仅用 t 及以前数据）。
- **买卖示意**：信号由空转多记买入收盘、由多转空记卖出收盘；返回最近若干笔区间涨跌与汇总（非实盘、未扣费）。
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
    _: None = Depends(optional_api_key),
):
    try:
        sym = normalize_symbol(symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        raw = run_forecast_validate(
            sym,
            horizon=horizon,
            min_train_rows=min_train_rows,
            retrain_every=retrain_every,
            trade_limit=trade_limit,
            ma_short=ma_short,
            ma_long=ma_long,
        )
        return ForecastValidateOut.model_validate(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
            "docs": "/docs",
            "self_use": "/meta/self-use",
            "journal": "/journal",
            "research_forecast_validate": "/research/forecast-validate",
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


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
