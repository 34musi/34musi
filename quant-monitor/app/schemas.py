"""
Pydantic 请求/响应模型：与 FastAPI 的 response_model、请求体验证对齐。

类型别名（Literal）约束 trend / strength / position_hint 的合法取值，便于 OpenAPI 展示枚举。
"""

from datetime import date
from enum import Enum
from typing import Any, Literal

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


class IngestUpdateIn(BaseModel):
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


class WatchlistIn(BaseModel):
    """POST /watchlist 请求体：原始代码字符串，服务端会规范为 6 位数字。"""

    symbol: str = Field(
        ...,
        description="股票代码，填 6 位数字即可（可带 sz/sh 等前缀，系统会自动去掉）。示例：茅台 600519，平安银行 000001。",
        examples=["600519"],
    )


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
    last_daily_close_label: str | None = Field(
        None,
        description="给人看的「最后收盘」说明（含交易日与 A 股常规收盘时刻）",
    )


class QuantWatchlistStockRowIn(BaseModel):
    """单条待写入自选的量化结果行（与 sector-screen 返回的 stocks 项字段对齐）。"""

    code: str = Field(..., description="股票代码，可为带前缀形式，服务端会规范为 6 位数字")
    name: str = Field("", description="证券简称，可空")


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

    top_sectors: int = Field(5, ge=1, le=200, description="取排名前多少板块（按 get_sector_rankings 行序）")
    stocks_per_sector: int = Field(5, ge=1, le=50, description="每板块过滤 ST/科创板后至多几只")
    board_type: str = Field(
        "all",
        description="板块类型：all / concept / industry（与核心数据源一致）",
    )
    exclude_st: bool = Field(True, description="排除名称含 ST/*ST 的成分股")
    exclude_kcb: bool = Field(True, description="排除科创板 688/689")
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


class FillHotSectorsSummary(BaseModel):
    """热门填充结果摘要。"""

    added: int = Field(..., description="本次新写入的 auto_hot 条数")
    skipped_existing_manual: int = Field(
        ...,
        description="已在自选且为手动的代码数（自动列表跳过，不覆盖）",
    )
    removed_auto: int = Field(..., description="填充前删除的旧 auto_hot + auto_quant 条数")
    warnings: list[str] = Field(default_factory=list, description="选股过程中的提示（如某板块成分不足）")


class FillHotSectorsOut(BaseModel):
    """热门板块填充或预览的完整响应。"""

    sectors_detail: list[dict[str, Any]] = Field(
        ...,
        description="按热度顺序的板块列表；每项含 sector_rank、sector_metrics、stocks（全列字典）",
    )
    summary: FillHotSectorsSummary


class SectorScreenDataSource(str, Enum):
    """与命令行 `quant_stock_selector.py --data-source` 一致。"""

    akshare = "akshare"
    tushare = "tushare"
    mootdx = "mootdx"


class SectorScreenIn(BaseModel):
    """
    对应仓库根目录 `quant_stock_selector.py` / 包 `app.quant_stock_selector` 的流水线：
    热门板块或指定板块/代码列表 → 拉行情 → 技术面初筛 + 双均线回测 → 综合分。
    """

    data_source: SectorScreenDataSource = Field(
        SectorScreenDataSource.mootdx,
        description="行情与板块数据源；akshare 板块最全，mootdx 更稳，tushare 需 token",
    )
    tushare_token: str | None = Field(None, description="data_source=tushare 时使用，或服务端已配置 TUSHARE_TOKEN")
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
    only_passed: bool = Field(False, description="为 true 时仅保留技术面初筛通过的股票")
    top_stocks_limit: int = Field(40, ge=1, le=500, description="响应中最多返回多少条股票结果")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _norm_ymd(cls, v: object) -> str:
        if v is None:
            return date.today().strftime("%Y%m%d")
        s = str(v).strip().replace("-", "")
        if len(s) != 8 or not s.isdigit():
            raise ValueError("start_date/end_date 须为 YYYYMMDD 或 YYYY-MM-DD")
        return s

    @model_validator(mode="after")
    def _exclusive_modes(self):
        if self.symbols and self.sector:
            raise ValueError("不能同时指定 symbols 与 sector")
        if self.symbols is not None and len(self.symbols) > 500:
            raise ValueError("symbols 最多 500 条")
        return self


class SectorScreenOut(BaseModel):
    """选股流水线 JSON 结果。"""

    sectors: list[dict[str, Any]]
    stocks: list[dict[str, Any]]
    stocks_total: int
    disclaimer: str
    note: str = Field(
        "",
        description="与命令行脚本差异说明（若有）",
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


class SelfUseMetaOut(BaseModel):
    """自用定位与风控检查摘要（不含真实资金数据）。"""

    tool_mode: str
    automatic_trading_supported: bool
    risk_checklist: list[str]
    related_doc_files: list[str]
    journal_api: str
    example_risk_policy_file: str


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
