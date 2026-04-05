"""
Pydantic 请求/响应模型：与 FastAPI 的 response_model、请求体验证对齐。

类型别名（Literal）约束 trend / strength / position_hint 的合法取值，便于 OpenAPI 展示枚举。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

TrendRegime = Literal["bullish", "sideways", "bearish"]
StrengthRegime = Literal["strong", "neutral", "weak"]
PositionHint = Literal["avoid", "cautious", "trial", "moderate"]


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


class SignalReason(BaseModel):
    """信号解释：机器可读 code + 人类可读 text。"""

    code: str
    text: str


class SignalOut(BaseModel):
    """单标的信号计算结果（趋势、强度、评分、仓位提示、风险标签等）。"""

    symbol: str
    name: str | None = None
    as_of_date: str | None = None
    close: float | None = None
    trend: TrendRegime
    strength: StrengthRegime
    buy_suitability_score: int = Field(..., ge=0, le=100)
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
