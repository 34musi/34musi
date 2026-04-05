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

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import get_settings
from app.db import FundamentalSnapshotRow, session_scope
from app.ingest import normalize_symbol
from app.schemas import FundamentalPanel, SignalReason

logger = logging.getLogger(__name__)

_spot_lock = threading.Lock()
_spot_mono_ts: float = 0.0
_spot_df: pd.DataFrame | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _get_spot_em_df() -> pd.DataFrame | None:
    global _spot_mono_ts, _spot_df
    ttl = max(10.0, float(get_settings().fundamentals_spot_cache_ttl_sec))
    now = time.monotonic()
    with _spot_lock:
        if _spot_df is not None and (now - _spot_mono_ts) < ttl:
            return _spot_df
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        logger.warning("stock_zh_a_spot_em failed: %s", e)
        with _spot_lock:
            if _spot_df is not None:
                return _spot_df
        return None
    with _spot_lock:
        _spot_df = df
        _spot_mono_ts = time.monotonic()
    return df


def _spot_row_for_symbol(sym: str, df: pd.DataFrame) -> pd.Series | None:
    if df is None or df.empty or "代码" not in df.columns:
        return None
    codes = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    hit = df.loc[codes == sym]
    if hit.empty:
        return None
    return hit.iloc[0]


def fetch_valuation_from_spot(sym: str) -> tuple[float | None, float | None]:
    df = _get_spot_em_df()
    row = _spot_row_for_symbol(sym, df) if df is not None else None
    if row is None:
        return None, None
    pe = pb = None
    if "市盈率-动态" in row.index:
        x = row["市盈率-动态"]
        pe = float(x) if pd.notna(x) else None
    if "市净率" in row.index:
        x = row["市净率"]
        pb = float(x) if pd.notna(x) else None
    if pe is not None and (not math.isfinite(pe)):
        pe = None
    if pb is not None and (not math.isfinite(pb)):
        pb = None
    return pe, pb


def fetch_financial_yoy(sym: str) -> tuple[float | None, float | None, str | None]:
    sec = em_seccode(sym)
    try:
        df = ak.stock_financial_analysis_indicator_em(symbol=sec)
    except Exception as e:
        logger.debug("financial_indicator_em %s: %s", sym, e)
        return None, None, None
    if df is None or df.empty:
        return None, None, None
    row = df.iloc[0]
    rev = row.get("TOTALOPERATEREVETZ")
    prof = row.get("PARENTNETPROFITTZ")
    rd = row.get("REPORT_DATE")
    rev_f = float(rev) if rev is not None and pd.notna(rev) else None
    prof_f = float(prof) if prof is not None and pd.notna(prof) else None
    rd_s = None
    if rd is not None and pd.notna(rd):
        try:
            rd_s = str(pd.Timestamp(rd).date())
        except (ValueError, TypeError, OSError):
            rd_s = str(rd)
    return rev_f, prof_f, rd_s


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


def build_fundamental_panel(sym: str) -> FundamentalPanel:
    """拉取远端并组装为面板（不写库）。"""
    sym = normalize_symbol(sym)
    pe, pb = fetch_valuation_from_spot(sym)
    rev_yoy, prof_yoy, rep_d = fetch_financial_yoy(sym)
    flow, flow_d = fetch_latest_main_flow(sym)
    return FundamentalPanel(
        pe_dynamic=pe,
        pb=pb,
        revenue_yoy_pct=rev_yoy,
        profit_yoy_pct=prof_yoy,
        financial_report_date=rep_d,
        main_net_inflow=flow,
        fund_flow_date=flow_d,
    )


def fundamental_score_delta(
    pe: float | None,
    pb: float | None,
    revenue_yoy_pct: float | None,
    profit_yoy_pct: float | None,
    main_net_inflow: float | None,
) -> tuple[int, list[SignalReason]]:
    """Demo：估值 / 成长 / 资金流启发式，总和限制在 [-15, 15]。"""
    raw = 0
    reasons: list[SignalReason] = []

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
