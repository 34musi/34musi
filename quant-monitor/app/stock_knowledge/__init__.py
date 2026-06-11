"""⑥ 金融从零学起：结构化课程（静态，供 /meta/stock-knowledge 返回）。"""

from __future__ import annotations

from app.schemas import StockKnowledgeOut
from app.stock_knowledge.curriculum import build_curriculum


def stock_knowledge_payload() -> StockKnowledgeOut:
    """返回完整金融入门课程。"""
    return build_curriculum()
