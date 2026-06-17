"""
个股速览（⑤）：行情、新闻、概念、业务与营收能力聚合。

联网拉取东财 / AkShare 公开数据，供控制台 **⑤ 个股咨询** 展示；**非投资建议**。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta
from typing import Any

import akshare as ak
import pandas as pd
import requests

from app.fundamentals import (
    em_seccode,
    fetch_financial_em_main,
    fetch_valuation_from_spot,
)
from app.ingest import (
    _temporary_clear_proxy_env,
    fetch_stock_name,
    list_bars_from_db,
    live_quote_fields_for_codes_enhanced,
    normalize_symbol,
)
from app.config import get_settings
from app.quant_stock_selector.hot_pick import is_st_stock_name

logger = logging.getLogger(__name__)

_F10_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def f10_market_code(sym: str) -> str:
    """6 位代码 → 东财 F10 前缀，如 600519 → SH600519。"""
    s = normalize_symbol(sym)
    if s.startswith("6"):
        return f"SH{s}"
    if s.startswith(("0", "3")):
        return f"SZ{s}"
    return f"BJ{s}"


def _em_web_get_json(path: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/{path}"
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            r = requests.get(url, params=params or {}, headers=_F10_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("em web %s %s: %s", path, params, e)
        return None


def _parse_individual_info(sym: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = ak.stock_individual_info_em(symbol=sym, timeout=12)
    except Exception as e:
        logger.debug("individual_info %s: %s", sym, e)
        return out
    if df is None or df.empty:
        return out
    cols = [str(c) for c in df.columns]
    if "item" in cols and "value" in cols:
        for _, row in df.iterrows():
            k = str(row.get("item") or "").strip()
            v = str(row.get("value") or "").strip()
            if k and v and v.lower() not in ("nan", "none", "-"):
                out[k] = v
    elif len(cols) >= 2:
        for _, row in df.iterrows():
            k = str(row.iloc[0]).strip()
            v = str(row.iloc[1]).strip()
            if k and v and v.lower() not in ("nan", "none", "-"):
                out[k] = v
    return out


def _fetch_company_profile(f10_code: str) -> dict[str, Any]:
    data = _em_web_get_json("CompanySurvey/PageAjax", params={"code": f10_code})
    if not data:
        return {}
    jbzl = data.get("jbzl")
    if isinstance(jbzl, list):
        jbzl = jbzl[0] if jbzl and isinstance(jbzl[0], dict) else {}
    if isinstance(jbzl, dict):
        return {
            "company_name": _clean_text(jbzl.get("ORG_NAME")),
            "industry": _clean_text(jbzl.get("EM2016") or jbzl.get("INDUSTRY")),
            "main_business": _clean_text(jbzl.get("BUSINESS_SCOPE") or jbzl.get("MAIN_BUSINESS")),
            "profile": _clean_text(jbzl.get("ORG_PROFILE")),
            "listing_date": _clean_text(jbzl.get("LISTING_DATE")),
        }
    return {}


def _fetch_concepts(f10_code: str) -> list[dict[str, str]]:
    data = _em_web_get_json("CoreConception/PageAjax", params={"code": f10_code})
    if not data:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in ("hxtc", "gntc", "ssbk"):
        block = data.get(key)
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            name = _clean_text(
                item.get("BOARD_NAME")
                or item.get("BOARDNAME")
                or item.get("PLATE_NAME")
                or item.get("NAME")
            )
            if not name or name in seen:
                continue
            seen.add(name)
            reason = _clean_text(
                item.get("SELECTED_BOARD_REASON")
                or item.get("BOARD_REASON")
                or item.get("REASON")
                or item.get("MAINPOINT")
            )
            out.append({"name": name, "reason": reason})
    return out[:30]


def _fetch_main_business_segments(f10_code: str) -> list[dict[str, Any]]:
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = ak.stock_zygc_em(symbol=f10_code)
    except Exception as e:
        logger.debug("zygc %s: %s", f10_code, e)
        return []
    if df is None or df.empty:
        return []
    work = df.copy()
    if "分类类型" in work.columns:
        prod = work[work["分类类型"].astype(str).str.contains("产品", na=False)]
        if not prod.empty:
            work = prod
    if "报告日期" in work.columns:
        try:
            work = work.sort_values("报告日期", ascending=False, na_position="last")
        except Exception:
            pass
    latest_date = None
    if "报告日期" in work.columns and not work.empty:
        latest_date = work.iloc[0].get("报告日期")
        work = work[work["报告日期"] == latest_date]
    rows: list[dict[str, Any]] = []
    for _, row in work.head(8).iterrows():
        name = _clean_text(row.get("主营构成"))
        if not name:
            continue
        rows.append(
            {
                "segment": name,
                "revenue_ratio_pct": _num_or_none(row.get("收入比例")),
                "gross_margin_pct": _num_or_none(row.get("毛利率")),
            }
        )
    if latest_date is not None:
        for r in rows:
            r["report_date"] = str(latest_date)
    return rows


def _fetch_news(sym: str, *, limit: int = 8) -> list[dict[str, str]]:
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = ak.stock_news_em(symbol=sym)
    except Exception as e:
        logger.debug("news %s: %s", sym, e)
        return []
    if df is None or df.empty:
        return []
    rows: list[dict[str, str]] = []
    for _, row in df.head(max(1, min(limit, 20))).iterrows():
        title = _clean_text(row.get("新闻标题"))
        if not title:
            continue
        content = _clean_text(row.get("新闻内容"))
        if len(content) > 280:
            content = content[:280] + "…"
        rows.append(
            {
                "title": title,
                "published_at": _clean_text(row.get("发布时间")),
                "source": _clean_text(row.get("文章来源")),
                "summary": content,
                "url": _clean_text(row.get("新闻链接")),
            }
        )
    return rows


def _quote_turnover_rate(sym: str, live: dict[str, Any]) -> float | None:
    tr = _num_or_none(live.get("spot_turnover_rate") or live.get("turnover_rate"))
    if tr is not None:
        return tr
    try:
        from app.fundamentals import spot_liquidity_fields_for_codes

        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            liq = spot_liquidity_fields_for_codes([sym], force_refresh=True).get(sym) or {}
        tr = _num_or_none(liq.get("spot_turnover_rate"))
        if tr is not None:
            return tr
    except Exception as e:
        logger.debug("quote turnover spot list %s: %s", sym, e)
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = ak.stock_bid_ask_em(symbol=sym)
        from app.ingest import _parse_bid_ask_em_df

        parsed = _parse_bid_ask_em_df(df)
        return _num_or_none(parsed.get("turnover"))
    except Exception as e:
        logger.debug("quote turnover bid_ask %s: %s", sym, e)
        return None


def _fetch_quote(sym: str) -> dict[str, Any]:
    live = live_quote_fields_for_codes_enhanced(
        [sym], force_spot_refresh=True
    ).get(sym, {})
    bars = list_bars_from_db(sym, limit=5)
    last_bar = bars[-1] if bars else None
    prev_bar = bars[-2] if len(bars) >= 2 else None
    price = live.get("live_last_price") or live.get("live_price") or live.get("last_close")
    if price is None and last_bar:
        price = last_bar.get("close")
    prev_close = live.get("prev_close")
    if prev_close is None and prev_bar:
        prev_close = prev_bar.get("close")
    change_pct = live.get("live_change_pct") or live.get("change_pct")
    if change_pct is None and price is not None and prev_close not in (None, 0):
        try:
            change_pct = (float(price) - float(prev_close)) / float(prev_close) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            change_pct = None
    return {
        "price": _num_or_none(price),
        "change_pct": _num_or_none(change_pct),
        "prev_close": _num_or_none(prev_close),
        "volume": _num_or_none(
            live.get("live_volume") or live.get("volume") or (last_bar or {}).get("volume")
        ),
        "amount": _num_or_none(
            live.get("spot_amount") or live.get("amount") or (last_bar or {}).get("amount")
        ),
        "turnover_rate_pct": _quote_turnover_rate(sym, live),
        "last_trade_date": _clean_text(
            live.get("live_quote_date") or live.get("trade_date") or (last_bar or {}).get("trade_date")
        ),
        "quote_source": _clean_text(live.get("live_price_source") or live.get("quote_source")),
        "quote_fetched_at": _clean_text(live.get("live_fetched_at") or live.get("spot_fetched_at")),
    }


def _build_revenue_summary(sym: str) -> dict[str, Any]:
    fin = fetch_financial_em_main(sym)
    pe, pb = fetch_valuation_from_spot(sym)
    return {
        "report_date": fin.get("financial_report_date"),
        "revenue_yoy_pct": fin.get("revenue_yoy_pct"),
        "profit_yoy_pct": fin.get("profit_yoy_pct"),
        "roe_pct": fin.get("roe_pct"),
        "roa_pct": fin.get("roa_pct"),
        "gross_margin_pct": fin.get("gross_margin_pct"),
        "net_margin_pct": fin.get("net_margin_pct"),
        "debt_to_assets_pct": fin.get("debt_to_assets_pct"),
        "ocf_per_share": fin.get("ocf_per_share"),
        "current_ratio": fin.get("current_ratio"),
        "quick_ratio": fin.get("quick_ratio"),
        "pe_dynamic": pe,
        "pb": pb,
    }


def _clean_text(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = re.sub(r"<[^>]+>", "", str(v))
    s = s.replace("\u3000", " ").strip()
    return s


def _num_or_none(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if pd.notna(f) else None


_RECENT_HOLDER_DATES = (
    "20241231",
    "20240930",
    "20240630",
    "20240331",
    "20231231",
    "20230930",
)


def _fetch_financial_indicator_rows(sym: str, *, limit: int = 2) -> list[pd.Series]:
    sec = em_seccode(sym)
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = ak.stock_financial_analysis_indicator_em(symbol=sec)
    except Exception as e:
        logger.debug("financial_indicator rows %s: %s", sym, e)
        return []
    if df is None or df.empty:
        return []
    if "REPORT_DATE" in df.columns:
        try:
            df = df.sort_values("REPORT_DATE", ascending=False, na_position="last")
        except Exception:
            pass
    n = max(1, min(limit, len(df)))
    return [df.iloc[i] for i in range(n)]


def _fetch_financial_quality(
    sym: str,
    fin: dict[str, Any],
    *,
    row: pd.Series | None = None,
    prev_row: pd.Series | None = None,
) -> dict[str, Any]:
    if row is None:
        rows = _fetch_financial_indicator_rows(sym, limit=2)
        row = rows[0] if rows else None
        prev_row = rows[1] if len(rows) > 1 else None
    eps = _fin_float(row, "EPSJB") if row is not None else None
    ocf_ps = fin.get("ocf_per_share")
    revenue_yoy = fin.get("revenue_yoy_pct")
    profit_yoy = fin.get("profit_yoy_pct")
    gross_now = fin.get("gross_margin_pct")
    gross_prev = _fin_float(prev_row, "XSMLL") if prev_row is not None else None
    gross_margin_chg_pp: float | None = None
    if gross_now is not None and gross_prev is not None:
        gross_margin_chg_pp = round(gross_now - gross_prev, 2)

    cash_match = None
    if eps is not None and ocf_ps is not None:
        if eps <= 0 and ocf_ps > 0:
            cash_match = "净利润偏弱但经营现金流为正，需核对利润质量与一次性因素。"
        elif eps > 0 and ocf_ps < 0:
            cash_match = "盈利为正但每股经营现金流为负，利润与现金回款匹配度偏弱。"
        elif eps > 0 and ocf_ps >= eps * 0.5:
            cash_match = "经营现金流与每股收益大致匹配，利润现金含量尚可（Demo 口径）。"
        else:
            cash_match = "经营现金流低于每股收益，建议结合财报原文核对应收与资本开支。"

    sustain = None
    if revenue_yoy is not None and profit_yoy is not None:
        if revenue_yoy > 0 and profit_yoy < -20:
            sustain = "营收仍增但净利同比明显下滑，存在业绩承压或费用/减值扰动。"
        elif revenue_yoy < 0 and profit_yoy < revenue_yoy - 15:
            sustain = "营收与净利同步走弱，需关注主业景气与成本结构。"
        elif profit_yoy > revenue_yoy + 10:
            sustain = "净利增速显著快于营收，留意非经常性损益或基数效应。"
        else:
            sustain = "营收与净利同比方向大致一致，可持续经营需结合现金流与行业周期判断。"

    return {
        "report_date": fin.get("financial_report_date"),
        "eps": eps,
        "ocf_per_share": ocf_ps,
        "debt_to_assets_pct": fin.get("debt_to_assets_pct"),
        "cash_profit_match_note": cash_match,
        "profit_sustainability_note": sustain,
        "gross_margin_prev_pct": gross_prev,
        "gross_margin_chg_pp": gross_margin_chg_pp,
    }


def _fetch_pledge_ratio(sym: str) -> dict[str, Any]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": "3",
        "pageNumber": "1",
        "reportName": "RPT_CSDC_LIST",
        "columns": "SECURITY_CODE,TRADE_DATE,PLEDGE_RATIO,PLEDGE_SHARES",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{sym}")',
    }
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            r = requests.get(url, params=params, timeout=12)
        data = r.json()
        rows = (data.get("result") or {}).get("data") or []
        if not rows:
            return {}
        hit = rows[0]
        td = str(hit.get("TRADE_DATE") or "")[:10]
        return {
            "pledge_ratio_pct": _num_or_none(hit.get("PLEDGE_RATIO")),
            "pledge_trade_date": td or None,
            "pledge_shares": _num_or_none(hit.get("PLEDGE_SHARES")),
        }
    except Exception as e:
        logger.debug("pledge ratio %s: %s", sym, e)
        return {}


def _fetch_top_float_holders(f10_code: str) -> tuple[str | None, list[dict[str, Any]]]:
    sym_key = f10_code.upper()
    for raw_date in _RECENT_HOLDER_DATES:
        try:
            with _temporary_clear_proxy_env(
                enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
            ):
                df = ak.stock_gdfx_free_top_10_em(symbol=sym_key, date=raw_date)
        except Exception as e:
            logger.debug("top holders %s %s: %s", sym_key, raw_date, e)
            continue
        if df is None or df.empty:
            continue
        report_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        holders: list[dict[str, Any]] = []
        for _, row in df.head(5).iterrows():
            name = _clean_text(row.get("股东名称"))
            if not name:
                continue
            holders.append(
                {
                    "rank": int(row.get("名次") or len(holders) + 1),
                    "name": name,
                    "holder_type": _clean_text(row.get("股东性质")),
                    "ratio_pct": _num_or_none(row.get("占总流通股本持股比例")),
                    "change_ratio_pct": _num_or_none(row.get("变动比率")),
                }
            )
        if holders:
            return report_date, holders
    return None, []


def _fetch_shareholders(sym: str, f10_code: str) -> dict[str, Any]:
    report_date, top_holders = _fetch_top_float_holders(f10_code)
    pledge = _fetch_pledge_ratio(sym)
    return {
        "report_date": report_date,
        "top_holders": top_holders,
        **pledge,
    }


def _hist_percentile(series: pd.Series, current: float | None) -> float | None:
    if current is None or not math.isfinite(current):
        return None
    vals = pd.to_numeric(series, errors="coerce").dropna()
    vals = vals[vals > 0]
    if vals.empty:
        return None
    return round(float((vals <= current).mean() * 100.0), 1)


def _fetch_valuation_compare(sym: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pe_ttm": None,
        "pb_mrq": None,
        "industry_pe_median": None,
        "industry_pb_median": None,
        "pe_industry_rank": None,
        "pe_hist_percentile_pct": None,
        "pb_hist_percentile_pct": None,
    }
    comp_code = f10_market_code(sym)
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            comp_df = ak.stock_zh_valuation_comparison_em(symbol=comp_code)
    except Exception as e:
        logger.debug("valuation comparison %s: %s", sym, e)
        comp_df = None
    if comp_df is not None and not comp_df.empty:
        self_row = comp_df.iloc[0]
        out["pe_industry_rank"] = _clean_text(self_row.get("排名"))
        out["pe_ttm"] = _num_or_none(self_row.get("市盈率-TTM"))
        out["pb_mrq"] = _num_or_none(self_row.get("市净率-MRQ"))
        peer = comp_df.iloc[1:].copy()
        if not peer.empty:
            pe_vals = pd.to_numeric(peer.get("市盈率-TTM"), errors="coerce").dropna()
            pe_vals = pe_vals[pe_vals > 0]
            if not pe_vals.empty:
                out["industry_pe_median"] = round(float(pe_vals.median()), 2)
            pb_vals = pd.to_numeric(peer.get("市净率-MRQ"), errors="coerce").dropna()
            pb_vals = pb_vals[pb_vals > 0]
            if not pb_vals.empty:
                out["industry_pb_median"] = round(float(pb_vals.median()), 2)
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            hist = ak.stock_value_em(symbol=sym)
    except Exception as e:
        logger.debug("stock_value_em %s: %s", sym, e)
        hist = None
    if hist is not None and not hist.empty:
        tail = hist.tail(750)
        cur_pe = out.get("pe_ttm")
        if cur_pe is None and "PE(TTM)" in tail.columns:
            cur_pe = _num_or_none(tail.iloc[-1].get("PE(TTM)"))
            out["pe_ttm"] = cur_pe
        cur_pb = out.get("pb_mrq")
        if cur_pb is None and "市净率" in tail.columns:
            cur_pb = _num_or_none(tail.iloc[-1].get("市净率"))
            out["pb_mrq"] = cur_pb
        if "PE(TTM)" in tail.columns:
            out["pe_hist_percentile_pct"] = _hist_percentile(tail["PE(TTM)"], cur_pe)
        if "市净率" in tail.columns:
            out["pb_hist_percentile_pct"] = _hist_percentile(tail["市净率"], cur_pb)
    return out


_RISK_LEVEL_RANK = {"danger": 3, "warn": 2, "info": 1}


def _risk_add(
    flags: list[dict[str, str]],
    index: dict[str, int],
    code: str,
    level: str,
    text: str,
) -> None:
    rank = _RISK_LEVEL_RANK.get(level, 0)
    if code in index:
        i = index[code]
        if _RISK_LEVEL_RANK.get(flags[i]["level"], 0) >= rank:
            return
        flags[i] = {"code": code, "level": level, "text": text}
        return
    index[code] = len(flags)
    flags.append({"code": code, "level": level, "text": text})


def _fmt_pct(v: float | None, *, signed: bool = False) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _build_risk_flags(
    *,
    name: str,
    sym: str,
    fin: dict[str, Any],
    shareholders: dict[str, Any],
    valuation: dict[str, Any] | None = None,
    gross_margin_chg_pp: float | None = None,
) -> dict[str, Any]:
    flags: list[dict[str, str]] = []
    idx: dict[str, int] = {}
    valuation = valuation or {}

    if is_st_stock_name(name):
        _risk_add(
            flags,
            idx,
            "st",
            "danger",
            "名称含 ST / *ST：涨跌幅受限，存在退市与监管特殊处理风险，须查阅最新公告。",
        )

    debt = fin.get("debt_to_assets_pct")
    if debt is not None:
        if debt >= 85:
            _risk_add(
                flags,
                idx,
                "high_debt",
                "danger",
                f"资产负债率 {_fmt_pct(debt)}（≥85%），杠杆极高，偿债与再融资压力显著。",
            )
        elif debt >= 70:
            _risk_add(
                flags,
                idx,
                "high_debt",
                "warn",
                f"资产负债率 {_fmt_pct(debt)}（70%～85%），杠杆偏高，需关注有息负债与利息覆盖。",
            )
        elif debt >= 60:
            _risk_add(
                flags,
                idx,
                "elevated_debt",
                "info",
                f"资产负债率 {_fmt_pct(debt)}（60%～70%），杠杆中等偏上，宜结合行业惯例判断。",
            )

    pledge = shareholders.get("pledge_ratio_pct")
    if pledge is not None:
        if pledge >= 60:
            _risk_add(
                flags,
                idx,
                "high_pledge",
                "danger",
                f"股权质押比例 {_fmt_pct(pledge)}（≥60%），平仓与控制权稳定性风险较高。",
            )
        elif pledge >= 40:
            _risk_add(
                flags,
                idx,
                "high_pledge",
                "warn",
                f"股权质押比例 {_fmt_pct(pledge)}（40%～60%），需跟踪质押率与大股东补充质押公告。",
            )
        elif pledge >= 25:
            _risk_add(
                flags,
                idx,
                "pledge",
                "info",
                f"股权质押比例 {_fmt_pct(pledge)}（25%～40%），建议持续关注解押/新增质押动态。",
            )

    rev_yoy = fin.get("revenue_yoy_pct")
    prof_yoy = fin.get("profit_yoy_pct")
    if rev_yoy is not None and prof_yoy is not None:
        gap = rev_yoy - prof_yoy
        if rev_yoy > 0 and prof_yoy < -50:
            _risk_add(
                flags,
                idx,
                "earnings_reversal",
                "danger",
                f"营收同比 {_fmt_pct(rev_yoy, signed=True)} 仍为正，但净利同比 {_fmt_pct(prof_yoy, signed=True)}，"
                "业绩严重背离，警惕减值、非经损益或主业失速。",
            )
        elif rev_yoy > 5 and prof_yoy < -20:
            _risk_add(
                flags,
                idx,
                "earnings_reversal",
                "warn",
                f"营收同比 {_fmt_pct(rev_yoy, signed=True)}、净利同比 {_fmt_pct(prof_yoy, signed=True)}，"
                "增利不增收或「业绩变脸」迹象，建议核对单季利润表。",
            )
        elif rev_yoy > 10 and prof_yoy < 0:
            _risk_add(
                flags,
                idx,
                "profit_lag_revenue",
                "warn",
                f"营收同比 {_fmt_pct(rev_yoy, signed=True)} 尚可，但净利同比 {_fmt_pct(prof_yoy, signed=True)} 转负，"
                "盈利质量边际走弱。",
            )
        elif gap >= 30:
            _risk_add(
                flags,
                idx,
                "profit_lag_revenue",
                "warn",
                f"净利增速落后营收约 {_fmt_pct(gap)}（营收 {_fmt_pct(rev_yoy, signed=True)} vs "
                f"净利 {_fmt_pct(prof_yoy, signed=True)}），费用率或毛利率可能承压。",
            )
        elif rev_yoy < -10 and prof_yoy < -20:
            _risk_add(
                flags,
                idx,
                "dual_decline",
                "warn",
                f"营收与净利同步下滑（{_fmt_pct(rev_yoy, signed=True)} / {_fmt_pct(prof_yoy, signed=True)}），"
                "基本面景气偏弱。",
            )
        elif rev_yoy < 0 and prof_yoy < rev_yoy - 15:
            _risk_add(
                flags,
                idx,
                "dual_decline",
                "warn",
                f"营收 {_fmt_pct(rev_yoy, signed=True)}、净利 {_fmt_pct(prof_yoy, signed=True)}，"
                "利润跌幅显著大于营收，存在缩利或一次性损失。",
            )

    if gross_margin_chg_pp is not None and gross_margin_chg_pp <= -5:
        _risk_add(
            flags,
            idx,
            "margin_compress",
            "warn",
            f"销售毛利率较近一期下降约 {abs(gross_margin_chg_pp):.1f} 个百分点，"
            "产品定价或成本端可能承压。",
        )
    elif gross_margin_chg_pp is not None and gross_margin_chg_pp <= -3:
        _risk_add(
            flags,
            idx,
            "margin_compress",
            "info",
            f"销售毛利率较近一期小幅下降约 {abs(gross_margin_chg_pp):.1f} 个百分点，宜观察能否企稳。",
        )

    eps = fin.get("eps")
    ocf = fin.get("ocf_per_share")
    if eps is not None and ocf is not None:
        if eps > 0 and ocf < 0:
            _risk_add(
                flags,
                idx,
                "cash_mismatch",
                "warn",
                f"每股收益 {eps:.3f} 元为正，但每股经营现金流 {ocf:.3f} 元为负，利润含金量偏弱。",
            )
        elif eps > 0 and ocf >= 0 and ocf < eps * 0.3:
            ratio = ocf / eps * 100.0
            _risk_add(
                flags,
                idx,
                "weak_cash_conversion",
                "warn",
                f"经营现金流/每股收益约 {ratio:.0f}%（<{30}% 阈值），回款对利润覆盖不足。",
            )
        elif eps > 0 and ocf >= 0 and ocf < eps * 0.5:
            ratio = ocf / eps * 100.0
            _risk_add(
                flags,
                idx,
                "weak_cash_conversion",
                "info",
                f"经营现金流/每股收益约 {ratio:.0f}%，现金转化一般，建议关注应收与存货。",
            )
        elif eps <= 0 and prof_yoy is not None and prof_yoy < -20:
            _risk_add(
                flags,
                idx,
                "loss_widening",
                "warn",
                f"每股收益为负且净利同比 {_fmt_pct(prof_yoy, signed=True)}，亏损有扩大趋势。",
            )

    cur = fin.get("current_ratio")
    quick = fin.get("quick_ratio")
    if cur is not None:
        if cur < 0.8:
            _risk_add(
                flags,
                idx,
                "liquidity",
                "danger",
                f"流动比率 {cur:.2f}（<0.8），短期偿债压力较大。",
            )
        elif cur < 1.2:
            _risk_add(
                flags,
                idx,
                "liquidity",
                "warn",
                f"流动比率 {cur:.2f}（<1.2），营运资金偏紧，需关注短债到期结构。",
            )
    if quick is not None and quick < 0.8:
        _risk_add(
            flags,
            idx,
            "quick_liquidity",
            "warn",
            f"速动比率 {quick:.2f}（<0.8），扣除存货后即时偿债能力偏弱。",
        )

    roe = fin.get("roe_pct")
    if roe is not None:
        if roe < 0:
            _risk_add(
                flags,
                idx,
                "low_roe",
                "warn",
                f"净资产收益率 ROE {_fmt_pct(roe, signed=True)} 为负，股东回报能力偏弱。",
            )
        elif roe < 5:
            _risk_add(
                flags,
                idx,
                "low_roe",
                "info",
                f"净资产收益率 ROE {_fmt_pct(roe)} 偏低（<5%），资本运用效率一般。",
            )
        elif roe < 8 and prof_yoy is not None and prof_yoy < 0:
            _risk_add(
                flags,
                idx,
                "roe_decline",
                "info",
                f"ROE {_fmt_pct(roe)} 尚可但净利同比 {_fmt_pct(prof_yoy, signed=True)}，盈利动能走弱。",
            )

    pe = valuation.get("pe_ttm") or fin.get("pe_dynamic")
    pe_med = valuation.get("industry_pe_median")
    pe_hist = valuation.get("pe_hist_percentile_pct")
    pb_hist = valuation.get("pb_hist_percentile_pct")
    if pe is not None and pe < 0:
        _risk_add(
            flags,
            idx,
            "neg_pe",
            "info",
            "市盈率 TTM 为负（亏损市），估值锚需改用 PB 或情景分析。",
        )
    elif pe is not None and pe_med is not None and pe > 0 and pe_med > 0:
        multiple = pe / pe_med
        if multiple >= 2.0:
            _risk_add(
                flags,
                idx,
                "val_expensive",
                "warn",
                f"PE-TTM {pe:.1f} 约为同行中位数 {pe_med:.1f} 的 {multiple:.1f} 倍，估值偏贵。",
            )
        elif multiple >= 1.5:
            _risk_add(
                flags,
                idx,
                "val_expensive",
                "info",
                f"PE-TTM {pe:.1f} 高于同行中位数 {pe_med:.1f}（约 {multiple:.1f} 倍），溢价需业绩兑现支撑。",
            )
    if pe_hist is not None and pe_hist >= 90:
        _risk_add(
            flags,
            idx,
            "pe_hist_high",
            "warn",
            f"PE-TTM 处于近约3年样本的 {pe_hist:.0f}% 分位（偏高），追高风险需自控。",
        )
    elif pe_hist is not None and pe_hist >= 75:
        _risk_add(
            flags,
            idx,
            "pe_hist_high",
            "info",
            f"PE-TTM 处于近约3年样本的 {pe_hist:.0f}% 分位，估值不便宜。",
        )
    if pb_hist is not None and pb_hist >= 90:
        _risk_add(
            flags,
            idx,
            "pb_hist_high",
            "info",
            f"PB 处于近约3年样本的 {pb_hist:.0f}% 分位，资产定价偏热。",
        )

    holders = shareholders.get("top_holders") or []
    if holders:
        top1 = holders[0]
        chg1 = top1.get("change_ratio_pct")
        if chg1 is not None and chg1 <= -20:
            _risk_add(
                flags,
                idx,
                "holder_reduce",
                "warn",
                f"第一大流通股东「{top1.get('name', '')}」持股变动约 {_fmt_pct(chg1, signed=True)}，"
                "存在较明显减持。",
            )
        elif chg1 is not None and chg1 <= -10:
            _risk_add(
                flags,
                idx,
                "holder_reduce",
                "info",
                f"第一大流通股东「{top1.get('name', '')}」持股变动约 {_fmt_pct(chg1, signed=True)}，"
                "可跟踪后续披露。",
            )
        top5_ratio = sum(h.get("ratio_pct") or 0 for h in holders[:5])
        if top5_ratio >= 75:
            _risk_add(
                flags,
                idx,
                "holder_concentration",
                "info",
                f"前五流通股东合计持股约 {_fmt_pct(top5_ratio)}，股权集中度较高，流通盘弹性或受限。",
            )

    if sym.startswith(("688", "689")):
        _risk_add(
            flags,
            idx,
            "board_star",
            "info",
            "科创板（688/689）：涨跌幅 20%、上市初期无涨跌幅限制等规则与主板不同。",
        )
    elif sym.startswith(("8", "4")):
        _risk_add(
            flags,
            idx,
            "board_bj",
            "info",
            "北交所（8/4 开头）：流动性、准入与涨跌幅规则与沪深主板不同。",
        )
    elif sym.startswith(("300", "301")):
        _risk_add(
            flags,
            idx,
            "board_cyb",
            "info",
            "创业板（300/301）：涨跌幅 20%，波动通常大于主板。",
        )

    flags.sort(key=lambda f: (-_RISK_LEVEL_RANK.get(f["level"], 0), f["code"]))
    n_d = sum(1 for f in flags if f["level"] == "danger")
    n_w = sum(1 for f in flags if f["level"] == "warn")
    n_i = sum(1 for f in flags if f["level"] == "info")
    if not flags:
        summary = "未命中 Demo 规则下的显著风险标签；仍须结合公告、财报原文与仓位管理自行判断。"
    else:
        summary = (
            f"共 {len(flags)} 条（严重 {n_d} / 关注 {n_w} / 提示 {n_i}），"
            "含定量阈值与同比数据；规则 Demo，非完整风控清单。"
        )
    return {"flags": flags, "summary": summary}


def _fin_float(row: pd.Series | None, key: str) -> float | None:
    if row is None or key not in row.index:
        return None
    return _num_or_none(row.get(key))


_CHANGE_UP_TYPES = ("封涨停板", "火箭发射", "大笔买入", "60日新高")
_CHANGE_DOWN_TYPES = ("封跌停板", "加速下跌", "大笔卖出", "60日新低")


def _trade_dates_from_quote(quote: dict[str, Any]) -> tuple[str, str]:
    """返回 (YYYY-MM-DD, YYYYMMDD)。"""
    raw = _clean_text(quote.get("last_trade_date"))
    display = ""
    if raw:
        digits = re.sub(r"\D", "", raw)[:8]
        if len(digits) == 8:
            display = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if not display:
        display = date.today().isoformat()
    return display, display.replace("-", "")


def _move_direction(change_pct: float | None) -> str:
    if change_pct is None:
        return "flat"
    if change_pct > 0.3:
        return "up"
    if change_pct < -0.3:
        return "down"
    return "flat"


def _df_row_for_sym(df: pd.DataFrame | None, sym: str) -> pd.Series | None:
    if df is None or df.empty:
        return None
    code_col = next((c for c in ("代码", "股票代码", "SECURITY_CODE") if c in df.columns), None)
    if code_col is None:
        return None
    target = normalize_symbol(sym).zfill(6)
    codes = df[code_col].astype(str).str.replace(r"\D", "", regex=True).str[-6:].str.zfill(6)
    hit = df[codes == target]
    return hit.iloc[0] if not hit.empty else None


def _ak_call_df(func, *args, **kwargs) -> pd.DataFrame | None:
    try:
        with _temporary_clear_proxy_env(
            enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
        ):
            df = func(*args, **kwargs)
    except Exception as e:
        logger.debug("ak %s: %s", getattr(func, "__name__", func), e)
        return None
    return df if df is not None and not df.empty else None


def _fetch_zt_pool_row(sym: str, date_compact: str) -> dict[str, Any] | None:
    row = _df_row_for_sym(_ak_call_df(ak.stock_zt_pool_em, date=date_compact), sym)
    if row is None:
        return None
    parts: list[str] = []
    lb = row.get("连板数")
    if lb is not None and pd.notna(lb):
        parts.append(f"连板 {int(lb)}")
    stat = _clean_text(row.get("涨停统计"))
    if stat:
        parts.append(f"涨停统计 {stat}")
    first = _clean_text(row.get("首次封板时间"))
    if first:
        parts.append(f"首次封板 {first}")
    zb = row.get("炸板次数")
    if zb is not None and pd.notna(zb) and int(zb) > 0:
        parts.append(f"炸板 {int(zb)} 次")
    industry = _clean_text(row.get("所属行业"))
    if industry:
        parts.append(f"行业 {industry}")
    return {
        "note": "涨停股池：" + "，".join(parts) if parts else "涨停股池入选",
        "change_pct": _num_or_none(row.get("涨跌幅")),
    }


def _fetch_strong_pool_row(sym: str, date_compact: str) -> str | None:
    row = _df_row_for_sym(_ak_call_df(ak.stock_zt_pool_strong_em, date=date_compact), sym)
    if row is None:
        return None
    reason = _clean_text(row.get("入选理由"))
    return reason or None


def _fetch_dt_pool_row(sym: str, date_compact: str) -> dict[str, Any] | None:
    row = _df_row_for_sym(_ak_call_df(ak.stock_zt_pool_dtgc_em, date=date_compact), sym)
    if row is None:
        return None
    parts: list[str] = []
    cont = row.get("连续跌停")
    if cont is not None and pd.notna(cont):
        parts.append(f"连续跌停 {int(cont)}")
    open_cnt = row.get("开板次数")
    if open_cnt is not None and pd.notna(open_cnt):
        parts.append(f"开板 {int(open_cnt)} 次")
    last = _clean_text(row.get("最后封板时间"))
    if last:
        parts.append(f"最后封板 {last}")
    return {
        "note": "跌停股池：" + "，".join(parts) if parts else "跌停股池入选",
        "change_pct": _num_or_none(row.get("涨跌幅")),
    }


def _fetch_lhb_day(sym: str, date_compact: str) -> dict[str, str] | None:
    df = _ak_call_df(ak.stock_lhb_detail_em, start_date=date_compact, end_date=date_compact)
    row = _df_row_for_sym(df, sym)
    if row is None:
        return None
    reason = _clean_text(row.get("上榜原因"))
    note = _clean_text(row.get("解读"))
    if not reason and not note:
        return None
    return {"reason": reason, "note": note}


def _fetch_notices_day(sym: str, date_compact: str, *, limit: int = 5) -> list[dict[str, str]]:
    return _fetch_notices_range(sym, date_compact, date_compact, limit=limit)


def _fetch_notices_range(
    sym: str, begin_compact: str, end_compact: str, *, limit: int = 12
) -> list[dict[str, str]]:
    df = _ak_call_df(
        ak.stock_individual_notice_report,
        security=sym,
        symbol="全部",
        begin_date=begin_compact,
        end_date=end_compact,
    )
    if df is None:
        return []
    rows: list[dict[str, str]] = []
    for _, row in df.head(max(1, min(limit, 20))).iterrows():
        title = _clean_text(row.get("公告标题"))
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "notice_type": _clean_text(row.get("公告类型")),
                "published_at": _clean_text(row.get("公告日期")),
                "url": _clean_text(row.get("网址")),
            }
        )
    return rows


def _fetch_intraday_events(sym: str, direction: str) -> list[str]:
    types = _CHANGE_UP_TYPES if direction == "up" else (
        _CHANGE_DOWN_TYPES if direction == "down" else _CHANGE_UP_TYPES + _CHANGE_DOWN_TYPES
    )
    target = normalize_symbol(sym).zfill(6)
    events: list[str] = []
    seen: set[str] = set()
    for kind in types[:4 if direction != "flat" else 3]:
        df = _ak_call_df(ak.stock_changes_em, symbol=kind)
        if df is None:
            continue
        code_col = "代码" if "代码" in df.columns else None
        if code_col is None:
            continue
        codes = df[code_col].astype(str).str.replace(r"\D", "", regex=True).str[-6:].str.zfill(6)
        if not (codes == target).any():
            continue
        times = df.loc[codes == target, "时间"].astype(str).tolist() if "时间" in df.columns else []
        time_hint = times[0][:8] if times else ""
        label = f"{kind}" + (f"（{time_hint}）" if time_hint else "")
        if label not in seen:
            seen.add(label)
            events.append(label)
    return events[:6]


def _news_for_trade_date(
    news: list[dict[str, str]], trade_display: str, *, limit: int = 5
) -> list[dict[str, str]]:
    compact = trade_display.replace("-", "")
    out: list[dict[str, str]] = []
    for item in news:
        pub = _clean_text(item.get("published_at"))
        if not pub:
            continue
        pub_digits = re.sub(r"\D", "", pub)
        if trade_display in pub or (len(pub_digits) >= 8 and pub_digits[:8] == compact):
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _parse_event_date_iso(raw: str) -> str | None:
    digits = re.sub(r"\D", "", _clean_text(raw))[:8]
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _date_compact_range(end_display: str, *, days: int = 3) -> tuple[str, str, str]:
    end_d = date.fromisoformat(end_display)
    start_d = end_d - timedelta(days=max(1, days) - 1)
    start_display = start_d.isoformat()
    return start_display.replace("-", ""), end_display.replace("-", ""), start_display


def _em_fund_flow_market(sym: str) -> str:
    s = normalize_symbol(sym)
    if s.startswith(("8", "4")):
        return "bj"
    if s.startswith("6"):
        return "sh"
    return "sz"


def _fmt_money_yuan(v: float | None) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    av = abs(v)
    sign = "-" if v < 0 else ""
    if av >= 1e8:
        return f"{sign}{av / 1e8:.2f}亿"
    if av >= 1e4:
        return f"{sign}{av / 1e4:.2f}万"
    return f"{sign}{av:.0f}元"


def _fund_flow_note(change_pct: float | None, main_net: float | None, main_ratio: float | None) -> str:
    if main_net is None:
        return "未能获取主力净流入数据。"
    net_txt = _fmt_money_yuan(main_net)
    ratio_txt = f"{main_ratio:.2f}%" if main_ratio is not None else "—"
    if change_pct is None:
        return f"主力净流入 {net_txt}（占成交额 {ratio_txt}）。"
    if change_pct > 0.3 and main_net > 0:
        return f"主力净流入 {net_txt}（{ratio_txt}），与收涨方向一致，资金面对涨幅有支撑。"
    if change_pct < -0.3 and main_net < 0:
        return f"主力净流出 {net_txt}（{ratio_txt}），与收跌方向一致，资金面与股价同步走弱。"
    if change_pct > 0.3 and main_net < 0:
        return f"收涨但主力净流出 {net_txt}（{ratio_txt}），价量资金存在分歧，需防拉高出货。"
    if change_pct < -0.3 and main_net > 0:
        return f"收跌但主力净流入 {net_txt}（{ratio_txt}），或有资金逆势吸纳/护盘。"
    return f"主力净流入 {net_txt}（{ratio_txt}），与当日涨跌幅方向未形成明显共振。"


def _fetch_fund_flow_em_day(sym: str, trade_display: str) -> dict[str, Any] | None:
    market = _em_fund_flow_market(sym)
    df = _ak_call_df(ak.stock_individual_fund_flow, stock=sym, market=market)
    if df is None or "日期" not in df.columns:
        try:
            from app.fundamentals import (
                _load_individual_fund_flow_df,
                _parse_fund_flow_row_date,
            )

            with _temporary_clear_proxy_env(
                enabled=bool(get_settings().ingest_eastmoney_bypass_proxy),
            ):
                df = _load_individual_fund_flow_df(sym)
        except Exception as e:
            logger.debug("fund_flow loader %s: %s", sym, e)
            df = None
    if df is None or df.empty or "日期" not in df.columns:
        return None
    work = df.copy()
    try:
        if "日期" in work.columns:
            work["_d"] = work["日期"].astype(str).str.replace(r"\D", "", regex=True).str[:8]
        else:
            from app.fundamentals import _parse_fund_flow_row_date

            work["_d"] = [
                (re.sub(r"\D", "", str(_parse_fund_flow_row_date(work.iloc[i]) or ""))[:8])
                for i in range(len(work))
            ]
    except Exception:
        return None
    target = trade_display.replace("-", "")
    hit = work[work["_d"] == target]
    if hit.empty:
        hit = work.head(1)
        if hit.empty:
            return None
    row = hit.iloc[0]
    main_net = _num_or_none(row.get("主力净流入-净额"))
    if main_net is None:
        return None
    main_ratio = _num_or_none(row.get("主力净流入-净占比"))
    chg = _num_or_none(row.get("涨跌幅"))
    return {
        "trade_date": _parse_event_date_iso(str(row.get("日期"))) or trade_display,
        "close": _num_or_none(row.get("收盘价")),
        "change_pct": chg,
        "main_net_inflow": main_net,
        "main_net_ratio_pct": main_ratio,
        "super_large_net": _num_or_none(row.get("超大单净流入-净额")),
        "large_net": _num_or_none(row.get("大单净流入-净额")),
        "source_key": "eastmoney_fund_flow",
    }


def _fetch_fund_flow_big_deal_day(sym: str, trade_display: str) -> dict[str, Any] | None:
    df = _ak_call_df(ak.stock_fund_flow_big_deal)
    if df is None or df.empty:
        return None
    code_col = _df_col_by_hint(df, ("代码",), fallback_idx=1)
    time_col = _df_col_by_hint(df, ("时间",), fallback_idx=0)
    amt_col = _df_col_by_hint(df, ("成交额",), fallback_idx=5)
    side_col = _df_col_by_hint(df, ("性质", "方向"), fallback_idx=6)
    if code_col is None or amt_col is None or side_col is None:
        return None
    target = normalize_symbol(sym).zfill(6)
    codes = df[code_col].astype(str).str.replace(r"\D", "", regex=True).str[-6:].str.zfill(6)
    work = df[codes == target].copy()
    if work.empty:
        return None
    if time_col is not None:
        day_prefix = trade_display.replace("-", "")
        times = work[time_col].astype(str)
        day_hit = work[times.str.replace(r"\D", "", regex=True).str.startswith(day_prefix)]
        if not day_hit.empty:
            work = day_hit
    buy_amt = 0.0
    sell_amt = 0.0
    for _, row in work.iterrows():
        amt = _num_or_none(row.get(amt_col))
        if amt is None:
            continue
        side = _clean_text(row.get(side_col))
        if "买" in side:
            buy_amt += float(amt)
        elif "卖" in side:
            sell_amt += float(amt)
    if buy_amt <= 0 and sell_amt <= 0:
        return None
    net_wan = buy_amt - sell_amt
    return {
        "trade_date": trade_display,
        "close": None,
        "change_pct": None,
        "main_net_inflow": round(net_wan * 10000.0, 2),
        "main_net_ratio_pct": None,
        "super_large_net": None,
        "large_net": None,
        "source_key": "eastmoney_big_deal_flow",
        "big_deal_buy_wan": round(buy_amt, 2),
        "big_deal_sell_wan": round(sell_amt, 2),
    }


def _fetch_fund_flow_day(sym: str, trade_display: str) -> dict[str, Any]:
    meta = _source_meta("eastmoney_fund_flow")
    empty: dict[str, Any] = {
        "trade_date": trade_display,
        "close": None,
        "change_pct": None,
        "main_net_inflow": None,
        "main_net_ratio_pct": None,
        "super_large_net": None,
        "large_net": None,
        "note": None,
        "source_label": meta["label"],
        "api": meta["api"],
        "available": False,
    }
    payload = _fetch_fund_flow_em_day(sym, trade_display)
    if payload is None:
        payload = _fetch_fund_flow_big_deal_day(sym, trade_display)
    if payload is None:
        empty["note"] = "未能拉取个股资金流（东财接口不可用，已尝试大单成交汇总）。"
        return empty
    source_key = str(payload.get("source_key") or "eastmoney_fund_flow")
    meta = _source_meta(source_key)
    main_net = payload.get("main_net_inflow")
    main_ratio = payload.get("main_net_ratio_pct")
    chg = payload.get("change_pct")
    note = _fund_flow_note(chg, main_net, main_ratio)
    if source_key == "eastmoney_big_deal_flow":
        buy_wan = payload.get("big_deal_buy_wan")
        sell_wan = payload.get("big_deal_sell_wan")
        note = (
            f"大单净买入约 {_fmt_money_yuan(main_net)}"
            f"（买 {buy_wan} 万 / 卖 {sell_wan} 万，为当日大单成交汇总，非完整主力口径）。"
        )
    return {
        "trade_date": payload.get("trade_date") or trade_display,
        "close": payload.get("close"),
        "change_pct": chg,
        "main_net_inflow": main_net,
        "main_net_ratio_pct": main_ratio,
        "super_large_net": payload.get("super_large_net"),
        "large_net": payload.get("large_net"),
        "note": note,
        "source_key": source_key,
        "source_label": meta["label"],
        "api": meta["api"],
        "available": main_net is not None,
    }


def _build_event_timeline(
    sym: str,
    trade_display: str,
    news: list[dict[str, str]],
    *,
    days: int = 3,
    notice_limit: int = 12,
) -> dict[str, Any]:
    begin_compact, end_compact, begin_display = _date_compact_range(trade_display, days=days)
    notice_meta = _source_meta("eastmoney_notice")
    news_meta = _source_meta("eastmoney_news")
    notices = _fetch_notices_range(sym, begin_compact, end_compact, limit=notice_limit)
    items: list[dict[str, Any]] = []
    for n in notices:
        event_date = _parse_event_date_iso(n.get("published_at") or "") or trade_display
        items.append(
            {
                "event_type": "notice",
                "event_date": event_date,
                "title": n.get("title") or "",
                "summary": n.get("notice_type") or None,
                "url": n.get("url") or None,
                "sub_type": n.get("notice_type") or None,
                "source_label": notice_meta["label"],
                "api": notice_meta["api"],
                "is_trade_day": event_date == trade_display,
            }
        )
    for n in news:
        event_date = _parse_event_date_iso(n.get("published_at") or "")
        if not event_date:
            continue
        if event_date < begin_display or event_date > trade_display:
            continue
        items.append(
            {
                "event_type": "news",
                "event_date": event_date,
                "title": n.get("title") or "",
                "summary": n.get("summary") or None,
                "url": n.get("url") or None,
                "sub_type": n.get("source") or None,
                "source_label": news_meta["label"],
                "api": news_meta["api"],
                "is_trade_day": event_date == trade_display,
            }
        )
    items.sort(key=lambda x: (x.get("event_date") or "", x.get("event_type") or ""), reverse=True)
    if not items and news:
        for n in news[:8]:
            title = _clean_text(n.get("title"))
            if not title:
                continue
            event_date = _parse_event_date_iso(n.get("published_at") or "") or trade_display
            items.append(
                {
                    "event_type": "news",
                    "event_date": event_date,
                    "title": title,
                    "summary": n.get("summary") or None,
                    "url": n.get("url") or None,
                    "sub_type": n.get("source") or "近期新闻",
                    "source_label": news_meta["label"],
                    "api": news_meta["api"],
                    "is_trade_day": event_date == trade_display,
                }
            )
    return {
        "days": days,
        "begin_date": begin_display,
        "end_date": trade_display,
        "items": items[:20],
        "source_notices": notice_meta["api"],
        "source_news": news_meta["api"],
    }


def _add_highlight(
    highlights: list[dict[str, str]], *, source: str, category: str, text: str
) -> None:
    text = _clean_text(text)
    if not text:
        return
    if any(h["text"] == text for h in highlights):
        return
    meta = _source_meta(source)
    highlights.append(
        {
            "source": source,
            "source_label": meta.get("label", source),
            "api": meta.get("api", ""),
            "category": category,
            "text": text,
        }
    )


_ATTRIBUTION_LABELS = {
    "company": "偏公司自身",
    "sector": "偏行业/板块",
    "market": "偏大盘/系统性",
    "sentiment": "偏资金/情绪",
    "mixed": "多重因素",
    "unknown": "暂无法判断",
}

_SOURCE_META: dict[str, dict[str, str]] = {
    "eastmoney_zt_pool": {"label": "东财涨停池", "api": "ak.stock_zt_pool_em"},
    "eastmoney_dt_pool": {"label": "东财跌停池", "api": "ak.stock_zt_pool_dtgc_em"},
    "eastmoney_strong_pool": {"label": "东财强势股池", "api": "ak.stock_zt_pool_strong_em"},
    "eastmoney_lhb": {"label": "东财龙虎榜", "api": "ak.stock_lhb_detail_em"},
    "eastmoney_notice": {"label": "东财公告大全", "api": "ak.stock_individual_notice_report"},
    "eastmoney_news": {"label": "东财个股新闻", "api": "ak.stock_news_em"},
    "eastmoney_changes": {"label": "东财盘口异动", "api": "ak.stock_changes_em"},
    "f10_concepts": {"label": "东财 F10 题材", "api": "F10 CoreConception/PageAjax"},
    "live_quote": {"label": "个股实时行情", "api": "live_quote_fields_for_codes_enhanced"},
    "index_spot": {"label": "A 股指数行情", "api": "ak.stock_zh_index_spot_em"},
    "index_spot_sina": {"label": "新浪 A 股指数", "api": "ak.stock_zh_index_spot_sina"},
    "industry_spot": {"label": "东财行业板块", "api": "ak.stock_board_industry_name_em"},
    "industry_spot_sina": {"label": "新浪行业板块", "api": "ak.stock_sector_spot"},
    "eastmoney_fund_flow": {"label": "东财个股资金流", "api": "ak.stock_individual_fund_flow"},
    "eastmoney_big_deal_flow": {
        "label": "东财大单成交汇总",
        "api": "ak.stock_fund_flow_big_deal",
    },
    "local_rule": {"label": "本地 Demo 规则", "api": "stock_brief._build_move_attribution"},
}


def _source_meta(source: str) -> dict[str, str]:
    return _SOURCE_META.get(source, {"label": source, "api": ""})


def _add_factor(
    factors: list[dict[str, str]],
    *,
    source: str,
    text: str,
    kind: str = "rule",
) -> None:
    text = _clean_text(text)
    if not text:
        return
    meta = _source_meta(source)
    factors.append(
        {
            "source": source,
            "source_label": meta.get("label", source),
            "api": meta.get("api", ""),
            "kind": kind,
            "text": text,
        }
    )

_COMPANY_NEGATIVE_KW = (
    "亏损",
    "预亏",
    "预减",
    "下滑",
    "下降",
    "减持",
    "立案",
    "警示",
    "处罚",
    "诉讼",
    "下修",
    "暴雷",
    "退市",
    "st",
    "违规",
    "调查",
    "终止",
    "风险提示",
    "业绩变脸",
    "质押",
    "被查",
    "失信",
    "下调",
)

_COMPANY_POSITIVE_KW = ("预增", "中标", "签约", "订单", "回购", "增持", "业绩大增", "超预期", "突破")


def _benchmark_index_for_sym(sym: str) -> tuple[str, str]:
    s = normalize_symbol(sym)
    if s.startswith(("688", "689")):
        return "000688", "科创50"
    if s.startswith(("300", "301")):
        return "399006", "创业板指"
    if s.startswith("6"):
        return "000001", "上证指数"
    if s.startswith(("0", "3")):
        return "399001", "深证成指"
    return "899050", "北证50"


def _sina_index_symbol(index_code: str) -> str:
    code = index_code.zfill(6)
    if code.startswith(("399", "899")):
        return f"sz{code}"
    return f"sh{code}"


def _df_col_by_hint(
    df: pd.DataFrame, hints: tuple[str, ...], *, fallback_idx: int | None = None
) -> str | None:
    for col in df.columns:
        name = str(col)
        if any(h in name for h in hints):
            return col
    if fallback_idx is not None and len(df.columns) > fallback_idx:
        return df.columns[fallback_idx]
    return None


def _index_change_pct_em(index_code: str) -> tuple[str | None, float | None]:
    df = _ak_call_df(ak.stock_zh_index_spot_em)
    if df is None:
        return None, None
    code_col = next((c for c in ("代码", "index_code") if c in df.columns), None)
    chg_col = next((c for c in ("涨跌幅", "change_pct") if c in df.columns), None)
    name_col = next((c for c in ("名称", "name") if c in df.columns), None)
    if code_col is None or chg_col is None:
        return None, None
    target = index_code.zfill(6)
    codes = df[code_col].astype(str).str.replace(r"\D", "", regex=True).str[-6:].str.zfill(6)
    hit = df[codes == target]
    if hit.empty:
        return None, None
    row = hit.iloc[0]
    name = _clean_text(row.get(name_col)) if name_col else None
    return name, _num_or_none(row.get(chg_col))


def _index_change_pct_sina(index_code: str, default_name: str) -> tuple[str | None, float | None]:
    df = _ak_call_df(ak.stock_zh_index_spot_sina)
    if df is None:
        return None, None
    code_col = _df_col_by_hint(df, ("代码",), fallback_idx=0)
    chg_col = _df_col_by_hint(df, ("涨跌幅",), fallback_idx=4)
    name_col = _df_col_by_hint(df, ("名称",), fallback_idx=1)
    if code_col is None or chg_col is None:
        return None, None
    target = _sina_index_symbol(index_code)
    codes = df[code_col].astype(str).str.lower()
    hit = df[codes == target]
    if hit.empty:
        hit = df[codes.str.endswith(index_code.zfill(6))]
    if hit.empty:
        return None, None
    row = hit.iloc[0]
    name = _clean_text(row.get(name_col)) if name_col else default_name
    return name or default_name, _num_or_none(row.get(chg_col))


def _index_change_pct(index_code: str, default_name: str) -> tuple[str | None, float | None, str]:
    name, chg = _index_change_pct_em(index_code)
    if chg is not None:
        return name or default_name, chg, "index_spot"
    name, chg = _index_change_pct_sina(index_code, default_name)
    if chg is not None:
        return name or default_name, chg, "index_spot_sina"
    return default_name, None, "index_spot"


def _industry_match_hints(industry: str, concept_hints: list[str] | None = None) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        text = _clean_text(raw)
        if not text or text in seen:
            return
        seen.add(text)
        hints.append(text)

    if industry:
        _add(industry)
        for part in re.split(r"[-/—|、]", industry):
            _add(part.strip())
        compact = industry.replace("-", "").replace("/", "")
        if compact and compact != industry:
            _add(compact)
    for name in concept_hints or []:
        _add(name)
    return sorted(hints, key=len, reverse=True)


def _match_industry_row(df: pd.DataFrame | None, industry: str) -> pd.Series | None:
    if df is None or df.empty or not industry:
        return None
    ind = industry.strip()
    if "板块名称" not in df.columns:
        return None
    names = df["板块名称"].astype(str)
    exact = df[names == ind]
    if not exact.empty:
        return exact.iloc[0]
    partial = df[names.apply(lambda x: ind in x or x in ind)]
    if not partial.empty:
        return partial.iloc[0]
    for hint in _industry_match_hints(industry):
        partial = df[names.apply(lambda x, h=hint: h in x or x in h)]
        if not partial.empty:
            return partial.iloc[0]
    return None


def _match_sina_industry_row(
    df: pd.DataFrame | None, hints: list[str]
) -> pd.Series | None:
    if df is None or df.empty or not hints:
        return None
    name_col = _df_col_by_hint(df, ("板块", "名称"), fallback_idx=1)
    if name_col is None:
        return None
    names = df[name_col].astype(str)
    for hint in hints:
        if len(hint) < 2:
            continue
        exact = df[names == hint]
        if not exact.empty:
            return exact.iloc[0]
        partial = df[names.apply(lambda x, h=hint: h in x or x in h)]
        if not partial.empty:
            return partial.iloc[0]
    return None


def _industry_change_pct_em(industry: str) -> tuple[str | None, float | None]:
    df = _ak_call_df(ak.stock_board_industry_name_em)
    row = _match_industry_row(df, industry)
    if row is None:
        return None, None
    name = _clean_text(row.get("板块名称"))
    chg = _num_or_none(row.get("涨跌幅"))
    return name or industry, chg


def _industry_change_pct_sina(
    industry: str, concept_hints: list[str] | None = None
) -> tuple[str | None, float | None]:
    hints = _industry_match_hints(industry, concept_hints)
    if not hints:
        return None, None
    df = _ak_call_df(ak.stock_sector_spot, indicator="行业")
    row = _match_sina_industry_row(df, hints)
    if row is None:
        return None, None
    name_col = _df_col_by_hint(df, ("板块", "名称"), fallback_idx=1)
    chg_col = _df_col_by_hint(df, ("涨跌幅",), fallback_idx=4)
    if name_col is None or chg_col is None:
        return None, None
    name = _clean_text(row.get(name_col))
    return name or industry, _num_or_none(row.get(chg_col))


def _industry_change_pct(
    industry: str, concept_hints: list[str] | None = None
) -> tuple[str | None, float | None, str]:
    if not industry and not concept_hints:
        return None, None, "industry_spot"
    name, chg = _industry_change_pct_em(industry) if industry else (None, None)
    if chg is not None:
        return name or industry, chg, "industry_spot"
    name, chg = _industry_change_pct_sina(industry, concept_hints)
    if chg is not None:
        return name or industry, chg, "industry_spot_sina"
    return industry or None, None, "industry_spot"


def _text_has_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(k in low or k in text for k in keywords)


def _collect_event_texts(
    notices: list[dict[str, str]], news: list[dict[str, str]]
) -> list[str]:
    texts: list[str] = []
    for n in notices:
        texts.append(_clean_text(n.get("title")))
    for n in news:
        texts.append(_clean_text(n.get("title")))
        summary = _clean_text(n.get("summary"))
        if summary:
            texts.append(summary)
    return [t for t in texts if t]


def _describe_relative_pp(vs: float | None, *, baseline: str) -> str:
    if vs is None or not math.isfinite(vs):
        return ""
    av = abs(vs)
    if av < 0.15:
        return f"与{baseline}幅度几乎一致"
    if vs > 0:
        return f"强于{baseline}约 {vs:+.2f} 个百分点"
    return f"弱于{baseline}约 {vs:+.2f} 个百分点"


def _compose_detailed_explanation(
    *,
    direction: str,
    change_pct: float | None,
    index_name: str,
    index_chg: float | None,
    vs_index: float | None,
    industry_name: str | None,
    industry_chg: float | None,
    vs_industry: float | None,
    primary: str,
    notices: list[dict[str, str]],
    related_news: list[dict[str, str]],
    has_negative_event: bool,
    has_positive_event: bool,
    limit_up_note: str | None,
    limit_down_note: str | None,
    lhb_reason: str | None,
    intraday_events: list[str],
    fund_flow: dict[str, Any] | None,
    concept_hints: list[str] | None,
    industry: str = "",
) -> tuple[str, list[str]]:
    points: list[str] = []

    if change_pct is not None:
        if direction == "up":
            points.append(f"个股当日收涨 {change_pct:+.2f}%。")
        elif direction == "down":
            points.append(f"个股当日收跌 {change_pct:+.2f}%。")
        else:
            points.append(f"个股当日涨跌幅 {change_pct:+.2f}%，整体震荡。")
    else:
        points.append("未能获取个股涨跌幅。")

    ind_label = industry_name or industry.strip() or None
    if ind_label and industry_chg is not None:
        rel_ind = _describe_relative_pp(vs_industry, baseline=f"行业「{ind_label}」")
        if direction == "up" and industry_chg > 0.3:
            if vs_industry is not None and abs(vs_industry) <= 1.5:
                points.append(
                    f"所属行业「{ind_label}」同期涨 {industry_chg:+.2f}%，"
                    f"个股与板块同向走强且幅度接近（{rel_ind}），"
                    f"上涨很大程度受板块整体带动。"
                )
            elif vs_industry is not None and vs_industry >= 2.0:
                points.append(
                    f"行业「{ind_label}」涨 {industry_chg:+.2f}%，"
                    f"个股超行业约 {vs_industry:+.2f} 个百分点，"
                    f"在板块上行基础上还有个股层面的额外弹性或催化。"
                )
            elif vs_industry is not None and vs_industry <= -2.0:
                points.append(
                    f"行业「{ind_label}」涨 {industry_chg:+.2f}%，"
                    f"但个股弱于板块约 {abs(vs_industry):.2f} 个百分点，"
                    f"板块虽强但该股跟涨乏力，需关注个股自身因素。"
                )
            else:
                points.append(
                    f"行业「{ind_label}」涨 {industry_chg:+.2f}%，{rel_ind}，"
                    f"板块环境偏暖，对个股上涨有支撑。"
                )
        elif direction == "down" and industry_chg < -0.3:
            if vs_industry is not None and abs(vs_industry) <= 1.5:
                points.append(
                    f"所属行业「{ind_label}」同期跌 {industry_chg:+.2f}%，"
                    f"个股与板块同步走弱（{rel_ind}），"
                    f"下跌更可能受板块整体拖累。"
                )
            elif vs_industry is not None and vs_industry <= -2.0:
                points.append(
                    f"行业「{ind_label}」跌 {industry_chg:+.2f}%，"
                    f"个股跌幅显著大于行业（超行业 {vs_industry:+.2f}pp），"
                    f"除板块因素外或还有公司/资金层面的额外压力。"
                )
            elif vs_industry is not None and vs_industry >= 2.0:
                points.append(
                    f"行业「{ind_label}」跌 {industry_chg:+.2f}%，"
                    f"个股相对抗跌（{rel_ind}），"
                    f"板块走弱但该股跌幅小于同业。"
                )
            else:
                points.append(
                    f"行业「{ind_label}」跌 {industry_chg:+.2f}%，{rel_ind}，"
                    f"板块环境偏弱，对个股形成下行压力。"
                )
        elif direction == "up" and industry_chg <= -0.3:
            points.append(
                f"行业「{ind_label}」跌 {industry_chg:+.2f}%，"
                f"个股却逆势收涨（{rel_ind}），"
                f"上涨更可能来自个股独立催化或资金聚焦，而非板块带动。"
            )
        elif direction == "down" and industry_chg >= 0.3:
            points.append(
                f"行业「{ind_label}」涨 {industry_chg:+.2f}%，"
                f"个股却逆势走弱（{rel_ind}），"
                f"下跌更可能来自公司自身或个股资金面的独立利空。"
            )
        else:
            points.append(
                f"行业「{ind_label}」涨跌幅 {industry_chg:+.2f}%，{rel_ind}。"
            )
    elif ind_label:
        points.append(f"未能拉取行业「{ind_label}」当日涨跌幅，板块联动暂无法量化。")
    elif concept_hints:
        points.append(
            "暂无匹配行业板块数据；可能关联题材："
            + "、".join(concept_hints[:4])
            + "（题材关联非因果认定）。"
        )

    if index_chg is not None:
        rel_idx = _describe_relative_pp(vs_index, baseline=index_name or "大盘")
        if direction == "up":
            if vs_index is not None and vs_index >= 2.0:
                points.append(
                    f"{index_name}涨 {index_chg:+.2f}%，"
                    f"个股明显强于大盘（{rel_idx}），"
                    f"表现具备独立强势特征。"
                )
            elif vs_index is not None and vs_index <= -1.0:
                points.append(
                    f"{index_name}涨 {index_chg:+.2f}%，"
                    f"个股弱于大盘（{rel_idx}），"
                    f"虽收涨但跑输市场平均水平。"
                )
            elif index_chg >= 0.3 and vs_index is not None and abs(vs_index) <= 1.0:
                points.append(
                    f"{index_name}涨 {index_chg:+.2f}%，"
                    f"个股与大盘共振（{rel_idx}），"
                    f"存在系统性行情带动可能。"
                )
            else:
                points.append(f"{index_name}涨 {index_chg:+.2f}%，{rel_idx}。")
        elif direction == "down":
            if vs_index is not None and vs_index <= -2.0:
                points.append(
                    f"{index_name}跌 {index_chg:+.2f}%，"
                    f"个股跌幅显著大于大盘（{rel_idx}），"
                    f"可能存在个股或资金层面的额外抛压。"
                )
            elif index_chg <= -0.3 and vs_index is not None and abs(vs_index) <= 1.0:
                points.append(
                    f"{index_name}跌 {index_chg:+.2f}%，"
                    f"个股与大盘同步走弱（{rel_idx}），"
                    f"存在系统性拖累可能。"
                )
            elif index_chg >= 0.3:
                points.append(
                    f"{index_name}涨 {index_chg:+.2f}%，"
                    f"大盘收涨但个股下跌（{rel_idx}），"
                    f"更偏个股独立利空或资金撤离。"
                )
            else:
                points.append(f"{index_name}涨跌幅 {index_chg:+.2f}%，{rel_idx}。")
        else:
            points.append(f"{index_name}涨跌幅 {index_chg:+.2f}%，{rel_idx}。")
    elif index_name:
        points.append(f"未能拉取{index_name}涨跌幅，大盘对比暂缺。")

    event_bits: list[str] = []
    if notices:
        titles = [_clean_text(n.get("title")) for n in notices[:2]]
        titles = [t for t in titles if t]
        if titles:
            event_bits.append(f"当日公告 {len(notices)} 条（如「{titles[0]}」）")
        else:
            event_bits.append(f"当日公告 {len(notices)} 条")
    if related_news:
        titles = [_clean_text(n.get("title")) for n in related_news[:2]]
        titles = [t for t in titles if t]
        if titles:
            event_bits.append(f"相关新闻 {len(related_news)} 条（如「{titles[0]}」）")
        else:
            event_bits.append(f"相关新闻 {len(related_news)} 条")
    if has_negative_event and direction == "down":
        event_bits.append("公告/新闻标题含业绩下滑、监管、减持等偏空关键词")
    elif has_positive_event and direction == "up":
        event_bits.append("公告/新闻标题含订单、预增、回购等偏暖关键词")
    if event_bits:
        points.append("公司层面：" + "；".join(event_bits) + "。")
    elif direction in ("up", "down"):
        points.append("当日未发现匹配的公司公告或相关新闻，公司自身事件线索较弱。")

    trade_bits: list[str] = []
    if limit_up_note:
        trade_bits.append(limit_up_note)
    if limit_down_note:
        trade_bits.append(limit_down_note)
    if lhb_reason:
        trade_bits.append(f"龙虎榜：{lhb_reason}")
    if intraday_events:
        trade_bits.append("盘口异动：" + "、".join(intraday_events[:3]))
    if trade_bits:
        points.append("交易层线索：" + "；".join(trade_bits) + "。")

    ff = fund_flow or {}
    if ff.get("available"):
        main_net = ff.get("main_net_inflow")
        main_ratio = ff.get("main_net_ratio_pct")
        ff_note = _fund_flow_note(change_pct, main_net, main_ratio)
        if ff_note:
            points.append("资金面：" + ff_note)
    elif ff.get("note"):
        points.append("资金面：" + str(ff.get("note")))

    conclusion_map = {
        "sector": (
            "综合来看，涨跌与所属行业/板块联动最为密切，宜优先关注板块景气与同业走势。"
            if direction != "flat"
            else "板块联动可能是主要观察方向。"
        ),
        "market": (
            "综合来看，涨跌与大盘/系统性环境方向接近，宜结合指数与宏观情绪理解。"
            if direction != "flat"
            else "大盘环境可能是主要观察方向。"
        ),
        "company": (
            "综合来看，公司公告/新闻或个股相对同业明显偏离，宜优先核查公司自身信息与基本面。"
            if direction != "flat"
            else "公司自身因素值得优先关注。"
        ),
        "sentiment": (
            "综合来看，缺少明确公司事件，但资金博弈、龙虎榜或盘口异动等交易层信号较突出，"
            "宜结合量价与题材热度理解，勿简单等同于基本面变化。"
            if direction != "flat"
            else "交易层与资金信号可能是主要线索。"
        ),
        "mixed": "综合来看，板块、大盘、公司与资金多条线索交织，建议对照上文各点综合判断，勿单因子定论。",
        "unknown": "综合来看，各维度线索均不显著，建议结合后续公告与盘面再确认。",
    }
    points.append(conclusion_map.get(primary, conclusion_map["unknown"]))

    paragraph = " ".join(points)
    return paragraph, points


def _build_move_attribution(
    sym: str,
    *,
    change_pct: float | None,
    direction: str,
    industry: str,
    quote_source: str | None,
    notices: list[dict[str, str]],
    related_news: list[dict[str, str]],
    limit_down_note: str | None,
    limit_up_note: str | None,
    lhb_reason: str | None,
    intraday_events: list[str],
    fund_flow: dict[str, Any] | None = None,
    concept_hints: list[str] | None = None,
) -> dict[str, Any]:
    idx_code, idx_default_name = _benchmark_index_for_sym(sym)
    index_name, index_chg, index_source = _index_change_pct(idx_code, idx_default_name)
    if not index_name:
        index_name = idx_default_name
    industry_name, industry_chg, industry_source = _industry_change_pct(
        industry, concept_hints
    )

    vs_index: float | None = None
    vs_industry: float | None = None
    if change_pct is not None and index_chg is not None:
        vs_index = round(change_pct - index_chg, 2)
    if change_pct is not None and industry_chg is not None:
        vs_industry = round(change_pct - industry_chg, 2)

    event_texts = _collect_event_texts(notices, related_news)
    has_notice = bool(notices)
    has_news = bool(related_news)
    has_negative_event = any(_text_has_keywords(t, _COMPANY_NEGATIVE_KW) for t in event_texts)
    has_positive_event = any(_text_has_keywords(t, _COMPANY_POSITIVE_KW) for t in event_texts)

    scores: dict[str, int] = {
        "company": 0,
        "sector": 0,
        "market": 0,
        "sentiment": 0,
    }
    factors: list[dict[str, str]] = []

    stock_meta = _source_meta("live_quote")
    comparison_sources: list[dict[str, Any]] = [
        {
            "key": "stock",
            "label": stock_meta["label"],
            "api": quote_source or stock_meta["api"],
            "detail": "个股涨跌幅",
            "available": change_pct is not None,
        },
    ]

    if change_pct is not None and index_chg is not None:
        _add_factor(
            factors,
            source=index_source,
            kind="data",
            text=f"基准指数 {index_name} 涨跌幅 {index_chg:+.2f}%，个股相对大盘 {vs_index:+.2f} 个百分点",
        )
        if direction == "down":
            if index_chg <= -0.3:
                scores["market"] += 2
                if vs_index is not None and vs_index >= -0.8:
                    scores["market"] += 2
                    _add_factor(
                        factors,
                        source="local_rule",
                        kind="rule",
                        text="个股跌幅与大盘方向一致且幅度接近，存在系统性拖累可能",
                    )
                elif vs_index is not None and vs_index <= -2.0:
                    scores["company"] += 1
                    scores["sentiment"] += 1
                    _add_factor(
                        factors,
                        source="local_rule",
                        kind="rule",
                        text="个股明显弱于大盘，可能存在个股或资金层面额外压力",
                    )
            elif index_chg >= 0.3 and change_pct <= -1.0:
                scores["company"] += 2
                scores["sentiment"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="大盘收涨但个股走弱，更偏个股或资金因素",
                )
        elif direction == "up":
            if index_chg >= 0.3 and vs_index is not None and vs_index <= 0.8:
                scores["market"] += 2
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="个股涨幅与大盘共振，存在系统性带动可能",
                )
            elif vs_index is not None and vs_index >= 2.0:
                scores["company"] += 1
                scores["sentiment"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="个股明显强于大盘，可能存在个股催化或资金聚焦",
                )
    elif change_pct is not None:
        _add_factor(
            factors,
            source=index_source,
            kind="data",
            text=f"未能拉取基准指数 {index_name} 涨跌幅（接口可能限流或不可用）",
        )

    idx_meta = _source_meta(index_source)
    comparison_sources.append(
        {
            "key": "index",
            "label": idx_meta["label"],
            "api": idx_meta["api"],
            "detail": index_name,
            "available": index_chg is not None,
        }
    )

    if industry_name and industry_chg is not None and vs_industry is not None:
        _add_factor(
            factors,
            source=industry_source,
            kind="data",
            text=f"行业板块「{industry_name}」涨跌幅 {industry_chg:+.2f}%，个股相对行业 {vs_industry:+.2f} 个百分点",
        )
        if direction == "down":
            if industry_chg is not None and industry_chg <= -0.5 and change_pct is not None and change_pct <= -0.3:
                if vs_industry is not None and abs(vs_industry) <= 1.5:
                    scores["sector"] += 4
                    _add_factor(
                        factors,
                        source="local_rule",
                        kind="rule",
                        text="行业板块明显走弱且个股跌幅与板块接近，板块拖累特征显著",
                    )
            elif industry_chg <= -0.3 and vs_industry >= -1.0:
                scores["sector"] += 3
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="行业板块同步走弱，个股或受板块拖累",
                )
            elif vs_industry <= -2.0:
                scores["company"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="个股跌幅显著大于行业，可能存在公司层面额外因素",
                )
        elif direction == "up":
            if industry_chg is not None and industry_chg >= 0.5 and change_pct is not None and change_pct >= 0.3:
                if vs_industry is not None and abs(vs_industry) <= 1.5:
                    scores["sector"] += 4
                    _add_factor(
                        factors,
                        source="local_rule",
                        kind="rule",
                        text="行业板块明显走强且个股涨幅与板块接近，板块带动特征显著",
                    )
                elif vs_industry is not None and vs_industry >= 2.0:
                    scores["company"] += 2
                    scores["sector"] += 1
                    _add_factor(
                        factors,
                        source="local_rule",
                        kind="rule",
                        text="板块上行但个股显著超行业，存在个股额外催化",
                    )
            elif industry_chg >= 0.3 and vs_industry is not None and vs_industry <= 1.0:
                scores["sector"] += 2
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="行业板块同步走强，个股或受益板块景气",
                )
            elif vs_industry >= 2.0:
                scores["company"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="个股涨幅显著大于行业，可能存在公司层面催化",
                )
    elif industry or concept_hints:
        label = industry.strip() if industry else "、".join((concept_hints or [])[:3])
        _add_factor(
            factors,
            source=industry_source,
            kind="data",
            text=f"未能匹配行业板块「{label}」的涨跌幅（名称不一致或接口不可用）",
        )

    ind_meta = _source_meta(industry_source)
    comparison_sources.append(
        {
            "key": "industry",
            "label": ind_meta["label"],
            "api": ind_meta["api"],
            "detail": industry_name or industry or None,
            "available": industry_chg is not None,
        }
    )

    if has_notice:
        scores["company"] += 4
        _add_factor(
            factors,
            source="eastmoney_notice",
            kind="data",
            text=f"当日有 {len(notices)} 条公司公告，优先关注公司自身事件",
        )
    if has_news:
        scores["company"] += 2
        _add_factor(
            factors,
            source="eastmoney_news",
            kind="data",
            text=f"当日有相关新闻 {len(related_news)} 条",
        )
    if has_negative_event and direction == "down":
        scores["company"] += 3
        _add_factor(
            factors,
            source="local_rule",
            kind="rule",
            text="公告/新闻标题含业绩、监管、减持等偏空关键词（本地关键词匹配）",
        )
    elif has_positive_event and direction == "up":
        scores["company"] += 3
        _add_factor(
            factors,
            source="local_rule",
            kind="rule",
            text="公告/新闻标题含订单、预增、回购等偏暖关键词（本地关键词匹配）",
        )

    if limit_down_note:
        scores["sentiment"] += 2
        if not has_notice and not has_news:
            scores["sentiment"] += 2
        _add_factor(
            factors,
            source="eastmoney_dt_pool",
            kind="data",
            text="命中跌停股池，通常伴随集中抛售或情绪冲击",
        )
    if limit_up_note:
        scores["sentiment"] += 1
        _add_factor(
            factors,
            source="eastmoney_zt_pool",
            kind="data",
            text="命中涨停股池，通常伴随资金集中博弈",
        )

    if lhb_reason and not has_notice:
        scores["sentiment"] += 2
        _add_factor(
            factors,
            source="eastmoney_lhb",
            kind="data",
            text=f"龙虎榜上榜：{lhb_reason}",
        )

    if intraday_events and not has_notice and not has_news:
        scores["sentiment"] += 1
        _add_factor(
            factors,
            source="eastmoney_changes",
            kind="data",
            text="主要线索为盘口异动，暂无明确公司事件披露",
        )

    ff = fund_flow or {}
    ff_source = str(ff.get("source_key") or "eastmoney_fund_flow")
    if ff.get("available"):
        main_net = ff.get("main_net_inflow")
        main_ratio = ff.get("main_net_ratio_pct")
        ff_chg = ff.get("change_pct") if ff.get("change_pct") is not None else change_pct
        net_txt = _fmt_money_yuan(main_net)
        ratio_txt = f"{main_ratio:.2f}%" if main_ratio is not None else "—"
        ff_text = f"主力净流入 {net_txt}（占成交额 {ratio_txt}）"
        if ff_source == "eastmoney_big_deal_flow":
            ff_text = ff.get("note") or f"大单净买入 {net_txt}"
        _add_factor(
            factors,
            source=ff_source,
            kind="data",
            text=ff_text,
        )
        if ff_chg is not None and main_net is not None:
            if ff_chg > 0.3 and main_net > 0:
                scores["sentiment"] += 2
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="收涨且主力净流入，资金方向与股价一致",
                )
            elif ff_chg < -0.3 and main_net < 0:
                scores["sentiment"] += 2
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="收跌且主力净流出，资金方向与股价一致",
                )
            elif ff_chg > 0.3 and main_net < 0:
                scores["sentiment"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="收涨但主力净流出，价量资金存在分歧",
                )
            elif ff_chg < -0.3 and main_net > 0:
                scores["sentiment"] += 1
                _add_factor(
                    factors,
                    source="local_rule",
                    kind="rule",
                    text="收跌但主力净流入，或有逆势吸纳/护盘资金",
                )
    elif ff.get("note"):
        _add_factor(
            factors,
            source=ff_source,
            kind="data",
            text=str(ff.get("note")),
        )

    if change_pct is None and index_chg is None and industry_chg is None:
        primary = "unknown"
        confidence = "low"
        explanation = "缺少个股与大盘/行业涨跌幅，暂无法做相对归因。"
        explanation_points: list[str] = [explanation]
    else:
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        top_key, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0
        active = [k for k, v in scores.items() if v >= 2]
        if top_score < 2:
            primary = "unknown"
            confidence = "low"
        elif len(active) >= 2 and top_score - second_score <= 1:
            primary = "mixed"
            confidence = "medium" if top_score >= 4 else "low"
        else:
            primary = top_key
            confidence = (
                "high"
                if top_score >= 6 and top_score - second_score >= 3
                else ("medium" if top_score >= 4 else "low")
            )
        explanation, explanation_points = _compose_detailed_explanation(
            direction=direction,
            change_pct=change_pct,
            index_name=index_name or "",
            index_chg=index_chg,
            vs_index=vs_index,
            industry_name=industry_name,
            industry_chg=industry_chg,
            vs_industry=vs_industry,
            primary=primary,
            notices=notices,
            related_news=related_news,
            has_negative_event=has_negative_event,
            has_positive_event=has_positive_event,
            limit_up_note=limit_up_note,
            limit_down_note=limit_down_note,
            lhb_reason=lhb_reason,
            intraday_events=intraday_events,
            fund_flow=ff if ff else fund_flow,
            concept_hints=concept_hints,
            industry=industry,
        )
        if top_score < 2 and not explanation_points:
            explanation = "相对大盘/行业与事件线索均不明显，暂无法给出倾向性归因。"
            explanation_points = [explanation]

    return {
        "primary": primary,
        "primary_label": _ATTRIBUTION_LABELS.get(primary, "暂无法判断"),
        "confidence": confidence,
        "explanation": explanation,
        "explanation_points": explanation_points,
        "factors": factors,
        "comparison_sources": comparison_sources,
        "index_name": index_name,
        "index_change_pct": index_chg,
        "industry_name": industry_name,
        "industry_change_pct": industry_chg,
        "vs_index_pp": vs_index,
        "vs_industry_pp": vs_industry,
        "score_breakdown": scores,
    }


def _build_move_interpretation(
    sym: str,
    *,
    quote: dict[str, Any],
    news: list[dict[str, str]],
    concepts: list[dict[str, str]],
    industry: str = "",
) -> dict[str, Any]:
    trade_display, trade_compact = _trade_dates_from_quote(quote)
    change_pct = _num_or_none(quote.get("change_pct"))
    direction = _move_direction(change_pct)
    highlights: list[dict[str, str]] = []

    zt = _fetch_zt_pool_row(sym, trade_compact)
    limit_up_note = zt["note"] if zt else None
    if limit_up_note:
        _add_highlight(
            highlights,
            source="eastmoney_zt_pool",
            category="limit_up",
            text=limit_up_note,
        )

    dt = _fetch_dt_pool_row(sym, trade_compact)
    limit_down_note = dt["note"] if dt else None
    if limit_down_note:
        _add_highlight(
            highlights,
            source="eastmoney_dt_pool",
            category="limit_down",
            text=limit_down_note,
        )

    strong_reason = _fetch_strong_pool_row(sym, trade_compact)
    if strong_reason:
        _add_highlight(
            highlights,
            source="eastmoney_strong_pool",
            category="strong",
            text=f"强势股池入选理由：{strong_reason}",
        )

    lhb = _fetch_lhb_day(sym, trade_compact)
    lhb_reason = lhb.get("reason") if lhb else None
    lhb_note = lhb.get("note") if lhb else None
    if lhb_reason:
        _add_highlight(
            highlights,
            source="eastmoney_lhb",
            category="lhb",
            text=f"龙虎榜上榜原因：{lhb_reason}",
        )
    if lhb_note:
        _add_highlight(
            highlights,
            source="eastmoney_lhb",
            category="lhb",
            text=f"龙虎榜解读：{lhb_note}",
        )

    notices = _fetch_notices_day(sym, trade_compact)
    for n in notices:
        _add_highlight(
            highlights,
            source="eastmoney_notice",
            category="notice",
            text=f"公告：{n['title']}",
        )

    related_news = _news_for_trade_date(news, trade_display)
    for n in related_news:
        title = _clean_text(n.get("title"))
        if title:
            _add_highlight(
                highlights,
                source="eastmoney_news",
                category="news",
                text=f"新闻：{title}",
            )

    intraday_events = _fetch_intraday_events(sym, direction)
    for ev in intraday_events:
        _add_highlight(
            highlights,
            source="eastmoney_changes",
            category="intraday",
            text=f"盘口异动：{ev}",
        )

    concept_hints = [_clean_text(c.get("name")) for c in concepts[:6] if _clean_text(c.get("name"))]
    if concept_hints and direction != "flat":
        _add_highlight(
            highlights,
            source="f10_concepts",
            category="concept",
            text="可能关联题材（非因果）：" + "、".join(concept_hints[:5]),
        )

    summary_parts: list[str] = []
    if change_pct is not None:
        if direction == "up":
            summary_parts.append(f"{trade_display} 收涨 {change_pct:.2f}%")
        elif direction == "down":
            summary_parts.append(f"{trade_display} 收跌 {abs(change_pct):.2f}%")
        else:
            summary_parts.append(f"{trade_display} 涨跌幅 {change_pct:+.2f}%（震荡）")
    else:
        summary_parts.append(f"行情日 {trade_display}")

    if limit_up_note:
        summary_parts.append(limit_up_note)
    elif limit_down_note:
        summary_parts.append(limit_down_note)
    elif strong_reason:
        summary_parts.append(f"强势股池：{strong_reason}")
    elif lhb_reason:
        summary_parts.append(f"龙虎榜：{lhb_reason}")
    elif notices:
        summary_parts.append(f"当日公告 {len(notices)} 条")
    elif related_news:
        summary_parts.append(f"当日相关新闻 {len(related_news)} 条")
    elif intraday_events:
        summary_parts.append("命中盘口异动：" + "、".join(intraday_events[:3]))
    else:
        summary_parts.append(
            "未命中涨停/跌停/龙虎榜等结构化标签，请结合公告、新闻与题材线索自行判断"
        )

    fund_flow = _fetch_fund_flow_day(sym, trade_display)
    event_timeline = _build_event_timeline(sym, trade_display, news, days=3)
    if fund_flow.get("available") and fund_flow.get("note"):
        _add_highlight(
            highlights,
            source=str(fund_flow.get("source_key") or "eastmoney_fund_flow"),
            category="fund_flow",
            text=fund_flow["note"],
        )

    attribution = _build_move_attribution(
        sym,
        change_pct=change_pct,
        direction=direction,
        industry=industry,
        quote_source=quote.get("quote_source"),
        notices=notices,
        related_news=related_news,
        limit_down_note=limit_down_note,
        limit_up_note=limit_up_note,
        lhb_reason=lhb_reason,
        intraday_events=intraday_events,
        fund_flow=fund_flow,
        concept_hints=concept_hints,
    )
    if attribution.get("primary") not in (None, "unknown"):
        summary_parts.append(f"归因（Demo）：{attribution.get('primary_label')}")

    return {
        "trade_date": trade_display,
        "change_pct": change_pct,
        "direction": direction,
        "summary": "；".join(summary_parts),
        "highlights": highlights,
        "limit_up_note": limit_up_note,
        "limit_down_note": limit_down_note,
        "strong_pool_reason": strong_reason,
        "lhb_reason": lhb_reason,
        "lhb_note": lhb_note,
        "intraday_events": intraday_events,
        "related_notices": notices,
        "related_news": related_news,
        "concept_hints": concept_hints,
        "attribution": attribution,
        "fund_flow": fund_flow,
        "event_timeline": event_timeline,
        "disclaimer": (
            "以下为公开数据线索与相对强弱 Demo 规则推断，可能关联因素而非官方认定的涨跌原因；"
            "请结合公告原文与盘面自行判断。"
        ),
    }


def build_stock_brief(sym: str, *, news_limit: int = 8) -> dict[str, Any]:
    """聚合单只 A 股速览；各块失败时对应字段为空，不阻断整体响应。"""
    sym_n = normalize_symbol(sym)
    f10 = f10_market_code(sym_n)
    name = fetch_stock_name(sym_n) or ""
    info = _parse_individual_info(sym_n)
    if not name:
        name = info.get("股票简称") or info.get("证券简称") or ""

    profile = _fetch_company_profile(f10)
    if not profile.get("company_name"):
        profile["company_name"] = name
    if not profile.get("industry"):
        profile["industry"] = info.get("行业") or info.get("所属行业") or ""
    if not profile.get("main_business"):
        profile["main_business"] = info.get("主营业务") or info.get("经营范围") or ""

    concepts = _fetch_concepts(f10)
    segments = _fetch_main_business_segments(f10)
    news = _fetch_news(sym_n, limit=max(news_limit, 20))
    quote = _fetch_quote(sym_n)
    revenue = _build_revenue_summary(sym_n)
    fin_rows = _fetch_financial_indicator_rows(sym_n, limit=2)
    fin_cur = fin_rows[0] if fin_rows else None
    fin_prev = fin_rows[1] if len(fin_rows) > 1 else None
    financial_quality = _fetch_financial_quality(
        sym_n, revenue, row=fin_cur, prev_row=fin_prev
    )
    shareholders = _fetch_shareholders(sym_n, f10)
    valuation_compare = _fetch_valuation_compare(sym_n)
    risk = _build_risk_flags(
        name=name or sym_n,
        sym=sym_n,
        fin={
            **revenue,
            "eps": financial_quality.get("eps"),
            "ocf_per_share": financial_quality.get("ocf_per_share"),
        },
        shareholders=shareholders,
        valuation=valuation_compare,
        gross_margin_chg_pp=financial_quality.get("gross_margin_chg_pp"),
    )
    move_interpretation = _build_move_interpretation(
        sym_n,
        quote=quote,
        news=news,
        concepts=concepts,
        industry=profile.get("industry") or info.get("行业") or info.get("所属行业") or "",
    )

    warnings: list[str] = []
    if not concepts:
        warnings.append("未能拉取相关概念（东财核心题材接口可能限流或暂无数据）。")
    if not news:
        warnings.append("未能拉取近期新闻。")
    if revenue.get("revenue_yoy_pct") is None and revenue.get("profit_yoy_pct") is None:
        warnings.append("营收同比等指标缺失；可在「③ 更新行情」中拉取扩展因子后重试。")
    if not shareholders.get("top_holders"):
        warnings.append("未能拉取十大流通股东（可能报告期未披露或接口限流）。")

    return {
        "symbol": sym_n,
        "name": name,
        "secucode": em_seccode(sym_n),
        "quote": quote,
        "news": news,
        "concepts": concepts,
        "business": {
            "company_name": profile.get("company_name") or name,
            "industry": profile.get("industry") or "",
            "listing_date": profile.get("listing_date") or info.get("上市时间") or "",
            "main_business": profile.get("main_business") or "",
            "profile": profile.get("profile") or "",
            "segments": segments,
        },
        "revenue": revenue,
        "financial_quality": financial_quality,
        "shareholders": shareholders,
        "valuation_compare": valuation_compare,
        "risk": risk,
        "move_interpretation": move_interpretation,
        "warnings": warnings,
        "disclaimer": "以下为公开数据源聚合展示，仅供研究参考，不构成投资建议。",
    }
