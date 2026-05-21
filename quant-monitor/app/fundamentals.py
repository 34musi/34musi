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


def fetch_valuation_from_spot(sym: str) -> tuple[float | None, float | None]:
    df = _get_spot_em_df(force_refresh=False)
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


def fetch_individual_fund_flow_latest_metrics(sym: str) -> dict[str, Any] | None:
    """
    东财个股日级资金流向表最后一行（旧→新取末行）：收盘价、交易日、大单/小单净流入净占比。

    「大单」为东财口径下「大单 + 超大单」净占比之和；非交易所逐笔拆单，盘中请以官方披露为准。
    """
    sym_n = normalize_symbol(sym)
    mkt = em_fund_flow_market(sym_n)
    try:
        df = ak.stock_individual_fund_flow(stock=sym_n, market=mkt)
    except Exception as e:
        logger.debug("fund_flow latest metrics %s: %s", sym_n, e)
        return None
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    dt = last.get("日期")
    trade_date: str | None = None
    if dt is not None and not pd.isna(dt):
        if hasattr(dt, "isoformat"):
            trade_date = dt.isoformat()[:10]
        else:
            trade_date = str(dt)[:10]
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
    }


def fetch_latest_main_flow(sym: str) -> tuple[float | None, str | None]:
    mkt = em_fund_flow_market(sym)
    try:
        df = ak.stock_individual_fund_flow(stock=normalize_symbol(sym), market=mkt)
    except Exception as e:
        logger.debug("fund_flow %s: %s", sym, e)
        return None, None
    if df is None or df.empty:
        return None, None
    last = df.iloc[-1]
    amt = last.get("主力净流入-净额")
    dt = last.get("日期")
    flow = float(amt) if amt is not None and pd.notna(amt) else None
    ds = str(dt) if dt is not None and pd.notna(dt) else None
    return flow, ds


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


def build_fundamental_panel(sym: str) -> FundamentalPanel:
    """拉取远端并组装为面板（不写库）。"""
    sym = normalize_symbol(sym)
    pe, pb = fetch_valuation_from_spot(sym)
    fin = fetch_financial_em_main(sym)
    flow, flow_d = fetch_latest_main_flow(sym)
    return FundamentalPanel(
        pe_dynamic=pe,
        pb=pb,
        revenue_yoy_pct=fin["revenue_yoy_pct"],
        profit_yoy_pct=fin["profit_yoy_pct"],
        financial_report_date=fin["financial_report_date"],
        main_net_inflow=flow,
        fund_flow_date=flow_d,
        roe_pct=fin["roe_pct"],
        roa_pct=fin["roa_pct"],
        net_margin_pct=fin["net_margin_pct"],
        gross_margin_pct=fin["gross_margin_pct"],
        debt_to_assets_pct=fin["debt_to_assets_pct"],
        current_ratio=fin["current_ratio"],
        quick_ratio=fin["quick_ratio"],
        ocf_per_share=fin["ocf_per_share"],
    )


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


def upsert_fundamental_snapshot(sym: str) -> dict[str, Any]:
    """拉取远端并 upsert；返回 JSON 友好摘要。"""
    sym = normalize_symbol(sym)
    try:
        panel = build_fundamental_panel(sym)
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
    return {
        "symbol": sym,
        "ok": True,
        "updated_at": now,
        "snapshot": panel.model_dump(),
    }
