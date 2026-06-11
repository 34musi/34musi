"""组装完整课程：分阶段模块合并。"""

from __future__ import annotations

from app.schemas import StockKnowledgeOut
from app.stock_knowledge.modules import (
    phase0_intro,
    phase1_time_value,
    phase2_money,
    phase3_macro,
    phase4_markets,
    phase5_finance,
    phase6_invest,
    phase7_ashare,
    phase8_tool,
)


def build_curriculum() -> StockKnowledgeOut:
    modules = [
        *phase0_intro.modules(),
        *phase1_time_value.modules(),
        *phase2_money.modules(),
        *phase3_macro.modules(),
        *phase4_markets.modules(),
        *phase5_finance.modules(),
        *phase6_invest.modules(),
        *phase7_ashare.modules(),
        *phase8_tool.modules(),
    ]
    return StockKnowledgeOut(
        title="金融从零学起",
        intro=(
            "这是一套面向完全零基础的学习路线，目标不是背术语，而是理解："
            "金融在经济社会里解决什么问题、钱如何流动、资产为何有价格、"
            "个人如何管理风险与预期。\n\n"
            "建议按阶段顺序阅读（约 8～12 周，每天 40～60 分钟）。"
            "每节勾选「已读完」记录进度（仅保存在本机浏览器）。"
            "内容为通用教育科普，不构成投资建议。"
        ),
        roadmap=[
            "阶段 0～1（约 1 周）：金融是什么、时间价值与利率——建立思维框架",
            "阶段 2～3（约 2～3 周）：货币银行、宏观经济学——读懂新闻里的「大环境」",
            "阶段 4～5（约 3～4 周）：市场体系、公司财务与估值——理解资产定价逻辑",
            "阶段 6～7（约 2～3 周）：组合与行为金融、A 股实务——落到投资实践",
            "阶段 8：与本工具配合——把知识用在 quant-monitor 日常流程中",
        ],
        learning_tips=[
            "每学完一节，用三句话向自己解释「这节解决了什么问题」",
            "宏观类内容：对照当天 1 条财经要闻，标注本节关键词",
            "公司财务类：选一只自选，在⑤个股咨询里对照营收与业务描述",
            "不要急于预测涨跌；先建立「因果链」：利率→估值→情绪→价格",
            "遇到公式先理解直觉：DCF 本质是「未来现金折现」，Beta 本质是「跟市场一起波动的程度」",
        ],
        modules=modules,
    )
