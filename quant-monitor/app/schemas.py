"""
Pydantic 请求/响应模型：与 FastAPI 的 response_model、请求体验证对齐。

类型别名（Literal）约束 trend / strength / position_hint 的合法取值，便于 OpenAPI 展示枚举。
"""

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

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

    buy_date: str
    sell_date: str
    buy_close: float
    sell_close: float
    return_pct: float = Field(..., description="按卖出/买入收盘计算的区间涨跌幅 %（简单收益）")


class ForecastTradeSummaryOut(BaseModel):
    """示意交易汇总；未完成笔不计入 completed_trades。"""

    completed_trades: int
    win_rate: float | None = Field(None, description="completed 笔中 return_pct>0 占比")
    avg_return_pct: float | None = Field(None, description="各笔 return_pct 算术平均")
    total_simple_return_pct: float | None = Field(
        None,
        description="各笔 return_pct 相加（非复利，仅示意）",
    )


class ForecastOpenLegOut(BaseModel):
    """样本外序列末尾仍看多、尚未出现卖出信号时返回。"""

    buy_date: str
    buy_close: float
    note: str


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
    fundamentals_backtest: ForecastFundamentalsBacktestOut = Field(
        ...,
        description="扩展因子是否参与历史回测、以及本地是否有快照",
    )
