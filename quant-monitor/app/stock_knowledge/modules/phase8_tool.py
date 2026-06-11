"""阶段 8：与本工具配合。"""

from __future__ import annotations

from app.schemas import StockKnowledgeLesson, StockKnowledgeModule, StockKnowledgeQuiz

PHASE = "阶段 8 · 与本工具配合"


def modules() -> list[StockKnowledgeModule]:
    return [
        StockKnowledgeModule(
            id="p8-tool",
            phase=PHASE,
            title="quant-monitor 学习路径",
            description="把金融知识落到本工具各模块的日常流程",
            lessons=[
                StockKnowledgeLesson(
                    id="p8-workflow",
                    title="建议使用顺序（知识 + 操作）",
                    summary="每完成一个阶段，可在工具里做对应练手",
                    body=(
                        "【阶段 0～3 宏观】\n"
                        "– ①测试连接；读财经要闻时用本节词汇标注；\n"
                        "– 暂不必频繁交易，建立宏观日历习惯（CPI、PMI、央行例会）。\n\n"
                        "【阶段 4～5 市场与公司】\n"
                        "– ②添加 5～10 只不同行业自选；\n"
                        "– ⑤查询个股：对照概念、营收、PE；\n"
                        "– ③拉取日线，④看信号，理解趋势/强度含义。\n\n"
                        "【阶段 6～7 组合与 A 股】\n"
                        "– ⑩录入模拟或真实持仓，看集中度；\n"
                        "– ⑦写决策日志：依据、计划仓位、是否执行；\n"
                        "– ⑨量化选股（可选，耗时长，理解因子+回测逻辑）。\n\n"
                        "【持续】每周末 30 分钟复盘：宏观一条 + 自选一条 + 执行一致性。"
                    ),
                    key_terms=["自选", "日线", "信号", "决策日志", "复盘"],
                    practice="按你当前学习阶段，完成对应模块的一项操作并记入⑦。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="为何③更新行情应早于④查看信号？",
                            answer="信号依赖本地 K 线；数据缺失或过旧则计算无意义。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p8-disclaimer",
                    title="数据边界、合规与免责",
                    summary="公开日线、非实时；教育内容 + 工具演示均非投资建议",
                    body=(
                        "【数据】公开接口日线（前复权），存在延时、缺失、"
                        "复权与源差异；非 Level-2 实时行情。\n\n"
                        "【信号与选股】规则化 Demo / 研究工具，"
                        "不包含对任何标的的推荐、承诺收益或买卖指令。\n\n"
                        "【本课程】通用金融教育，作者非持牌顾问，"
                        "不替代专业意见。实盘需独立判断并承担全部风险。\n\n"
                        "完整免责与数据源说明：本页底部「数据源与免责」。"
                        "建议完成阶段 0 后阅读一遍。"
                    ),
                    key_terms=["前复权", "非实时", "Demo", "不构成投资建议", "合规"],
                    practice="展开底部免责全文，确认数据源与延时说明各一条。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="本工具信号能否直接当作买卖指令？",
                            answer="不能；仅为历史数据上的规则演示，不构成投资建议。",
                        ),
                    ],
                ),
            ],
        ),
    ]
