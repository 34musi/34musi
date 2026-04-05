"""
行情摄取：通过 AkShare 拉 A 股前复权日线，规范化后写入 SQLite（bars 表）。

职责划分：
- normalize_symbol：统一为 6 位数字代码。
- fetch_ak_daily：单次区间拉取并转为内部列名。
- incremental_refresh：相对库内最新日期做增量（带重叠窗口防漏日）。
- load_bars_df：给信号模块读库；不足 min_bars 时自动触发一次刷新。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import BarRow, session_scope

logger = logging.getLogger(__name__)

# AkShare 返回的中文列名 → 内部统一英文列名
_AK_COL_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def normalize_symbol(symbol: str) -> str:
    """去掉非数字字符，校验长度为 6；否则抛 ValueError。"""
    s = re.sub(r"\D", "", symbol.strip())
    if len(s) != 6:
        raise ValueError("A 股代码须为 6 位数字")
    return s


def _today_str() -> str:
    """AkShare 日期参数格式 YYYYMMDD。"""
    return date.today().strftime("%Y%m%d")


def fetch_ak_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    拉取指定区间前复权日线。

    start_date / end_date：可为 YYYY-MM-DD 或 YYYYMMDD（内部会去掉横线）。
    返回列：symbol, trade_date, open, high, low, close, volume, amount；空则返回空表结构。
    """
    sym = normalize_symbol(symbol)
    df = ak.stock_zh_a_hist(
        symbol=sym,
        period="daily",
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        adjust="qfq",
    )
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["trade_date", "open", "high", "low", "close", "volume", "amount"]
        )
    ren = {}
    for c in df.columns:
        if c in _AK_COL_MAP:
            ren[c] = _AK_COL_MAP[c]
    df = df.rename(columns=ren)
    if "trade_date" not in df.columns:
        for alt in ("date", "Date", "trade_date"):
            if alt in df.columns:
                df = df.rename(columns={alt: "trade_date"})
                break
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col not in df.columns:
            df[col] = 0.0 if col in ("volume", "amount") else float("nan")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["symbol"] = sym
    return df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]]


def max_stored_date(symbol: str) -> str | None:
    """该标的在库中最后一根 K 线的 trade_date（YYYY-MM-DD）；无数据返回 None。"""
    sym = normalize_symbol(symbol)
    with session_scope() as s:
        q = select(func.max(BarRow.trade_date)).where(BarRow.symbol == sym)
        return s.execute(q).scalar_one_or_none()


def upsert_bars(df: pd.DataFrame) -> int:
    """
    将 DataFrame 行写入 bars；SQLite 下用 INSERT ... ON CONFLICT DO UPDATE 实现按日覆盖。
    返回成功处理的行数。
    """
    if df.empty:
        return 0
    rows = df.to_dict(orient="records")
    n = 0
    with session_scope() as s:
        for r in rows:
            stmt = sqlite_insert(BarRow.__table__).values(
                symbol=r["symbol"],
                trade_date=r["trade_date"],
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"] or 0),
                amount=float(r.get("amount") or 0),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "trade_date"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                },
            )
            s.execute(stmt)
            n += 1
    return n


def incremental_refresh(symbol: str, lookback_years: int = 5) -> dict:
    """
    拉取并写入 SQLite。

    - 若库中已有该标的：从「最后交易日往前 14 天」起拉到今日，减少重复全量。
    - 若无历史：从约 lookback_years 年前拉到今日。
    返回摘要 dict；网络或 AkShare 异常时抛 RuntimeError。
    """
    sym = normalize_symbol(symbol)
    end = _today_str()
    last = max_stored_date(sym)
    if last:
        # 重叠窗口：覆盖停牌、复权修正等导致的尾部修正
        start_dt = datetime.strptime(last, "%Y-%m-%d") - timedelta(days=14)
        start = start_dt.strftime("%Y%m%d")
    else:
        start = (date.today() - timedelta(days=365 * lookback_years)).strftime("%Y%m%d")
    try:
        df = fetch_ak_daily(sym, start, end)
        n = upsert_bars(df)
        return {"symbol": sym, "rows_upserted": n, "start": start, "end": end}
    except Exception as e:
        logger.exception("ingest failed for %s", sym)
        raise RuntimeError(f"行情拉取失败: {e}") from e


def load_bars_df(symbol: str, min_bars: int = 80) -> pd.DataFrame:
    """
    从库中读出该标的全部日线为 DataFrame（按日期升序）。

    若行数 < min_bars，先调用 incremental_refresh 再读一次（仍不足则返回当前能读到的行）。
    """
    sym = normalize_symbol(symbol)
    with session_scope() as s:
        q = (
            select(BarRow)
            .where(BarRow.symbol == sym)
            .order_by(BarRow.trade_date.asc())
        )
        rows = s.execute(q).scalars().all()
    if len(rows) < min_bars:
        incremental_refresh(sym)
        with session_scope() as s:
            q = (
                select(BarRow)
                .where(BarRow.symbol == sym)
                .order_by(BarRow.trade_date.asc())
            )
            rows = s.execute(q).scalars().all()
    data = [
        {
            "trade_date": r.trade_date,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "amount": r.amount,
        }
        for r in rows
    ]
    return pd.DataFrame(data)


def fetch_stock_name(symbol: str) -> str | None:
    """东财个股信息页解析简称；失败返回 None。"""
    sym = normalize_symbol(symbol)
    try:
        df = ak.stock_individual_info_em(symbol=sym)
        if df is None or df.empty:
            return None
        # 列名通常为 item / value
        m = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
        return m.get("股票简称") or m.get("证券简称")
    except Exception:
        logger.debug("name lookup failed for %s", sym, exc_info=True)
        return None


def is_trade_day(d: date | None = None) -> bool:
    """
    使用新浪交易日历近似判断某日是否交易日（依赖 akshare 内部请求）。

    拉取失败时默认 True，避免阻塞上层逻辑。
    """
    d = d or date.today()
    try:
        cal = ak.tool_trade_date_hist_sina()
        if cal is None or cal.empty:
            return True
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
        days = set(pd.to_datetime(cal[col]).dt.date.astype(str))
        return str(d) in days
    except Exception:
        return True


def purge_symbol_bars(symbol: str) -> int:
    """删除该标的全部 K 线；返回删除行数（维护/测试用）。"""
    sym = normalize_symbol(symbol)
    with session_scope() as s:
        r = s.execute(delete(BarRow).where(BarRow.symbol == sym))
        return r.rowcount or 0
