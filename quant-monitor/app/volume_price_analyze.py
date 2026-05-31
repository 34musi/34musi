"""
③ 拉取结果「量」列：量价联合评价与危险量提示（非固定百科文案）。

- 量比主口径：当日量（盘中按交易进度折算全日）÷ 近 20 日均量
- 辅助口径：相对上一完整交易日全日量
- 结合当日涨跌幅给出对本股强弱的一句话评价
"""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_SH_TZ = ZoneInfo("Asia/Shanghai")
_TRADING_MINUTES = 240
_PRICE_FLAT_PCT = 0.25
_VOL_SHRINK = 0.8
_VOL_EXPAND = 1.2
_VOL_STRONG = 1.5
_VOL_EXTREME = 2.2
_VOL_PANIC = 1.8


def shanghai_trading_elapsed_minutes() -> int:
    """A 股连续竞价累计分钟数（9:30–11:30、13:00–15:00，共 240 分钟）。"""
    now = datetime.now(_SH_TZ)
    if now.weekday() >= 5:
        return 0
    t = now.time()
    if t < time(9, 30):
        return 0
    if t <= time(11, 30):
        base = datetime.combine(now.date(), time(9, 30), tzinfo=_SH_TZ)
        cur = datetime.combine(now.date(), t, tzinfo=_SH_TZ)
        return max(0, int((cur - base).total_seconds() // 60))
    if t < time(13, 0):
        return 120
    if t <= time(15, 0):
        base = datetime.combine(now.date(), time(13, 0), tzinfo=_SH_TZ)
        cur = datetime.combine(now.date(), t, tzinfo=_SH_TZ)
        return 120 + max(0, int((cur - base).total_seconds() // 60))
    return _TRADING_MINUTES


def _price_side(chg_pct: float | None) -> str:
    if chg_pct is None or not math.isfinite(chg_pct):
        return "unknown"
    if chg_pct > _PRICE_FLAT_PCT:
        return "up"
    if chg_pct < -_PRICE_FLAT_PCT:
        return "down"
    return "flat"


def _vol_label_from_ratio(ratio: float) -> str:
    if ratio < _VOL_SHRINK:
        return "缩量"
    if ratio > _VOL_EXPAND:
        return "放量"
    return "平量"


def _avg_vol_20_from_row(row: dict[str, Any]) -> float | None:
    for key in ("spot_strength", "prev_strength", "strength"):
        st = row.get(key)
        if isinstance(st, dict):
            v = st.get("avg_volume_20")
            if v is not None and math.isfinite(float(v)) and float(v) > 0:
                return float(v)
    return None


def _avg_amount_20_100m_from_row(row: dict[str, Any]) -> float | None:
    for key in ("spot_strength", "prev_strength", "strength"):
        st = row.get(key)
        if isinstance(st, dict):
            v = st.get("avg_amount_20d_100m")
            if v is not None and math.isfinite(float(v)) and float(v) > 0:
                return float(v)
    return None


def _normalize_today_volume(row: dict[str, Any], today_vol: float) -> float:
    ref = row.get("last_volume") or row.get("display_prev_volume")
    if ref is None or not math.isfinite(float(ref)) or float(ref) <= 0:
        return today_vol
    try:
        from app.eastmoney_liquidity import coerce_volume_to_bar_unit

        v, note = coerce_volume_to_bar_unit(float(today_vol), float(ref))
        if note:
            row["live_volume_scale_note"] = note
        return v
    except Exception:
        return today_vol


def _price_chg_pct(row: dict[str, Any]) -> float | None:
    for k in ("live_change_pct", "spot_change_pct"):
        v = row.get(k)
        if v is not None and math.isfinite(float(v)):
            return float(v)
    px = row.get("live_last_price") or row.get("spot_last_price")
    ref = row.get("display_today_ref_close") or row.get("last_close")
    if (
        px is not None
        and ref is not None
        and math.isfinite(float(px))
        and math.isfinite(float(ref))
        and float(ref) > 0
    ):
        return round((float(px) / float(ref) - 1) * 100, 2)
    return None


def _compose_hint(
    *,
    side: str,
    vol_label: str,
    ratio_ma20: float,
    ratio_yesterday: float | None,
    intraday: bool,
    elapsed: int,
    turnover_rate: float | None = None,
    main_net_inflow: float | None = None,
) -> tuple[str, bool, str | None]:
    """返回 (主评价, 是否危险量, 危险说明)。"""
    danger = False
    danger_hint: str | None = None
    r = ratio_ma20
    ry = ratio_yesterday

    if side == "up":
        if r >= _VOL_EXTREME and ratio_ma20 > 0:
            hint = "价涨巨量：短线情绪过热，警惕冲高回落或利好兑现"
            danger = True
            danger_hint = "危险量：急涨伴随异常放量"
        elif r >= _VOL_STRONG:
            hint = "价涨放量：多头占优，趋势延续或突破有量能配合"
        elif vol_label == "缩量":
            hint = "价涨量缩：上攻动能偏弱或筹码锁定，追高宜谨慎"
            if r < 0.7:
                danger = True
                danger_hint = "危险量：价涨量缩，易冲高回落"
        elif vol_label == "平量":
            hint = "价涨平量：上涨中量能一般，持续性需后续放量确认"
        else:
            hint = "价涨小幅放量：偏多但需观察能否维持"
    elif side == "down":
        if r >= _VOL_PANIC:
            hint = "价跌放量：抛压显著，不宜盲目抄底"
            danger = True
            danger_hint = "危险量：价跌放量，恐慌或主力出货"
        elif vol_label == "放量":
            hint = "价跌量增：空头占优，等待缩量企稳再考虑"
        elif vol_label == "缩量":
            hint = "价跌量缩：杀跌动能减弱，但未反转前宜观望"
        elif vol_label == "平量":
            hint = "价跌平量：弱势整理，未见恐慌盘集中涌出"
        else:
            hint = "价跌量能中性：弱势震荡，等待方向"
    elif side == "flat":
        if r >= _VOL_STRONG:
            hint = "价平放量：多空分歧加大，即将选择方向"
        elif vol_label == "缩量":
            hint = "价平缩量：观望浓厚，突破需放量配合"
        else:
            hint = "价平量平：震荡蓄势，量价均未给出明确方向"
    else:
        if vol_label == "放量":
            hint = "放量但涨跌不明：留意随后价格方向"
        elif vol_label == "缩量":
            hint = "缩量且涨跌不明：交投清淡，等待变盘"
        else:
            hint = "量价均平淡：暂无明确强弱信号"

    if ry is not None and math.isfinite(ry):
        if side == "down" and ry >= _VOL_EXTREME:
            danger = True
            danger_hint = danger_hint or "危险量：相对昨日全日出现巨量下跌"
        if side == "up" and ry < 0.65 and r < 0.85:
            danger = True
            danger_hint = danger_hint or "危险量：价涨但量能远弱于昨日，上攻可信度低"

    if turnover_rate is not None and math.isfinite(turnover_rate):
        if turnover_rate >= 15 and side == "up":
            danger = True
            danger_hint = danger_hint or "危险量：换手率偏高且上涨，警惕短线过热"
        elif turnover_rate >= 12 and side == "down":
            danger = True
            danger_hint = danger_hint or "危险量：高换手下跌，抛压活跃"
        hint += f"（东财换手≈{turnover_rate:.2f}%）"
    if main_net_inflow is not None and math.isfinite(main_net_inflow):
        if main_net_inflow < 0 and vol_label == "放量" and side == "down":
            danger = True
            danger_hint = danger_hint or "危险量：放量下跌且主力净流入为负"
        elif main_net_inflow > 0 and side == "up" and ratio_ma20 >= _VOL_STRONG:
            hint += "；主力净流入为正"

    prefix = f"量比(估)≈{ratio_ma20:.2f}（相对20日均量"
    if turnover_rate is not None and math.isfinite(turnover_rate):
        prefix += f"，换手≈{turnover_rate:.2f}%"
    prefix += "）"
    if intraday and elapsed > 0:
        pct_done = min(100, int(elapsed / _TRADING_MINUTES * 100))
        prefix += f" · 盘中已交易约{pct_done}%时段"
    return f"{prefix}：{hint}", danger, danger_hint


def analyze_ingest_volume_price(row: dict[str, Any]) -> None:
    """
    写入 row：volume_vs_prev_*、volume_ratio_vs_ma20、volume_danger、volume_danger_hint 等。
    """
    exec_d = str(row.get("ingest_exec_date") or "")[:10]
    last_td = str(row.get("last_trade_date") or "")[:10]

    ref_vol = row.get("display_prev_volume")
    if ref_vol is None:
        ref_vol = row.get("prev_volume")
    ref_f: float | None = None
    if ref_vol is not None and math.isfinite(float(ref_vol)) and float(ref_vol) > 0:
        ref_f = float(ref_vol)

    today_vol = row.get("live_volume")
    today_basis = "none"
    intraday = False
    if today_vol is not None and math.isfinite(float(today_vol)) and float(today_vol) > 0:
        today_basis = "live_cumulative"
        intraday = bool(exec_d and last_td and exec_d > last_td)
    elif exec_d and last_td and exec_d == last_td:
        lv = row.get("last_volume")
        if lv is not None and math.isfinite(float(lv)) and float(lv) > 0:
            today_vol = float(lv)
            today_basis = "last_close_full_day"
    row["display_today_volume"] = today_vol
    row["volume_today_basis"] = today_basis

    if ref_f is None or today_vol is None or float(today_vol) <= 0:
        for k in (
            "volume_vs_prev_label",
            "volume_vs_prev_ratio",
            "volume_vs_prev_pct",
            "volume_vs_prev_hint",
            "volume_ratio_vs_ma20",
            "volume_danger",
            "volume_danger_hint",
            "spot_turnover_rate",
        ):
            row.pop(k, None)
        return

    cur_f = _normalize_today_volume(row, float(today_vol))
    row["display_today_volume"] = cur_f
    elapsed = shanghai_trading_elapsed_minutes() if intraday else _TRADING_MINUTES
    projected = cur_f
    if intraday and elapsed >= 10:
        projected = cur_f / elapsed * _TRADING_MINUTES
    elif intraday and elapsed < 10:
        projected = cur_f  # 开盘初段不做外推，量比仅作粗参考

    ratio_yesterday = cur_f / ref_f if ref_f else None
    ratio_proj_yesterday = projected / ref_f if ref_f else None

    avg20 = _avg_vol_20_from_row(row)
    if avg20 and avg20 > 0:
        ratio_ma20 = projected / avg20
        ratio_basis = "vs_ma20_projected" if intraday else "vs_ma20_full_day"
    else:
        ratio_ma20 = ratio_proj_yesterday if ratio_proj_yesterday else ratio_yesterday or 1.0
        ratio_basis = "vs_prev_day_fallback"

    vol_label = _vol_label_from_ratio(ratio_ma20)
    pct_ma20 = (ratio_ma20 - 1.0) * 100.0
    chg = _price_chg_pct(row)
    side = _price_side(chg)

    turnover = row.get("spot_turnover_rate")
    tr_f: float | None = None
    if turnover is not None and math.isfinite(float(turnover)):
        tr_f = float(turnover)

    main_flow: float | None = None
    fund = row.get("fundamentals_snapshot") or row.get("fundamentals")
    if isinstance(fund, dict):
        inf = fund.get("main_net_inflow")
        if inf is not None and math.isfinite(float(inf)):
            main_flow = float(inf)
    elif fund is not None and hasattr(fund, "main_net_inflow"):
        inf = getattr(fund, "main_net_inflow", None)
        if inf is not None and math.isfinite(float(inf)):
            main_flow = float(inf)

    avg_amt = _avg_amount_20_100m_from_row(row)
    spot_amt = row.get("spot_amount")
    if spot_amt and avg_amt and avg_amt > 0:
        amt_ratio = float(spot_amt) / 1e8 / avg_amt
        row["amount_ratio_vs_ma20"] = round(amt_ratio, 4)

    hint, danger, danger_hint = _compose_hint(
        side=side,
        vol_label=vol_label,
        ratio_ma20=ratio_ma20,
        ratio_yesterday=ratio_proj_yesterday if intraday else ratio_yesterday,
        intraday=intraday,
        elapsed=elapsed,
        turnover_rate=tr_f,
        main_net_inflow=main_flow,
    )

    if intraday and elapsed < 10:
        hint += "（开盘初段，量比仅供参考）"
    elif intraday:
        hint += "（盘中按已交易时段折算全日量；收盘后以实际全日量为准）"

    row["volume_vs_prev_ratio"] = round(ratio_ma20, 4)
    row["volume_vs_prev_pct"] = round(pct_ma20, 2)
    row["volume_vs_prev_label"] = vol_label
    row["volume_vs_prev_hint"] = hint
    row["volume_ratio_vs_ma20"] = round(ratio_ma20, 4)
    row["volume_ratio_vs_prev_day"] = round(ratio_yesterday, 4) if ratio_yesterday else None
    row["volume_ratio_basis"] = ratio_basis
    row["volume_price_side"] = side
    row["volume_danger"] = danger
    row["volume_danger_hint"] = danger_hint
    if chg is not None:
        row["volume_price_chg_pct"] = chg
