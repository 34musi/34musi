"""Constants for A-share selector pipeline."""

from __future__ import annotations

import pandas as pd

TRADING_DAYS_PER_YEAR = 252
DEFAULT_START_DATE = "20230101"
DEFAULT_END_DATE = pd.Timestamp.today().strftime("%Y%m%d")

PRICE_COLUMN_ALIASES = {
    "date": "date",
    "datetime": "date",
    "日期": "date",
    "交易日期": "date",
    "open": "open",
    "开盘": "open",
    "high": "high",
    "最高": "high",
    "low": "low",
    "最低": "low",
    "close": "close",
    "收盘": "close",
    "latest": "close",
    "最新价": "close",
    "volume": "volume",
    "vol": "volume",
    "成交量": "volume",
    "成交额": "turnover",
    "amount": "turnover",
    "turnover": "turnover",
    "code": "code",
    "symbol": "code",
    "代码": "code",
    "stock_code": "code",
    "name": "name",
    "名称": "name",
    "股票名称": "name",
}
