"""
行情摄取：通过 AkShare / Baostock 拉 A 股前复权日线，规范化后写入 SQLite（bars 表）。

路线 `auto`：新浪 → 腾讯 → Baostock；`eastmoney` / `akshare`：东财日线（`stock_zh_a_hist`），
相邻请求全局随机间隔约 3–5 秒（可配置）以防限流；`mootdx` / `tushare` 经 `app.quant_stock_selector` 核心拉取后转入库格式；亦可固定其它单一源（见 resolve_data_source）。

职责划分：
- normalize_symbol：统一为 6 位数字代码。
- fetch_ak_daily：单次区间拉取并转为内部列名（支持 data_source 覆盖）。
- incremental_refresh：相对库内最新日期做增量（带重叠窗口防漏日）。
- load_bars_df：给信号模块读库；不足 min_bars 时自动触发一次刷新。
- load_bars_from_db：仅读库、不联网刷新；供 walk-forward 验证等可复现研究使用。
- load_bars_for_forecast：可选先联网拉取 incremental 窗口并与本地行合并（可不写库），供行内回测。
- list_bars_from_db：按日期倒序取最近 limit 根再转为升序，供行情展示 API 使用。
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.db import BarRow, session_scope

logger = logging.getLogger(__name__)

ALLOWED_DATA_SOURCES = frozenset(
    {"auto", "eastmoney", "akshare", "sina", "tencent", "baostock", "mootdx", "tushare"}
)
_baostock_lock = threading.Lock()
_eastmoney_throttle_lock = threading.Lock()
_eastmoney_next_monotonic: float = 0.0


def resolve_data_source(explicit: str | None) -> str:
    """
    解析本次拉取使用的路线关键字。

    explicit 非空时须为 allowed 之一；否则用 Settings.ingest_data_source。
    """
    if explicit is not None and str(explicit).strip():
        k = str(explicit).strip().lower()
        if k not in ALLOWED_DATA_SOURCES:
            raise ValueError(f"无效 data_source: {explicit!r}，允许 {sorted(ALLOWED_DATA_SOURCES)}")
        return k
    return get_settings().ingest_data_source


def _yyyymmdd_to_iso(yyyymmdd: str) -> str:
    s = yyyymmdd.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期须为 YYYYMMDD: {yyyymmdd!r}")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _baostock_symbol(sym: str) -> str:
    return f"sh.{sym}" if sym.startswith("6") else f"sz.{sym}"


def _is_transient_http_error(exc: BaseException) -> bool:
    """远端限流、短暂断连、超时等，适合重试。"""
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    try:
        from urllib3.exceptions import IncompleteRead, ProtocolError

        if isinstance(exc, (ProtocolError, IncompleteRead)):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    for needle in (
        "remote end closed",
        "connection aborted",
        "connection reset",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "502",
        "503",
        "429",
        "unable to connect to proxy",
        "proxyerror",
    ):
        if needle in msg:
            return True
    if isinstance(exc, requests.exceptions.ProxyError):
        return True
    return False


def explain_ingest_failure(exc: BaseException) -> str:
    """面向操作者的中文说明（与日志中的完整堆栈配合使用）。"""
    low = str(exc).lower()
    parts: list[str] = []
    if isinstance(exc, requests.exceptions.ProxyError) or "unable to connect to proxy" in low:
        parts.append(
            "【原因】本机或环境变量配置了 HTTP/HTTPS **代理**，但代理无法连通或提前断开，"
            "导致访问东财等地址失败（常见于公司代理失效、Clash/VPN 未启动、系统代理残留）。"
        )
        parts.append(
            "【建议】① 在系统设置里关闭代理，或清空环境变量 HTTP_PROXY / HTTPS_PROXY 后重启 uvicorn；"
            "② 若本机可直连外网、仅代理损坏，可在 .env 设置 INGEST_EASTMONEY_BYPASS_PROXY=true，"
            "让东财日线请求临时不走代理；③ 或改用行情路线 sina / tencent / auto，避开东财。"
        )
    elif (
        "remote end closed" in low
        or "connection aborted" in low
        or "remote disconnected" in low
    ):
        parts.append(
            "【原因】数据源接口在返回内容前主动断开了 TCP 连接，本程序没有收到完整 HTTP 响应。"
        )
        parts.append(
            "【常见触发】请求过于频繁被限流/反爬；对方服务器短暂故障；本机网络、公司防火墙或代理不稳定。"
        )
        parts.append(
            "【建议】先点「测试数据源连接」；隔几分钟再拉；在 .env 增大 AKSHARE_PAUSE_BETWEEN_SYMBOLS_SEC、"
            "AKSHARE_FETCH_RETRIES；减少自选只数分批更新；换网络或关闭系统代理后再试。"
        )
    elif "timed out" in low or "timeout" in low:
        parts.append("【原因】等待数据源响应超时，多见于网络慢、链路抖动或对方繁忙。")
        parts.append("【建议】检查网络后重试，或稍后在非高峰时段再拉。")
    elif "429" in low or "too many requests" in low:
        parts.append("【原因】可能被对方识别为访问过频（HTTP 429 类限流）。")
        parts.append("【建议】拉长请求间隔、减少连续点击更新。")
    if not parts:
        parts.append("【原因】拉取或解析行情时发生未分类错误，请结合下方技术信息或服务器日志排查。")
    return "\n".join(parts)


def _format_ingest_error(exc: BaseException) -> str:
    return f"{explain_ingest_failure(exc)}\n\n【技术信息】{type(exc).__name__}: {exc}"


def _fetch_and_upsert(
    sym: str, start: str, end: str, mode: str, *, data_source: str | None = None
) -> dict:
    try:
        try:
            before_rows = list_bars_from_db(sym, limit=3)
        except ValueError:
            before_rows = []
        df, provider = fetch_daily_with_provider(sym, start, end, data_source=data_source)
        n = upsert_bars(df)
        out: dict = {
            "symbol": sym,
            "rows_upserted": n,
            "start": start,
            "end": end,
            "mode": mode,
            "data_source": resolve_data_source(data_source),
            "provider": provider,
        }
        try:
            after_rows = list_bars_from_db(sym, limit=3)
        except ValueError:
            after_rows = []
        if after_rows:
            lb = after_rows[-1]
            last_td = str(lb["trade_date"])[:10]
            out["last_trade_date"] = lb["trade_date"]
            out["last_close"] = round(float(lb["close"]), 4)
            out["last_volume"] = round(float(lb.get("volume") or 0), 4)
            prev_bar: dict[str, Any] | None = None
            if len(after_rows) >= 2 and str(after_rows[-2]["trade_date"])[:10] < last_td:
                prev_bar = after_rows[-2]
            else:
                for row in reversed(before_rows):
                    if str(row.get("trade_date") or "")[:10] < last_td:
                        prev_bar = row
                        break
            if prev_bar is not None:
                out["prev_trade_date"] = prev_bar["trade_date"]
                out["prev_close"] = round(float(prev_bar["close"]), 4)
                out["prev_volume"] = round(float(prev_bar.get("volume") or 0), 4)
        else:
            out["last_trade_date"] = None
            out["last_close"] = None
            out["last_volume"] = None
        return out
    except Exception as e:
        logger.warning("ingest failed for %s: %s", sym, _format_ingest_error(e))
        raise RuntimeError(_format_ingest_error(e)) from e


def normalize_symbol(symbol: str) -> str:
    """去掉非数字字符，校验长度为 6；否则抛 ValueError。"""
    s = re.sub(r"\D", "", symbol.strip())
    if len(s) != 6:
        raise ValueError("A 股代码须为 6 位数字")
    return s


def shanghai_today_date() -> date:
    """东八区自然日（A 股行情截止日对齐用）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _today_str() -> str:
    """AkShare 日期参数格式 YYYYMMDD（东八区今日）。"""
    return shanghai_today_date().strftime("%Y%m%d")


def _legacy_sh_sz_symbol(sym: str) -> str:
    """新浪/腾讯等接口常用前缀：6 字头为上交所，其余默认深交所。"""
    return f"sh{sym}" if sym.startswith("6") else f"sz{sym}"


def _empty_daily_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "volume", "amount"])


_AK_COL_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def _normalize_chinese_hist_df(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """东财 stock_zh_a_hist 中文列 → 内部标准列。"""
    ren = {c: _AK_COL_MAP[c] for c in df.columns if c in _AK_COL_MAP}
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


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def _temporary_clear_proxy_env(*, enabled: bool) -> Iterator[None]:
    """临时移除常见代理环境变量，便于 AkShare/requests 直连（用毕恢复）。"""
    if not enabled:
        yield
        return
    saved: dict[str, str] = {}
    for k in _PROXY_ENV_KEYS:
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ[k] = v


def _eastmoney_schedule_next_gap() -> None:
    """在锁内调用：根据配置设定下一次允许发起东财日线请求的时间点。"""
    global _eastmoney_next_monotonic
    s = get_settings()
    lo = max(0.0, float(s.eastmoney_request_min_interval_sec))
    hi = max(lo, float(s.eastmoney_request_max_interval_sec))
    _eastmoney_next_monotonic = time.monotonic() + (
        random.uniform(lo, hi) if hi > lo else lo
    )


def _fetch_eastmoney_hist(sym: str, start_yyyymmdd: str, end_yyyymmdd: str) -> pd.DataFrame:
    """
    东财日线；全局串行，相邻两次请求间隔随机落在 [min, max] 秒。

    短暂网络错误时按 Settings 重试；每次尝试均遵守间隔。
    """
    s = get_settings()
    max_retries = max(0, s.akshare_fetch_retries)
    base_delay = max(0.1, float(s.akshare_retry_base_delay_sec))
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        with _eastmoney_throttle_lock:
            now = time.monotonic()
            if now < _eastmoney_next_monotonic:
                time.sleep(_eastmoney_next_monotonic - now)
            try:
                with _temporary_clear_proxy_env(
                    enabled=bool(s.ingest_eastmoney_bypass_proxy),
                ):
                    df = ak.stock_zh_a_hist(
                        symbol=sym,
                        period="daily",
                        start_date=start_yyyymmdd,
                        end_date=end_yyyymmdd,
                        adjust="qfq",
                    )
                _eastmoney_schedule_next_gap()
                return df
            except Exception as e:
                last_err = e
                _eastmoney_schedule_next_gap()
        if attempt < max_retries and _is_transient_http_error(last_err):
            delay = base_delay * (2**attempt) + random.uniform(0, 0.4)
            logger.warning(
                "stock_zh_a_hist %s 短暂失败 (%s)，第 %s/%s 次重试，%.2fs 后再试",
                sym,
                last_err,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            continue
        raise last_err  # type: ignore[misc]
    raise RuntimeError("unreachable")


def _normalize_english_hist_df(df: pd.DataFrame, sym: str, *, volume_if_missing: float | None) -> pd.DataFrame:
    """stock_zh_a_daily / stock_zh_a_hist_tx 等英文列 → 内部标准列。"""
    if "date" in df.columns:
        df = df.rename(columns={"date": "trade_date"})
    elif "trade_date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "trade_date"})
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col not in df.columns:
            if col == "volume" and volume_if_missing is not None:
                df[col] = volume_if_missing
            else:
                df[col] = 0.0 if col in ("volume", "amount") else float("nan")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["symbol"] = sym
    return df[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]]


def _fetch_baostock_daily(sym: str, start_yyyymmdd: str, end_yyyymmdd: str) -> pd.DataFrame:
    """Baostock 前复权日线；login/logout 加锁以防并发争用同一连接。"""
    try:
        import baostock as bs
    except ImportError as e:
        raise RuntimeError("未安装 baostock，请执行: pip install baostock") from e
    start_iso = _yyyymmdd_to_iso(start_yyyymmdd)
    end_iso = _yyyymmdd_to_iso(end_yyyymmdd)
    code = _baostock_symbol(sym)
    rows: list[list[str]] = []
    with _baostock_lock:
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume,amount",
                start_date=start_iso,
                end_date=end_iso,
                frequency="d",
                adjustflag="2",
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    if not rows:
        return _empty_daily_df()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    return _normalize_english_hist_df(df, sym, volume_if_missing=None)


def _tushare_token_resolved() -> str | None:
    s = get_settings()
    raw = (getattr(s, "tushare_token", None) or os.getenv("TUSHARE_TOKEN") or "").strip()
    return raw or None


def _selector_core_df_to_bars(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """quant_stock_selector 标准行情列 → 与 _normalize_chinese_hist_df 一致的入库列。"""
    if df is None or df.empty:
        return _empty_daily_df()
    work = df.copy()
    if "date" not in work.columns:
        raise RuntimeError("核心数据源返回的 DataFrame 缺少 date 列")
    work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    work["symbol"] = sym
    if "turnover" in work.columns:
        work["amount"] = pd.to_numeric(work["turnover"], errors="coerce").fillna(0.0)
    elif "amount" in work.columns:
        work["amount"] = pd.to_numeric(work["amount"], errors="coerce").fillna(0.0)
    else:
        work["amount"] = 0.0
    for col in ("open", "high", "low", "close", "volume"):
        if col not in work.columns:
            raise RuntimeError(f"核心数据源 DataFrame 缺少列 {col}")
        work[col] = pd.to_numeric(work[col], errors="coerce")
    out = work[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]].dropna(
        subset=["trade_date", "open", "high", "low", "close"]
    )
    return out


def _fetch_quant_selector_daily(
    sym: str, start_y: str, end_y: str, route: str
) -> tuple[pd.DataFrame, str | None]:
    """经 quant_stock_selector 包拉取 mootdx / tushare 日线并转为入库格式。"""
    from app.quant_stock_selector import DataSourceError, get_data_source

    token = _tushare_token_resolved() if route == "tushare" else None
    try:
        ds = get_data_source(route, tushare_token=token)
        frame = ds.get_price_history(sym, start_y, end_y, adjust="qfq")
    except DataSourceError as e:
        raise RuntimeError(str(e)) from e
    if frame is None or frame.empty:
        return _empty_daily_df(), None
    bars = _selector_core_df_to_bars(frame, sym)
    if bars.empty:
        return _empty_daily_df(), None
    return bars, route


def _single_source_daily(
    sym: str, start_y: str, end_y: str, route: str
) -> tuple[pd.DataFrame, str | None]:
    """固定单一数据源；无行时返回空表且 provider 为 None。"""
    leg = _legacy_sh_sz_symbol(sym)
    if route in ("eastmoney", "akshare"):
        df = _fetch_eastmoney_hist(sym, start_y, end_y)
        if df is None or df.empty:
            return _empty_daily_df(), None
        return _normalize_chinese_hist_df(df, sym), route
    if route == "sina":
        df2 = ak.stock_zh_a_daily(symbol=leg, start_date=start_y, end_date=end_y, adjust="qfq")
        if df2 is None or df2.empty:
            return _empty_daily_df(), None
        return _normalize_english_hist_df(df2, sym, volume_if_missing=None), "sina"
    if route == "tencent":
        df3 = ak.stock_zh_a_hist_tx(symbol=leg, start_date=start_y, end_date=end_y, adjust="qfq")
        if df3 is None or df3.empty:
            return _empty_daily_df(), None
        return _normalize_english_hist_df(df3, sym, volume_if_missing=0.0), "tencent"
    if route == "baostock":
        df4 = _fetch_baostock_daily(sym, start_y, end_y)
        if df4.empty:
            return _empty_daily_df(), None
        return df4, "baostock"
    if route == "mootdx":
        return _fetch_quant_selector_daily(sym, start_y, end_y, "mootdx")
    if route == "tushare":
        return _fetch_quant_selector_daily(sym, start_y, end_y, "tushare")
    raise ValueError(f"未知路线: {route}")


def _fetch_auto_chain(sym: str, start_y: str, end_y: str) -> tuple[pd.DataFrame, str | None]:
    """新浪优先；失败或无行则腾讯 → Baostock。"""
    leg = _legacy_sh_sz_symbol(sym)
    errs: list[str] = []

    try:
        df2 = ak.stock_zh_a_daily(symbol=leg, start_date=start_y, end_date=end_y, adjust="qfq")
        if df2 is not None and not df2.empty:
            logger.info("ingest %s: 使用数据源 新浪财经(stock_zh_a_daily)", sym)
            return _normalize_english_hist_df(df2, sym, volume_if_missing=None), "sina"
        errs.append("新浪财经(stock_zh_a_daily): 区间内无数据行")
    except Exception as e2:
        errs.append(f"新浪财经(stock_zh_a_daily): {type(e2).__name__}: {e2}")
        logger.debug("新浪失败 %s: %s", sym, e2)

    try:
        df3 = ak.stock_zh_a_hist_tx(symbol=leg, start_date=start_y, end_date=end_y, adjust="qfq")
        if df3 is not None and not df3.empty:
            logger.info(
                "ingest %s: 使用数据源 腾讯(stock_zh_a_hist_tx)；成交量字段缺失已填 0",
                sym,
            )
            return _normalize_english_hist_df(df3, sym, volume_if_missing=0.0), "tencent"
        errs.append("腾讯(stock_zh_a_hist_tx): 区间内无数据行")
    except Exception as e3:
        errs.append(f"腾讯(stock_zh_a_hist_tx): {type(e3).__name__}: {e3}")
        logger.debug("腾讯失败 %s: %s", sym, e3)

    try:
        df4 = _fetch_baostock_daily(sym, start_y, end_y)
        if df4 is not None and not df4.empty:
            logger.info("ingest %s: 使用数据源 baostock", sym)
            return df4, "baostock"
        errs.append("baostock: 区间内无数据行")
    except Exception as e4:
        errs.append(f"baostock: {type(e4).__name__}: {e4}")
        logger.debug("baostock 失败 %s: %s", sym, e4)

    raise RuntimeError("日线拉取失败，新浪与备选源均未成功:\n" + "\n".join(errs))


def fetch_daily_with_provider(
    symbol: str, start_date: str, end_date: str, *, data_source: str | None = None
) -> tuple[pd.DataFrame, str | None]:
    """拉取日线并返回 (DataFrame, 实际数据提供方)；provider 在无行时为 None。"""
    route = resolve_data_source(data_source)
    sym = normalize_symbol(symbol)
    start_y = start_date.replace("-", "")
    end_y = end_date.replace("-", "")
    if route == "auto":
        return _fetch_auto_chain(sym, start_y, end_y)
    return _single_source_daily(sym, start_y, end_y, route)


def fetch_ak_daily(
    symbol: str, start_date: str, end_date: str, *, data_source: str | None = None
) -> pd.DataFrame:
    """
    拉取指定区间前复权日线。

    data_source 不传则用 Settings.ingest_data_source。auto：新浪→腾讯→Baostock；
    eastmoney：东财日线（请求间隔见 Settings）；单一源时仅走该线路。

    start_date / end_date：可为 YYYY-MM-DD 或 YYYYMMDD（内部会去掉横线）。
    返回列：symbol, trade_date, open, high, low, close, volume, amount；空则返回空表结构。
    """
    df, _ = fetch_daily_with_provider(symbol, start_date, end_date, data_source=data_source)
    return df


def max_stored_date(symbol: str) -> str | None:
    """该标的在库中最后一根 K 线的 trade_date（YYYY-MM-DD）；无数据返回 None。"""
    sym = normalize_symbol(symbol)
    with session_scope() as s:
        q = select(func.max(BarRow.trade_date)).where(BarRow.symbol == sym)
        return s.execute(q).scalar_one_or_none()


def list_bars_from_db(
    symbol: str,
    *,
    limit: int = 30,
    trade_date_from: str | None = None,
    trade_date_to: str | None = None,
) -> list[dict[str, Any]]:
    """
    读取本地 bars（前复权日线）。

    - 未传 trade_date_from / trade_date_to：按 trade_date **降序**取最近 limit 根，再转为**升序**（旧→新）。
    - 传入任一日期界：按闭区间筛选 trade_date（字符串 YYYY-MM-DD），**升序**，至多 limit 根（建议 500）。

    每根附带 change_pct：非区间模式相对返回序列内上一根；区间模式第一根相对**区间内**上一交易日的库内收盘（若无则 None）。
    limit 有效范围 1～500。
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit 须在 1～500")
    sym = normalize_symbol(symbol)
    use_range = trade_date_from is not None or trade_date_to is not None
    if use_range:
        d_from = (trade_date_from or "").strip() or None
        d_to = (trade_date_to or "").strip() or None
        if d_from and d_to and d_from > d_to:
            d_from, d_to = d_to, d_from
        with session_scope() as s:
            q = select(BarRow).where(BarRow.symbol == sym)
            if d_from:
                q = q.where(BarRow.trade_date >= d_from)
            if d_to:
                q = q.where(BarRow.trade_date <= d_to)
            q = q.order_by(BarRow.trade_date.asc()).limit(limit)
            orm_rows = list(s.execute(q).scalars().all())
            prev_close: float | None = None
            if orm_rows:
                first_td = orm_rows[0].trade_date
                pc = (
                    s.execute(
                        select(BarRow.close)
                        .where(BarRow.symbol == sym, BarRow.trade_date < first_td)
                        .order_by(BarRow.trade_date.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                )
                prev_close = float(pc) if pc is not None else None
            data = [
                {
                    "trade_date": r.trade_date,
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                    "volume": float(r.volume or 0),
                    "amount": float(r.amount or 0),
                }
                for r in orm_rows
            ]
        out: list[dict[str, Any]] = []
        for row in data:
            cp: float | None = None
            if prev_close is not None and abs(prev_close) > 1e-12:
                cp = round((row["close"] - prev_close) / abs(prev_close) * 100, 4)
            row["change_pct"] = cp
            prev_close = row["close"]
            out.append(row)
        return out

    with session_scope() as s:
        q = (
            select(BarRow)
            .where(BarRow.symbol == sym)
            .order_by(BarRow.trade_date.desc())
            .limit(limit)
        )
        orm_rows = s.execute(q).scalars().all()
        data = [
            {
                "trade_date": r.trade_date,
                "open": float(r.open),
                "high": float(r.high),
                "low": float(r.low),
                "close": float(r.close),
                "volume": float(r.volume or 0),
                "amount": float(r.amount or 0),
            }
            for r in orm_rows
        ]
    data.reverse()
    out2: list[dict[str, Any]] = []
    prev_close2: float | None = None
    for row in data:
        cp2: float | None = None
        if prev_close2 is not None and abs(prev_close2) > 1e-12:
            cp2 = round((row["close"] - prev_close2) / abs(prev_close2) * 100, 4)
        row["change_pct"] = cp2
        prev_close2 = row["close"]
        out2.append(row)
    return out2


def strength_snapshot_for_symbol(
    symbol: str,
    *,
    bar_limit: int = 120,
    last_price_override: float | None = None,
) -> dict[str, Any] | None:
    """
    基于本地已入库日线做简要强弱摘要（教学/自览用，非投资建议）。

    使用最近约 5 / 20 个交易日的收盘涨跌与收盘相对 MA20 位置打标签。
    last_price_override：用快照现价等替代最后一根日线收盘，供「当前强弱」列展示。
    """
    try:
        bars = list_bars_from_db(symbol, limit=bar_limit)
    except ValueError:
        return None
    if len(bars) < 2:
        return None
    closes = [float(b["close"]) for b in bars]
    if last_price_override is not None and math.isfinite(float(last_price_override)):
        last = float(last_price_override)
    else:
        last = closes[-1]
    out: dict[str, Any] = {}
    if len(closes) >= 6:
        base5 = closes[-6]
        if abs(base5) > 1e-12:
            out["ret_5d_pct"] = round((last / base5 - 1) * 100, 2)
    if len(closes) >= 21:
        base20 = closes[-21]
        if abs(base20) > 1e-12:
            out["ret_20d_pct"] = round((last / base20 - 1) * 100, 2)
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20.0
        out["ma20"] = round(ma20, 4)
        if abs(ma20) > 1e-12:
            out["vs_ma20_pct"] = round((last / ma20 - 1) * 100, 2)
    r20 = out.get("ret_20d_pct")
    vs = out.get("vs_ma20_pct")
    if r20 is not None and vs is not None:
        if r20 >= 0 and vs >= 0:
            out["strength_label"] = "相对偏强"
        elif r20 <= 0 and vs <= 0:
            out["strength_label"] = "相对偏弱"
        else:
            out["strength_label"] = "分化/震荡"
    elif vs is not None:
        out["strength_label"] = "站上MA20" if vs >= 0 else "跌破MA20"
    else:
        out["strength_label"] = "数据不足"
    return out


def _prev_bar_close_before(sym: str, before_ymd: str) -> float | None:
    """取 strictly 早于 before_ymd 的最近一根日线收盘，用于涨跌幅回退计算。"""
    try:
        rows = list_bars_from_db(sym, limit=8)
    except ValueError:
        return None
    before = before_ymd[:10]
    for row in reversed(rows):
        td = str(row.get("trade_date") or "")[:10]
        if not td or td >= before:
            continue
        c = float(row.get("close") or 0)
        if math.isfinite(c) and c > 0:
            return c
    return None


def _parse_bid_ask_em_df(df: pd.DataFrame) -> dict[str, float | None]:
    """东财单股 push2 报价表 → 最新价、涨跌幅%、昨收。"""
    if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
        return {}
    m = {
        str(r["item"]).strip(): r["value"]
        for _, r in df.iterrows()
        if pd.notna(r.get("value"))
    }
    out: dict[str, float | None] = {"px": None, "chg": None, "prev_close": None, "volume": None}
    for key in ("最新", "最新价", "现价"):
        if key in m:
            try:
                v = float(m[key])
                if math.isfinite(v) and v > 0:
                    out["px"] = v
                    break
            except (TypeError, ValueError):
                pass
    if "涨幅" in m:
        try:
            v = float(m["涨幅"])
            if math.isfinite(v):
                out["chg"] = round(v, 2)
        except (TypeError, ValueError):
            pass
    if "昨收" in m:
        try:
            v = float(m["昨收"])
            if math.isfinite(v) and v > 0:
                out["prev_close"] = v
        except (TypeError, ValueError):
            pass
    if out["px"] is not None and out["chg"] is None and out["prev_close"]:
        out["chg"] = round((float(out["px"]) / float(out["prev_close"]) - 1) * 100, 2)
    if "总手" in m:
        try:
            v = float(m["总手"])
            if math.isfinite(v) and v >= 0:
                out["volume"] = v
        except (TypeError, ValueError):
            pass
    return out


def live_quote_fields_for_codes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """
    单股东财 push2 报价（stock_bid_ask_em），供 ③ 下行现价定时刷新；不用全市场 stock_zh_a_spot_em。
    """
    from app.fundamentals import _now_iso, _shanghai_today_ymd

    uniq: list[str] = []
    seen: set[str] = set()
    for c in codes:
        nc = normalize_symbol(c) if c else ""
        if len(nc) != 6 or nc in seen:
            continue
        seen.add(nc)
        uniq.append(nc)
    out: dict[str, dict[str, Any]] = {c: {} for c in uniq}
    if not uniq:
        return out
    s = get_settings()
    sh_today = _shanghai_today_ymd()
    fetched_at = _now_iso()
    for sym in uniq:
        try:
            with _temporary_clear_proxy_env(enabled=bool(s.ingest_eastmoney_bypass_proxy)):
                df = ak.stock_bid_ask_em(symbol=sym)
            parsed = _parse_bid_ask_em_df(df)
            px = parsed.get("px")
            if px is None or not math.isfinite(float(px)) or float(px) <= 0:
                continue
            row: dict[str, Any] = {
                "live_last_price": round(float(px), 4),
                "live_quote_date": sh_today,
                "live_fetched_at": fetched_at,
                "live_price_source": "eastmoney_bid_ask",
            }
            chg = parsed.get("chg")
            if chg is not None and math.isfinite(float(chg)):
                row["live_change_pct"] = round(float(chg), 2)
            vol = parsed.get("volume")
            if vol is not None and math.isfinite(float(vol)) and float(vol) >= 0:
                row["live_volume"] = round(float(vol), 4)
            out[sym] = row
        except Exception as e:
            logger.debug("live_quote %s: %s", sym, e)
    return out


def _live_row_has_price(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    p = row.get("live_last_price")
    return p is not None and math.isfinite(float(p)) and float(p) > 0


def augment_live_quote_fields(
    codes: list[str],
    live_by: dict[str, dict[str, Any]] | None = None,
    *,
    data_source: str | None = None,
    force_spot_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """
    ③ 现价：东财单股 push2 → 东财全 A 列表快照 → 通达信批量行情（东财失败时兜底，与日线路线无关）。
    """
    from app.fundamentals import _now_iso, _shanghai_today_ymd, spot_liquidity_fields_for_codes

    uniq: list[str] = []
    seen: set[str] = set()
    for c in codes:
        try:
            nc = normalize_symbol(c)
        except ValueError:
            continue
        if len(nc) != 6 or nc in seen:
            continue
        seen.add(nc)
        uniq.append(nc)
    out: dict[str, dict[str, Any]] = dict(live_by or {})
    for c in uniq:
        if c not in out:
            out[c] = {}
    if not uniq:
        return out

    missing = [s for s in uniq if not _live_row_has_price(out.get(s))]
    if missing:
        try:
            spot_by = spot_liquidity_fields_for_codes(missing, force_refresh=force_spot_refresh)
        except Exception as e:
            logger.warning("augment_live_quote spot list failed: %s", e)
            spot_by = {}
        fetched_at = _now_iso()
        sh_today = _shanghai_today_ymd()
        for sym in missing:
            row = spot_by.get(sym) or {}
            p = row.get("spot_last_price")
            if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                continue
            chg = row.get("spot_change_pct")
            chg_f = round(float(chg), 2) if chg is not None and math.isfinite(float(chg)) else None
            out[sym] = {
                "live_last_price": round(float(p), 4),
                "live_change_pct": chg_f,
                "live_quote_date": row.get("spot_quote_date") or sh_today,
                "live_fetched_at": row.get("spot_fetched_at") or fetched_at,
                "live_price_source": "eastmoney_spot_list",
            }
            vol = row.get("spot_volume")
            if vol is not None and math.isfinite(float(vol)) and float(vol) >= 0:
                out[sym]["live_volume"] = round(float(vol), 4)

    missing = [s for s in uniq if not _live_row_has_price(out.get(s))]
    if missing:
        try:
            from app.quant_stock_selector import get_data_source
            from app.quant_stock_selector.market_utils import normalize_code

            ds_mx = get_data_source("mootdx")
            fn = getattr(ds_mx, "quote_snapshot_for_codes", None)
            if callable(fn):
                raw = fn(missing)
                fetched_at = _now_iso()
                sh_today = _shanghai_today_ymd()
                for k, row in (raw or {}).items():
                    if not isinstance(row, dict):
                        continue
                    sym = normalize_code(str(k))
                    if len(sym) != 6 or _live_row_has_price(out.get(sym)):
                        continue
                    p = row.get("tdx_last_price")
                    if p is None or not math.isfinite(float(p)) or float(p) <= 0:
                        continue
                    chg = row.get("tdx_change_pct")
                    chg_f = round(float(chg), 2) if chg is not None and math.isfinite(float(chg)) else None
                    qd = row.get("tdx_quote_date")
                    out[sym] = {
                        "live_last_price": round(float(p), 4),
                        "live_change_pct": chg_f,
                        "live_quote_date": (
                            str(qd)[:10]
                            if isinstance(qd, str) and len(qd) >= 10
                            else sh_today
                        ),
                        "live_fetched_at": fetched_at,
                        "live_price_source": "mootdx_snapshot",
                    }
        except Exception as e:
            logger.debug("augment_live_quote mootdx: %s", e)
    return out


def live_quote_fields_for_codes_enhanced(
    codes: list[str],
    *,
    data_source: str | None = None,
    force_spot_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """单股 push2 + 列表快照 + mootdx（按路线）的现价合并。"""
    base = live_quote_fields_for_codes(codes)
    return augment_live_quote_fields(
        codes, base, data_source=data_source, force_spot_refresh=force_spot_refresh
    )


def _daily_snapshot_fields_for_symbol(
    sym: str,
    ingest_row: dict[str, Any],
    *,
    data_source: str | None = None,
) -> dict[str, Any]:
    """
    ③/④ 用与入库一致的日线末根作为「现价」：本地 bars 或按 data_source 短拉日线（如新浪 stock_zh_a_daily）。
    不调用东财全市场 stock_zh_a_spot_em。
    """
    from app.fundamentals import _now_iso

    route = resolve_data_source(data_source)
    td_raw = ingest_row.get("last_trade_date")
    lc_raw = ingest_row.get("last_close")
    td = str(td_raw)[:10] if td_raw else None
    lc: float | None = None
    if lc_raw is not None and math.isfinite(float(lc_raw)) and float(lc_raw) > 0:
        lc = float(lc_raw)
    if lc is None or not td:
        try:
            bars = list_bars_from_db(sym, limit=3)
        except ValueError:
            bars = []
        if bars:
            last = bars[-1]
            td = str(last.get("trade_date") or "")[:10] or td
            c = float(last.get("close") or 0)
            if math.isfinite(c) and c > 0:
                lc = c
    if lc is None:
        try:
            end_y = _today_str()
            start_d = shanghai_today_date() - timedelta(days=14)
            start_y = start_d.strftime("%Y%m%d")
            df, prov = _single_source_daily(sym, start_y, end_y, route)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                td = str(row["trade_date"])[:10]
                c = float(row["close"])
                if math.isfinite(c) and c > 0:
                    lc = c
                    route = str(prov or route)
        except Exception as e:
            logger.debug("daily snapshot short fetch %s route=%s: %s", sym, route, e)
    if lc is None or not td:
        return {}
    out: dict[str, Any] = {
        "spot_last_price": round(lc, 4),
        "spot_quote_date": td,
        "spot_fetched_at": _now_iso(),
        "spot_price_source": f"daily_{route}",
    }
    prev = _prev_bar_close_before(sym, td)
    if prev is not None:
        out["spot_change_pct"] = round((lc / prev - 1) * 100, 2)
    return out


def resolve_ingest_row_display_pair(sym: str, row: dict[str, Any]) -> None:
    """
    ③ 双行「昨/今」与东八区执行日对齐。

    - **今（下行）**：`ingest_exec_date` = 本次执行自然日；收盘/入库参考 `display_bar_trade_date`（最近一根完整日线）。
    - **昨（上行）**：执行日晚于末根交易日时，昨行 = 该末根（即盘中「昨收」参照，而非库中倒数第二根）。
      执行日等于末根日时，昨行 = 末根的前一根日线。
    """
    exec_d = shanghai_today_date().isoformat()
    row["ingest_exec_date"] = exec_d
    last_td = str(row.get("last_trade_date") or "")[:10] or None
    last_close = row.get("last_close")
    last_vol = row.get("last_volume")
    row["display_bar_trade_date"] = last_td
    row["display_today_trade_date"] = exec_d

    if not last_td:
        row["display_prev_trade_date"] = row.get("prev_trade_date")
        row["display_prev_close"] = row.get("prev_close")
        row["display_prev_volume"] = row.get("prev_volume")
        row["display_pair_basis"] = "no_last_bar"
        return

    if exec_d > last_td:
        row["display_prev_trade_date"] = last_td
        row["display_prev_close"] = last_close
        row["display_prev_volume"] = last_vol
        row["display_pair_basis"] = "exec_after_last_bar"
    elif exec_d == last_td:
        row["display_prev_trade_date"] = row.get("prev_trade_date")
        row["display_prev_close"] = row.get("prev_close")
        row["display_prev_volume"] = row.get("prev_volume")
        row["display_pair_basis"] = "exec_same_as_last_bar"
    else:
        row["display_prev_trade_date"] = row.get("prev_trade_date")
        row["display_prev_close"] = row.get("prev_close")
        row["display_prev_volume"] = row.get("prev_volume")
        row["display_pair_basis"] = "exec_before_last_bar"


def enrich_ingest_results_with_spot(
    results: list[dict[str, Any]],
    *,
    data_source: str | None = None,
) -> None:
    """
    ③ 结果：上行用 display_prev_*（昨收参照），下行用 last_* + live_*；并写入 ingest_exec_date 等展示字段。
    """
    from app.fundamentals import _now_iso

    ok_syms = [str(r["symbol"]) for r in results if r.get("symbol") and "error" not in r]
    live_by = (
        live_quote_fields_for_codes_enhanced(
            ok_syms, data_source=data_source, force_spot_refresh=True
        )
        if ok_syms
        else {}
    )
    req_at = _now_iso()
    for r in results:
        if r.get("error"):
            continue
        sym = str(r["symbol"])
        resolve_ingest_row_display_pair(sym, r)
        ref_prev = r.get("display_prev_close")
        if ref_prev is None or not math.isfinite(float(ref_prev)) or float(ref_prev) <= 0:
            ref_prev = r.get("prev_close")
        if ref_prev is not None and math.isfinite(float(ref_prev)) and float(ref_prev) > 0:
            ps = strength_snapshot_for_symbol(sym, last_price_override=float(ref_prev))
            if ps is not None:
                r["prev_strength"] = ps
        live = live_by.get(sym) or {}
        if not _live_row_has_price(live):
            snap = _daily_snapshot_fields_for_symbol(sym, r, data_source=data_source)
            if snap.get("spot_last_price") is not None:
                live = {
                    "live_last_price": snap["spot_last_price"],
                    "live_change_pct": snap.get("spot_change_pct"),
                    "live_quote_date": snap.get("spot_quote_date"),
                    "live_fetched_at": snap.get("spot_fetched_at") or req_at,
                    "live_price_source": "daily_close_not_realtime",
                }
        if live:
            for k, v in live.items():
                r[k] = v
            if live.get("live_volume") is not None:
                r["live_volume"] = live["live_volume"]
            r["spot_last_price"] = live.get("live_last_price")
            r["spot_change_pct"] = live.get("live_change_pct")
            r["spot_quote_date"] = live.get("live_quote_date") or r.get("last_trade_date")
            r["spot_fetched_at"] = live.get("live_fetched_at") or req_at
            r["spot_price_source"] = live.get("live_price_source")
        elif r.get("last_close") is not None:
            lc = float(r["last_close"])
            r["live_last_price"] = round(lc, 4)
            r["live_fetched_at"] = req_at
            r["live_price_source"] = "last_close_static"
            r["spot_last_price"] = round(lc, 4)
        ref_close = r.get("display_prev_close") or r.get("prev_close")
        px_live = r.get("live_last_price") or r.get("spot_last_price")
        if (
            px_live is not None
            and math.isfinite(float(px_live))
            and ref_close is not None
            and math.isfinite(float(ref_close))
            and float(ref_close) > 0
            and (
                r.get("display_pair_basis") == "exec_after_last_bar"
                or r.get("live_change_pct") is None
            )
        ):
            chg = round((float(px_live) / float(ref_close) - 1) * 100, 2)
            r["live_change_pct"] = chg
            r["spot_change_pct"] = chg
        px = r.get("live_last_price") or r.get("spot_last_price")
        if px is not None and math.isfinite(float(px)) and float(px) > 0:
            st = strength_snapshot_for_symbol(sym, last_price_override=float(px))
            if st is not None:
                r["spot_strength"] = st


def upsert_bars(df: pd.DataFrame) -> int:
    """
    将 DataFrame 行写入 bars；SQLite 下用 INSERT ... ON CONFLICT DO UPDATE 实现按日覆盖。
    返回成功处理的行数。
    """
    if df.empty:
        return 0
    rows = df.to_dict(orient="records")
    n = 0
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
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
                ingested_at=now_iso,
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
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            s.execute(stmt)
            n += 1
    return n


def watchlist_bar_fields_for_session(session: Session, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """
    自选表展示用：每标的在本地 bars 中的最新一行收盘价、最后交易日、
    以及该标的任意 K 线最近一次入库时间（max ingested_at）。
    """
    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        row = (
            session.execute(
                select(BarRow).where(BarRow.symbol == sym).order_by(BarRow.trade_date.desc()).limit(1)
            )
            .scalars()
            .first()
        )
        max_ing = session.execute(select(func.max(BarRow.ingested_at)).where(BarRow.symbol == sym)).scalar_one_or_none()
        if row is None:
            out[sym] = {
                "bars_last_ingested_at": None,
                "bars_last_trade_date": None,
                "last_close": None,
                "last_daily_close_label": None,
            }
            continue
        td = row.trade_date
        close_v = round(float(row.close), 4)
        label = f"{td} 交易日日线收盘（A 股常规 15:00 北京时间）"
        out[sym] = {
            "bars_last_ingested_at": max_ing,
            "bars_last_trade_date": td,
            "last_close": close_v,
            "last_daily_close_label": label,
        }
    return out


def incremental_fetch_window_yyyymmdd(
    symbol: str,
    lookback_years: int = 5,
    *,
    as_of_date: date | None = None,
) -> tuple[str, str]:
    """
    与 incremental_refresh 相同的闭区间起止（YYYYMMDD 字符串，含首尾）。

    用于「只拉不写库」时与 fetch_daily_with_provider 对齐的窗口。
    """
    sym = normalize_symbol(symbol)
    today = shanghai_today_date()
    end_d = as_of_date if as_of_date is not None else today
    if end_d > today:
        raise ValueError("截止日期不能晚于东八区今日")
    end = end_d.strftime("%Y%m%d")
    last = max_stored_date(sym)
    if last:
        last_d = datetime.strptime(last, "%Y-%m-%d").date()
        if last_d <= end_d:
            start_d = last_d - timedelta(days=14)
        else:
            start_d = end_d - timedelta(days=365 * lookback_years)
    else:
        start_d = end_d - timedelta(days=365 * lookback_years)
    if start_d > end_d:
        start_d = end_d
    start = start_d.strftime("%Y%m%d")
    if start > end:
        start = end
    return start, end


def incremental_refresh(
    symbol: str,
    lookback_years: int = 5,
    *,
    as_of_date: date | None = None,
    data_source: str | None = None,
) -> dict:
    """
    拉取并写入 SQLite。

    - as_of_date：请求行情的**截止日期**（含当日）；默认今天。
    - 若库中最后交易日 ≤ 截止日期：从「最后交易日往前 14 天」拉到截止日期（增量）。
    - 若库中数据新于截止日期：从截止日期往前约 lookback_years 年拉到截止日期（补历史）。
    - 若无历史：从截止日期往前约 lookback_years 年拉取。
    返回摘要 dict；网络或数据源异常时抛 RuntimeError；截止日期晚于今天抛 ValueError。
    data_source：None 时用 Settings.ingest_data_source。
    """
    sym = normalize_symbol(symbol)
    start, end = incremental_fetch_window_yyyymmdd(sym, lookback_years, as_of_date=as_of_date)
    return _fetch_and_upsert(sym, start, end, "incremental", data_source=data_source)


def load_bars_for_forecast(
    symbol: str,
    *,
    live_bars: bool,
    live_persist: bool = True,
    data_source: str | None = None,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """
    供 walk-forward 回测取日线 DataFrame（列与 load_bars_from_db 一致，按 trade_date 升序）。

    - live_bars=False：仅 SQLite。
    - live_bars=True 且 live_persist=True：incremental_refresh 后读库（写入本地）。
    - live_bars=True 且 live_persist=False：按 incremental 窗口联网拉取，与当前库内数据按 trade_date 合并（远程覆盖同日），**不写库**。
    as_of_date：联网时的行情截止日期（含当日）；None 表示今天。应与用户③结束日期或期望样本外终点一致。
    """
    sym = normalize_symbol(symbol)
    if not live_bars:
        return load_bars_from_db(sym)
    if live_persist:
        incremental_refresh(sym, data_source=data_source, as_of_date=as_of_date)
        return load_bars_from_db(sym)
    df_db = load_bars_from_db(sym)
    start_y, end_y = incremental_fetch_window_yyyymmdd(sym, as_of_date=as_of_date)
    df_new, _ = fetch_daily_with_provider(sym, start_y, end_y, data_source=data_source)
    cols = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    if df_db.empty:
        if df_new is None or df_new.empty:
            raise RuntimeError("联网拉取无数据且本地库无该标的日线，无法回测")
        return df_new.drop(columns=["symbol"])
    if df_new is None or df_new.empty:
        return df_db
    db2 = df_db.copy()
    if "symbol" not in db2.columns:
        db2["symbol"] = sym
    for c in cols:
        if c not in db2.columns:
            raise RuntimeError(f"本地 bars 缺少列 {c!r}")
    db2 = db2[cols]
    new2 = df_new.reindex(columns=cols)
    out = pd.concat([db2, new2], ignore_index=True)
    out = out.drop_duplicates(subset=["trade_date"], keep="last").sort_values("trade_date").reset_index(drop=True)
    return out.drop(columns=["symbol"])


def ingest_symbol_range(
    symbol: str,
    *,
    range_start: date | None = None,
    range_end: date | None = None,
    lookback_years: int = 5,
    data_source: str | None = None,
) -> dict:
    """
    按日期参数拉取并入库（自选批量更新入口）。

    - 同时传 start、end：按闭区间 [start, end] 拉取（若 start>end 会自动对调）；结束日不能晚于今天。
    - 仅 start：从 start 拉到「今天」。
    - 仅 end：等价于 incremental_refresh(..., as_of_date=end)。
    - 都不传：等价于 incremental_refresh 默认（增量到今天）。
    data_source：None 时用 Settings.ingest_data_source。
    """
    sym = normalize_symbol(symbol)
    today = date.today()
    if range_start is not None and range_end is not None:
        a, b = range_start, range_end
        if a > b:
            a, b = b, a
        if b > today:
            raise ValueError("结束日期不能晚于今天")
        if a > today:
            raise ValueError("开始日期不能晚于今天")
        start = a.strftime("%Y%m%d")
        end = b.strftime("%Y%m%d")
        return _fetch_and_upsert(sym, start, end, "explicit_range", data_source=data_source)
    if range_start is not None and range_end is None:
        if range_start > today:
            raise ValueError("开始日期不能晚于今天")
        start = range_start.strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        return _fetch_and_upsert(sym, start, end, "start_to_today", data_source=data_source)
    if range_start is None and range_end is not None:
        return incremental_refresh(
            sym, lookback_years=lookback_years, as_of_date=range_end, data_source=data_source
        )
    return incremental_refresh(
        sym, lookback_years=lookback_years, as_of_date=None, data_source=data_source
    )


def test_akshare_connectivity(*, data_source: str | None = None) -> dict:
    """
    用极短区间请求探测本机能否访问配置的行情路线（不代表任意个股一定成功）。
    """
    import time as time_module

    route = resolve_data_source(data_source)
    sym = normalize_symbol(get_settings().akshare_test_symbol)
    end_d = date.today()
    start_d = end_d - timedelta(days=5)
    start = start_d.strftime("%Y%m%d")
    end = end_d.strftime("%Y%m%d")
    t0 = time_module.perf_counter()
    try:
        df, provider = fetch_daily_with_provider(sym, start, end, data_source=route)
        ms = int((time_module.perf_counter() - t0) * 1000)
        n = 0 if df is None else len(df.index)
        return {
            "ok": True,
            "probe_symbol": sym,
            "rows_received": n,
            "latency_ms": ms,
            "range": f"{start} ~ {end}",
            "data_source": route,
            "provider": provider,
            "user_message": "已连通数据源并收到日线数据（探测用短区间）。若个股仍失败，多为该股请求被限流，请隔段时间再试。",
        }
    except Exception as e:
        ms = int((time_module.perf_counter() - t0) * 1000)
        logger.warning("connectivity test failed: %s", e)
        return {
            "ok": False,
            "probe_symbol": sym,
            "rows_received": 0,
            "latency_ms": ms,
            "range": f"{start} ~ {end}",
            "data_source": route,
            "provider": None,
            "user_message": explain_ingest_failure(e),
            "error_type": type(e).__name__,
            "detail": str(e),
        }


def load_bars_df(
    symbol: str, min_bars: int = 80, *, data_source: str | None = None
) -> pd.DataFrame:
    """
    从库中读出该标的全部日线为 DataFrame（按日期升序）。

    若行数 < min_bars，先调用 incremental_refresh 再读一次（仍不足则返回当前能读到的行）。
    data_source：传给 incremental_refresh；None 时用 Settings.ingest_data_source。
    """
    sym = normalize_symbol(symbol)

    def _load_records() -> list[dict]:
        with session_scope() as s:
            q = (
                select(BarRow)
                .where(BarRow.symbol == sym)
                .order_by(BarRow.trade_date.asc())
            )
            rows = s.execute(q).scalars().all()
            # 在 Session 内展开 ORM 属性，避免关闭会话后的 DetachedInstanceError
            return [
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

    data = _load_records()
    if len(data) < min_bars:
        incremental_refresh(sym, data_source=data_source)
        data = _load_records()
    return pd.DataFrame(data)


def load_bars_from_db(symbol: str) -> pd.DataFrame:
    """
    仅从 SQLite 读取该标的全部日线（日期升序），不触发 incremental_refresh。

    用于可复现的回测 / 预测验证；若行数为 0，请先 POST /ingest/update。
    """
    sym = normalize_symbol(symbol)

    with session_scope() as s:
        q = select(BarRow).where(BarRow.symbol == sym).order_by(BarRow.trade_date.asc())
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
    """
    证券简称：优先东财个股信息接口；失败则用沪深京 A 股代码表（交易所源，AkShare 聚合）。

    与行情路线（新浪/Baostock 等）无关；仅展示用。
    """
    sym = normalize_symbol(symbol)
    try:
        df = ak.stock_individual_info_em(symbol=sym)
        if df is not None and not df.empty:
            m = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
            n = m.get("股票简称") or m.get("证券简称")
            if n and str(n).strip():
                return str(n).strip()
    except Exception:
        logger.debug("name EM lookup failed for %s", sym, exc_info=True)
    try:
        tab = ak.stock_info_a_code_name()
        if tab is None or tab.empty or "code" not in tab.columns:
            return None
        codes = tab["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        hit = tab.loc[codes == sym]
        if hit.empty or "name" not in hit.columns:
            return None
        n2 = hit.iloc[0]["name"]
        if n2 is not None and pd.notna(n2) and str(n2).strip():
            return str(n2).strip()
    except Exception:
        logger.debug("name code_name fallback failed for %s", sym, exc_info=True)
    return None


def fetch_stock_names_map(symbols: list[str]) -> dict[str, str]:
    """
    批量解析证券简称：一次拉取 A 股代码表，再筛出 symbols 子集。

    失败或缺列时返回空 dict；与 fetch_stock_name 的第二段逻辑一致，避免列表页 N 次请求。
    """
    wanted: set[str] = set()
    for raw in symbols:
        try:
            wanted.add(normalize_symbol(raw))
        except ValueError:
            continue
    if not wanted:
        return {}
    try:
        tab = ak.stock_info_a_code_name()
        if tab is None or tab.empty or "code" not in tab.columns or "name" not in tab.columns:
            return {}
        codes = tab["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        names = tab["name"]
        out: dict[str, str] = {}
        for i in range(len(tab)):
            c = str(codes.iloc[i])
            if c not in wanted:
                continue
            n2 = names.iloc[i]
            if n2 is not None and pd.notna(n2) and str(n2).strip():
                out[c] = str(n2).strip()
        return out
    except Exception:
        logger.debug("fetch_stock_names_map failed", exc_info=True)
        return {}


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
