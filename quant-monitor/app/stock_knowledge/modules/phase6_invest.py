"""阶段 6：投资组合与行为金融。"""

from __future__ import annotations

from app.schemas import StockKnowledgeLesson, StockKnowledgeModule, StockKnowledgeQuiz

PHASE = "阶段 6 · 投资理论与行为"


def modules() -> list[StockKnowledgeModule]:
    return [
        StockKnowledgeModule(
            id="p6-portfolio",
            phase=PHASE,
            title="现代投资组合理论",
            description="分散化、有效前沿、Beta——从「选股」到「配资产」",
            lessons=[
                StockKnowledgeLesson(
                    id="p6-mean-variance",
                    title="均值-方差与分散化",
                    summary="马科维茨：通过组合降低非系统性风险，而不必然牺牲预期收益",
                    body=(
                        "【核心思想】不要把所有鸡蛋放一个篮子。"
                        "若两类资产不完全正相关，组合波动可能低于单资产加权平均。\n\n"
                        "【系统性 vs 非系统性风险】\n"
                        "– 非系统性（公司、行业特有）：可通过分散降低；\n"
                        "– 系统性（宏观、市场整体）：无法通过分散消除，"
                        "需靠资产配置、对冲或承受。\n\n"
                        "【有效前沿】在给定风险下最大化预期收益的组合集合。"
                        "个人实践：宽基 + 行业适度分散 + 债券/现金缓冲，"
                        "比押单票更符合理论（也不保证盈利）。"
                    ),
                    key_terms=["分散化", "系统性风险", "非系统性风险", "有效前沿", "相关性"],
                    practice="数一下自选涉及几个一级行业，评估是否过度集中单一主题。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="分散投资主要降低哪类风险？",
                            answer="非系统性（个股、行业特有）风险；无法消除系统性市场风险。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p6-capm-beta",
                    title="CAPM 与 Beta",
                    summary="预期收益 = 无风险利率 + Beta × 市场风险溢价",
                    body=(
                        "【CAPM 资本资产定价模型（简化）】\n"
                        "E(Ri) = Rf + βi × (E(Rm) − Rf)\n\n"
                        "【Beta β】衡量个股（或组合）相对大盘波动的敏感度：\n"
                        "– β=1：与大盘同步；\n"
                        "– β>1：波动更大（进攻型）；\n"
                        "– β<1：更防御。\n\n"
                        "【局限】CAPM 假设理想化，现实中还有规模、价值、"
                        "动量等因子（Fama-French）。但 Beta 直觉仍常用："
                        "牛市高 Beta 可能涨更多，熊市跌也更狠。\n\n"
                        "⑨量化选股与④信号中的「强度」等，"
                        "可理解为规则化因子暴露，非严格 CAPM。"
                    ),
                    key_terms=["CAPM", "Beta", "市场风险溢价", "因子", "Fama-French"],
                    practice="思考：科创板小盘成长股通常 Beta 偏高还是偏低？为何？",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="Beta 大于 1 通常表示什么？",
                            answer="该资产历史波动相对大盘更大，对市场涨跌更敏感。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p6-sharpe-drawdown",
                    title="夏普比率与最大回撤",
                    summary="衡量「每承担一单位风险赚多少」以及最坏情况有多坏",
                    body=(
                        "【夏普比率 Sharpe】≈ (组合收益 − 无风险收益) / 组合波动。"
                        "越高说明风险调整后表现越好（样本内）。\n"
                        "【最大回撤 Max Drawdown】从历史高点到最低点的最大跌幅，"
                        "衡量「最惨时有多惨」。\n\n"
                        "【实践】回测（③ walk-forward）若只看收益不看回撤，"
                        "容易过拟合历史。应同时看：\n"
                        "– 年化收益；\n"
                        "– 最大回撤；\n"
                        "– 夏普（或 Sortino 只惩罚下行波动）。\n\n"
                        "心理承受力应匹配最大回撤："
                        "若 −30% 会迫使你割肉，则初始仓位过高。"
                    ),
                    key_terms=["夏普比率", "最大回撤", "风险调整收益", "Sortino", "过拟合"],
                    practice="假设某策略年化 15%、最大回撤 40%，问自己能否持有不动。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="为何不能只看收益率评价策略？",
                            answer="未考虑波动与回撤，可能承担不可持续的风险或存在过拟合。",
                        ),
                    ],
                ),
            ],
        ),
        StockKnowledgeModule(
            id="p6-behavioral",
            phase=PHASE,
            title="行为金融",
            description="人非完全理性——认知偏差如何让你亏钱",
            lessons=[
                StockKnowledgeLesson(
                    id="p6-biases",
                    title="常见认知偏差",
                    summary="损失厌恶、过度自信、锚定、 herd——对照自省",
                    body=(
                        "【损失厌恶】亏 100 元的痛苦 > 赚 100 元的快乐 → "
                        "不愿止损、死扛亏损单。\n"
                        "【过度自信】高估自己的信息与预测能力 → 频繁交易、集中押注。\n"
                        "【锚定】过度依赖买入价、历史高点 → 忽视基本面变化。\n"
                        "【羊群效应】别人买我也买 → 泡沫与踩踏。\n"
                        "【确认偏误】只找支持自己观点的信息 → 忽视反面证据。\n\n"
                        "【对策】写交易/投资规则（⑦决策日志）、"
                        "固定复盘、设仓位上限与停机条件（①自用风控摘要）。"
                    ),
                    key_terms=["损失厌恶", "过度自信", "锚定", "羊群效应", "确认偏误"],
                    practice="回顾一次冲动交易或差点冲动的情况，标注属于哪种偏差。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="损失厌恶可能导致什么错误行为？",
                            answer="不愿 realiz 亏损、死扛下跌仓位，或过早卖出盈利单（处置效应）。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p6-market-efficiency",
                    title="泡沫、恐慌与均值回归",
                    summary="价格会偏离价值，但偏离可持续多久无法精确预测",
                    body=(
                        "【泡沫】价格远超基本面支撑，靠叙事与流动性推动"
                        "（2000 互联网、2015 A 股杠杆牛、2021 部分赛道）。\n"
                        "【恐慌】流动性枯竭、强制平仓、信心崩溃 → 超卖。\n"
                        "【均值回归】极端涨跌后可能向长期均值靠拢，"
                        "但「均值」本身随经济变化，且时间不确定。\n\n"
                        "【教训】\n"
                        "– 不用杠杆参与纯叙事交易；\n"
                        "– 泡沫期谈「这次不一样」最危险；\n"
                        "– 恐慌期需区分「价格跌」与「价值毁损」。"
                    ),
                    key_terms=["泡沫", "恐慌", "均值回归", "杠杆", "叙事"],
                    practice="读一篇 A 股历史泡沫复盘（2015 或 2021 赛道），列出 3 个共同特征。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="「这次不一样」为何危险？",
                            answer="常为泡沫期忽视估值与风险的理由；历史表明极端偏离多会回归或修正。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p6-risk-management",
                    title="个人风控框架",
                    summary="仓位、止损、分散、停机——比预测更重要",
                    body=(
                        "【建议框架（非标准答案）】\n"
                        "1）单票上限（如账户 10%～20%，视经验调整）；\n"
                        "2）总股票仓位上限（留现金应对波动）；\n"
                        "3）单笔最大可接受亏损占账户比例；\n"
                        "4）连续亏损或单日回撤触发「停机复盘」；\n"
                        "5）区分观察仓、试错仓、主仓。\n\n"
                        "【与本工具】⑩持仓记录可估算盈亏；"
                        "⑦决策日志记录计划 vs 执行；"
                        "①可加载自用风控 checklist。\n\n"
                        "风控不是「胆小」，而是让长期留在牌桌上。"
                    ),
                    key_terms=["仓位管理", "止损", "停机", "观察仓", "复盘"],
                    practice="写一份个人风控 5 条，保存到⑦决策日志或本地笔记。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="为何说风控比预测涨跌更重要？",
                            answer="预测不可靠，风控限制单次与总体损失，提高长期生存与理性决策概率。",
                        ),
                    ],
                ),
            ],
        ),
    ]
