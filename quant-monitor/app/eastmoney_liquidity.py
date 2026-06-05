"""
东财流动性字段合并：将全 A 列表快照中的换手率/成交额/成交量写入 ingest 与信号行。

## 功能作用

东财 `stock_zh_a_spot_em` 全 A 列表（由 `fundamentals.spot_liquidity_fields_for_codes`
拉取）提供当日换手率、成交额、成交量等流动性指标。本模块负责：

1. 把这些 spot 字段合并到 ingest 结果行或信号计算用的 dict 中；
2. 将 spot 成交量与入库日线 `volume` **单位对齐**（东财日线多为「手」，spot 可能是手或股）；
3. 批量场景下一次性拉表、逐行合并，减少重复请求。

典型调用方：

- `ingest.enrich_ingest_results_with_spot` — ③ 批量更新结果展示；
- `ingest._apply_spot_enrich_to_ingest_row` — 单只 enrich；
- `signals.compute_signal` — 信号计算时补充 `live_liquidity`；
- `volume_price_analyze._normalize_today_volume` — 量价分析前统一成交量单位。

仅在行情路线为 `eastmoney` / `akshare` / `auto` 时由调用方触发（其它路线无东财 spot 表）。

## 写入字段（merge 后）

| 字段 | 含义 |
|------|------|
| `spot_turnover_rate` | 换手率（%） |
| `spot_amount` | 成交额 |
| `spot_volume_raw` | spot 原始成交量（未换算） |
| `live_volume` | 与 bars 同单位的当日量（可能经 ×100 / ÷100 换算） |
| `live_volume_source` | 固定为 `eastmoney_spot_list` |
| `live_volume_scale_note` | 换算说明（`spot_volume_scaled_div100` / `mul100`） |

## 对外接口

| 函数 | 用途 |
|------|------|
| `coerce_volume_to_bar_unit` | 启发式判断 spot 量与日线 volume 是否差 100 倍，并换算 |
| `merge_eastmoney_spot_into_row` | 单只：把 spot dict 合并进目标 row（原地修改） |
| `merge_eastmoney_spot_batch` | 批量：拉东财全 A 表并合并到 `rows_by_sym` 各行 |

## 单位对齐说明

`coerce_volume_to_bar_unit(today_vol, ref_bar_vol)` 用当日 spot 量与最近一根 K 线 volume
的比值判断：

- 比值 > 50 → spot 可能是「股」，除以 100 转为「手」；
- 比值 < 0.02 → spot 可能是「手」而 bar 为「股」，乘以 100；
- 否则认为已同单位，不换算。

无法判断或数据无效时原样返回，`note` 为 None。
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def coerce_volume_to_bar_unit(today_vol: float, ref_bar_vol: float) -> tuple[float, str | None]:
    """
    将「当日 spot 成交量」换算为与 bars 表 `volume` 同一量级。

    东财日线 volume 多为「手」；全 A 列表 spot 的成交量字段可能是手或股，
    通过与最近一根 K 线 volume 的比值做启发式判断。

    参数:
        today_vol:   spot 侧当日成交量。
        ref_bar_vol: 参照用 K 线 volume（通常为 `last_volume` 或 `display_prev_volume`）。

    返回:
        (换算后 volume, 说明字符串)；无法判断或无效输入时原样返回 `(today_vol, None)`。
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
    """
    把东财 spot 流动性字段写入 ingest / 信号用的 row dict（原地修改）。

    参数:
        row:                目标行，应含 `last_volume` 或 `display_prev_volume` 供单位对齐。
        spot:               `spot_liquidity_fields_for_codes` 返回的单只 dict。
        prefer_spot_volume: 为 True 时用 spot 成交量填充 `live_volume` 并做单位换算。
    """
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
    """
    批量拉东财全 A 列表快照，并将流动性字段合并到 `rows_by_sym` 各 row。

    内部调用 `fundamentals.spot_liquidity_fields_for_codes`（与估值 spot 共用缓存）；
    跳过含 `error` 的行；拉取失败时仅打 debug 日志，不抛异常。

    参数:
        rows_by_sym:    symbol → ingest 结果行的映射。
        codes:          待处理的代码列表（会去重、规范化）。
        force_refresh:  为 True 时跳过 spot 表 TTL，尽量拉最新（选股等场景用）。
    """
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
