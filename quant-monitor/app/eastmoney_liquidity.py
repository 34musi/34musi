"""
东财行情扩展：全 A 列表快照合并换手率/成交额/成交量，并与入库日线 volume 单位对齐。
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def coerce_volume_to_bar_unit(today_vol: float, ref_bar_vol: float) -> tuple[float, str | None]:
    """
    将「当日量」换算为与 bars 表 volume 同一量级（东财日线多为「手」，spot/bid 可能为手或股）。

    返回 (换算后 volume, 说明)；无法判断时原样返回。
    """
    if not math.isfinite(today_vol) or not math.isfinite(ref_bar_vol) or ref_bar_vol <= 0 or today_vol <= 0:
        return today_vol, None
    ratio = today_vol / ref_bar_vol
    if ratio > 50:
        return today_vol / 100.0, "spot_volume_scaled_div100"
    if ratio < 0.02:
        return today_vol * 100.0, "spot_volume_scaled_mul100"
    return today_vol, None


def merge_eastmoney_spot_into_row(
    row: dict[str, Any],
    spot: dict[str, Any],
    *,
    prefer_spot_volume: bool = True,
) -> None:
    """把东财 spot_liquidity 字段写入 ingest / live 行。"""
    if not spot:
        return
    tr = spot.get("spot_turnover_rate")
    if tr is not None and math.isfinite(float(tr)):
        row["spot_turnover_rate"] = round(float(tr), 4)
    amt = spot.get("spot_amount")
    if amt is not None and math.isfinite(float(amt)) and float(amt) > 0:
        row["spot_amount"] = round(float(amt), 2)
    if spot.get("spot_volume") is not None:
        row["spot_volume_raw"] = spot.get("spot_volume")
    if prefer_spot_volume:
        sv = spot.get("spot_volume")
        if sv is not None and math.isfinite(float(sv)) and float(sv) > 0:
            v = float(sv)
            ref = row.get("last_volume") or row.get("display_prev_volume")
            if ref is not None and math.isfinite(float(ref)) and float(ref) > 0:
                v, note = coerce_volume_to_bar_unit(v, float(ref))
                if note:
                    row["live_volume_scale_note"] = note
            row["live_volume"] = round(v, 4)
            row["live_volume_source"] = "eastmoney_spot_list"


def merge_eastmoney_spot_batch(
    rows_by_sym: dict[str, dict[str, Any]],
    codes: list[str],
    *,
    force_refresh: bool = False,
) -> None:
    """批量拉东财全 A 列表并合并到各行（仅东财路由或强制刷新时调用）。"""
    from app.fundamentals import spot_liquidity_fields_for_codes
    from app.ingest import normalize_symbol

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
    if not uniq:
        return
    try:
        spot_by = spot_liquidity_fields_for_codes(uniq, force_refresh=force_refresh)
    except Exception as e:
        logger.debug("merge_eastmoney_spot_batch: %s", e)
        return
    for sym in uniq:
        row = rows_by_sym.get(sym)
        if not row or row.get("error"):
            continue
        merge_eastmoney_spot_into_row(row, spot_by.get(sym) or {})
