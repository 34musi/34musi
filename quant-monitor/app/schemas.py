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
    sina = "sina"
    tencent = "tencent"
    baostock = "baostock"


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
        description="行情路线：不传则用服务端默认（INGEST_DATA_SOURCE）。auto=东财→新浪→腾讯→Baostock；其余为仅使用该源。",
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
    cached_at: str | None = Field(None, description="写入本地快照的时间（UTC ISO），仅读库时有值")


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
    position_range_text: str
    risk_tags: list[str]
    reasons: list[SignalReason]
    meta: dict[str, Any] = Field(default_factory=dict)


class DisclaimerOut(BaseModel):
    """免责与数据源说明（部分接口嵌在 JSON 里返回）。"""

    disclaimer: str
    data_source_note: str
    data_delay_note: str
