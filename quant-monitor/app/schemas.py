"""
Pydantic 请求/响应模型：与 FastAPI 的 response_model、请求体验证对齐。

类型别名（Literal）约束 trend / strength / position_hint 的合法取值，便于 OpenAPI 展示枚举。
"""

from datetime import date
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

TrendRegime = Literal["bullish", "sideways", "bearish"]
StrengthRegime = Literal["strong", "neutral", "weak"]
PositionHint = Literal["avoid", "cautious", "trial", "moderate"]


class IngestDataSource(str, Enum):
    """行情拉取路线（与 Body / Query / 环境变量 INGEST_DATA_SOURCE 一致）。"""

    auto = "auto"
    eastmoney = "eastmoney"
    akshare = "akshare"
    sina = "sina"
    tencent = "tencent"
    baostock = "baostock"
    mootdx = "mootdx"
    tushare = "tushare"


class CancelBatchIn(BaseModel):
    """POST /meta/cancel-batch：中断进行中的批量任务（与控制台「取消请求」配合）。"""

    scopes: list[str] = Field(
        default_factory=lambda: ["all"],
        description="ingest / signals / alerts / fundamentals / pre_refresh / hot_sectors / sector_screen / all",
    )


class AlertsPreviewIn(BaseModel):
    """POST /alerts/preview：可选先按指定路线增量更新日线再对比信号。"""

    pre_refresh: bool = Field(
        False,
        description="为 true 时先对自选各标的按 data_source 执行增量 ingest，再计算信号并对比缓存",
    )
    data_source: IngestDataSource | None = Field(
        None,
        description="行情路线，与 POST /ingest/update 一致；不传则用服务端 INGEST_DATA_SOURCE",
    )


class IngestSymbolSubsetOptional(BaseModel):
    """可选：只处理自选中的部分代码（日线更新 / 扩展因子等共用）。"""

    symbols: list[str] | None = Field(
        default=None,
        description="仅处理列表中的代码（须在自选池内）；不传、null 或去重后为空表示自选全部。最多 200 个。",
    )

    @field_validator("symbols", mode="before")
    @classmethod
    def _symbols_strip(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            return v
        out = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return out or None

    @field_validator("symbols", mode="after")
    @classmethod
    def _symbols_cap(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) > 200:
            raise ValueError("symbols 最多 200 个代码")
        return v


class IngestUpdateIn(IngestSymbolSubsetOptional):
    """POST /ingest/update 可选参数。"""

    start_date: date | None = Field(
        None,
        description="区间起始日（含当日），YYYY-MM-DD。与 end_date 同时传则按固定区间拉取；仅传 start 则拉到今日；仅传 end 则按「增量到该日」逻辑。",
    )
    end_date: date | None = Field(
        None,
        description="区间结束日（含当日），YYYY-MM-DD；不传则结束日默认为今天（与 start 组合时）。",
    )
    data_source: IngestDataSource | None = Field(
        None,
        description=(
            "行情路线：不传则用服务端默认（INGEST_DATA_SOURCE）。auto=新浪→腾讯→Baostock；"
            "eastmoney 与 akshare 等价（均为东财日线，带请求间隔）；mootdx=通达信协议；"
            "tushare=TuShare 日线（需 TUSHARE_TOKEN 或配置 tushare_token）；其余为仅使用该源。"
        ),
    )
    include_fundamentals: bool = Field(
        False,
        description=(
            "为 true 时，在日线入库完成后对同一批标的（同 symbols 子集规则）"
            "拉取扩展因子并写入 fundamental_snapshots：PE/PB、营收与净利同比、财报期、"
            "ROE/ROA/毛利率/净利率、资产负债率/流动比/速动比、每股经营现金流、主力净流入与资金流日期。"
        ),
    )


class IngestFundamentalsIn(IngestSymbolSubsetOptional):
    """POST /ingest/fundamentals：可选 symbols 子集，其余字段无。"""


class WebDataPreviewIn(BaseModel):
    """POST /ingest/web-data-preview：单标的拉取 K 线（可写库）与东财日级资金流预览。"""

    symbol: str = Field(..., description="6 位 A 股代码（可带交易所前缀，服务端会规范化）", examples=["600519"])
    data_source: IngestDataSource | None = Field(
        None,
        description="K 线增量写入使用的路线；不传则用服务端 INGEST_DATA_SOURCE",
    )
    refresh_kline: bool = Field(
        True,
        description="为 true 时先对该标的执行 incremental_refresh（联网写入本地 bars），再读库返回 K 线",
    )
    bar_limit: int = Field(60, ge=1, le=500, description="返回最近多少根已入库日线（含 OHLCV、change_pct）")
    fund_flow_recent_days: int = Field(
        10,
        ge=1,
        le=120,
        description="资金流表返回最近多少个交易日的行（东财日级；「当日」以数据源最新行为准）",
    )


WatchlistOrigin = Literal["manual", "auto_hot", "auto_quant"]


class SelectorSectorDataSource(str, Enum):
    """热门板块/成分股选股路线。"""

    akshare = "akshare"
    mootdx = "mootdx"
    tushare = "tushare"


class HotPickConditionGroup(str, Enum):
    """热门板块候选：可选的选股条件组（可多选，结果分表展示）。"""

    sector_hot = "sector_hot"
    ma5_capital = "ma5_capital"
    rising_3d = "rising_3d"


class WatchlistIn(BaseModel):
    """POST /watchlist 请求体：原始代码字符串，服务端会规范为 6 位数字。"""

    symbol: str = Field(
        ...,
        description="股票代码，填 6 位数字即可（可带 sz/sh 等前缀，系统会自动去掉）。示例：茅台 600519，平安银行 000001。",
        examples=["600519"],
    )
    auto_ingest_kline: bool = Field(
        True,
        description="为 true 时添加成功后自动联网拉取近 ingest_days 个日历日的日线并写入本地 bars（默认开启）",
    )
    ingest_days: int | None = Field(
        None,
        ge=7,
        le=120,
        description="自动拉取日线的日历天数；不传则用服务端 WATCHLIST_AUTO_INGEST_DAYS（默认 30）",
    )
    data_source: IngestDataSource | None = Field(
        None,
        description="自动拉取使用的行情路线；不传则用 INGEST_DATA_SOURCE",
    )


class WatchlistBatchDeleteIn(BaseModel):
    """POST /watchlist/batch-delete：按代码列表批量删除自选。"""

    symbols: list[str] = Field(
        ...,
        min_length=1,
        max_length=300,
        description="待删除代码列表；无效项忽略，规范化后去重再执行删除",
    )


class WatchlistBatchDeleteOut(BaseModel):
    ok: bool = True
    removed: int = Field(..., description="数据库中实际删除的行数")
    requested_unique: int = Field(..., description="规范化并去重后的有效代码数")


class WatchlistDeleteAllIn(BaseModel):
    """POST /watchlist/delete-all：按范围一次性清空自选。"""

    scope: str = Field(
        "all",
        description="all=删除自选池全部记录；auto=仅删除热门自动与量化自动，保留手动",
    )


class WatchlistDeleteAllOut(BaseModel):
    ok: bool = True
    removed: int = Field(..., description="实际删除的行数")
    scope: str = Field(..., description="本次删除范围：all 或 auto")


class WatchlistHotSnapshotImportIn(BaseModel):
    """POST /watchlist/import-hot-market-snapshot：从 hot_market_snapshot.json 导入热门股到自选。"""

    replace_auto_pool: bool = Field(
        True,
        description="为 true 时先删除全部 auto_hot 与 auto_quant，再写入快照中的热门股（来源 auto_hot）；手动条目保留并跳过",
    )


class WatchlistHotSnapshotImportOut(BaseModel):
    added: int = Field(..., description="新写入的 auto_hot 条数")
    skipped_existing_manual: int = Field(..., description="已在自选且为手动的代码数（跳过）")
    removed_auto: int = Field(..., description="若 replace_auto_pool，为删除的 auto_hot+auto_quant 条数；否则为 0")
    snapshot_stock_rows: int = Field(..., description="快照 JSON 中 stocks 列表长度")
    candidates: int = Field(..., description="规范化去重后可尝试导入的代码数")
    warnings: list[str] = Field(default_factory=list, description="无效代码等提示")


class WatchlistItem(BaseModel):
    """自选列表单项输出。"""

    symbol: str
    name: str = Field("", description="证券简称；未知或未解析时为空字符串")
    origin: WatchlistOrigin = Field(
        "manual",
        description="manual=手动；auto_hot=热门板块自动填充；auto_quant=⑨量化选股同步（与 auto_hot 同属自动池，会互清）",
    )
    bars_last_ingested_at: str | None = Field(
        None,
        description="该标的在本地 bars 中最近一次写入/覆盖的 UTC ISO 时间（无则尚未经本服务入库）",
    )
    bars_last_trade_date: str | None = Field(
        None,
        description="本地库中最新一根日线的交易日 YYYY-MM-DD",
    )
    last_close: float | None = Field(
        None,
        description="上一字段对应交易日的收盘价（日线，非实时 tick）",
    )
    display_prev_close: float | None = Field(
        None,
        description="昨日收盘展示价：与③ 上行「收/昨」一致（resolve_ingest_row_display_pair）",
    )
    display_prev_trade_date: str | None = Field(
        None,
        description="昨日收盘对应交易日 YYYY-MM-DD",
    )
    display_pair_basis: str | None = Field(
        None,
        description="昨收参照依据：如 last_close_as_ingest_prev_ref、exec_same_as_last_bar",
    )
    ingest_exec_date: str | None = Field(
        None,
        description="东八区展示用执行自然日（与③ 对齐昨/今双行）",
    )
    last_daily_close_label: str | None = Field(
        None,
        description="给人看的「最后收盘」说明（含交易日与 A 股常规收盘时刻）",
    )
    spot_last_price: float | None = Field(
        None,
        description="现价：东财单股/全 A 列表；东财失败时通达信批量快照兜底（非交易所 tick）",
    )
    spot_change_pct: float | None = Field(
        None,
        description="快照涨跌幅（%），与 spot_last_price 同源",
    )
    spot_quote_date: str | None = Field(
        None,
        description="快照列表中的日期/更新时间（若有，YYYY-MM-DD）",
    )
    kline_ingest_ok: bool | None = Field(
        None,
        description="本次 POST /watchlist 是否已成功执行自动日线入库；未开启 auto_ingest_kline 时为 null",
    )
    kline_ingest_error: str | None = Field(
        None,
        description="自动拉取失败时的简要原因；成功时为 null",
    )
    kline_ingest_rows: int | None = Field(
        None,
        description="本次自动入库 upsert 的 K 线条数（成功时）",
    )


class QuantWatchlistStockRowIn(BaseModel):
    """单条待写入自选的量化结果行（与 sector-screen 返回的 stocks 项字段对齐）。"""

    code: str = Field(..., description="股票代码，可为带前缀形式，服务端会规范为 6 位数字")
    name: str = Field("", description="证券简称，可空")


class WatchlistBatchAddIn(BaseModel):
    """POST /watchlist/batch-add：批量加入自选（来源 manual，不删其它条目）。"""

    stocks: list[QuantWatchlistStockRowIn] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="至少含 code；name 可空。已存在则保留并标为手动（不覆盖已有简称）",
    )


class WatchlistBatchAddOut(BaseModel):
    ok: bool = True
    added: int = Field(..., description="新写入条数")
    updated: int = Field(..., description="已在池中、本次改为或保持手动的条数")
    skipped: int = Field(0, description="无效代码条数")
    warnings: list[str] = Field(default_factory=list)


class WatchlistReplaceAllIn(BaseModel):
    """POST /watchlist/replace-all：用给定列表完全替换自选池（删除全部既有记录后写入）。"""

    stocks: list[QuantWatchlistStockRowIn] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="按顺序写入；每项至少含 code，name 可空（服务端会尝试补全简称）",
    )


class WatchlistReplaceAllOut(BaseModel):
    removed: int = Field(..., description="删除的原自选行数（含手动、热门自动、量化自动）")
    added: int = Field(..., description="新写入条数（来源均为 auto_hot）")
    warnings: list[str] = Field(default_factory=list, description="无效代码等提示")


class QuantWatchlistSyncIn(BaseModel):
    """POST /watchlist/sync-from-quant-screen：将⑨选股结果写入自选（origin=auto_quant）。"""

    stocks: list[QuantWatchlistStockRowIn] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="来自 POST /research/sector-screen 的 stocks；至少含 code",
    )


class QuantWatchlistSyncOut(BaseModel):
    """量化选股结果写入自选的统计。"""

    added: int = Field(..., description="新写入的 auto_quant 条数")
    skipped_existing_manual: int = Field(..., description="已在自选且为手动的代码数（跳过）")
    removed_auto: int = Field(..., description="写入前删除的 auto_hot + auto_quant 条数")
    warnings: list[str] = Field(default_factory=list, description="如同一输入中重复代码等提示")


class FillHotSectorsIn(BaseModel):
    """POST /watchlist/fill-hot-sectors：按热门板块写入自选（仅新增 auto_hot，不覆盖手动）。"""

    top_sectors: int = Field(5, ge=1, le=200, description="仅 sector_hot：取排名前多少板块")
    stocks_per_sector: int = Field(5, ge=1, le=50, description="仅 sector_hot：每板块过滤 ST/科创板后至多几只")
    board_type: str = Field(
        "all",
        description="板块类型：all / concept / industry（与核心数据源一致）",
    )
    exclude_st: bool = Field(True, description="sector_hot：排除名称含 ST/*ST 的成分股")
    exclude_kcb: bool = Field(True, description="sector_hot：排除科创板 688/689")
    ma5_exclude_st: bool = Field(True, description="ma5_capital：排除名称含 ST/*ST 的成分股")
    ma5_exclude_kcb: bool = Field(True, description="ma5_capital：排除科创板 688/689")
    rising_3d_exclude_st: bool = Field(True, description="rising_3d：排除名称含 ST/*ST 的成分股")
    rising_3d_exclude_kcb: bool = Field(True, description="rising_3d：排除科创板 688/689")
    selector_data_source: SelectorSectorDataSource = Field(
        ...,
        description="akshare（东财板块较全）、mootdx（通达信板块较少）或 tushare（同花顺 ths_index/ths_daily/ths_member，通常需 6000 积分）",
    )
    use_sector_snapshot: bool = Field(
        True,
        description="是否优先使用本地板块热度快照；为 false 时强制重新请求最新板块数据，并刷新快照",
    )
    tushare_token: str | None = Field(
        None,
        description="selector_data_source=tushare 时必填（除非服务端已配置 TUSHARE_TOKEN / tushare_token）；请勿写入版本库",
    )
    sort_by_trend_strength: bool = Field(
        True,
        description="板块内按技术面趋势强度重新排序（会为候选股拉日线并计算 screen）",
    )
    require_technical_pass: bool = Field(
        False,
        description="仅保留技术面初筛通过（evaluate_screen.passed=true）的股票",
    )
    exclude_overextended: bool = Field(
        False,
        description="剔除近 20 日累计涨幅超过阈值的股票（用于避免短线过热追高）",
    )
    max_return_20d_pct: float = Field(
        25.0,
        ge=0,
        le=500,
        description="exclude_overextended=true 时使用：近 20 日累计涨幅上限（%）",
    )
    enable_liquidity_filter: bool = Field(
        False,
        description="按近 20 日平均成交额过滤流动性不足的股票",
    )
    min_avg_turnover_20d_100m: float = Field(
        1.0,
        ge=0,
        le=10000,
        description="enable_liquidity_filter=true 时使用：近 20 日平均成交额下限，单位亿元",
    )
    pick_condition_groups: list[HotPickConditionGroup] = Field(
        default_factory=lambda: [
            HotPickConditionGroup.sector_hot,
            HotPickConditionGroup.ma5_capital,
        ],
        min_length=1,
        description="选股条件组（可多选）：sector_hot=热门板块筛选；ma5_capital=五日强承接；rising_3d=连续三日上涨",
    )
    ma5_stand_min_days: int = Field(
        3,
        ge=2,
        le=10,
        description="连续站上五日线最少交易日数（收盘>=MA5）",
    )
    capital_flow_lookback_days: int = Field(
        3,
        ge=1,
        le=10,
        description="资金承接判定：考察最近几个交易日的主力净流入",
    )
    capital_min_positive_days: int = Field(
        2,
        ge=1,
        le=10,
        description="上述窗口内至少几日主力净流入为正，且合计为正",
    )

    @field_validator("pick_condition_groups", mode="before")
    @classmethod
    def _normalize_pick_condition_groups(cls, v: object) -> list[str]:
        if v is None:
            return [HotPickConditionGroup.sector_hot.value, HotPickConditionGroup.ma5_capital.value]
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            raise ValueError("pick_condition_groups 须为列表")
        out: list[str] = []
        seen: set[str] = set()
        allowed = {e.value for e in HotPickConditionGroup}
        for item in v:
            key = str(getattr(item, "value", item)).strip().lower()
            if not key or key in seen:
                continue
            if key not in allowed:
                raise ValueError(f"不支持的选股条件: {key}，可选: {', '.join(sorted(allowed))}")
            seen.add(key)
            out.append(key)
        if not out:
            raise ValueError("请至少选择一种选股条件（sector_hot / ma5_capital / rising_3d）")
        return out


class FillHotSectorsSummary(BaseModel):
    """热门填充结果摘要。"""

    added: int = Field(..., description="本次新写入的 auto_hot 条数")
    skipped_existing_manual: int = Field(
        ...,
        description="已在自选且为手动的代码数（自动列表跳过，不覆盖）",
    )
    removed_auto: int = Field(..., description="填充前删除的旧 auto_hot + auto_quant 条数")
    warnings: list[str] = Field(default_factory=list, description="选股过程中的提示（如某板块成分不足）")
    progress_log: list[str] = Field(
        default_factory=list,
        description="服务端筛选过程日志（回传控制台 #hotPickLog，便于确认首选数据源是否在运转）",
    )


class FillHotSectorsOut(BaseModel):
    """热门板块填充或预览的完整响应。"""

    sectors_detail: list[dict[str, Any]] = Field(
        ...,
        description="按热度顺序的板块列表（原热门筛选条件）；每项含 sector_rank、sector_metrics、stocks",
    )
    ma5_capital_sectors_detail: list[dict[str, Any]] = Field(
        default_factory=list,
        description="同一批热门板块下「连续站上 MA5 + 资金承接强」候选，与原表分开展示",
    )
    rising_3d_sectors_detail: list[dict[str, Any]] = Field(
        default_factory=list,
        description="同一批热门板块下「连续三日上涨」候选，与其他组分表展示",
    )
    pick_condition_groups: list[str] = Field(
        default_factory=list,
        description="本次实际执行的选股条件组（与请求一致）",
    )
    summary: FillHotSectorsSummary


class SectorScreenDataSource(str, Enum):
    """与命令行 `quant_stock_selector.py --data-source` 一致。"""

    hot_chain = "hot_chain"
    baostock = "baostock"
    akshare = "akshare"
    tushare = "tushare"
    mootdx = "mootdx"


class SectorScreenPoolMode(str, Enum):
    """未指定 sector、symbols 时，股票池如何构建。"""

    hot_sectors = "hot_sectors"
    universe = "universe"


UniverseBoardSegment = Literal["sh_sz_main", "cyb", "kcb", "bj"]


class SectorScreenIn(BaseModel):
    """
    对应仓库根目录 `quant_stock_selector.py` / 包 `app.quant_stock_selector` 的流水线：
    热门板块或指定板块/代码列表 → 拉行情 →（可选）末根 K 线策略筛选 → 技术面初筛 + 双均线回测 → 综合分。
    """

    data_source: SectorScreenDataSource = Field(
        SectorScreenDataSource.mootdx,
        description=(
            "行情与板块数据源：hot_chain=新浪优先热门链+快照；baostock=**日 K 用 Baostock**（较稳），"
            "板块/成分仍东财；akshare=东财全线；mootdx=通达信；tushare=同花顺（需 token+积分）"
        ),
    )
    tushare_token: str | None = Field(None, description="data_source=tushare 时使用，或服务端已配置 TUSHARE_TOKEN")
    hot_chain_prefer_snapshot: bool = Field(
        True,
        description="data_source=hot_chain 时：为 true 则优先使用本地 data/hot_market_snapshot.json，不存在再联网拉链",
    )
    hot_chain_refresh_snapshot: bool = Field(
        False,
        description="data_source=hot_chain 时：为 true 则忽略本地快照，先按链重拉并覆盖 hot_market_snapshot.json 再选股",
    )
    include_hot_snapshot_stocks: bool = Field(
        False,
        description=(
            "仅「热门板块」模式（未指定 sector、未传 symbols）有效：为 true 时除各热门板块成分股外，"
            "再并入本地 data/hot_market_snapshot.json 中的热门股列表，去重后与板块股统一拉日线并做双均线回测与综合分"
        ),
    )
    hot_snapshot_stocks_cap: int = Field(
        80,
        ge=1,
        le=500,
        description="并入快照热门股时，按文件顺序至多取几只（过大易超时）；已在板块池内的代码会跳过",
    )
    pool_mode: SectorScreenPoolMode = Field(
        SectorScreenPoolMode.hot_sectors,
        description=(
            "仅当未传 sector、未传 symbols 时生效：hot_sectors=按热门板块排名取池；"
            "universe=从当前 data_source 的全市场股票列表顺序截取 universe_max_stocks 只（不经热门板块/热门股）"
        ),
    )
    universe_max_stocks: int = Field(
        200,
        ge=1,
        le=500,
        description="pool_mode=universe 时，从 get_stock_universe 取前多少只参与日线与回测",
    )
    universe_segments: list[UniverseBoardSegment] = Field(
        default_factory=lambda: ["sh_sz_main"],
        description=(
            "仅 pool_mode=universe 时生效：按代码所属市场板块过滤（多选取并集）。"
            "sh_sz_main=沪/深主板与原中小板（00/60 开头且非 688/689）；cyb=创业板 300/301；"
            "kcb=科创板 688/689；bj=北交所常见前缀。在列表顺序下取满 universe_max_stocks 只。"
        ),
    )
    universe_exclude_st: bool = Field(
        True,
        description="pool_mode=universe 时：为 true 则排除名称含 ST/*ST/＊ST 的股票",
    )
    universe_scan_cap: int = Field(
        3000,
        ge=500,
        le=8000,
        description=(
            "仅 pool_mode=universe 且启用「双均线/三均线」策略筛选时生效：从全市场列表按顺序至多扫描多少只，"
            "在末根 K 线满足条件者中按策略强度取 universe_max_stocks 只再完整评估；未启用策略筛选时忽略。"
        ),
    )
    board_type: Literal["all", "concept", "industry"] = "all"
    top_sectors: int = Field(5, ge=1, le=60, description="热门板块模式下取前 N 个板块")
    max_stocks_per_sector: int = Field(
        8,
        ge=1,
        le=100,
        description="每板块（或自定义列表视为单池）最多分析几只，宜小以免超时",
    )
    start_date: str = Field(
        "20230101",
        description="历史行情起始 YYYYMMDD（可传 YYYY-MM-DD，服务端会规范化）",
    )
    end_date: str = Field(
        default_factory=lambda: date.today().strftime("%Y%m%d"),
        description="历史行情结束 YYYYMMDD",
    )
    sector: str | None = Field(
        None,
        max_length=128,
        description="若填写则只分析该板块（与热门模式互斥）；与 symbols 互斥",
    )
    symbols: list[str] | None = Field(
        None,
        description="自定义 6 位代码列表（与 sector、默认热门模式互斥）；等价于 --codes CSV",
    )
    adjust: str = Field("qfq", description="复权方式，AkShare 常用 qfq")
    fast_period: int = Field(10, ge=2, le=120)
    slow_period: int = Field(30, ge=3, le=250)
    initial_cash: float = Field(100_000.0, gt=0)
    commission: float = Field(0.001, ge=0, le=0.05)
    stop_loss: float = Field(0.08, gt=0, le=0.5)
    scoring_strategy: Literal["v2", "v1"] = Field(
        "v2",
        description="综合评分策略：v2（短线模式下自动用 v2_short 权重）/ v1（旧版，板块热度权重更高）",
    )
    screen_mode: Literal["short_term", "legacy"] = Field(
        "short_term",
        description=(
            "技术面初筛模式。short_term=短线强化（MA5/10、5日涨跌、MA20斜率、末根量比、贴近新高、"
            "回撤与波动过滤；≥60 根日线）；legacy=原规则（≥120 根，收盘>MA20>MA60 等）"
        ),
    )
    only_passed: bool = Field(
        False,
        description="为 true 时仅保留初筛通过的股票（short_term 模式下为短线初筛通过）",
    )
    top_stocks_limit: int = Field(40, ge=1, le=500, description="响应中最多返回多少条股票结果")
    show_dual_ma_strategy: bool = Field(
        False,
        description=(
            "为 true 时：① 仅保留末根 K 线出现快慢线金叉的标的；② 仍计算并返回 dual_ma_* 对照列。"
            "与综合分所用默认回测（含 MA20/60 过滤）无关。"
        ),
    )
    show_triple_ma_strategy: bool = Field(
        False,
        description=(
            "为 true 时：① 仅保留末根 K 线满足收盘>MA短>MA中>MA长（三均线多头）的标的；② 仍计算并返回 triple_ma_* 对照列。"
            "两项均勾选时为同时满足（且）。"
        ),
    )
    show_ma5_stand_strategy: bool = Field(
        False,
        description=(
            "为 true 时：① 仅保留末根收盘>=MA5 的标的；② 返回 ma5_stand_count（最近 ma5_stand_lookback 根内站上五日线次数），"
            "用于评估后续上涨强度。"
        ),
    )
    ma5_stand_lookback: int = Field(
        60,
        ge=10,
        le=250,
        description="show_ma5_stand_strategy 时：统计最近多少根 K 线内收盘>=MA5 的次数",
    )
    show_ma5_stand_3d_strategy: bool = Field(
        False,
        description=(
            "为 true 时：仅保留末根起连续至少 ma5_stand_3d_min_days 个交易日"
            "「收盘>=MA5 且收盘>=前一交易日收盘」的标的；返回 ma5_consecutive_stand_days。"
        ),
    )
    ma5_stand_3d_min_days: int = Field(
        3,
        ge=2,
        le=10,
        description="连续站上五日线且不跌的最少交易日数，默认 3",
    )

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _norm_ymd(cls, v: object) -> str:
        if v is None:
            return date.today().strftime("%Y%m%d")
        s = str(v).strip().replace("-", "")
        if len(s) != 8 or not s.isdigit():
            raise ValueError("start_date/end_date 须为 YYYYMMDD 或 YYYY-MM-DD")
        return s

    @field_validator("universe_segments", mode="before")
    @classmethod
    def _v_univ_seg(cls, v: object) -> list[str]:
        allowed = {"sh_sz_main", "cyb", "kcb", "bj"}
        if v is None:
            return ["sh_sz_main"]
        if not isinstance(v, list):
            return ["sh_sz_main"]
        out = [str(x).strip() for x in v if str(x).strip() in allowed]
        out = list(dict.fromkeys(out))
        if not out:
            return sorted(allowed)
        return out

    @model_validator(mode="after")
    def _exclusive_modes(self):
        if self.symbols and self.sector:
            raise ValueError("不能同时指定 symbols 与 sector")
        if self.symbols is not None and len(self.symbols) > 500:
            raise ValueError("symbols 最多 500 条")
        if self.include_hot_snapshot_stocks and (
            (self.symbols is not None and len(self.symbols) > 0)
            or (self.sector is not None and str(self.sector).strip() != "")
        ):
            object.__setattr__(self, "include_hot_snapshot_stocks", False)
        return self


class SectorScreenOut(BaseModel):
    """选股流水线 JSON 结果。"""

    sectors: list[dict[str, Any]]
    stocks: list[dict[str, Any]]
    stocks_total: int
    start_date: str = Field(
        ...,
        description="本次用于拉日线的起始日（与请求体一致，YYYYMMDD）",
    )
    end_date: str = Field(
        ...,
        description="结束日（YYYYMMDD）",
    )
    disclaimer: str
    note: str = Field(
        "",
        description="与命令行脚本差异说明（若有）",
    )
    show_dual_ma_strategy: bool = Field(
        False,
        description="与请求一致：为 true 时已按末根金叉筛选并返回 dual_ma_*",
    )
    show_triple_ma_strategy: bool = Field(
        False,
        description="与请求一致：为 true 时已按末根三均线多头筛选并返回 triple_ma_*",
    )
    show_ma5_stand_strategy: bool = Field(
        False,
        description="与请求一致：为 true 时已按末根站上 MA5 筛选并返回 ma5_stand_count",
    )
    ma5_stand_lookback: int = Field(
        60,
        description="与请求一致：ma5_stand_count 的统计窗口（根 K 线）",
    )
    show_ma5_stand_3d_strategy: bool = Field(
        False,
        description="与请求一致：为 true 时已按连续站上 MA5 且不跌筛选",
    )
    ma5_stand_3d_min_days: int = Field(
        3,
        description="与请求一致：连续天数下限",
    )


class SectorConstituentsTopIn(BaseModel):
    """POST /research/sector-constituents-top：仅拉板块成分（不跑 K 线与回测），用于控制台「前 10 成分」。"""

    sector_name: str = Field(..., min_length=1, max_length=128, description="板块名称，与列表中「板块」列一致")
    board_type: Literal["concept", "industry", "all"] = Field(
        "all",
        description="与列表行「类型」一致；all 时由数据源在全表内按名称解析板块",
    )
    data_source: SectorScreenDataSource = Field(
        SectorScreenDataSource.mootdx,
        description="与 POST /research/sector-screen 的 data_source 一致；成分股仍由各源对应接口拉取",
    )
    tushare_token: str | None = Field(None, description="data_source=tushare 时使用")
    hot_chain_prefer_snapshot: bool = Field(
        True,
        description="data_source=hot_chain 时是否优先读本地 hot_market_snapshot.json",
    )
    hot_chain_refresh_snapshot: bool = Field(
        False,
        description="data_source=hot_chain 时是否强制重拉快照再解析板块表",
    )
    limit: int = Field(10, ge=1, le=50, description="返回成分条数上限（按数据源原始顺序，过滤 ST/科创板后截取）")
    exclude_st: bool = Field(True, description="名称含 ST 的成分是否排除")
    exclude_kcb: bool = Field(True, description="是否排除 688/689 科创板代码")


class SectorConstituentsTopOut(BaseModel):
    """板块成分预览。"""

    sector_name: str
    board_type: str | None = Field(None, description="成分 DataFrame 中解析到的板块类型（若有）")
    stocks: list[dict[str, Any]]
    stocks_total: int = Field(..., description="本次响应中返回的条数，至多 limit")
    constituents_total_after_filter: int = Field(
        0,
        description="过滤 ST/科创板后，该板块在数据源中的可交易成分总数（可能大于返回的 stocks 条数）",
    )
    disclaimer: str
    note: str = Field(
        "",
        description="说明：成分股列表；服务端可合并东财 spot 与个股日级资金流向（收盘价/交易日/大单小单净占比、成交量额）",
    )


class DailyBarOut(BaseModel):
    """本地 SQLite 中的单根日线（前复权）；用于控制台与接口展示真实 OHLCV。"""

    trade_date: str = Field(..., description="交易日 YYYY-MM-DD")
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(..., description="成交量（股/手依数据源，与入库一致）")
    amount: float = Field(0, description="成交额（元，缺省为 0）")
    change_pct: float | None = Field(
        None,
        description="较前一日收盘涨跌幅 %；返回区间内的第一根为 null",
    )


class SignalReason(BaseModel):
    """信号解释：机器可读 code + 人类可读 text。"""

    code: str
    text: str


class FundamentalPanel(BaseModel):
    """扩展因子面板（与库表 / 远端字段对齐）；缺失字段为 None。"""

    pe_dynamic: float | None = Field(None, description="市盈率（动态），来自东财全 A 列表")
    pb: float | None = Field(None, description="市净率")
    revenue_yoy_pct: float | None = Field(None, description="营业收入同比 %，最近一期财报")
    profit_yoy_pct: float | None = Field(None, description="归属净利润同比 %，最近一期财报")
    financial_report_date: str | None = Field(None, description="上述同比对应的报告期日期")
    main_net_inflow: float | None = Field(None, description="最近交易日主力净流入净额（元，东财日级）")
    fund_flow_date: str | None = Field(None, description="资金流向数据对应日期")
    fund_flow_pick_basis: str | None = Field(
        None,
        description="主力净流入取值说明：today=当日行；last_close=上一完整交易日；fallback_last=表末行",
    )
    roe_pct: float | None = Field(None, description="净资产收益率 ROE（加权 %，东财 ROEJQ）")
    roa_pct: float | None = Field(None, description="总资产净利率 %（东财 ZZCJLL）")
    net_margin_pct: float | None = Field(None, description="销售净利率 %（东财 XSJLL）")
    gross_margin_pct: float | None = Field(None, description="销售毛利率 %（东财 XSMLL）")
    debt_to_assets_pct: float | None = Field(None, description="资产负债率 %（东财 ZCFZL）")
    current_ratio: float | None = Field(None, description="流动比率（东财 LD）")
    quick_ratio: float | None = Field(None, description="速动比率（东财 SD）")
    ocf_per_share: float | None = Field(None, description="每股经营活动产生的现金流量净额（东财 MGJYXJJE）")
    cached_at: str | None = Field(None, description="写入本地快照的时间（UTC ISO），仅读库时有值")


class SuggestedPositionPctOut(BaseModel):
    """
    与 `position_hint` 对齐的示例总资金仓位区间（%）。

    数值与 `position_range_text` 中 Demo 文案一致，便于程序读取；**非**下单指令。
    """

    low_pct: float = Field(..., ge=0, le=100)
    high_pct: float = Field(..., ge=0, le=100)


class TrialExitGuidanceOut(BaseModel):
    """
    在「试错 / 轻仓参与」语境下的 Demo 离场参考。

    系统**不记录**您的建仓价：止损比例为教育用常数；MA20 为当日根据收盘价算出的结构参考位。
    **非**实盘卖出指令。
    """

    applies: bool = Field(..., description="当前是否适用「试错错了如何卖」的讨论（回避新开仓时多为 false）")
    stop_loss_pct_from_entry_demo: float | None = Field(
        None,
        ge=0,
        le=40,
        description="示例：相对**自建仓成本**最大可接受回撤 %；需自行记录成本价",
    )
    reference_exit_ma20: float | None = Field(
        None,
        description="截止日收盘价计算的 MA20，可作「跌破减仓」的结构参考（非唯一标准）",
    )
    note: str = Field(..., description="人可读的综合说明")


class SignalOut(BaseModel):
    """单标的信号计算结果（趋势、强度、评分、仓位提示、风险标签等）。"""

    symbol: str
    name: str | None = None
    as_of_date: str | None = None
    close: float | None = None
    prev_close: float | None = Field(
        None,
        description="上一交易日收盘价（本地日线索引倒数第二根，与 as_of_date 相邻前一交易日）",
    )
    prev_as_of_date: str | None = Field(
        None,
        description="上一交易日（与 prev_close 对应）",
    )
    spot_last_price: float | None = Field(
        None,
        description="现价：东财单股/列表或通达信快照；失败时为日线末根收盘（见 meta.spot_price_source）",
    )
    spot_change_pct: float | None = Field(None, description="现价涨跌幅 %，与 spot_last_price 同源")
    spot_buy_suitability_score: int | None = Field(
        None,
        ge=0,
        le=100,
        description="用快照现价代入技术面规则后的买入适合度（0–100 Demo）",
    )
    spot_buy_hint: str | None = Field(
        None,
        description="基于快照现价与合成得分给出的仓位提示文案（非交易指令）",
    )
    spot_position_hint: PositionHint | None = Field(
        None,
        description="与 spot_buy_hint 对应的仓位档位",
    )
    trend: TrendRegime
    strength: StrengthRegime
    buy_suitability_score: int = Field(..., ge=0, le=100)
    technical_score: int = Field(..., ge=0, le=100, description="仅技术面启发式得分（合成前）")
    fundamental_adjustment: int = Field(
        ...,
        ge=-50,
        le=50,
        description="扩展因子对总分的调整（Demo 有界合成，通常为 -15～15）",
    )
    fundamentals: FundamentalPanel | None = Field(None, description="本地缓存的扩展因子；未执行 ingest 时为 None")
    position_hint: PositionHint
    suggested_position_pct: SuggestedPositionPctOut = Field(
        ...,
        description="与仓位提示对应的示例区间（%）；回避为 0–0",
    )
    trial_exit_guidance: TrialExitGuidanceOut = Field(
        ...,
        description="试错/轻仓时 Demo 止损比例与 MA20 结构参考；回避时说明为何不讨论卖出位",
    )
    position_range_text: str
    risk_tags: list[str]
    reasons: list[SignalReason]
    meta: dict[str, Any] = Field(default_factory=dict)


class DisclaimerOut(BaseModel):
    """免责与数据源说明（部分接口嵌在 JSON 里返回）。"""

    disclaimer: str
    data_source_note: str
    data_delay_note: str


class JournalIn(BaseModel):
    """POST /journal：自用决策记录；字段均可按需留空（除 title/body 外）。"""

    title: str = Field(..., max_length=200, description="短标题，如「本周 600519 趋势结论」")
    body: str = Field(..., description="依据与计划，建议 3 条以内要点")
    symbol: str | None = Field(None, description="6 位代码；可选")
    attach_current_signal: bool = Field(
        False,
        description="为 true 且 symbol 有效时，尝试附加当前 compute_signal 的 JSON 快照",
    )
    planned_action: str | None = Field(None, max_length=128, description="计划动作，如观望/试错/减仓")
    planned_position_pct: float | None = Field(
        None,
        ge=0,
        le=100,
        description="计划仓位占资金 %（实盘复盘用）",
    )
    executed_as_planned: bool | None = Field(None, description="事后填写：是否按计划执行")
    actual_action: str | None = Field(None, max_length=256, description="实际动作简述")


class JournalOut(BaseModel):
    """决策日志单条输出。"""

    id: int
    created_at: str
    symbol: str | None
    title: str
    body: str
    signal_snapshot_json: str | None = None
    planned_action: str | None = None
    planned_position_pct: float | None = None
    executed_as_planned: bool | None = None
    actual_action: str | None = None


HoldingStatus = Literal["holding", "closed"]
HOLDING_LOT_SIZE = 100


def _validate_holding_lot_shares(v: float) -> float:
    n = int(v)
    if n < HOLDING_LOT_SIZE or abs(v - n) > 1e-6 or n % HOLDING_LOT_SIZE != 0:
        raise ValueError(f"股数须为 {HOLDING_LOT_SIZE} 的整数倍（1 手 = {HOLDING_LOT_SIZE} 股）")
    return float(n)


class HoldingIn(BaseModel):
    """POST /holdings：新增一条持仓记录。"""

    symbol: str = Field(..., description="6 位 A 股代码", examples=["600519"])
    shares: float = Field(..., gt=0, description="持仓股数，须为 100 的整数倍（1 手 = 100 股）")
    cost_price: float = Field(..., gt=0, description="成本价（元/股）")
    buy_date: date = Field(..., description="买入日期 YYYY-MM-DD，不能晚于今天")
    notes: str | None = Field(None, max_length=2000, description="备注")
    name: str | None = Field(None, max_length=64, description="简称；不传则联网解析")

    @field_validator("shares")
    @classmethod
    def _shares_lot(cls, v: float) -> float:
        return _validate_holding_lot_shares(v)

    @field_validator("buy_date")
    @classmethod
    def _buy_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("买入日期不能晚于今天")
        return v


class HoldingUpdateIn(BaseModel):
    """PATCH /holdings/{id}：更新持仓字段（仅传要改的项）。"""

    shares: float | None = Field(None, gt=0, description="须为 100 的整数倍")
    cost_price: float | None = Field(None, gt=0)
    buy_date: date | None = Field(None, description="买入日期；不能晚于今天")
    sell_price: float | None = Field(None, gt=0, description="仅已平仓：修正卖出均价（元/股）")
    sell_date: date | None = Field(None, description="仅已平仓：修正卖出日期")
    notes: str | None = Field(None, max_length=2000)
    name: str | None = Field(None, max_length=64)

    @field_validator("shares")
    @classmethod
    def _shares_lot(cls, v: float | None) -> float | None:
        if v is None:
            return None
        return _validate_holding_lot_shares(v)

    @field_validator("buy_date", "sell_date")
    @classmethod
    def _trade_date_not_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("日期不能晚于今天")
        return v


class HoldingCloseIn(BaseModel):
    """POST /holdings/{id}/close：标记已平仓。"""

    sell_price: float = Field(..., gt=0, description="卖出均价（元/股）")
    sell_date: date | None = Field(None, description="卖出日期；默认今天")
    notes: str | None = Field(None, max_length=2000, description="平仓备注（追加到原备注后）")


class HoldingClosedRecordIn(BaseModel):
    """POST /holdings/closed-record：补录一条已平仓记录（复盘用，非券商回报）。"""

    symbol: str = Field(..., description="6 位 A 股代码", examples=["600519"])
    shares: float = Field(..., gt=0, description="持仓股数，须为 100 的整数倍")
    cost_price: float = Field(..., gt=0, description="买入均价（元/股）")
    buy_date: date = Field(..., description="买入日期 YYYY-MM-DD")
    sell_price: float = Field(..., gt=0, description="卖出均价（元/股）")
    sell_date: date = Field(..., description="卖出日期 YYYY-MM-DD")
    notes: str | None = Field(None, max_length=2000, description="复盘备注")
    name: str | None = Field(None, max_length=64, description="简称；不传则联网解析")

    @field_validator("shares")
    @classmethod
    def _shares_lot(cls, v: float) -> float:
        return _validate_holding_lot_shares(v)

    @field_validator("buy_date", "sell_date")
    @classmethod
    def _trade_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("日期不能晚于今天")
        return v

    @model_validator(mode="after")
    def _sell_on_or_after_buy(self) -> Self:
        if self.sell_date < self.buy_date:
            raise ValueError("卖出日期不能早于买入日期")
        return self


class HoldingReviewSummaryOut(BaseModel):
    """已平仓记录复盘汇总（本机 SQLite，非交易所回报）。"""

    closed_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    flat_count: int = 0
    total_realized_pnl_amt: float | None = None
    avg_realized_pnl_pct: float | None = None
    avg_holding_days: float | None = None


class HoldingOut(BaseModel):
    """持仓单条输出（含估算盈亏，非交易所回报）。"""

    id: int
    symbol: str
    name: str = ""
    status: HoldingStatus
    shares: float
    cost_price: float
    buy_date: str | None = None
    sell_price: float | None = None
    sell_date: str | None = None
    notes: str | None = None
    holding_days: int | None = Field(
        None,
        description="持仓天数（含买入日当天为第 1 天）：至今日（持仓中）或至卖出日（已平仓）；无买入日为 null",
    )
    created_at: str
    updated_at: str
    last_close: float | None = Field(None, description="本地最新日线收盘价")
    bars_last_trade_date: str | None = None
    spot_last_price: float | None = Field(None, description="盘口现价估算（非 tick）")
    spot_change_pct: float | None = None
    current_price: float | None = Field(
        None, description="当前价格（优先盘口现价，用于展示与平仓建议）"
    )
    current_price_source: str | None = Field(
        None, description="spot=盘口；daily_close=日线收盘；sell=已平仓卖出价"
    )
    ref_price: float | None = Field(None, description="用于浮动盈亏的参考价")
    ref_price_source: str | None = Field(
        None, description="spot=盘口；daily_close=日线收盘；sell=已平仓卖出价"
    )
    cost_basis: float | None = Field(None, description="成本总额 = 股数 × 成本价")
    market_value: float | None = Field(None, description="市值估算")
    unrealized_pnl_amt: float | None = Field(None, description="浮动盈亏（元），仅 holding")
    unrealized_pnl_pct: float | None = Field(None, description="浮动盈亏（%），仅 holding")
    realized_pnl_amt: float | None = Field(None, description="已实现盈亏（元），仅 closed")
    realized_pnl_pct: float | None = Field(None, description="已实现盈亏（%），仅 closed")


HoldingExitAction = Literal["strong_close", "consider_close", "watch", "hold"]


class HoldingExitAdviceOut(BaseModel):
    """持仓平仓建议：结合成本浮盈亏 + ④ 同源技术面规则（Demo，非交易指令）。"""

    holding_id: int
    symbol: str
    name: str = ""
    suggest_close: bool = Field(
        ...,
        description="为 true 时表示综合得分达到「可考虑减仓/平仓」阈值（≥45 分）",
    )
    action: HoldingExitAction = Field(
        ...,
        description="strong_close=倾向离场；consider_close=可考虑减仓；watch=观察；hold=暂不卖",
    )
    score: int = Field(..., ge=0, le=100, description="离场压力得分，越高越倾向减仓")
    summary_zh: str = Field(..., description="一句话结论")
    reasons: list[str] = Field(default_factory=list, description="触发因子说明（最多约 8 条）")
    cost_price: float
    current_price: float | None = Field(None, description="计算建议时采用的当前价")
    current_price_source: str | None = None
    ref_price: float | None = Field(None, description="同 current_price（兼容）")
    ref_price_source: str | None = None
    unrealized_pnl_pct: float | None = None
    stop_loss_demo_pct: float | None = Field(
        None, description="与 ④ 一致的 Demo 止损参考 %（相对您的成本）"
    )
    reference_exit_ma20: float | None = None
    trend: TrendRegime | None = None
    strength: StrengthRegime | None = None
    buy_suitability_score: int | None = None
    position_hint: PositionHint | None = None
    signal_as_of_date: str | None = None
    disclaimer_note: str = Field(
        default="以下为规则化 Demo 参考，不记录券商成交，不构成卖出指令。",
        description="固定免责提示",
    )


HoldingEntryAction = Literal["strong_open", "consider_open", "watch", "avoid"]


class HoldingEntryAdviceOut(BaseModel):
    """可否建仓建议：按最新现价 + ④ 同源技术面规则（Demo，非买入指令）。"""

    holding_id: int
    symbol: str
    name: str = ""
    record_status: str = Field(
        ...,
        description="持仓记录状态：holding=持仓中；closed=已平仓（评估为重新开仓参考）",
    )
    already_holding: bool = Field(
        ...,
        description="记录是否为持仓中（true 时表示本条为加仓/持有延展参考，非首次建仓）",
    )
    suggest_open: bool = Field(
        ...,
        description="为 true 时表示综合得分达到「可考虑建仓/试仓」阈值（≥45 分）",
    )
    action: HoldingEntryAction = Field(
        ...,
        description="strong_open=适合建仓；consider_open=可考虑轻仓；watch=观望；avoid=不宜建仓",
    )
    score: int = Field(..., ge=0, le=100, description="建仓适合度得分，越高越适合新开仓")
    summary_zh: str = Field(..., description="一句话结论")
    reasons: list[str] = Field(default_factory=list, description="触发因子说明（最多约 8 条）")
    current_price: float | None = Field(None, description="计算建议时采用的最新市价")
    current_price_source: str | None = None
    reference_entry_ma20: float | None = None
    trend: TrendRegime | None = None
    strength: StrengthRegime | None = None
    buy_suitability_score: int | None = None
    position_hint: PositionHint | None = None
    signal_as_of_date: str | None = None
    disclaimer_note: str = Field(
        default="以下为规则化 Demo 参考，不记录券商成交，不构成买入指令。",
        description="固定免责提示",
    )


class HoldingGoalProgressOut(BaseModel):
    """GET /holdings/goal-progress：按全部持仓盈亏估算距目标还差多少。"""

    start_capital: float
    target_capital: float
    current_equity: float = Field(..., description="起始资金 + 各持仓已实现/浮动盈亏")
    total_pnl_amt: float = Field(..., description="相对起始资金的合计盈亏（元）")
    gap_to_target: float = Field(..., description="距目标还差多少元（≤0 表示已达或超过）")
    progress_pct: float | None = Field(
        None, description="相对「起始→目标」区间的完成度 %（0–100，可超过 100）"
    )
    holding_pnl_amt: float = Field(0, description="持仓中浮动盈亏合计")
    closed_pnl_amt: float = Field(0, description="已平仓已实现盈亏合计")
    holding_count: int = 0
    closed_count: int = 0
    summary_zh: str = Field(..., description="一句话进度说明")


class HoldingGoalPlanIn(BaseModel):
    """POST /holdings/{id}/goal-plan：目标资金测算请求体。"""

    start_capital: float = Field(..., gt=0, description="起始资金（元），如 5000")
    target_capital: float = Field(..., gt=0, description="目标资金（元），如 10000")
    current_price: float | None = Field(
        None,
        gt=0,
        description="与列表「当前价格」一致时可传入，用于浮盈亏与信号计算",
    )

    @model_validator(mode="after")
    def _target_gt_start(self) -> Self:
        if self.target_capital <= self.start_capital:
            raise ValueError("目标资金须大于起始资金")
        return self


class WatchlistPickOut(BaseModel):
    """目标测算：自选池候选标的摘要。"""

    symbol: str
    name: str = ""
    buy_suitability_score: int = Field(..., ge=0, le=100)
    trend: str = ""
    strength: str = ""
    position_hint: str = ""
    last_close: float | None = None
    spot_last_price: float | None = None
    reason: str = ""


HoldingGoalDecision = Literal["hold", "switch", "watch", "goal_reached"]
DailyVerdict = Literal["留", "走", "换", "达标"]


class HoldingGoalPlanOut(BaseModel):
    """持仓目标测算结果：留仓/换仓建议 + 操作步骤。"""

    holding_id: int
    symbol: str
    name: str = ""
    start_capital: float
    target_capital: float
    current_equity: float = Field(..., description="起始资金 + 各持仓盈亏估算")
    gap_to_target: float = Field(..., description="距目标还差多少元（≤0 表示已达或超过）")
    progress_pct: float | None = Field(None, description="相对起点的目标完成度 %")
    position_decision: HoldingGoalDecision = Field(
        ...,
        description="hold=继续持有；switch=倾向清仓换自选；watch=观察；goal_reached=已达目标",
    )
    decision_summary_zh: str
    daily_verdict: DailyVerdict = Field(
        ...,
        description="收盘口径一句话：留=持有；走=卖出；换=卖出并换自选；达标=已达目标",
    )
    daily_verdict_detail: str = Field(..., description="留/走/换的具体说明与操作建议")
    switch_to_symbol: str | None = Field(
        None, description="当 daily_verdict=换 时，优先换入的 6 位代码"
    )
    session_phase: str = Field(
        ...,
        description="intraday=盘中；after_close=已收盘；pre_open=开盘前；non_trading=非交易时段",
    )
    session_phase_note: str = Field(..., description="当前时段对结论有效性的说明")
    price_basis: str | None = Field(None, description="测算采用的现价来源（本地收盘/新浪等）")
    signal_as_of_date: str | None = Field(None, description="④ 信号所依据的 K 线日期")
    steps: list[str] = Field(default_factory=list, description="分步操作建议（Demo）")
    exit_advice: HoldingExitAdviceOut
    watchlist_picks: list[WatchlistPickOut] = Field(default_factory=list)
    disclaimer_note: str


class ForwardOutlookSyncIn(BaseModel):
    """POST /forward-outlook/sync：③ 后手动触发或补同步。"""

    symbols: list[str] | None = Field(
        None,
        description="6 位代码列表；省略则对当前自选全部成功标的同步",
    )
    horizon: int = Field(3, ge=1, le=60, description="展望的未来交易日跨度 H")


class ForwardOutlookSyncOut(BaseModel):
    synced: int
    failed_symbols: list[str]
    extra_settled: int
    horizon: int


class ForwardOutlookOut(BaseModel):
    """单条自动前向展望（⑦ 展示）。"""

    id: int
    symbol: str
    stock_name: str | None = None
    horizon: int
    signal_trade_date: str
    signal_close: float | None = None
    bars_count: int | None = None
    data_quality: dict[str, Any] | None = None
    data_quality_ok: bool | None = None
    predicted_up: bool | None = None
    outlook_summary_zh: str | None = None
    status: str
    actual_return_pct: float | None = None
    actual_up: bool | None = None
    settled_at: str | None = None
    created_at: str
    updated_at: str


class SelfUseMetaOut(BaseModel):
    """自用定位与风控检查摘要（不含真实资金数据）。"""

    tool_mode: str
    automatic_trading_supported: bool
    risk_checklist: list[str]
    related_doc_files: list[str]
    journal_api: str
    holdings_api: str = "/holdings"
    example_risk_policy_file: str


class HotMarketSnapshotOut(BaseModel):
    """热门板块 + 热门股本地快照（JSON 内容同构）。"""

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


class HotMarketSnapshotFileOut(BaseModel):
    """GET /meta/hot-market-snapshot：文件路径与已存快照（若无则为 null）。"""

    path: str
    snapshot: HotMarketSnapshotOut | None = None


class HotMarketSnapshotRefreshIn(BaseModel):
    """POST /meta/hot-market-snapshot/refresh：拉取并覆盖写入 hot_market_snapshot.json。"""

    top_stocks: int = Field(
        100,
        ge=10,
        le=500,
        description="热门股条数上限（新浪/腾讯为按涨跌幅重排后截断；东财步为人气榜截断）",
    )
    chain: list[str] | None = Field(
        None,
        description=(
            "尝试顺序，默认 sina → tencent → baostock → eastmoney → akshare；"
            "可改为子集以加快失败回退"
        ),
    )


class HotMarketSnapshotRefreshOut(BaseModel):
    """刷新结果：落盘路径与快照正文。"""

    saved_to: str
    snapshot: HotMarketSnapshotOut


class ForecastConfusionOut(BaseModel):
    """二分类混淆矩阵（正类=未来 H 日上涨）。"""

    tp: int
    fp: int
    tn: int
    fn: int


class ForecastTradeLegOut(BaseModel):
    """按「翻多买入、翻空卖出」示意的一笔完整交易（收盘价，非实盘）。"""

    buy_signal_date: str | None = Field(None, description="买入信号产生于哪天收盘后")
    sell_signal_date: str | None = Field(None, description="卖出信号产生于哪天收盘后")
    buy_date: str
    sell_date: str
    buy_close: float
    sell_close: float
    buy_open_raw: float | None = Field(None, description="买入执行日原始开盘价")
    sell_open_raw: float | None = Field(None, description="卖出执行日原始开盘价")
    shares: int | None = Field(None, description="本笔成交股数")
    holding_days: int | None = Field(None, description="持有了多少个交易日")
    gross_return_pct: float | None = Field(None, description="未扣成本的区间涨跌幅 %")
    cost_pct: float | None = Field(None, description="该笔按默认费率估算的总成本 %")
    net_return_pct: float | None = Field(None, description="扣成本后的区间涨跌幅 %")
    buy_fee: float | None = Field(None, description="买入佣金等费用（元）")
    sell_fee: float | None = Field(None, description="卖出佣金+印花税等费用（元）")
    slippage_cost_cny: float | None = Field(None, description="本笔因滑点产生的估算成本（元）")
    fee_total_cny: float | None = Field(None, description="本笔总费用（元，不含滑点已并入则看字段说明）")
    cash_before_buy: float | None = Field(None, description="买入前现金（元）")
    cash_after_buy: float | None = Field(None, description="买入后现金（元）")
    cash_after_sell: float | None = Field(None, description="卖出后现金（元）")
    return_pct: float = Field(..., description="为兼容旧前端保留；当前等于 net_return_pct")


class ForecastTradeSummaryOut(BaseModel):
    """示意交易汇总；未完成笔不计入 completed_trades。"""

    completed_trades: int
    win_rate: float | None = Field(None, description="completed 笔中 return_pct>0 占比")
    avg_return_pct: float | None = Field(None, description="各笔 return_pct 算术平均")
    total_simple_return_pct: float | None = Field(
        None,
        description="各笔 return_pct 相加（非复利，仅示意）",
    )
    gross_return_pct: float | None = Field(None, description="各笔未扣成本收益简单相加 %")
    total_net_return_pct: float | None = Field(None, description="各笔扣成本收益简单相加 %")
    compounded_return_pct: float | None = Field(None, description="按每笔净收益复利得到的累计收益 %")
    daily_compounded_return_pct: float | None = Field(None, description="按样本外逐日持仓收益复利得到的累计收益 %")
    annualized_return_pct: float | None = Field(None, description="按样本外日收益折算的年化收益 %")
    max_drawdown_pct: float | None = Field(None, description="按样本外日收益曲线计算的最大回撤 %")
    sharpe_ratio: float | None = Field(None, description="按样本外日收益近似计算的年化夏普")
    profit_factor: float | None = Field(None, description="总盈利 / 总亏损绝对值")
    avg_holding_days: float | None = Field(None, description="完整交易的平均持有天数")
    avg_win_return_pct: float | None = Field(None, description="盈利交易平均净收益 %")
    avg_loss_return_pct: float | None = Field(None, description="亏损交易平均净收益 %")
    total_cost_pct: float | None = Field(None, description="所有完整交易的估算总成本 %")
    final_nav: float | None = Field(None, description="最终策略净值，如 1.1265 表示净值 1.1265")
    ending_equity: float | None = Field(None, description="样本外结束时总权益（元）")
    ending_cash: float | None = Field(None, description="样本外结束时现金（元）")
    ending_shares: int | None = Field(None, description="样本外结束时持仓股数")
    total_fee_cny: float | None = Field(None, description="样本外累计手续费与税费（元）")
    total_slippage_cny: float | None = Field(None, description="样本外累计滑点成本（元）")


class ForecastOpenLegOut(BaseModel):
    """样本外序列末尾仍看多、尚未出现卖出信号时返回。"""

    buy_signal_date: str | None = Field(None, description="买入信号产生于哪天收盘后")
    buy_date: str
    buy_close: float
    holding_days: int | None = Field(None, description="截至样本外末尾已持有的交易日数")
    shares: int | None = Field(None, description="当前持仓股数")
    unrealized_return_pct: float | None = Field(None, description="按样本外最后收盘估算的未实现收益 %")
    market_value: float | None = Field(None, description="按样本外最后收盘估算的持仓市值（元）")
    note: str


class ForecastEquityPointOut(BaseModel):
    """样本外尾部净值快照。"""

    trade_date: str
    cash: float
    shares: int
    market_value: float
    equity: float
    nav: float


class ForecastMethodOut(BaseModel):
    """单种预测方式在样本外段的指标。"""

    method: str
    description: str
    n_oos: int
    accuracy: float = Field(..., description="方向命中率")
    balanced_accuracy: float = Field(..., description="平衡准确率（灵敏与特异平均）")
    precision_up: float | None = Field(None, description="预测涨时的精确率")
    recall_up: float | None = Field(None, description="对真实上涨的召回")
    confusion: ForecastConfusionOut
    mean_forward_return_pred_up: float | None = Field(
        None,
        description="预测涨的那些日子里，实际 H 日累计收益均值",
    )
    mean_forward_return_pred_down: float | None = Field(
        None,
        description="预测跌的那些日子里，实际 H 日累计收益均值",
    )
    auc_roc: float | None = Field(None, description="概率分数的秩 AUC；非概率模型为 null")
    trade_summary: ForecastTradeSummaryOut | None = Field(
        None,
        description="由预测翻多/翻空生成的示意交易汇总",
    )
    trades: list[ForecastTradeLegOut] = Field(
        default_factory=list,
        description="最近若干笔完整买卖（时间从旧到新）",
    )
    open_leg: ForecastOpenLegOut | None = Field(
        None,
        description="若样本外结束时仍「看多」则此处为未平仓示意",
    )
    equity_curve_tail: list[ForecastEquityPointOut] = Field(
        default_factory=list,
        description="样本外尾部净值轨迹（最近若干点）",
    )


class ForecastReadingRefOut(BaseModel):
    """外部学习材料链接（非官方背书，仅作路线参考）。"""

    title: str
    url: str
    anchor_hint: str | None = Field(None, description="文中锚点说明，如 #_label4")


class ForecastPedagogyOut(BaseModel):
    """
    与常见「Python + pandas 量化入门」叙事对齐的说明块。

    参考公开笔记中的路径：策略假设 → 代码实现 → 回测检验 → 仿真/实盘。
    """

    title: str
    workflow_steps: list[str]
    stack_note: str
    reading: ForecastReadingRefOut | None = None


class ForecastStrategyParamsOut(BaseModel):
    """本次回测使用的可复现参数摘要。"""

    horizon: int
    ma_short: int
    ma_long: int
    min_train_rows: int
    retrain_every: int
    trade_limit: int
    commission_bps: float = Field(..., description="佣金/过户等单边成本估算，单位 bps")
    sell_tax_bps: float = Field(..., description="卖出印花税估算，单位 bps")
    slippage_bps: float = Field(..., description="单边滑点估算，单位 bps")
    initial_cash: float = Field(..., description="回测初始资金（元）")
    lot_size: int = Field(..., description="整手股数")
    min_commission_cny: float = Field(..., description="最低佣金（元）")
    oos_from: str | None = Field(
        None,
        description="样本外统计与成交回放起始日（含），YYYY-MM-DD；null 表示自首次 OOS 起",
    )
    oos_to: str | None = Field(
        None,
        description="样本外统计与成交回放结束日（含），YYYY-MM-DD；null 表示至最后 OOS",
    )
    methods_included: list[str] = Field(
        default_factory=list,
        description="本次返回的 walk-forward 方法键名（与响应 body.methods 顺序一致）",
    )
    live_bars: bool = Field(
        False,
        description="是否在回测前对该标的联网增量更新日线（写入 SQLite 后再读库）",
    )
    live_data_source: str | None = Field(
        None,
        description="联网增量时传入的 data_source；null 表示服务端默认 ingest 路线",
    )
    live_persist: bool = Field(
        False,
        description="live_bars 时是否写入 SQLite；false 表示仅内存合并联网窗口与本地行",
    )
    live_as_of: str | None = Field(
        None,
        description="联网增量使用的截止日 YYYY-MM-DD；null 表示用服务器当天",
    )
    bars_last_trade_date: str | None = Field(
        None,
        description="本次回测实际用到的日线最后一根 trade_date（便于核对数据源是否滞后）",
    )


class ForecastExecutionAssumptionsOut(BaseModel):
    """回测交易规则摘要。"""

    signal_timing: str
    order_timing: str
    execution_price_rule: str
    sizing_rule: str
    position_update_rule: str
    cost_rule: str


class ForecastFundamentalsBacktestOut(BaseModel):
    """
    说明扩展因子与当前「按交易日 walk-forward」回测的关系。

    本地仅存每标的最新一条扩展因子快照，无法复现「历史上每一天当时可知的基本面」，
    故本接口的历史回测路径**不**把扩展因子拼进特征，避免伪精度与前视。
    """

    merged_into_walkforward: bool = Field(
        False,
        description="是否已将基本面/估值等并入 Logistic 的历史逐日特征（当前恒为 false）",
    )
    snapshot_cached: bool = Field(
        ...,
        description="该标的是否在 fundamental_snapshots 中有记录（可先 POST /ingest/fundamentals）",
    )
    note: str = Field(..., description="给人看的完整说明：缺快照 vs 有快照但未并入的原因")


class ForecastValidateOut(BaseModel):
    """
    walk-forward 方向预测验证摘要。

    仅使用本地已入库日线；与实时信号接口独立，用于检验「简单因子能否带来样本外区分度」。
    """

    symbol: str
    horizon: int
    n_bars_db: int
    n_valid_rows: int
    first_oos_trade_date: str | None
    last_oos_trade_date: str | None
    n_oos: int
    min_train_rows: int
    retrain_every: int
    target_definition: str
    oos_positive_rate: float = Field(..., description="样本外真实上涨比例")
    baseline_always_majority_oos: float = Field(
        ...,
        description="若样本外始终猜多数类，可达的准确率上限",
    )
    feature_names: list[str]
    methods: list[ForecastMethodOut]
    disclaimer: str
    how_to_read: str = Field(
        ...,
        description="给人看的简短说明：准确率含义、买卖点定义",
    )
    ui_focus_method: str = Field(
        "dual_ma_cross",
        description="控制台优先展示的 method 键名（默认双均线教材向）",
    )
    pedagogy: ForecastPedagogyOut = Field(
        ...,
        description="与学习路线、技术栈对照的说明",
    )
    strategy_params: ForecastStrategyParamsOut = Field(
        ...,
        description="本次请求的策略参数，便于复现实验",
    )
    execution_assumptions: ForecastExecutionAssumptionsOut = Field(
        ...,
        description="信号、下单、成交、仓位与成本的规则摘要",
    )
    fundamentals_backtest: ForecastFundamentalsBacktestOut = Field(
        ...,
        description="扩展因子是否参与历史回测、以及本地是否有快照",
    )
