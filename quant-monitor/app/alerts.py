"""
信号变动检测与告警推送（占位模块）。

## 功能作用

本模块为 quant-monitor 的「⑤ 变动预览」提供核心逻辑：对比自选池中各标的
**当前信号** 与 **上次缓存快照**，找出值得关注的变动，供控制台展示或后续推送。

典型流程（由 `POST /alerts/preview` 在 main.py 中编排）：

1. 从数据库 `signal_cache` 表读取各标的上一版快照（`SignalCacheRow.payload_json`）。
2. 对自选列表逐只调用 `compute_signal` 得到当前 `SignalOut`（可选先增量更新日线）。
3. 调用 `detect_changes(prev_map, current)` 生成 `new` / `shift` 事件列表。
4. 用 `signal_to_snapshot` 将当前信号压缩为 JSON 可序列化 dict，写回 `signal_cache`。
5. 将事件返回前端渲染；未来可在此处或调用方接入 `post_webhook_placeholder` 做外部通知。

## 事件类型

- **new**：该标的首次出现在对比缓存中（无历史快照）。
- **shift**：以下任一条件成立：
  - `trend`（趋势档位）发生变化；
  - 综合评分 `score` 的「十位档」发生变化（`score // 10`，例如 59→61 算变动，50→59 不算）。

## 对外接口

| 函数 | 用途 |
|------|------|
| `signal_to_snapshot` | 从完整 `SignalOut` 提取告警对比所需字段，避免 reasons/meta 等大字段入库 |
| `detect_changes` | 逐标的对比上一快照与当前信号，返回事件列表 |
| `post_webhook_placeholder` | 异步 POST JSON 到自定义 URL 的示例实现（Webhook / 企业微信等扩展点） |

## 扩展说明

- 当前 Webhook 为占位实现，生产使用前需配置可信 URL 并评估 SSRF 风险。
- 邮件、企业微信、钉钉等通知可在检测到 events 非空后，复用 `post_webhook_placeholder`
  或在此模块新增对应 sender。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.schemas import SignalOut

logger = logging.getLogger(__name__)


def signal_to_snapshot(sig: SignalOut) -> dict[str, Any]:
    """
    将单标的 `SignalOut` 压缩为可 JSON 化的小 dict，供缓存存储与前后对比。

    只保留趋势、强度、评分、仓位提示、试仓退出指引等告警相关字段，
    不包含 name、close、reasons、meta 等体积较大或与变动检测无关的字段。

    评分字段 `score` 优先取 `enhanced_buy_score`，若无则回退到 `buy_suitability_score`，
    与 `detect_changes` 中 shift 判定的十位档逻辑一致。
    """
    sp = sig.suggested_position_pct
    te = sig.trial_exit_guidance
    return {
        "symbol": sig.symbol,
        "trend": sig.trend,
        "strength": sig.strength,
        # 综合评分：增强分优先，否则用传统买入适合度
        "score": sig.enhanced_buy_score if sig.enhanced_buy_score is not None else sig.buy_suitability_score,
        "legacy_score": sig.buy_suitability_score,
        "enhanced_buy_score": sig.enhanced_buy_score,
        "buy_verdict": sig.buy_verdict,
        "technical_score": sig.technical_score,
        "fundamental_adjustment": sig.fundamental_adjustment,
        "position_hint": sig.position_hint,
        "suggested_position_pct_low": sp.low_pct,
        "suggested_position_pct_high": sp.high_pct,
        "trial_exit_applies": te.applies,
        "trial_stop_loss_pct_demo": te.stop_loss_pct_from_entry_demo,
        "reference_exit_ma20": te.reference_exit_ma20,
        "as_of_date": sig.as_of_date,
    }


def detect_changes(
    previous: dict[str, dict[str, Any]] | None,
    current: dict[str, SignalOut],
) -> list[dict[str, Any]]:
    """
    对比每个标的的上一快照与当前信号，生成变动事件列表。

    参数:
        previous: symbol → 快照 dict 的映射（来自 signal_cache；None 或空 dict 表示无历史）。
        current:  symbol → 当前 `SignalOut` 的映射（由 compute_signal 逐只计算）。

    返回:
        事件列表，每项为 dict，结构如下：
        - type=new:   {"type": "new", "symbol", "snapshot"}
        - type=shift: {"type": "shift", "symbol", "before", "after"}

    shift 判定规则:
        - trend 字段值变化；或
        - score 的十位档变化（old["score"] // 10 != snap["score"] // 10）。
    """
    prev = previous or {}
    events: list[dict[str, Any]] = []
    for sym, sig in current.items():
        snap = signal_to_snapshot(sig)
        old = prev.get(sym)
        if old is None:
            events.append({"type": "new", "symbol": sym, "snapshot": snap})
            continue
        # 趋势变化，或评分跨十位档（如 59→61），视为 shift
        if old.get("trend") != snap["trend"] or old.get("score", 0) // 10 != snap["score"] // 10:
            events.append({"type": "shift", "symbol": sym, "before": old, "after": snap})
    return events


async def post_webhook_placeholder(url: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    """
    将告警 payload 异步 POST 到指定 URL（Webhook 扩展示例）。

    参数:
        url:     目标地址，须由调用方配置为可信白名单地址，避免 SSRF。
        payload: 通常为 {"events": [...], ...} 等 JSON 可序列化 dict。
        timeout: HTTP 超时秒数，默认 5.0。

    返回:
        HTTP 2xx 时为 True；网络异常或非 2xx 时为 False（失败会写 warning 日志）。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return r.is_success
    except Exception:
        logger.warning("webhook post failed", exc_info=True)
        return False
