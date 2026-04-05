"""
告警钩子占位：对比前后信号快照，可扩展为 Webhook / 邮件 / 企业微信等。

- signal_to_snapshot：把 SignalOut 压成可 JSON 化的小 dict，便于存库与对比。
- detect_changes：根据上一版与当前版生成 new / shift 事件（趋势变或评分十位档变）。
- post_webhook_placeholder：异步 POST JSON 到自定义 URL 的示例实现。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.schemas import SignalOut

logger = logging.getLogger(__name__)


def signal_to_snapshot(sig: SignalOut) -> dict[str, Any]:
    """提取用于告警对比的核心字段（避免整份 reasons/meta 过大）。"""
    return {
        "symbol": sig.symbol,
        "trend": sig.trend,
        "strength": sig.strength,
        "score": sig.buy_suitability_score,
        "position_hint": sig.position_hint,
        "as_of_date": sig.as_of_date,
    }


def detect_changes(
    previous: dict[str, dict[str, Any]] | None,
    current: dict[str, SignalOut],
) -> list[dict[str, Any]]:
    """
    对比每个标的上一快照与当前信号。

    - type=new：该标的首次出现在缓存对比中。
    - type=shift：trend 变化，或 score 的「十位档」变化（//10）。
    返回事件列表，供 API 或后续推送消费。
    """
    prev = previous or {}
    events: list[dict[str, Any]] = []
    for sym, sig in current.items():
        snap = signal_to_snapshot(sig)
        old = prev.get(sym)
        if old is None:
            events.append({"type": "new", "symbol": sym, "snapshot": snap})
            continue
        if old.get("trend") != snap["trend"] or old.get("score", 0) // 10 != snap["score"] // 10:
            events.append({"type": "shift", "symbol": sym, "before": old, "after": snap})
    return events


async def post_webhook_placeholder(url: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    """
    可选：将事件 POST 到自定义 URL（需自行配置可信地址，注意 SSRF 风险）。

    成功（2xx）返回 True，否则 False。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return r.is_success
    except Exception:
        logger.warning("webhook post failed", exc_info=True)
        return False
