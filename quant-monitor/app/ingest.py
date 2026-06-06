"""
行情摄取（ingest）：拉 A 股前复权日线、写 SQLite，并为信号/自选/持仓提供读库与现价。

## 功能作用

本模块是 quant-monitor 的**数据层核心**，负责：

1. **联网拉取**多源前复权日线（AkShare / Baostock / mootdx / TuShare 等）；
2. **规范化**列名与代码，**upsert** 到 SQLite `bars` 表；
3. **增量更新**（相对库内末根日期重叠窗口防漏日）；
4. **读库**供 `signals`、`forecast_validate`、持仓、自选展示使用；
5. **现价/强弱** enrichment（东财 push2、列表快照、mootdx 等）供 ③ 结果行与 ② 列表。

控制台 **③ 更新行情**（`POST /ingest/update`）、`GET /ingest/bars`、信号计算、
walk-forward 回测、前向展望 pre_refresh 等均依赖本模块。

## 行情路线（data_source）

| 路线 | 行为 |
|------|------|
| `auto` | 新浪 → 腾讯 → Baostock 链式尝试 |
| `eastmoney` / `akshare` | 东财 `stock_zh_a_hist`，全局随机间隔 3–5s（可配置） |
| `sina` / `tencent` / `baostock` | 固定单一源 |
| `mootdx` / `tushare` | 经 `quant_stock_selector` 核心拉取后转入库格式 |

解析：`resolve_data_source(explicit)`，未传则用 `Settings.ingest_data_source`。

## 职责分层（常用入口）

| 层级 | 函数 | 说明 |
|------|------|------|
| 拉取 | `fetch_ak_daily` / `fetch_daily_with_provider` | 指定区间日线 DataFrame |
| 入库 | `incremental_refresh` / `ingest_symbol_range` | 增量或区间拉取并 upsert |
| 写库 | `upsert_bars` | INSERT ON CONFLICT 按日覆盖 |
| 读库（信号） | `load_bars_df` | 全量升序；不足 min_bars 可自动 incremental |
| 读库（研究） | `load_bars_from_db` / `load_bars_for_forecast` | 仅库或可选联网合并（可不写库） |
| 读库（API） | `list_bars_from_db` | 最近 N 根或日期区间，带 change_pct |
| 现价 | `live_quote_fields_for_codes_enhanced` | 多源合并现价/涨跌幅 |
| ③ 展示 | `enrich_*_ingest_*` / `local_ingest_result_row` | 结果行上下行、spot、强弱 |
| 工具 | `normalize_symbol` / `shanghai_today_date` / `fetch_stock_name` | 全项目共用 |

## 与其它模块

- `ingest_batch_job`：③ 批量拉日线时的进度（skip_bars=false）；
- `fundamentals` / `eastmoney_liquidity`：扩展因子与流动性 spot；
- `forward_outlook` / `alerts`：pre_refresh 调用 `incremental_refresh`。

## 配置相关

东财节流：`eastmoney_request_min_interval_sec` / `max`；
重试：`akshare_fetch_retries`；批量间隔：`akshare_pause_between_symbols_sec`；
代理绕过：`ingest_eastmoney_bypass_proxy`（`_temporary_clear_proxy_env`）。
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
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.db import BarRow, SymbolIngestMetaRow, session_scope

logger = logging.getLogger(__name__)

ALLOWED_DATA_SOURCES = frozenset(
    {"auto", "eastmoney", "akshare", "sina", "tencent", "baostock", "mootdx", "tushare"}
)
_baostock_lock = threading.Lock()
_eastmoney_throttle_lock = threading.Lock()
_eastmoney_next_monotonic: float = 0.0


# --- 路线解析 ---


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


# --- 网络错误与拉取入库核心 ---


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
    sym: str,
    start: str,
    end: str,
    mode: str,
    *,
    data_source: str | None = None,
    range_only: bool = False,
) -> dict:
    """
    拉取 [start,end] 区间日线、upsert，并组装 ingest 结果摘要 dict。

    含 last/prev 交易日、可选当日 bar 补写、bars 首次/末次入库时间等字段。
    range_only=True 时仅拉取并 upsert 闭区间内 K 线，不补写当日 bar、不联网补前一交易日。
    """
    try:
        try:
            before_rows = list_bars_from_db(sym, limit=3)
        except ValueError:
            before_rows = []
        df, provider = fetch_daily_with_provider(sym, start, end, data_source=data_source)
        n = upsert_bars(df)
        n_backfill = 0
        if not range_only:
            n_backfill = _try_backfill_today_bar_from_live(sym, data_source=data_source)
            if n_backfill:
                n += n_backfill
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
            if n_backfill:
                out["today_bar_backfill"] = True
                out["today_bar_backfill_source"] = "live_quote_after_close"
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
            elif last_td and not range_only:
                remote_prev = _fetch_prev_trading_bar_remote(
                    sym, last_td, data_source=data_source, upsert=True
                )
                if remote_prev is not None:
                    out["prev_trade_date"] = remote_prev["trade_date"]
                    out["prev_close"] = remote_prev["close"]
                    out["prev_volume"] = remote_prev.get("volume")
                    out["prev_bar_backfill_source"] = remote_prev.get("source")
        else:
            out["last_trade_date"] = None
            out["last_close"] = None
            out["last_volume"] = None
        try:
            with session_scope() as s:
                first_ing, max_ing = bars_ingest_timestamp_bounds(s, sym)
            out["bars_first_ingested_at"] = first_ing
            out["bars_last_ingested_at"] = max_ing
        except Exception:
            pass
        return out
    except Exception as e:
        logger.warning("ingest failed for %s: %s", sym, _format_ingest_error(e))
        raise RuntimeError(_format_ingest_error(e)) from e


# --- 代码规范与时间（东八区） ---


def normalize_symbol(symbol: str) -> str:
    """去掉非数字字符，校验长度为 6；否则抛 ValueError。"""
    s = re.sub(r"\D", "", symbol.strip())
    if len(s) != 6:
        raise ValueError("A 股代码须为 6 位数字")
    return s


def shanghai_today_date() -> date:
    """东八区自然日（A 股行情截止日对齐用）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _shanghai_a_share_session_closed() -> bool:
    """周一至周五且已过 15:00（东八区），视为当日收盘后可补日线。"""
    now = shanghai_now()
    if now.weekday() >= 5:
        return False
    return now.time() >= time(15, 0)


# --- 当日 K 线补写（盘中现价 → 临时 bar） ---


def backfill_today_bar_from_live(
    sym: str,
    *,
    trade_date: str | None = None,
    data_source: str | None = None,
    allow_intraday: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    用联网现价补写或更新「今日」日线 bar（OHLC=现价），供②「当日收盘」展示。

    - 默认仅东八区工作日 15:00 后执行（与 ingest 自动补写一致）；
    - allow_intraday=True 时盘中也可补写（盘中价为参考，非交易所正式收盘）；
    - force_refresh=True 时若已有今日 bar 仍用最新现价覆盖。
    """
    sym = normalize_symbol(sym)
    today = shanghai_today_date().isoformat()
    target = trade_date or today
    if trade_date:
        try:
            # validate YYYY-MM-DD
            date.fromisoformat(str(trade_date))
        except Exception as e:
            return {
                "ok": False,
                "symbol": sym,
                "rows_upserted": 0,
                "skipped_reason": "invalid_trade_date",
                "error": f"trade_date 无效：{trade_date}",
            }
    last = max_stored_date(sym)
    last_td = str(last)[:10] if last else ""
    if target == today and (not allow_intraday) and (not _shanghai_a_share_session_closed()):
        return {
            "ok": False,
            "symbol": sym,
            "rows_upserted": 0,
            "skipped_reason": "before_session_close",
            "error": "未过当日 15:00（东八区），请使用 allow_intraday 或收盘后再试",
        }
    if last_td >= target and not force_refresh:
        return {
            "ok": True,
            "symbol": sym,
            "rows_upserted": 0,
            "skipped_reason": "already_has_today_bar",
            "trade_date": target,
            "close": None,
        }
    live = (
        live_quote_fields_for_codes_enhanced(
            [sym], data_source=data_source, force_spot_refresh=True
        ).get(sym)
        or {}
    )
    px = live.get("live_last_price")
    if px is None:
        return {
            "ok": False,
            "symbol": sym,
            "rows_upserted": 0,
            "skipped_reason": "no_live_price",
            "error": "未能获取联网现价",
        }
    try:
        px_f = float(px)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "symbol": sym,
            "rows_upserted": 0,
            "skipped_reason": "invalid_live_price",
            "error": "现价无效",
        }
    if not (math.isfinite(px_f) and px_f > 0):
        return {
            "ok": False,
            "symbol": sym,
            "rows_upserted": 0,
            "skipped_reason": "invalid_live_price",
            "error": "现价无效",
        }
    src = str(live.get("live_price_source") or "live_quote")
    px_f = round(px_f, 4)
    vol = 0.0
    lv = live.get("live_volume")
    if lv is not None:
        try:
            v = float(lv)
            if math.isfinite(v) and v >= 0:
                vol = v
        except (TypeError, ValueError):
            pass
    if vol <= 0:
        try:
            prev = list_bars_from_db(sym, limit=1)
            if prev:
                vol = float(prev[-1].get("volume") or 0)
        except ValueError:
            pass
    row = {
        "symbol": sym,
        "trade_date": target,
        "open": px_f,
        "high": px_f,
        "low": px_f,
        "close": px_f,
        "volume": vol,
        "amount": 0.0,
    }
    n = upsert_bars(pd.DataFrame([row]))
    provisional = target == today and allow_intraday and not _shanghai_a_share_session_closed()
    if n:
        logger.info(
            "backfill today daily bar %s trade_date=%s close=%s source=%s provisional=%s",
            sym,
            target,
            px_f,
            src,
            provisional,
        )
    return {
        "ok": n > 0,
        "symbol": sym,
        "rows_upserted": int(n),
        "trade_date": target,
        "close": px_f,
        "live_price_source": src,
        "provisional": provisional,
        "skipped_reason": None if n else "upsert_failed",
    }


def _try_backfill_today_bar_from_live(
    sym: str,
    *,
    data_source: str | None = None,
) -> int:
    """收盘后自动补今日 bar（ingest 拉日线后调用）。"""
    r = backfill_today_bar_from_live(sym, data_source=data_source, allow_intraday=False)
    return int(r.get("rows_upserted") or 0)


def ensure_today_bar_for_live_signal(
    sym: str,
    *,
    data_source: str | None = None,
) -> dict[str, Any]:
    """
    测算当日：用联网现价补写或刷新东八区「今日」日线，使信号末根为当日（盘中为参考价）。

    周末/节假日不写入「今日」bar，沿用最近交易日 K 线。失败时抛出 ValueError。
    """
    sym_n = normalize_symbol(sym)
    if shanghai_today_date().weekday() >= 5:
        return {
            "ok": True,
            "symbol": sym_n,
            "rows_upserted": 0,
            "skipped_reason": "non_trading_day",
        }
    r = backfill_today_bar_from_live(
        sym_n,
        data_source=data_source,
        allow_intraday=True,
        force_refresh=True,
    )
    if r.get("ok"):
        return r
    err = str(r.get("error") or "未能写入今日 K 线")
    reason = str(r.get("skipped_reason") or "")
    raise ValueError(
        f"{sym_n} {err}"
        + (f"（{reason}）" if reason else "")
        + "；请点「刷新列表」并确认 ③ 数据源与网络"
    )


def backfill_watchlist_today_close_batch(
    symbols: list[str],
    *,
    trade_date: str | None = None,
    data_source: str | None = None,
    allow_intraday: bool = True,
    force_refresh: bool = True,
) -> list[dict[str, Any]]:
    """批量用现价补写/更新自选标的的当日收盘 bar。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in symbols:
        try:
            sym = normalize_symbol(raw)
        except ValueError as e:
            out.append(
                {
                    "ok": False,
                    "symbol": str(raw),
                    "rows_upserted": 0,
                    "error": str(e),
                    "skipped_reason": "invalid_symbol",
                }
            )
            continue
        if sym in seen:
            continue
        seen.add(sym)
        try:
            out.append(
                backfill_today_bar_from_live(
                    sym,
                    trade_date=trade_date,
                    data_source=data_source,
                    allow_intraday=allow_intraday,
                    force_refresh=force_refresh,
                )
            )
        except Exception as e:
            logger.warning("backfill today close %s: %s", sym, e)
            out.append(
                {
                    "ok": False,
                    "symbol": sym,
                    "rows_upserted": 0,
                    "error": str(e),
                    "skipped_reason": "exception",
                }
            )
    return out


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


# --- 东财日线节流与拉取 ---


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


# --- 对外拉取 API ---


def fetch_daily_with_provider(    symbol: str, start_date: str, end_date: str, *, data_source: str | None = None
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


# --- 读库（bars 表） ---


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
    volumes = [float(b.get("volume") or 0) for b in bars]
    amounts = [float(b.get("amount") or 0) for b in bars]
    if len(volumes) >= 20:
        avg_v20 = sum(volumes[-20:]) / 20.0
        if avg_v20 > 0:
            out["avg_volume_20"] = round(avg_v20, 4)
    if len(amounts) >= 20:
        avg_a20 = sum(amounts[-20:]) / 20.0
        if avg_a20 > 0:
            out["avg_amount_20d_yuan"] = round(avg_a20, 2)
            out["avg_amount_20d_100m"] = round(avg_a20 / 1e8, 4)
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


def _prev_trading_bar_from_db(sym: str, before_ymd: str) -> dict[str, Any] | None:
    """取 strictly 早于 before_ymd 的最近一根本地日线（旧→新序）。"""
    try:
        rows = list_bars_from_db(sym, limit=16)
    except ValueError:
        return None
    before = before_ymd[:10]
    for row in reversed(rows):
        td = str(row.get("trade_date") or "")[:10]
        if not td or td >= before:
            continue
        c = float(row.get("close") or 0)
        if math.isfinite(c) and c > 0:
            return {
                "trade_date": td,
                "close": round(c, 4),
                "volume": round(float(row.get("volume") or 0), 4),
                "source": "local",
            }
    return None


def _has_local_prev_bar_before(sym: str, last_td: str) -> bool:
    return _prev_trading_bar_from_db(sym, last_td) is not None


def _fetch_prev_trading_bar_remote(
    sym: str,
    before_ymd: str,
    *,
    data_source: str | None = None,
    upsert: bool = True,
) -> dict[str, Any] | None:
    """联网拉取 before_ymd 之前最近一根完整日线（最近收盘的前一交易日）。"""
    before = before_ymd[:10]
    try:
        end_d = datetime.strptime(before, "%Y-%m-%d").date() - timedelta(days=1)
    except ValueError:
        return None
    start_d = end_d - timedelta(days=120)
    start_y = start_d.strftime("%Y%m%d")
    end_y = end_d.strftime("%Y%m%d")
    try:
        df, prov = fetch_daily_with_provider(sym, start_y, end_y, data_source=data_source)
    except Exception as e:
        logger.debug("fetch prev trading bar %s before=%s: %s", sym, before, e)
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df["_td"] = df["trade_date"].astype(str).str[:10]
    sub = df[df["_td"] < before]
    if sub.empty:
        return None
    row = sub.iloc[-1]
    td = str(row["trade_date"])[:10]
    c = float(row["close"])
    if not math.isfinite(c) or c <= 0:
        return None
    out: dict[str, Any] = {
        "trade_date": td,
        "close": round(c, 4),
        "volume": round(float(row.get("volume") or 0), 4),
        "source": f"remote_{prov or resolve_data_source(data_source)}",
    }
    if upsert:
        try:
            upsert_bars(sub.tail(60).drop(columns=["_td"], errors="ignore"))
        except Exception as e:
            logger.debug("upsert prev bars %s: %s", sym, e)
    return out


def ensure_ingest_prev_bar(
    sym: str,
    row: dict[str, Any],
    *,
    data_source: str | None = None,
) -> None:
    """
    入库结果若缺「末根前一交易日」，则从本地 bars 或按 data_source 短拉日线补齐 prev_*。

    同时写入 ingest_has_local_prev_bar（是否能在本地读到末根前一交易日）。
    """
    last_td = str(row.get("last_trade_date") or "")[:10] or None
    if not last_td:
        row["ingest_has_local_prev_bar"] = False
        return
    had_local = _has_local_prev_bar_before(sym, last_td)
    row["ingest_has_local_prev_bar"] = had_local
    pt = str(row.get("prev_trade_date") or "")[:10] or None
    pc = row.get("prev_close")
    has_valid_prev = (
        pt is not None
        and pt < last_td
        and pc is not None
        and math.isfinite(float(pc))
        and float(pc) > 0
    )
    if has_valid_prev and had_local:
        return
    if pt and last_td and pt >= last_td:
        has_valid_prev = False
    bar = _prev_trading_bar_from_db(sym, last_td)
    if bar is None:
        bar = _fetch_prev_trading_bar_remote(sym, last_td, data_source=data_source, upsert=True)
    if bar is None:
        return
    row["prev_trade_date"] = bar["trade_date"]
    row["prev_close"] = bar["close"]
    row["prev_volume"] = bar.get("volume")
    row["prev_bar_backfill_source"] = bar.get("source")


def _ensure_min_bars_for_strength(
    sym: str,
    min_count: int = 6,
    *,
    data_source: str | None = None,
) -> None:
    """本地 K 线不足时短拉历史并入库，供 prev_strength / spot_strength 计算。"""
    try:
        bars = list_bars_from_db(sym, limit=min_count + 5)
    except ValueError:
        bars = []
    if len(bars) >= min_count:
        return
    end_y = _today_str()
    start_d = shanghai_today_date() - timedelta(days=180)
    start_y = start_d.strftime("%Y%m%d")
    try:
        df, _ = fetch_daily_with_provider(sym, start_y, end_y, data_source=data_source)
        if df is not None and not df.empty:
            upsert_bars(df)
    except Exception as e:
        logger.debug("ensure min bars for strength %s: %s", sym, e)


def _prev_bar_close_before(sym: str, before_ymd: str) -> float | None:
    """取 strictly 早于 before_ymd 的最近一根日线收盘，用于涨跌幅回退计算。"""
    bar = _prev_trading_bar_from_db(sym, before_ymd)
    return bar["close"] if bar else None


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
    for vol_key in ("总手", "成交量", "成交总手"):
        if vol_key in m:
            try:
                v = float(m[vol_key])
                if math.isfinite(v) and v >= 0:
                    out["volume"] = v
                    break
            except (TypeError, ValueError):
                pass
    return out


# --- 现价 / 盘口（多源合并） ---


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
                row["live_volume_source"] = "eastmoney_bid_ask_lots"
            out[sym] = row
        except Exception as e:
            logger.debug("live_quote %s: %s", sym, e)
    return out


def _live_row_has_price(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    p = row.get("live_last_price")
    return p is not None and math.isfinite(float(p)) and float(p) > 0


def live_quote_fields_from_local_bars(codes: list[str]) -> dict[str, dict[str, Any]]:
    """本地 bars 末根收盘作现价（③ 已入库即可，不联网）。"""
    from app.fundamentals import _now_iso

    out: dict[str, dict[str, Any]] = {}
    fetched_at = _now_iso()
    for c in codes:
        try:
            sym = normalize_symbol(c)
        except ValueError:
            continue
        if len(sym) != 6 or sym in out:
            continue
        try:
            bars = list_bars_from_db(sym, limit=1)
        except ValueError:
            bars = []
        if not bars:
            continue
        last = bars[-1]
        cpx = float(last.get("close") or 0)
        if not math.isfinite(cpx) or cpx <= 0:
            continue
        td = str(last.get("trade_date") or "")[:10] or None
        out[sym] = {
            "live_last_price": round(cpx, 4),
            "live_quote_date": td,
            "live_fetched_at": fetched_at,
            "live_price_source": "local_daily_close",
        }
    return out


def live_quote_fields_from_sina_daily(codes: list[str]) -> dict[str, dict[str, Any]]:
    """新浪 stock_zh_a_daily 末根作现价回退（不拉东财全 A 表）。"""
    from app.fundamentals import _now_iso

    out: dict[str, dict[str, Any]] = {}
    if not codes:
        return out
    end_y = _today_str()
    start_d = shanghai_today_date() - timedelta(days=14)
    start_y = start_d.strftime("%Y%m%d")
    fetched_at = _now_iso()
    for c in codes:
        try:
            sym = normalize_symbol(c)
        except ValueError:
            continue
        if len(sym) != 6 or sym in out:
            continue
        try:
            df, _prov = _single_source_daily(sym, start_y, end_y, "sina")
        except Exception as e:
            logger.debug("sina daily quote %s: %s", sym, e)
            continue
        if df is None or df.empty:
            continue
        row = df.iloc[-1]
        cpx = float(row["close"])
        if not math.isfinite(cpx) or cpx <= 0:
            continue
        td = str(row["trade_date"])[:10]
        out[sym] = {
            "live_last_price": round(cpx, 4),
            "live_quote_date": td,
            "live_fetched_at": fetched_at,
            "live_price_source": "sina_daily_last",
        }
    return out


def _merge_live_quote_rows(
    out: dict[str, dict[str, Any]],
    src: dict[str, dict[str, Any]],
    *,
    only_missing: list[str],
) -> None:
    for sym in only_missing:
        if _live_row_has_price(out.get(sym)):
            continue
        row = src.get(sym) or {}
        if not _live_row_has_price(row):
            continue
        out[sym] = dict(row)


def augment_live_quote_fields(
    codes: list[str],
    live_by: dict[str, dict[str, Any]] | None = None,
    *,
    data_source: str | None = None,
    force_spot_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """
    ③/⑩ 现价：东财单股 push2（入参 live_by）→ 东财全 A 列表 → 通达信 → 新浪/腾讯日线 → 本地 bars。

    日线收盘价回退必须排在盘口源之后，否则盘中会误用「上一根入库 K 线收盘」充当现价。
    """
    from app.fundamentals import _now_iso, _shanghai_today_ymd, spot_liquidity_fields_for_codes

    route = resolve_data_source(data_source)
    s = get_settings()
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
    if missing and route != "sina":
        try:
            with _temporary_clear_proxy_env(enabled=bool(s.ingest_eastmoney_bypass_proxy)):
                spot_by = spot_liquidity_fields_for_codes(
                    missing, force_refresh=force_spot_refresh
                )
        except Exception as e:
            logger.debug("augment_live_quote spot list failed: %s", e)
            spot_by = {}
        fetched_at = _now_iso()
        sh_today = _shanghai_today_ymd()
        for sym in missing:
            if _live_row_has_price(out.get(sym)):
                continue
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
                out[sym]["live_volume_source"] = "eastmoney_spot_list"
            tr = row.get("spot_turnover_rate")
            if tr is not None and math.isfinite(float(tr)):
                out[sym]["spot_turnover_rate"] = round(float(tr), 4)
            amt = row.get("spot_amount")
            if amt is not None and math.isfinite(float(amt)):
                out[sym]["spot_amount"] = round(float(amt), 2)

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

    missing = [s for s in uniq if not _live_row_has_price(out.get(s))]
    if missing and route in ("sina", "auto", "tencent"):
        if route in ("sina", "auto"):
            _merge_live_quote_rows(
                out, live_quote_fields_from_sina_daily(missing), only_missing=missing
            )
        else:
            end_y = _today_str()
            start_d = shanghai_today_date() - timedelta(days=14)
            start_y = start_d.strftime("%Y%m%d")
            fetched_at = _now_iso()
            tenc: dict[str, dict[str, Any]] = {}
            for sym in missing:
                if _live_row_has_price(out.get(sym)):
                    continue
                try:
                    df, _ = _single_source_daily(sym, start_y, end_y, "tencent")
                    if df is None or df.empty:
                        continue
                    row = df.iloc[-1]
                    cpx = float(row["close"])
                    if math.isfinite(cpx) and cpx > 0:
                        tenc[sym] = {
                            "live_last_price": round(cpx, 4),
                            "live_quote_date": str(row["trade_date"])[:10],
                            "live_fetched_at": fetched_at,
                            "live_price_source": "tencent_daily_last",
                        }
                except Exception as e:
                    logger.debug("tencent daily quote %s: %s", sym, e)
            _merge_live_quote_rows(out, tenc, only_missing=missing)

    missing = [s for s in uniq if not _live_row_has_price(out.get(s))]
    if missing:
        _merge_live_quote_rows(out, live_quote_fields_from_local_bars(missing), only_missing=missing)

    # --- volume fallback: fill missing live_volume from spot list ---
    vol_missing = [
        sym for sym in uniq
        if _live_row_has_price(out.get(sym))
        and not (
            out.get(sym, {}).get("live_volume") is not None
            and math.isfinite(float(out[sym]["live_volume"]))
            and float(out[sym]["live_volume"]) > 0
        )
    ]
    if vol_missing and route != "sina":
        try:
            with _temporary_clear_proxy_env(enabled=bool(s.ingest_eastmoney_bypass_proxy)):
                spot_vol_by = spot_liquidity_fields_for_codes(
                    vol_missing, force_refresh=False
                )
        except Exception as e:
            logger.debug("augment_live_quote volume fallback: %s", e)
            spot_vol_by = {}
        for sym in vol_missing:
            row = spot_vol_by.get(sym) or {}
            vol = row.get("spot_volume")
            if vol is not None and math.isfinite(float(vol)) and float(vol) > 0:
                out[sym]["live_volume"] = round(float(vol), 4)
                out[sym]["live_volume_source"] = "eastmoney_spot_list_vol_fallback"
            tr = row.get("spot_turnover_rate")
            if tr is not None and math.isfinite(float(tr)) and sym in out:
                out[sym]["spot_turnover_rate"] = round(float(tr), 4)
            amt = row.get("spot_amount")
            if amt is not None and math.isfinite(float(amt)) and sym in out:
                out[sym]["spot_amount"] = round(float(amt), 2)

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


# --- ③ ingest 结果行 enrichment ---


def local_ingest_result_row(sym: str, *, data_source: str | None = None) -> dict[str, Any]:
    """不联网拉日线；仅用本地 bars 构造 ingest/update 结果行（skip_bars 模式）。"""
    pair = local_bars_pair_row(sym)
    out: dict[str, Any] = {
        "symbol": sym,
        "rows_upserted": 0,
        "mode": "skip_bars_local",
        "data_source": resolve_data_source(data_source),
    }
    out.update(pair)
    return out


def _daily_snapshot_fields_for_symbol(
    sym: str,
    ingest_row: dict[str, Any],
    *,
    data_source: str | None = None,
    skip_bar_fetch: bool = False,
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
    if lc is None and not skip_bar_fetch:
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


def apply_ingest_volume_compare(row: dict[str, Any]) -> None:
    """③ 量价评价：量比(相对20日均量) + 涨跌联合一句话 + 危险量提示。"""
    from app.volume_price_analyze import analyze_ingest_volume_price

    analyze_ingest_volume_price(row)


def resolve_ingest_row_display_pair(sym: str, row: dict[str, Any]) -> None:
    """
    ③ 双行「昨/今」与东八区执行日对齐。

    - **今（下行）**：`ingest_exec_date` = 执行自然日；收盘价列 = 最近一根完整日线 `last_*`；涨跌幅分母 = `display_today_ref_close`（最近收盘）。
    - **昨（上行）**：执行日晚于末根日线时展示**最近完整交易日收盘**（`last_*`）；否则为末根**上一交易日**（`prev_*`）。
    """
    exec_d = shanghai_today_date().isoformat()
    row["ingest_exec_date"] = exec_d
    last_td = str(row.get("last_trade_date") or "")[:10] or None
    last_close = row.get("last_close")
    row["display_bar_trade_date"] = last_td
    row["display_today_trade_date"] = exec_d
    row["display_today_ref_trade_date"] = last_td
    row["display_today_ref_close"] = last_close

    if not last_td:
        row["display_prev_trade_date"] = row.get("prev_trade_date")
        row["display_prev_close"] = row.get("prev_close")
        row["display_prev_volume"] = row.get("prev_volume")
        row["display_pair_basis"] = "no_last_bar"
        return

    if exec_d > last_td:
        # 执行日已晚于末根日线（如周一盘前/盘中）：上行展示「最近完整收盘」而非再前一交易日
        row["display_prev_trade_date"] = last_td
        row["display_prev_close"] = last_close
        lv = row.get("last_volume")
        row["display_prev_volume"] = lv if lv is not None else row.get("prev_volume")
        row["display_pair_basis"] = "last_close_as_ingest_prev_ref"
    else:
        row["display_prev_trade_date"] = row.get("prev_trade_date")
        row["display_prev_close"] = row.get("prev_close")
        row["display_prev_volume"] = row.get("prev_volume")
        if exec_d == last_td:
            row["display_pair_basis"] = "exec_same_as_last_bar"
        else:
            row["display_pair_basis"] = "exec_before_last_bar"


def _apply_spot_enrich_to_ingest_row(
    r: dict[str, Any],
    live: dict[str, Any],
    *,
    data_source: str | None = None,
    skip_bar_fetch: bool = False,
    req_at: str | None = None,
) -> None:
    """将单条 ingest 结果行与现价/强弱等展示字段合并。"""
    from app.fundamentals import _now_iso

    if req_at is None:
        req_at = _now_iso()
    sym = str(r["symbol"])
    apply_watchlist_prev_display(sym, r)
    ref_prev = r.get("display_prev_close")
    if ref_prev is None or not math.isfinite(float(ref_prev)) or float(ref_prev) <= 0:
        ref_prev = r.get("prev_close")
    if ref_prev is not None and math.isfinite(float(ref_prev)) and float(ref_prev) > 0:
        if not skip_bar_fetch:
            _ensure_min_bars_for_strength(sym, data_source=data_source)
        ps = strength_snapshot_for_symbol(sym, last_price_override=float(ref_prev))
        if ps is not None:
            r["prev_strength"] = ps
    if not _live_row_has_price(live):
        snap = _daily_snapshot_fields_for_symbol(
            sym, r, data_source=data_source, skip_bar_fetch=skip_bar_fetch
        )
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
    exec_d = str(r.get("ingest_exec_date") or "")[:10]
    last_td = str(r.get("last_trade_date") or "")[:10]
    if exec_d and last_td and exec_d > last_td:
        ref_close = r.get("display_today_ref_close") or r.get("last_close")
    elif exec_d and last_td and exec_d == last_td:
        ref_close = r.get("display_prev_close") or r.get("prev_close")
    else:
        ref_close = r.get("display_today_ref_close") or r.get("last_close")
    px_live = r.get("live_last_price") or r.get("spot_last_price")
    if (
        px_live is not None
        and math.isfinite(float(px_live))
        and ref_close is not None
        and math.isfinite(float(ref_close))
        and float(ref_close) > 0
        and r.get("live_change_pct") is None
    ):
        chg = round((float(px_live) / float(ref_close) - 1) * 100, 2)
        r["live_change_pct"] = chg
        r["spot_change_pct"] = chg
    px = r.get("live_last_price") or r.get("spot_last_price")
    if px is not None and math.isfinite(float(px)) and float(px) > 0:
        if not skip_bar_fetch:
            _ensure_min_bars_for_strength(sym, data_source=data_source)
        st = strength_snapshot_for_symbol(sym, last_price_override=float(px))
        if st is not None:
            r["spot_strength"] = st
    route = resolve_data_source(data_source)
    if route in ("eastmoney", "akshare", "auto"):
        try:
            from app.eastmoney_liquidity import merge_eastmoney_spot_into_row
            from app.fundamentals import load_fundamental_panel_from_db, spot_liquidity_fields_for_codes

            spot_ex = spot_liquidity_fields_for_codes([sym], force_refresh=False).get(sym) or {}
            merge_eastmoney_spot_into_row(r, spot_ex, prefer_spot_volume=True)
            if r.get("live_volume") is None:
                logger.debug(
                    "enrich %s: live_volume still None after spot merge; spot_volume=%s, last_volume=%s",
                    sym, spot_ex.get("spot_volume"), r.get("last_volume"),
                )
            if r.get("spot_turnover_rate") is not None:
                r["volume_data_source"] = "eastmoney"
            fp = load_fundamental_panel_from_db(sym)
            if fp is not None:
                r["fundamentals"] = fp
        except Exception as e:
            logger.debug("eastmoney spot merge %s: %s", sym, e)
    apply_ingest_volume_compare(r)


def enrich_ingest_results_with_spot(
    results: list[dict[str, Any]],
    *,
    data_source: str | None = None,
    skip_bar_fetch: bool = False,
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
    route = resolve_data_source(data_source)
    for r in results:
        if r.get("error"):
            continue
        sym = str(r["symbol"])
        _apply_spot_enrich_to_ingest_row(
            r,
            live_by.get(sym) or {},
            data_source=data_source,
            skip_bar_fetch=skip_bar_fetch,
            req_at=req_at,
        )
    if route in ("eastmoney", "akshare", "auto") and ok_syms:
        try:
            from app.eastmoney_liquidity import merge_eastmoney_spot_batch

            by_sym = {str(r["symbol"]): r for r in results if r.get("symbol") and "error" not in r}
            merge_eastmoney_spot_batch(by_sym, ok_syms, force_refresh=False)
            for r in by_sym.values():
                if r.get("spot_turnover_rate") is not None:
                    apply_ingest_volume_compare(r)
        except Exception as e:
            logger.debug("enrich batch eastmoney spot: %s", e)


def watchlist_spot_entry_to_live_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """将 ② WatchlistItem 现价字段转为 ingest enrich 用的 live 字典。"""
    px = entry.get("live_last_price")
    if px is None:
        px = entry.get("spot_last_price")
    if px is None or not math.isfinite(float(px)) or float(px) <= 0:
        return {}
    chg = entry.get("live_change_pct")
    if chg is None:
        chg = entry.get("spot_change_pct")
    chg_f: float | None = None
    if chg is not None and math.isfinite(float(chg)):
        chg_f = round(float(chg), 2)
    qd = entry.get("live_quote_date") or entry.get("spot_quote_date")
    fa = entry.get("live_fetched_at") or entry.get("spot_fetched_at")
    src = entry.get("live_price_source") or "watchlist_spot_reuse"
    out: dict[str, Any] = {
        "live_last_price": round(float(px), 4),
        "live_change_pct": chg_f,
        "live_quote_date": str(qd)[:10] if qd else None,
        "live_fetched_at": fa,
        "live_price_source": src,
    }
    vol = entry.get("live_volume")
    if vol is None:
        vol = entry.get("spot_volume")
    if vol is not None and math.isfinite(float(vol)) and float(vol) >= 0:
        out["live_volume"] = round(float(vol), 4)
    return out


def parse_watchlist_spot_reuse_map(
    reuse: bool,
    raw: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """解析 ③ Body.watchlist_spot_by_symbol → symbol → live 字段（仅含有效现价）。"""
    if not reuse or not raw:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in raw.items():
        try:
            sym = normalize_symbol(str(key))
        except ValueError:
            continue
        if isinstance(val, dict):
            entry = val
        else:
            try:
                entry = val.model_dump(exclude_none=True)  # type: ignore[union-attr]
            except AttributeError:
                continue
        live = watchlist_spot_entry_to_live_fields(entry)
        if _live_row_has_price(live):
            out[sym] = live
    return out


def enrich_one_ingest_result_spot(
    r: dict[str, Any],
    *,
    data_source: str | None = None,
    skip_bar_fetch: bool = False,
    prefetched_live: dict[str, Any] | None = None,
    skip_spot_network: bool = False,
) -> None:
    """单条 ingest 结果：联网补现价并写入展示字段（③ 表格一行完整数据）。"""
    from app.fundamentals import _now_iso

    if r.get("error"):
        return
    sym = str(r.get("symbol") or "")
    if not sym:
        return
    req_at = _now_iso()
    live: dict[str, Any] = {}
    if prefetched_live and _live_row_has_price(prefetched_live):
        live = dict(prefetched_live)
    elif not skip_spot_network:
        try:
            live_by = live_quote_fields_for_codes_enhanced(
                [sym], data_source=data_source, force_spot_refresh=True
            )
        except Exception:
            live_by = {}
        live = live_by.get(sym) or {}
    _apply_spot_enrich_to_ingest_row(
        r,
        live,
        data_source=data_source,
        skip_bar_fetch=skip_bar_fetch,
        req_at=req_at,
    )


def enrich_ingest_results_with_spot_progress(
    results: list[dict[str, Any]],
    *,
    data_source: str | None = None,
    skip_bar_fetch: bool = False,
    should_cancel: Callable[[], bool] | None = None,
    on_symbol_done: Callable[[str], None] | None = None,
) -> bool:
    """
    逐只联网补现价并 enrich；每完成一只（含失败行）调用 on_symbol_done，供进度条 +1。
    返回 True 表示用户已取消。
    """
    from app.fundamentals import _now_iso

    req_at = _now_iso()
    for r in results:
        if should_cancel and should_cancel():
            return True
        sym = str(r.get("symbol") or "")
        if not sym:
            continue
        if r.get("error"):
            if on_symbol_done:
                on_symbol_done(sym)
            continue
        try:
            live_by = live_quote_fields_for_codes_enhanced(
                [sym], data_source=data_source, force_spot_refresh=True
            )
        except Exception:
            live_by = {}
        _apply_spot_enrich_to_ingest_row(
            r,
            live_by.get(sym) or {},
            data_source=data_source,
            skip_bar_fetch=skip_bar_fetch,
            req_at=req_at,
        )
        if on_symbol_done:
            on_symbol_done(sym)
    return False


# --- 写库 ---


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
    sym_n = normalize_symbol(str(rows[0]["symbol"]))
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
                    # 保留各交易日首次写入时间，供②「首次入库」展示
                    "ingested_at": func.coalesce(
                        BarRow.ingested_at, stmt.excluded.ingested_at
                    ),
                },
            )
            s.execute(stmt)
            n += 1
        if n > 0:
            ensure_symbol_first_ingest(sym_n, session=s, at=now_iso)
    return n


def local_bars_pair_row(
    sym: str,
    *,
    session: Session | None = None,
    seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    从本地 SQLite bars 构造 last/prev 并 resolve 展示对（与②自选昨日收盘同源）。

    不调用 ensure_ingest_prev_bar / 不联网短拉补昨收；③④ 展示昨日收盘应走本函数。
    seed 可带入 ingest 行已有的 last_trade_date、last_close 等。
    """
    sym_n = normalize_symbol(sym)
    pair: dict[str, Any] = dict(seed) if seed else {}

    def _fill_from_orm_rows(recent: list[Any]) -> None:
        if not recent:
            return
        row_orm = recent[0]
        pair["last_trade_date"] = row_orm.trade_date
        pair["last_close"] = round(float(row_orm.close), 4)
        pair["last_volume"] = float(row_orm.volume or 0)
        if len(recent) >= 2:
            pb = recent[1]
            pair["prev_trade_date"] = pb.trade_date
            pair["prev_close"] = round(float(pb.close), 4)
            pair["prev_volume"] = float(pb.volume or 0)

    if pair.get("last_trade_date") and pair.get("last_close") is not None:
        last_td = str(pair["last_trade_date"])[:10]
        pair["ingest_has_local_prev_bar"] = _has_local_prev_bar_before(sym_n, last_td)
        if not pair.get("prev_close"):
            bar = _prev_trading_bar_from_db(sym_n, last_td)
            if bar is not None:
                pair["prev_trade_date"] = bar["trade_date"]
                pair["prev_close"] = bar["close"]
                pair["prev_volume"] = bar.get("volume")
    else:
        if session is not None:
            recent = list(
                session.execute(
                    select(BarRow)
                    .where(BarRow.symbol == sym_n)
                    .order_by(BarRow.trade_date.desc())
                    .limit(2)
                )
                .scalars()
                .all()
            )
            _fill_from_orm_rows(recent)
        else:
            try:
                bars = list_bars_from_db(sym_n, limit=5)
            except ValueError:
                bars = []
            if bars:
                lb = bars[-1]
                pair["last_trade_date"] = lb["trade_date"]
                pair["last_close"] = round(float(lb["close"]), 4)
                pair["last_volume"] = lb.get("volume")
                if len(bars) >= 2:
                    pb = bars[-2]
                    pair["prev_trade_date"] = pb["trade_date"]
                    pair["prev_close"] = round(float(pb["close"]), 4)
                    pair["prev_volume"] = pb.get("volume")
        last_td2 = str(pair.get("last_trade_date") or "")[:10] or None
        pair["ingest_has_local_prev_bar"] = (
            bool(last_td2 and _has_local_prev_bar_before(sym_n, last_td2))
            if last_td2
            else False
        )

    resolve_ingest_row_display_pair(sym_n, pair)
    return pair


def apply_watchlist_prev_display(sym: str, row: dict[str, Any]) -> None:
    """将②自选同款昨日收盘（display_prev_*）写入 ingest/结果行，仅用本地库。"""
    pair = local_bars_pair_row(
        sym,
        seed={
            "last_trade_date": row.get("last_trade_date"),
            "last_close": row.get("last_close"),
            "last_volume": row.get("last_volume"),
            "prev_trade_date": row.get("prev_trade_date"),
            "prev_close": row.get("prev_close"),
            "prev_volume": row.get("prev_volume"),
        },
    )
    prev_disp = watchlist_prev_display_from_row(pair)
    for k in (
        "display_prev_close",
        "display_prev_trade_date",
        "display_pair_basis",
        "ingest_exec_date",
    ):
        if prev_disp.get(k) is not None:
            row[k] = prev_disp[k]
    if pair.get("prev_close") is not None and row.get("prev_close") is None:
        row["prev_close"] = pair["prev_close"]
    if pair.get("prev_trade_date") and not row.get("prev_trade_date"):
        row["prev_trade_date"] = pair["prev_trade_date"]
    row["ingest_has_local_prev_bar"] = pair.get("ingest_has_local_prev_bar")


def watchlist_prev_display_for_symbol(
    sym: str, *, session: Session | None = None
) -> dict[str, Any]:
    """②/③/④ 共用：返回 display_prev_close / display_prev_trade_date 等（仅本地 bars）。"""
    pair = local_bars_pair_row(sym, session=session)
    return watchlist_prev_display_from_row(pair)


def watchlist_today_close_fields(pair_row: dict[str, Any]) -> dict[str, Any]:
    """
    ② 自选「当日收盘」：对齐东八区执行自然日。

    - 末根 K 线交易日 == 执行日 → 用末根收盘（真正当日收盘）；
    - 执行日晚于末根（如周一、或尚未拉到今日 bar）→ 不拿末根冒充当日，标为待入库。
    """
    exec_d = str(pair_row.get("ingest_exec_date") or shanghai_today_date().isoformat())[:10]
    last_td = str(pair_row.get("last_trade_date") or "")[:10] or None
    last_close = pair_row.get("last_close")
    out_close: float | None = None
    out_td: str | None = None
    basis = "no_last_bar"

    if last_td and last_close is not None:
        try:
            lc = float(last_close)
        except (TypeError, ValueError):
            lc = 0.0
        if math.isfinite(lc) and lc > 0:
            if exec_d == last_td:
                out_close = round(lc, 4)
                out_td = last_td
                basis = "last_bar_same_day"
            elif exec_d > last_td:
                out_close = None
                out_td = exec_d
                basis = "pending_today_bar"
            else:
                out_close = round(lc, 4)
                out_td = last_td
                basis = "last_bar"

    return {
        "display_today_close": out_close,
        "display_today_trade_date": out_td,
        "display_today_close_basis": basis,
    }


def watchlist_prev_display_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    从已填充 last/prev 并执行 resolve_ingest_row_display_pair 的 dict 取出③ 上行「收/昨」展示价。
    """
    exec_d = str(row.get("ingest_exec_date") or "")[:10]
    last_td = str(row.get("last_trade_date") or "")[:10] or None
    if exec_d and last_td and exec_d > last_td:
        pc = row.get("last_close")
        pt = row.get("last_trade_date")
    else:
        pc = row.get("display_prev_close")
        if pc is None:
            pc = row.get("prev_close")
        pt = row.get("display_prev_trade_date")
        if pt is None:
            pt = row.get("prev_trade_date")
    out_pc: float | None = None
    if pc is not None:
        try:
            v = float(pc)
            if math.isfinite(v) and v > 0:
                out_pc = round(v, 4)
        except (TypeError, ValueError):
            pass
    out_pt = str(pt)[:10] if pt is not None else None
    return {
        "display_prev_close": out_pc,
        "display_prev_trade_date": out_pt,
        "display_pair_basis": row.get("display_pair_basis"),
        "ingest_exec_date": exec_d or None,
    }


def utc_iso_to_shanghai_ymd(iso: str | None) -> str | None:
    """UTC ISO 时间转东八区自然日 YYYY-MM-DD。"""
    if iso is None or not str(iso).strip():
        return None
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except ValueError:
        return None


def normalize_ingest_date_range(
    range_start: str | None,
    range_end: str | None,
) -> tuple[str | None, str | None]:
    """校验并规范化东八区首次入库筛选闭区间 [start, end]；起止可只填一侧。"""
    start = str(range_start).strip()[:10] if range_start else None
    end = str(range_end).strip()[:10] if range_end else None
    for label, val in (("开始", start), ("结束", end)):
        if val:
            try:
                date.fromisoformat(val)
            except ValueError as e:
                raise ValueError(f"首次入库{label}日期无效：{val}") from e
    if start and end and start > end:
        start, end = end, start
    return start, end


def first_ingest_ymd_in_range(
    iso: str | None,
    *,
    range_start: str | None,
    range_end: str | None,
) -> bool:
    """首次入库 UTC ISO 是否落在东八区日期闭区间内（无起止则视为匹配）。"""
    start, end = normalize_ingest_date_range(range_start, range_end)
    if not start and not end:
        return True
    ymd = utc_iso_to_shanghai_ymd(iso)
    if not ymd:
        return False
    if start and ymd < start:
        return False
    if end and ymd > end:
        return False
    return True


def ensure_symbol_first_ingest(
    sym: str,
    *,
    session: Session | None = None,
    at: str | None = None,
) -> str | None:
    """
    记录标的首次 K 线入库时间；若已有记录则原样返回，不再更新。
    在 upsert_bars / 现价补当日收盘 等首次写入时调用。
    """
    sym_n = normalize_symbol(sym)
    now_iso = (at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()).strip()[
        :40
    ]

    def _apply(s: Session) -> str | None:
        row = s.get(SymbolIngestMetaRow, sym_n)
        if row is not None and (row.first_ingested_at or "").strip():
            return str(row.first_ingested_at).strip()[:40]
        if row is None:
            s.add(SymbolIngestMetaRow(symbol=sym_n, first_ingested_at=now_iso))
            return now_iso
        if not (row.first_ingested_at or "").strip():
            row.first_ingested_at = now_iso
            return now_iso
        return str(row.first_ingested_at).strip()[:40]

    if session is not None:
        return _apply(session)
    with session_scope() as s:
        return _apply(s)


def bars_ingest_timestamp_bounds(
    session: Session,
    sym: str,
) -> tuple[str | None, str | None]:
    """返回 (首次入库 UTC ISO, 最近入库 UTC ISO)；首次以 symbol_ingest_meta 为准。"""
    sym_n = normalize_symbol(sym)
    meta_row = session.get(SymbolIngestMetaRow, sym_n)
    first: str | None = None
    if meta_row is not None and (meta_row.first_ingested_at or "").strip():
        first = str(meta_row.first_ingested_at).strip()[:40]
    if not first:
        min_ing = session.execute(
            select(func.min(BarRow.ingested_at)).where(
                BarRow.symbol == sym_n,
                BarRow.ingested_at.is_not(None),
            )
        ).scalar_one_or_none()
        if min_ing:
            first = str(min_ing).strip()[:40]
            if meta_row is None:
                session.add(SymbolIngestMetaRow(symbol=sym_n, first_ingested_at=first))
            elif not (meta_row.first_ingested_at or "").strip():
                meta_row.first_ingested_at = first
    max_ing = session.execute(
        select(func.max(BarRow.ingested_at)).where(BarRow.symbol == sym_n)
    ).scalar_one_or_none()
    last = str(max_ing).strip()[:40] if max_ing else None
    return first, last


def watchlist_bar_fields_for_session(
    session: Session,
    symbols: list[str],
    *,
    data_source: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    自选表展示用：每标的在本地 bars 中的最新一行收盘价、最后交易日、
    首次/最近入库时间（min/max ingested_at）。
    昨日收盘字段与③ resolve_ingest_row_display_pair 上行「收/昨」一致。
    """
    route = data_source or get_settings().ingest_data_source
    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        recent = list(
            session.execute(
                select(BarRow)
                .where(BarRow.symbol == sym)
                .order_by(BarRow.trade_date.desc())
                .limit(2)
            )
            .scalars()
            .all()
        )
        first_ing, max_ing = bars_ingest_timestamp_bounds(session, sym)
        if not recent:
            exec_d = shanghai_today_date().isoformat()
            empty_pair = {
                "last_trade_date": None,
                "last_close": None,
                "ingest_exec_date": exec_d,
            }
            out[sym] = {
                "bars_first_ingested_at": None,
                "bars_last_ingested_at": None,
                "bars_last_trade_date": None,
                "last_close": None,
                "last_daily_close_label": None,
                "display_prev_close": None,
                "display_prev_trade_date": None,
                "display_pair_basis": "no_last_bar",
                "ingest_exec_date": exec_d,
                **watchlist_today_close_fields(empty_pair),
            }
            continue
        row_orm = recent[0]
        td = row_orm.trade_date
        close_v = round(float(row_orm.close), 4)
        label = f"{td} 交易日日线收盘（A 股常规 15:00 北京时间）"
        pair_row = local_bars_pair_row(sym, session=session)
        if not pair_row.get("last_trade_date"):
            pair_row["last_trade_date"] = td
            pair_row["last_close"] = close_v
        prev_disp = watchlist_prev_display_from_row(pair_row)
        today_disp = watchlist_today_close_fields(pair_row)
        out[sym] = {
            "bars_first_ingested_at": first_ing,
            "bars_last_ingested_at": max_ing,
            "bars_last_trade_date": td,
            "last_close": close_v,
            "last_daily_close_label": label,
            **prev_disp,
            **today_disp,
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


# --- 增量/区间入库 ---


def incremental_refresh(    symbol: str,
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
    strict_range: bool = False,
) -> dict:
    """
    按日期参数拉取并入库（自选批量更新入口）。

    - 同时传 start、end：按闭区间 [start, end] 拉取（若 start>end 会自动对调）；结束日不能晚于今天。
    - 仅 start：从 start 拉到「今天」。
    - 仅 end：等价于 incremental_refresh(..., as_of_date=end)。
    - 都不传：等价于 incremental_refresh 默认（增量到今天）。
    strict_range=True：闭区间拉取时不把结束日抬到今天，且仅 upsert 区间内 K 线（②「按日期拉取日线」）。
    data_source：None 时用 Settings.ingest_data_source。
    """
    sym = normalize_symbol(symbol)
    today = shanghai_today_date()
    if range_start is not None and range_end is not None:
        a, b = range_start, range_end
        if a > b:
            a, b = b, a
        if b > today:
            raise ValueError("结束日期不能晚于东八区今日")
        if a > today:
            raise ValueError("开始日期不能晚于东八区今日")
        # 结束日落在近几日内但早于今日（日期框未改）：自动拉到今日，避免缺当日 bar
        if not strict_range and b < today and (today - b).days <= 7:
            b = today
        start = a.strftime("%Y%m%d")
        end = b.strftime("%Y%m%d")
        return _fetch_and_upsert(
            sym,
            start,
            end,
            "explicit_range",
            data_source=data_source,
            range_only=strict_range,
        )
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


# --- 读库（信号 / 研究） ---


def load_bars_df(    symbol: str, min_bars: int = 80, *, data_source: str | None = None
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


# --- 证券简称与交易日 ---


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
