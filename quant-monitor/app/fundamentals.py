"""
扩展因子（Demo）：东财 A 股列表估值、财务主要指标同比、个股日级主力净流入。

- 全市场 spot 表按 TTL 内存缓存，批量更新自选时共用，减轻限流。
- 入库表 fundamental_snapshots；信号侧读取后与技术面分数做有界合成（非投资建议）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.db import FundamentalSnapshotRow, session_scope
from app.ingest import normalize_symbol
from app.quant_stock_selector.market_utils import normalize_code as _norm_stock_code6
from app.schemas import FundamentalPanel, SignalReason

logger = logging.getLogger(__name__)

_spot_lock = threading.Lock()
_spot_mono_ts: float = 0.0
_spot_df: pd.DataFrame | None = None
_spot_fetched_at_iso: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _shanghai_today_ymd() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def em_seccode(sym: str) -> str:
    s = normalize_symbol(sym)
    if s.startswith("6"):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    return f"{s}.BJ"


def em_fund_flow_market(sym: str) -> str:
    s = normalize_symbol(sym)
    if s.startswith(("8", "4")):
        return "bj"
    if s.startswith("6"):
        return "sh"
    return "sz"


def _get_spot_em_df(*, force_refresh: bool = False) -> pd.DataFrame | None:
    """
    东财全 A 列表快照。默认 TTL 内存缓存；force_refresh=True 时跳过 TTL，尽量拉取新表（失败则仍回退到旧缓存）。
    """
    global _spot_mono_ts, _spot_df, _spot_fetched_at_iso
    ttl = max(10.0, float(get_settings().fundamentals_spot_cache_ttl_sec))
    now = time.monotonic()
    with _spot_lock:
        if _spot_df is not None and not force_refresh and (now - _spot_mono_ts) < ttl:
            return _spot_df
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        logger.warning("stock_zh_a_spot_em failed: %s", e)
        with _spot_lock:
            if _spot_df is not None:
                return _spot_df
        return None
    fetched_at = _now_iso()
    with _spot_lock:
        _spot_df = df
        _spot_mono_ts = time.monotonic()
        _spot_fetched_at_iso = fetched_at
    return df


def _spot_row_for_symbol(sym: str, df: pd.DataFrame) -> pd.Series | None:
    if df is None or df.empty or "代码" not in df.columns:
        return None
    codes = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    hit = df.loc[codes == sym]
    if hit.empty:
        return None
    return hit.iloc[0]


def _read_pe_pb_from_spot_row(row: pd.Series) -> tuple[float | None, float | None]:
    """东财 spot 行解析 PE/PB；列名随 AkShare 版本可能略有差异。"""
    pe = pb = None
    for col in ("市盈率-动态", "动态市盈率"):
        if col in row.index:
            x = row[col]
            pe = float(x) if pd.notna(x) else None
            break
    if "市净率" in row.index:
        x = row["市净率"]
        pb = float(x) if pd.notna(x) else None
    if pe is not None and not math.isfinite(pe):
        pe = None
    if pb is not None and not math.isfinite(pb):
        pb = None
    return pe, pb


def _fetch_valuation_from_value_em(sym: str) -> tuple[float | None, float | None]:
    """
    东财估值分析接口取最近一日 PE(TTM)、市净率；作 spot 全表失败或字段为空时的兜底。

    pe 对应面板 pe_dynamic（用 TTM 近似动态市盈）。
    """
    try:
        df = ak.stock_value_em(symbol=normalize_symbol(sym))
    except Exception as e:
        logger.debug("stock_value_em %s: %s", sym, e)
        return None, None
    if df is None or df.empty:
        return None, None
    last = df.iloc[-1]
    pe_raw = last.get("PE(TTM)")
    pb_raw = last.get("市净率")
    pe = float(pe_raw) if pe_raw is not None and pd.notna(pe_raw) else None
    pb = float(pb_raw) if pb_raw is not None and pd.notna(pb_raw) else None
    if pe is not None and not math.isfinite(pe):
        pe = None
    if pb is not None and not math.isfinite(pb):
        pb = None
    return pe, pb


def fetch_valuation_from_spot(
    sym: str, *, force_refresh: bool = False
) -> tuple[float | None, float | None]:
    df = _get_spot_em_df(force_refresh=force_refresh)
    row = _spot_row_for_symbol(sym, df) if df is not None else None
    pe, pb = (None, None) if row is None else _read_pe_pb_from_spot_row(row)
    if pe is None or pb is None:
        pe2, pb2 = _fetch_valuation_from_value_em(sym)
        if pe is None:
            pe = pe2
        if pb is None:
            pb = pb2
    return pe, pb


def _fin_float(row: pd.Series, key: str) -> float | None:
    if key not in row.index:
        return None
    v = row[key]
    if v is None or pd.isna(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fetch_financial_em_main(sym: str) -> dict[str, Any]:
    """
    东财主要财务指标表最近一行（按报告期排序后 iloc[0]）：同比、盈利能力、杠杆与流动性等。

    列名与 AkShare `stock_financial_analysis_indicator_em` 返回一致。
    """
    sec = em_seccode(sym)
    out: dict[str, Any] = {
        "revenue_yoy_pct": None,
        "profit_yoy_pct": None,
        "financial_report_date": None,
        "roe_pct": None,
        "roa_pct": None,
        "net_margin_pct": None,
        "gross_margin_pct": None,
        "debt_to_assets_pct": None,
        "current_ratio": None,
        "quick_ratio": None,
        "ocf_per_share": None,
    }
    try:
        df = ak.stock_financial_analysis_indicator_em(symbol=sec)
    except Exception as e:
        logger.debug("financial_indicator_em %s: %s", sym, e)
        return out
    if df is None or df.empty:
        return out
    if "REPORT_DATE" in df.columns:
        try:
            df = df.sort_values("REPORT_DATE", ascending=False, na_position="last")
        except Exception:
            pass
    row = df.iloc[0]
    out["revenue_yoy_pct"] = _fin_float(row, "TOTALOPERATEREVETZ")
    out["profit_yoy_pct"] = _fin_float(row, "PARENTNETPROFITTZ")
    rd = row.get("REPORT_DATE")
    if rd is not None and pd.notna(rd):
        try:
            out["financial_report_date"] = str(pd.Timestamp(rd).date())
        except (ValueError, TypeError, OSError):
            out["financial_report_date"] = str(rd)
    out["roe_pct"] = _fin_float(row, "ROEJQ")
    out["roa_pct"] = _fin_float(row, "ZZCJLL")
    out["net_margin_pct"] = _fin_float(row, "XSJLL")
    out["gross_margin_pct"] = _fin_float(row, "XSMLL")
    out["debt_to_assets_pct"] = _fin_float(row, "ZCFZL")
    out["current_ratio"] = _fin_float(row, "LD")
    out["quick_ratio"] = _fin_float(row, "SD")
    out["ocf_per_share"] = _fin_float(row, "MGJYXJJE")
    return out


def _spot_snapshot_price_from_row(row: pd.Series) -> float | None:
    """东财 spot 行取现价：列名随 AkShare/东财改版可能变化。"""
    for key in ("最新价", "现价", "收盘", "成交价", "price", "close", "最新"):
        v = _fin_float(row, key)
        if v is not None and math.isfinite(v) and v > 0:
            return v
    return None


def _spot_quote_calendar_date_str(row: pd.Series) -> str | None:
    """若列表含日期/时间列，规范为 YYYY-MM-DD，供与日线末根区分。"""
    for key in ("数据日期", "日期", "更新时间", "时间"):
        if key not in row.index:
            continue
        raw = row[key]
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            if hasattr(raw, "strftime"):
                return raw.strftime("%Y-%m-%d")[:10]
            s = str(raw).strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                return s[:10]
            if len(s) >= 8 and s[:8].isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        except (ValueError, TypeError, OSError):
            continue
    return None


def spot_liquidity_fields_for_codes(
    codes: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """
    东财沪深京 A 股列表快照（`stock_zh_a_spot_em`）：现价、昨收、成交量、成交额。
    与 `fetch_valuation_from_spot` 共用内存缓存；force_refresh=True 时跳过 TTL 尽量拉新表（选股结果合并最新价用）。
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for c in codes:
        if c is None:
            continue
        nc = _norm_stock_code6(c)
        if len(nc) != 6 or nc in seen:
            continue
        seen.add(nc)
        uniq.append(nc)
    out: dict[str, dict[str, Any]] = {c: {} for c in uniq}
    if not uniq:
        return out
    df = _get_spot_em_df(force_refresh=force_refresh)
    if df is None or df.empty:
        return out
    for sym in uniq:
        row = _spot_row_for_symbol(sym, df)
        if row is None:
            continue
        px = _spot_snapshot_price_from_row(row)
        chg = _fin_float(row, "涨跌幅")
        if chg is not None and math.isfinite(chg):
            chg = round(float(chg), 2)
        else:
            chg = None
        qd = _spot_quote_calendar_date_str(row) or _shanghai_today_ymd()
        with _spot_lock:
            fetched_at = _spot_fetched_at_iso
        out[sym] = {
            "spot_last_price": px,
            "spot_prev_close": _fin_float(row, "昨收"),
            "spot_change_pct": chg,
            "spot_volume": _fin_float(row, "成交量"),
            "spot_amount": _fin_float(row, "成交额"),
            "spot_quote_date": qd,
            "spot_fetched_at": fetched_at or _now_iso(),
        }
    return out


def _parse_fund_flow_row_date(row: pd.Series) -> str | None:
    dt = row.get("日期")
    if dt is None or (isinstance(dt, float) and pd.isna(dt)):
        return None
    try:
        if hasattr(dt, "strftime"):
            return dt.strftime("%Y-%m-%d")[:10]
        s = str(dt).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        if len(s) >= 8 and s[:8].isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except (ValueError, TypeError, OSError):
        return None
    return None


def _preferred_fund_flow_dates(sym: str) -> list[str]:
    """优先东八区今日，再按本地 bars 最近交易日（新→旧）。"""
    seen: set[str] = set()
    out: list[str] = []
    for d in (_shanghai_today_ymd(),):
        if d not in seen:
            seen.add(d)
            out.append(d)
    try:
        from app.ingest import list_bars_from_db

        rows = list_bars_from_db(sym, limit=10)
        for r in reversed(rows):
            td = str(r.get("trade_date") or "")[:10]
            if len(td) == 10 and td not in seen:
                seen.add(td)
                out.append(td)
    except Exception as e:
        logger.debug("preferred_fund_flow_dates %s: %s", sym, e)
    return out


def _load_individual_fund_flow_df(sym: str) -> pd.DataFrame | None:
    sym_n = normalize_symbol(sym)
    mkt = em_fund_flow_market(sym_n)
    try:
        df = ak.stock_individual_fund_flow(stock=sym_n, market=mkt)
    except Exception as e:
        logger.debug("fund_flow df %s: %s", sym_n, e)
        return None
    if df is None or df.empty:
        return None
    return df


def _fund_flow_pick_basis(want: str, *, exec_d: str, bar_d: str | None) -> str:
    """资金流行选用依据（供 ③ 下行 tooltip）。"""
    sh_today = _shanghai_today_ymd()
    want = want[:10]
    exec_d = exec_d[:10]
    bar_d = (bar_d or "")[:10] or None
    if want == sh_today and want == exec_d:
        return "today"
    if want == exec_d:
        return "exec_today"
    if bar_d and want == bar_d:
        return "exec_fallback_bar"
    return "last_close"


def _pick_fund_flow_row_for_dates(
    df: pd.DataFrame,
    prefer_dates: list[str],
    *,
    exec_d: str,
    bar_d: str | None = None,
) -> tuple[pd.Series | None, str | None, str]:
    """
    按给定日期顺序在资金表中取第一行命中；均不命中则表末行。
    prefer_dates 通常：执行日 → 入库末根日 → 其它候选。
    """
    exec_d = exec_d[:10]
    row_dates: list[str | None] = [_parse_fund_flow_row_date(df.iloc[i]) for i in range(len(df))]
    seen: set[str] = set()
    for raw in prefer_dates:
        want = str(raw or "")[:10]
        if not want or want in seen:
            continue
        seen.add(want)
        for i, d in enumerate(row_dates):
            if d != want:
                continue
            return df.iloc[i], d, _fund_flow_pick_basis(want, exec_d=exec_d, bar_d=bar_d)
    i = len(df) - 1
    d_last = row_dates[i]
    return df.iloc[i], d_last, "fallback_last"


def _pick_fund_flow_row(
    df: pd.DataFrame, sym: str
) -> tuple[pd.Series, str | None, str]:
    """
    优先取「当日」资金流行；若无则按本地 K 线最近交易日；再否则表末行。
    返回 (行, 选用日期, basis)。
    """
    exec_d = _shanghai_today_ymd()
    prefer = _preferred_fund_flow_dates(sym)
    row, d, basis = _pick_fund_flow_row_for_dates(df, [exec_d, *prefer], exec_d=exec_d)
    if row is None:
        i = len(df) - 1
        d_last = _parse_fund_flow_row_date(df.iloc[i])
        return df.iloc[i], d_last, "fallback_last"
    return row, d, basis


def fetch_individual_fund_flow_latest_metrics(sym: str) -> dict[str, Any] | None:
    """
    东财个股日级资金流向：优先当日行，否则上一完整交易日（与本地 bars 末根对齐）。

    「大单」为东财口径下「大单 + 超大单」净占比之和；非交易所逐笔拆单，盘中请以官方披露为准。
    """
    df = _load_individual_fund_flow_df(sym)
    if df is None or df.empty:
        return None
    last, trade_date, basis = _pick_fund_flow_row(df, sym)
    d_big = _fin_float(last, "大单净流入-净占比")
    d_sup = _fin_float(last, "超大单净流入-净占比")
    large_combined: float | None = None
    if d_big is not None or d_sup is not None:
        large_combined = (d_big or 0.0) + (d_sup or 0.0)
        if not math.isfinite(large_combined):
            large_combined = None
    return {
        "em_trade_date": trade_date,
        "em_close": _fin_float(last, "收盘价"),
        "em_small_net_pct": _fin_float(last, "小单净流入-净占比"),
        "em_large_net_pct": large_combined,
        "fund_flow_pick_basis": basis,
    }


def _main_flow_from_row(row: pd.Series) -> float | None:
    amt = row.get("主力净流入-净额")
    if amt is None or (isinstance(amt, float) and pd.isna(amt)):
        return None
    try:
        flow = float(amt)
    except (TypeError, ValueError):
        return None
    return flow if math.isfinite(flow) else None


def _pick_fund_flow_row_for_date(df: pd.DataFrame, want_date: str) -> pd.Series | None:
    for i in range(len(df)):
        if _parse_fund_flow_row_date(df.iloc[i]) == want_date:
            return df.iloc[i]
    return None


def _resolve_prev_trade_date(sym: str, cur_date: str | None) -> str | None:
    """相对「当日/当前」因子行，取上一交易日（本地 bars 次新一根）。"""
    prefer = _preferred_fund_flow_dates(sym)
    if not prefer:
        return None
    if not cur_date:
        return prefer[1] if len(prefer) > 1 else None
    try:
        idx = prefer.index(cur_date)
    except ValueError:
        return prefer[1] if len(prefer) > 1 else None
    if idx + 1 < len(prefer):
        return prefer[idx + 1]
    return None


def fetch_latest_main_flow(sym: str) -> tuple[float | None, str | None, str | None]:
    """主力净流入净额与对应交易日；第三项为取值 basis（today / last_close / fallback_last）。"""
    df = _load_individual_fund_flow_df(sym)
    if df is None or df.empty:
        return None, None, None
    last, trade_date, basis = _pick_fund_flow_row(df, sym)
    return _main_flow_from_row(last), trade_date, basis


def _assemble_fundamental_panel(
    fin: dict[str, Any],
    *,
    pe_dynamic: float | None = None,
    pb: float | None = None,
    main_net_inflow: float | None = None,
    fund_flow_date: str | None = None,
    fund_flow_pick_basis: str | None = None,
) -> FundamentalPanel:
    return FundamentalPanel(
        pe_dynamic=pe_dynamic,
        pb=pb,
        revenue_yoy_pct=fin["revenue_yoy_pct"],
        profit_yoy_pct=fin["profit_yoy_pct"],
        financial_report_date=fin["financial_report_date"],
        main_net_inflow=main_net_inflow,
        fund_flow_date=fund_flow_date,
        fund_flow_pick_basis=fund_flow_pick_basis,
        roe_pct=fin["roe_pct"],
        roa_pct=fin["roa_pct"],
        net_margin_pct=fin["net_margin_pct"],
        gross_margin_pct=fin["gross_margin_pct"],
        debt_to_assets_pct=fin["debt_to_assets_pct"],
        current_ratio=fin["current_ratio"],
        quick_ratio=fin["quick_ratio"],
        ocf_per_share=fin["ocf_per_share"],
    )


def _ingest_context_for_fundamentals(sym: str, ingest_row: dict[str, Any] | None) -> dict[str, str | None]:
    """从 ③ 入库行或本地 bars 推导执行日 / 末根日，供因子双行对齐。"""
    row: dict[str, Any] = dict(ingest_row) if ingest_row else {}
    if not row.get("last_trade_date"):
        try:
            from app.ingest import list_bars_from_db, resolve_ingest_row_display_pair

            bars = list_bars_from_db(sym, limit=3)
            if bars:
                lb = bars[-1]
                row["last_trade_date"] = lb["trade_date"]
                row["last_close"] = lb["close"]
                if len(bars) >= 2:
                    pb = bars[-2]
                    row["prev_trade_date"] = pb["trade_date"]
                    row["prev_close"] = pb["close"]
            resolve_ingest_row_display_pair(sym, row)
        except Exception as e:
            logger.debug("ingest context for fundamentals %s: %s", sym, e)
    else:
        try:
            from app.ingest import resolve_ingest_row_display_pair

            resolve_ingest_row_display_pair(sym, row)
        except Exception as e:
            logger.debug("resolve display pair for fundamentals %s: %s", sym, e)
    exec_d = str(row.get("ingest_exec_date") or _shanghai_today_ymd())[:10]
    bar_d = str(row.get("display_bar_trade_date") or row.get("last_trade_date") or "")[:10] or None
    return {"exec_date": exec_d, "bar_trade_date": bar_d}


def build_fundamental_panels_dual(
    sym: str,
    *,
    force_spot_refresh: bool = False,
    ingest_row: dict[str, Any] | None = None,
) -> tuple[FundamentalPanel, FundamentalPanel | None]:
    """
    返回 (今/执行日因子, 昨/上一档因子)。

    - **今**：优先东八区执行日资金流行；无则入库末根日；再否则表末行。PE/PB 拉取时刷新东财列表。
    - **昨**：相对「今」命中日期在资金表中的上一日（与行情昨收参照可不同日）。
    """
    sym_n = normalize_symbol(sym)
    ctx = _ingest_context_for_fundamentals(sym_n, ingest_row)
    exec_d = ctx["exec_date"] or _shanghai_today_ymd()
    bar_d = ctx["bar_trade_date"]
    fin = fetch_financial_em_main(sym_n)
    pe, pb = fetch_valuation_from_spot(sym_n, force_refresh=force_spot_refresh)
    df = _load_individual_fund_flow_df(sym_n)

    cur_prefs: list[str] = [exec_d]
    if bar_d and bar_d not in cur_prefs:
        cur_prefs.append(bar_d)
    for d in _preferred_fund_flow_dates(sym_n):
        if d not in cur_prefs:
            cur_prefs.append(d)

    if df is not None and not df.empty:
        cur_row, cur_d, cur_basis = _pick_fund_flow_row_for_dates(
            df, cur_prefs, exec_d=exec_d, bar_d=bar_d
        )
        if cur_row is None:
            i = len(df) - 1
            cur_row = df.iloc[i]
            cur_d = _parse_fund_flow_row_date(cur_row)
            cur_basis = "fallback_last"
        cur_flow = _main_flow_from_row(cur_row)
    else:
        cur_row, cur_d, cur_basis, cur_flow = None, None, "none", None

    panel_cur = _assemble_fundamental_panel(
        fin,
        pe_dynamic=pe,
        pb=pb,
        main_net_inflow=cur_flow,
        fund_flow_date=cur_d,
        fund_flow_pick_basis=cur_basis,
    )

    prev_d = _resolve_prev_trade_date(sym_n, cur_d)
    panel_prev: FundamentalPanel | None = None
    if prev_d and df is not None and not df.empty:
        prev_row = _pick_fund_flow_row_for_date(df, prev_d)
        if prev_row is not None:
            panel_prev = _assemble_fundamental_panel(
                fin,
                main_net_inflow=_main_flow_from_row(prev_row),
                fund_flow_date=prev_d,
                fund_flow_pick_basis="prev_day",
            )
        else:
            panel_prev = _assemble_fundamental_panel(
                fin,
                fund_flow_date=prev_d,
                fund_flow_pick_basis="prev_day_missing_row",
            )
    elif cur_d and df is not None and not df.empty:
        row_dates = [_parse_fund_flow_row_date(df.iloc[i]) for i in range(len(df))]
        for i, d in enumerate(row_dates):
            if d == cur_d and i > 0:
                prev_row = df.iloc[i - 1]
                prev_d2 = row_dates[i - 1]
                panel_prev = _assemble_fundamental_panel(
                    fin,
                    main_net_inflow=_main_flow_from_row(prev_row),
                    fund_flow_date=prev_d2,
                    fund_flow_pick_basis="prev_day",
                )
                break

    return panel_cur, panel_prev


def _jsonable_fund_flow_cell(v: Any) -> Any:
    """将 AkShare 资金流表单元格转为 JSON 友好类型。"""
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        try:
            return str(v.date())
        except (ValueError, OSError):
            return str(v)
    if isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if not math.isfinite(f) else f
    if pd.isna(v):
        return None
    return str(v)


def fetch_individual_fund_flow_recent_rows(sym: str, *, limit_rows: int = 10) -> list[dict[str, Any]]:
    """
    东财个股日级资金流向表最近若干行（旧→新），列名与 AkShare `stock_individual_fund_flow` 一致。

    limit_rows：最多返回多少个交易日；用于网页预览「当日/最近交易日」资金概况。
    """
    sym_n = normalize_symbol(sym)
    mkt = em_fund_flow_market(sym_n)
    n = max(1, min(int(limit_rows), 500))
    try:
        df = ak.stock_individual_fund_flow(stock=sym_n, market=mkt)
    except Exception as e:
        logger.debug("fund_flow recent %s: %s", sym_n, e)
        return []
    if df is None or df.empty:
        return []
    tail = df.tail(min(n, len(df)))
    out: list[dict[str, Any]] = []
    for _, row in tail.iterrows():
        out.append({str(k): _jsonable_fund_flow_cell(row[k]) for k in row.index})
    return out


def build_fundamental_panel(sym: str, *, force_spot_refresh: bool = False) -> FundamentalPanel:
    """拉取远端并组装为「当前」因子面板（不写库）；与 build_fundamental_panels_dual 的 cur 一致。"""
    panel_cur, _ = build_fundamental_panels_dual(sym, force_spot_refresh=force_spot_refresh)
    return panel_cur


def fundamental_score_delta(panel: FundamentalPanel) -> tuple[int, list[SignalReason]]:
    """Demo：估值 / 成长 / 资金流 / 盈利与杠杆启发式，总和限制在 [-15, 15]。"""
    raw = 0
    reasons: list[SignalReason] = []
    pe = panel.pe_dynamic
    pb = panel.pb
    revenue_yoy_pct = panel.revenue_yoy_pct
    profit_yoy_pct = panel.profit_yoy_pct
    main_net_inflow = panel.main_net_inflow

    if pe is not None and math.isfinite(pe):
        if pe < 0:
            raw -= 4
            reasons.append(SignalReason(code="fund_pe_loss", text="市盈率为负（亏损区域），Demo 规则略降权"))
        elif pe <= 20:
            raw += 3
            reasons.append(SignalReason(code="fund_pe_moderate", text="市盈率(动)处于偏低区间（Demo）"))
        elif pe > 45:
            raw -= 4
            reasons.append(SignalReason(code="fund_pe_high", text="市盈率(动)偏高（Demo），注意估值风险"))

    if pb is not None and math.isfinite(pb) and pb > 0:
        if pb <= 3:
            raw += 2
            reasons.append(SignalReason(code="fund_pb_low", text="市净率相对不高（Demo）"))
        elif pb >= 10:
            raw -= 2
            reasons.append(SignalReason(code="fund_pb_high", text="市净率偏高（Demo）"))

    if profit_yoy_pct is not None and math.isfinite(profit_yoy_pct):
        if profit_yoy_pct >= 15:
            raw += 6
            reasons.append(SignalReason(code="fund_profit_yoy_strong", text="归属净利润同比增势较强（财报%，东财）"))
        elif profit_yoy_pct >= 5:
            raw += 3
            reasons.append(SignalReason(code="fund_profit_yoy_ok", text="归属净利润同比正增长（财报%，东财）"))
        elif profit_yoy_pct <= -15:
            raw -= 5
            reasons.append(SignalReason(code="fund_profit_yoy_weak", text="归属净利润同比明显下滑（财报%，东财）"))

    if revenue_yoy_pct is not None and math.isfinite(revenue_yoy_pct):
        if revenue_yoy_pct >= 10:
            raw += 2
            reasons.append(SignalReason(code="fund_rev_yoy_ok", text="营业收入同比保持不错增速（财报%，东财）"))
        elif revenue_yoy_pct <= -5:
            raw -= 2
            reasons.append(SignalReason(code="fund_rev_yoy_weak", text="营业收入同比下滑（财报%，东财）"))

    if main_net_inflow is not None and math.isfinite(main_net_inflow):
        if main_net_inflow > 0:
            raw += 4
            reasons.append(SignalReason(code="fund_flow_main_in", text="最近交易日主力净流入为正（东财日级）"))
        elif main_net_inflow < 0:
            raw -= 3
            reasons.append(SignalReason(code="fund_flow_main_out", text="最近交易日主力净流入为负（东财日级）"))

    roe = panel.roe_pct
    if roe is not None and math.isfinite(roe):
        if roe >= 15:
            raw += 2
            reasons.append(SignalReason(code="fund_roe_strong", text="ROE（加权）处于较高区间（Demo，财报%）"))
        elif roe < 5:
            raw -= 1
            reasons.append(SignalReason(code="fund_roe_weak", text="ROE（加权）偏低（Demo，财报%）"))

    debt = panel.debt_to_assets_pct
    if debt is not None and math.isfinite(debt):
        if debt > 70:
            raw -= 2
            reasons.append(SignalReason(code="fund_debt_high", text="资产负债率偏高（Demo，财报%）"))
        elif debt < 35:
            raw += 1
            reasons.append(SignalReason(code="fund_debt_moderate", text="资产负债率相对温和（Demo）"))

    cr = panel.current_ratio
    if cr is not None and math.isfinite(cr) and cr < 1.0:
        raw -= 2
        reasons.append(SignalReason(code="fund_liquidity_tight", text="流动比率低于 1（Demo，短期偿债压力提示）"))

    nm = panel.net_margin_pct
    if nm is not None and math.isfinite(nm) and nm < 0:
        raw -= 1
        reasons.append(SignalReason(code="fund_net_margin_neg", text="销售净利率为负（Demo，财报%）"))

    gm = panel.gross_margin_pct
    if gm is not None and math.isfinite(gm) and gm >= 30:
        raw += 1
        reasons.append(SignalReason(code="fund_gross_margin_ok", text="销售毛利率不低于 30%（Demo）"))

    ocf = panel.ocf_per_share
    if ocf is not None and math.isfinite(ocf) and ocf < 0 and profit_yoy_pct is not None and profit_yoy_pct > 0:
        raw -= 1
        reasons.append(
            SignalReason(code="fund_ocf_warn", text="每股经营现金流为负但净利同比为正（Demo，盈利质量提示）")
        )

    delta = int(max(-15, min(15, raw)))
    if delta != raw:
        reasons.append(SignalReason(code="fund_delta_clamped", text="基本面分项合成已限制在 ±15 分（Demo）"))
    return delta, reasons


def load_fundamental_panel_from_db(symbol: str) -> FundamentalPanel | None:
    sym = normalize_symbol(symbol)
    with session_scope() as s:
        row = s.execute(select(FundamentalSnapshotRow).where(FundamentalSnapshotRow.symbol == sym)).scalar_one_or_none()
        if row is None:
            return None
        return FundamentalPanel(
            pe_dynamic=row.pe_dynamic,
            pb=row.pb,
            revenue_yoy_pct=row.revenue_yoy_pct,
            profit_yoy_pct=row.profit_yoy_pct,
            financial_report_date=row.financial_report_date,
            main_net_inflow=row.main_net_inflow,
            fund_flow_date=row.fund_flow_date,
            roe_pct=row.roe_pct,
            roa_pct=row.roa_pct,
            net_margin_pct=row.net_margin_pct,
            gross_margin_pct=row.gross_margin_pct,
            debt_to_assets_pct=row.debt_to_assets_pct,
            current_ratio=row.current_ratio,
            quick_ratio=row.quick_ratio,
            ocf_per_share=row.ocf_per_share,
            cached_at=row.updated_at,
        )


def upsert_fundamental_snapshot(
    sym: str, *, ingest_row: dict[str, Any] | None = None
) -> dict[str, Any]:
    """拉取远端并 upsert；返回 JSON 友好摘要（含 snapshot / snapshot_prev 双行）。"""
    sym = normalize_symbol(sym)
    try:
        panel, panel_prev = build_fundamental_panels_dual(
            sym, force_spot_refresh=True, ingest_row=ingest_row
        )
    except Exception as e:
        logger.warning("build_fundamental_panel %s: %s", sym, e)
        return {"symbol": sym, "ok": False, "error": str(e)}
    now = _now_iso()
    stmt = sqlite_insert(FundamentalSnapshotRow.__table__).values(
        symbol=sym,
        updated_at=now,
        pe_dynamic=panel.pe_dynamic,
        pb=panel.pb,
        revenue_yoy_pct=panel.revenue_yoy_pct,
        profit_yoy_pct=panel.profit_yoy_pct,
        financial_report_date=panel.financial_report_date,
        main_net_inflow=panel.main_net_inflow,
        fund_flow_date=panel.fund_flow_date,
        roe_pct=panel.roe_pct,
        roa_pct=panel.roa_pct,
        net_margin_pct=panel.net_margin_pct,
        gross_margin_pct=panel.gross_margin_pct,
        debt_to_assets_pct=panel.debt_to_assets_pct,
        current_ratio=panel.current_ratio,
        quick_ratio=panel.quick_ratio,
        ocf_per_share=panel.ocf_per_share,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={
            "updated_at": stmt.excluded.updated_at,
            "pe_dynamic": stmt.excluded.pe_dynamic,
            "pb": stmt.excluded.pb,
            "revenue_yoy_pct": stmt.excluded.revenue_yoy_pct,
            "profit_yoy_pct": stmt.excluded.profit_yoy_pct,
            "financial_report_date": stmt.excluded.financial_report_date,
            "main_net_inflow": stmt.excluded.main_net_inflow,
            "fund_flow_date": stmt.excluded.fund_flow_date,
            "roe_pct": stmt.excluded.roe_pct,
            "roa_pct": stmt.excluded.roa_pct,
            "net_margin_pct": stmt.excluded.net_margin_pct,
            "gross_margin_pct": stmt.excluded.gross_margin_pct,
            "debt_to_assets_pct": stmt.excluded.debt_to_assets_pct,
            "current_ratio": stmt.excluded.current_ratio,
            "quick_ratio": stmt.excluded.quick_ratio,
            "ocf_per_share": stmt.excluded.ocf_per_share,
        },
    )
    with session_scope() as s:
        s.execute(stmt)
    ctx = _ingest_context_for_fundamentals(sym, ingest_row)
    out: dict[str, Any] = {
        "symbol": sym,
        "ok": True,
        "updated_at": now,
        "ingest_exec_date": ctx.get("exec_date"),
        "display_bar_trade_date": ctx.get("bar_trade_date"),
        "snapshot": panel.model_dump(),
    }
    if panel_prev is not None:
        out["snapshot_prev"] = panel_prev.model_dump()
    return out
