"""
⑦ AI 潜力测算：汇总②自选、③行情、④信号等本地数据，交由大模型做 Demo 解读。

非投资建议；API Key 可在⑦控制台填写，或服务端 .env 配置 AI_API_KEY。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.schemas import AiConfigIn
from app.db_models import ForwardOutlookRow, WatchlistRow
from app.db_session import session_scope
from app.forward_outlook import DEFAULT_HORIZON, _audit_bars, row_to_dict
from app.ingest import fetch_stock_name, load_bars_from_db, normalize_symbol
from app.local_scores import compute_local_scores_at_date
from app.schemas import SignalOut
from app.signals import compute_signal

logger = logging.getLogger(__name__)

MAX_SYMBOLS_PER_CALL = 8


@dataclass(frozen=True)
class ResolvedAiConfig:
    api_key: str
    api_base: str
    model: str
    json_mode: bool
    timeout_sec: float


def ai_defaults_payload() -> dict[str, Any]:
    """供 GET /meta/ai-defaults 与控制台预填（不含密钥）。"""
    s = get_settings()
    return {
        "api_base": s.ai_api_base,
        "model": s.ai_model,
        "json_mode": s.ai_json_mode,
        "timeout_sec": s.ai_timeout_sec,
        "server_key_configured": bool((s.ai_api_key or "").strip()),
    }


def resolve_ai_config(ai: AiConfigIn | None = None) -> ResolvedAiConfig:
    """请求体 ai 字段优先，否则回退服务端 Settings。"""
    s = get_settings()
    key = (ai.api_key if ai and ai.api_key else s.ai_api_key or "").strip()
    if not key:
        raise ValueError("请在本页填写 AI API Key，或在服务端 .env 配置 AI_API_KEY")

    base = (ai.api_base if ai and ai.api_base else s.ai_api_base or "").strip()
    if not base:
        base = "https://api.openai.com/v1"
    model = (ai.model if ai and ai.model else s.ai_model or "").strip()
    if not model:
        model = "gpt-4o-mini"
    json_mode = s.ai_json_mode if ai is None or ai.json_mode is None else bool(ai.json_mode)
    timeout = s.ai_timeout_sec
    if ai is not None and ai.timeout_sec is not None:
        timeout = float(ai.timeout_sec)
    return ResolvedAiConfig(
        api_key=key,
        api_base=base.rstrip("/"),
        model=model,
        json_mode=json_mode,
        timeout_sec=max(5.0, min(300.0, timeout)),
    )


def _signal_summary(sig: SignalOut) -> dict[str, Any]:
    d = sig.model_dump()
    keys = (
        "symbol",
        "name",
        "as_of_date",
        "close",
        "spot_last_price",
        "spot_change_pct",
        "trend",
        "strength",
        "buy_suitability_score",
        "technical_score",
        "fundamental_adjustment",
        "enhanced_buy_score",
        "buy_verdict",
        "buy_verdict_text",
        "position_hint",
        "position_range_text",
        "risk_tags",
    )
    out = {k: d.get(k) for k in keys if d.get(k) is not None}
    reasons = d.get("reasons") or []
    if reasons:
        out["reasons_top3"] = [
            {"code": r.get("code"), "text": r.get("text")}
            for r in reasons[:3]
            if isinstance(r, dict)
        ]
    fund = d.get("fundamentals")
    if isinstance(fund, dict):
        out["fundamentals_pe_pb"] = {
            k: fund.get(k)
            for k in ("pe_ttm", "pb", "roe", "revenue_yoy_pct", "net_profit_yoy_pct")
            if fund.get(k) is not None
        }
    return out


def gather_symbol_context(sym: str) -> dict[str, Any]:
    """汇总单标的②③④相关本地数据（不联网增量）。"""
    sym = normalize_symbol(sym)
    ctx: dict[str, Any] = {"symbol": sym, "errors": []}

    with session_scope() as s:
        wl = s.execute(select(WatchlistRow).where(WatchlistRow.symbol == sym)).scalar_one_or_none()
        if wl is not None:
            ctx["watchlist"] = {
                "name": (wl.name or "").strip() or None,
                "origin": wl.origin,
            }
        else:
            ctx["watchlist"] = None
            ctx["errors"].append("不在自选池（仍可测算，建议先在②添加）")

    name = (ctx.get("watchlist") or {}).get("name")
    if not name:
        name = fetch_stock_name(sym) or None
    ctx["name"] = name

    df = load_bars_from_db(sym)
    audit = _audit_bars(df)
    ctx["bars"] = {
        "ok": audit.get("ok"),
        "bars_count": audit.get("bars_count"),
        "first_trade_date": audit.get("first_trade_date"),
        "last_trade_date": audit.get("last_trade_date"),
        "last_close": audit.get("last_close"),
        "issues": audit.get("issues") or [],
    }
    if df.empty:
        ctx["errors"].append("本地无 K 线，请先在③更新行情")
        ctx["signal"] = None
        ctx["local_scores"] = None
        ctx["forward_outlook"] = None
        return ctx

    last_td = str(audit.get("last_trade_date") or df["trade_date"].iloc[-1])

    try:
        sig = compute_signal(sym)
        ctx["signal"] = _signal_summary(sig)
    except Exception as e:
        ctx["signal"] = None
        ctx["errors"].append(f"④信号计算失败：{e}")

    try:
        ctx["local_scores"] = compute_local_scores_at_date(df, last_td)
    except Exception as e:
        ctx["local_scores"] = None
        logger.debug("local_scores %s: %s", sym, e)

    try:
        with session_scope() as s:
            row = s.execute(
                select(ForwardOutlookRow)
                .where(ForwardOutlookRow.symbol == sym)
                .order_by(ForwardOutlookRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                fo = row_to_dict(row)
                outlook_json = None
                if row.outlook_json:
                    try:
                        outlook_json = json.loads(row.outlook_json)
                    except json.JSONDecodeError:
                        outlook_json = None
                ctx["forward_outlook"] = {
                    "horizon": fo.get("horizon"),
                    "signal_trade_date": fo.get("signal_trade_date"),
                    "predicted_up": fo.get("predicted_up"),
                    "status": fo.get("status"),
                    "outlook_summary_zh": fo.get("outlook_summary_zh"),
                    "actual_return_pct": fo.get("actual_return_pct"),
                    "prediction_methods": (outlook_json or {}).get("prediction", {}).get("methods"),
                }
            else:
                ctx["forward_outlook"] = None
    except Exception as e:
        ctx["forward_outlook"] = None
        logger.debug("forward_outlook %s: %s", sym, e)

    return ctx


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("AI 返回为空")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("无法解析 AI 返回的 JSON")


def _chat_completion(messages: list[dict[str, str]], cfg: ResolvedAiConfig) -> str:
    url = cfg.api_base + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.35,
    }
    if cfg.json_mode:
        payload["response_format"] = {"type": "json_object"}

    with httpx.Client(timeout=cfg.timeout_sec) as client:
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise ValueError(f"AI 接口 HTTP {resp.status_code}：{detail}")
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("AI 接口未返回 choices")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise ValueError("AI 接口返回内容为空")
    return str(content)


def _build_messages(
    contexts: list[dict[str, Any]],
    *,
    horizon_days: int,
    user_note: str | None,
    user_question: str | None = None,
) -> list[dict[str, str]]:
    h = max(1, min(60, int(horizon_days)))
    q = (user_question or "").strip()
    sys_prompt = (
        "你是 A 股量化辅助分析助手，仅根据用户提供的本地测算数据做「潜力」Demo 解读。"
        "你必须输出一个 JSON 对象（不要 markdown 代码块），结构如下：\n"
        "{\n"
        '  "summary_zh": "整体一段话摘要",\n'
        '  "items": [\n'
        "    {\n"
        '      "symbol": "6位代码",\n'
        '      "name": "简称或null",\n'
        '      "potential_score": 0-100整数,\n'
        '      "potential_label": "偏高|中性|偏低",\n'
        f'      "horizon_days": {h},\n'
        '      "upside_factors": ["..."],\n'
        '      "downside_factors": ["..."],\n'
        '      "key_watchpoints": ["..."],\n'
        '      "data_gaps": ["若②③④数据不足请列出"],\n'
        '      "comment_zh": "该标的2-4句结论"\n'
        "    }\n"
        "  ],\n"
        '  "disclaimer": "固定写：本解读为算法与 AI Demo，不构成投资建议。"\n'
        "}\n"
        "评分须与④信号、本地打分、前向展望一致；数据缺失时降分并在 data_gaps 说明。"
        "禁止承诺收益、禁止给出具体买卖价位。"
    )
    if q:
        sys_prompt += (
            f"用户提问：「{q}」。请在 summary_zh 中直接回应；"
            "各标的 comment_zh 亦须结合该问题作答（数据不足则说明）。"
        )
    user_payload = {
        "horizon_days": h,
        "user_note": (user_note or "").strip() or None,
        "user_question": q or None,
        "contexts": contexts,
    }
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def run_ai_potential(
    symbols: list[str],
    *,
    horizon_days: int = DEFAULT_HORIZON,
    user_note: str | None = None,
    question: str | None = None,
    preview_only: bool = False,
    ai: AiConfigIn | None = None,
) -> dict[str, Any]:
    """对若干标的汇总②③④并（可选）调用 AI 测算潜力。"""
    if not symbols:
        raise ValueError("请至少指定一只 6 位 A 股代码")

    uniq: list[str] = []
    for raw in symbols:
        sym = normalize_symbol(raw)
        if sym not in uniq:
            uniq.append(sym)
    if len(uniq) > MAX_SYMBOLS_PER_CALL:
        raise ValueError(f"单次最多 {MAX_SYMBOLS_PER_CALL} 只，请缩小范围")

    contexts = [gather_symbol_context(s) for s in uniq]
    settings = get_settings()
    cfg: ResolvedAiConfig | None = None
    if not preview_only:
        cfg = resolve_ai_config(ai)
    ui_key = bool(ai and (ai.api_key or "").strip())
    srv_key = bool((settings.ai_api_key or "").strip())
    out: dict[str, Any] = {
        "symbols": uniq,
        "horizon_days": max(1, min(60, int(horizon_days))),
        "contexts": contexts,
        "ai_configured": preview_only or ui_key or srv_key,
        "ai_model": (cfg.model if cfg else None) or (settings.ai_model if srv_key else None),
        "preview_only": preview_only,
        "items": [],
        "summary_zh": None,
        "disclaimer": settings.disclaimer_short,
    }

    if preview_only:
        out["summary_zh"] = "已汇总②③④本地数据；填写上方 AI 配置后可点击「AI 测算潜力」。"
        return out

    assert cfg is not None
    messages = _build_messages(
        contexts,
        horizon_days=out["horizon_days"],
        user_note=user_note,
        user_question=question,
    )
    raw_ai = _chat_completion(messages, cfg)
    parsed = _extract_json_object(raw_ai)
    out["summary_zh"] = parsed.get("summary_zh")
    out["items"] = parsed.get("items") or []
    out["disclaimer"] = parsed.get("disclaimer") or out["disclaimer"]
    out["ai_raw"] = raw_ai[:4000] if logger.isEnabledFor(logging.DEBUG) else None
    return out


def resolve_symbols_for_ai(
    symbols: list[str] | None,
    *,
    use_watchlist: bool,
) -> list[str]:
    """解析请求中的代码列表；use_watchlist 时取自选池（可与 symbols 合并去重）。"""
    out: list[str] = []
    if use_watchlist:
        with session_scope() as s:
            rows = s.execute(select(WatchlistRow.symbol)).scalars().all()
            for sym in rows:
                try:
                    ns = normalize_symbol(str(sym))
                    if ns not in out:
                        out.append(ns)
                except ValueError:
                    continue
    if symbols:
        for raw in symbols:
            try:
                ns = normalize_symbol(raw)
                if ns not in out:
                    out.append(ns)
            except ValueError as e:
                raise ValueError(f"无效代码 {raw!r}：{e}") from e
    return out
