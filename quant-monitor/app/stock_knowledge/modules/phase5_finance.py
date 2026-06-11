"""阶段 5：公司财务与估值。"""

from __future__ import annotations

from app.schemas import StockKnowledgeLesson, StockKnowledgeModule, StockKnowledgeQuiz

PHASE = "阶段 5 · 公司财务与估值"


def modules() -> list[StockKnowledgeModule]:
    return [
        StockKnowledgeModule(
            id="p5-statements",
            phase=PHASE,
            title="财务报表三表",
            description="资产负债表、利润表、现金流量表——读懂企业「体检报告」",
            lessons=[
                StockKnowledgeLesson(
                    id="p5-balance-sheet",
                    title="资产负债表：家底与杠杆",
                    summary="资产 = 负债 + 所有者权益——某一时点的财务状况快照",
                    body=(
                        "【资产】企业控制的资源：现金、应收、存货、固定资产、无形资产等。\n"
                        "【负债】欠谁的钱：短期借款、应付账款、长期债券等。\n"
                        "【所有者权益】股东投入 + 留存收益。\n\n"
                        "【关键比率】\n"
                        "– 资产负债率 = 总负债/总资产，衡量杠杆；\n"
                        "– 流动比率 = 流动资产/流动负债，短期偿债能力。\n\n"
                        "【雷区信号】应收激增但现金流差、存货堆积、"
                        "商誉占比过高、表外负债——需结合附注细读。\n\n"
                        "⑤个股查询的营收能力部分可与此对照，"
                        "但完整分析需下载年报 PDF。"
                    ),
                    key_terms=["资产负债表", "杠杆", "流动比率", "商誉", "所有者权益"],
                    practice="选一只自选，搜索其最新年报「资产负债表」摘要，看负债率大致水平。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="会计恒等式？",
                            answer="资产 = 负债 + 所有者权益。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p5-income-statement",
                    title="利润表：盈利从哪来",
                    summary="收入 − 成本费用 = 利润——注意会计利润 vs 现金利润",
                    body=(
                        "自上而下：\n"
                        "营业收入 → 毛利润（减营业成本）→ 营业利润 → "
                        "利润总额 → 净利润（扣所得税）。\n\n"
                        "【关注点】\n"
                        "– 毛利率：定价力与成本控制能力；\n"
                        "– 净利率：综合盈利能力；\n"
                        "– 非经常性损益：政府补贴、资产处置等一次性项目，"
                        "分析「扣非净利润」更反映主业。\n\n"
                        "【局限】权责发生制：收入可在未收到现金时确认，"
                        "因此必须结合现金流量表。"
                    ),
                    key_terms=["毛利率", "净利率", "扣非净利润", "权责发生制", "非经常性损益"],
                    practice="对比同行业两家公司的毛利率，思考差异来自品牌、成本还是结构。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="为何要看「扣非净利润」？",
                            answer="剔除一次性损益，更好反映主营业务真实盈利能力。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p5-cash-flow",
                    title="现金流量表：真金白银",
                    summary="经营、投资、筹资三类现金流——好公司往往经营现金流持续为正",
                    body=(
                        "【经营活动现金流】主业收付现金，核心健康指标。\n"
                        "【投资活动】购建固定资产、并购等，成长期常为负（扩张）。\n"
                        "【筹资活动】借还贷款、发股分红，反映融资行为。\n\n"
                        "【经典对比】\n"
                        "净利润高但经营现金流长期为负 → 警惕利润质量（应收、存货堆积）。\n"
                        "自由现金流 FCF ≈ 经营现金流 − 资本开支，"
                        "可用于分红、回购、还债，是估值重要输入。\n\n"
                        "巴菲特强调「所有者盈余」与此思路相通。"
                    ),
                    key_terms=["经营现金流", "自由现金流", "资本开支", "利润质量", "FCF"],
                    practice="搜索「经营现金流 净利润 背离 风险」，读一篇案例摘要。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="经营现金流长期显著低于净利润可能说明？",
                            answer="利润可能未转化为现金（应收增加、存货积压等），需警惕盈利质量。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p5-roe-dupont",
                    title="ROE 与杜邦分析",
                    summary="净资产收益率拆解为利润率 × 周转率 × 杠杆",
                    body=(
                        "ROE = 净利润 / 净资产，衡量股东投入回报。\n\n"
                        "【杜邦分解】\n"
                        "ROE = 销售净利率 × 总资产周转率 × 权益乘数（杠杆）。\n\n"
                        "– 高 ROE 来自高利润（茅台类）？\n"
                        "– 来自高周转（零售类）？\n"
                        "– 来自高杠杆（金融、地产）？\n\n"
                        "高杠杆 ROE 可持续性与风险更高。"
                        "比较 ROE 时必须看驱动因子与行业属性，"
                        "不能单比数字大小。"
                    ),
                    key_terms=["ROE", "杜邦分析", "周转率", "权益乘数", "杠杆"],
                    practice="思考：银行 ROE 高往往来自哪一项杜邦因子？",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="杜邦分析把 ROE 拆成哪三类因素？",
                            answer="销售净利率（盈利能力）、总资产周转率（运营效率）、权益乘数（财务杠杆）。",
                        ),
                    ],
                ),
            ],
        ),
        StockKnowledgeModule(
            id="p5-valuation",
            phase=PHASE,
            title="估值方法",
            description="相对估值与绝对估值——价格围绕价值波动，但价值本身难精确",
            lessons=[
                StockKnowledgeLesson(
                    id="p5-relative-val",
                    title="相对估值：PE、PB、PS、PEG",
                    summary="与同行、历史、市场比——快捷但有陷阱",
                    body=(
                        "【PE 市盈率】股价/每股收益。亏损公司 PE 无意义。"
                        "周期股在盈利顶峰时 PE 反而低（陷阱）。\n"
                        "【PB 市净率】股价/每股净资产，适用于重资产、金融。\n"
                        "【PS 市销率】适用于尚未盈利的成长股。\n"
                        "【PEG】PE/盈利增速，粗略衡量成长匹配度（增速需可靠预测）。\n\n"
                        "【使用原则】\n"
                        "1）同行业比较；\n"
                        "2）看 3～5 年历史分位；\n"
                        "3）结合 ROE、增速、负债；\n"
                        "4）A 股注意壳价值、政策主题对估值的扰动。"
                    ),
                    key_terms=["PE", "PB", "PS", "PEG", "历史分位"],
                    practice="在⑤查询同板块两只股票 PE，分析差异是否合理。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="周期股盈利顶峰时 PE 往往较低，这是买入信号吗？",
                            answer="不一定，可能是「价值陷阱」——盈利即将下行，低 PE 反映周期顶点。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p5-dcf-intuition",
                    title="DCF 折现：绝对估值直觉",
                    summary="企业价值 ≈ 未来自由现金流折现之和——理解逻辑即可",
                    body=(
                        "【DCF Discounted Cash Flow】\n"
                        "预测未来若干年自由现金流，用加权平均资本成本 WACC 折现，"
                        "加总得到企业价值，减净负债得股权价值，再除以股本得每股价值。\n\n"
                        "【敏感因素】\n"
                        "– 永续增长率 g 假设微小变化，结果大幅波动；\n"
                        "– WACC 随无风险利率、Beta 变化；\n"
                        "– 预测期现金流主观性强。\n\n"
                        "【实践】专业分析师用 DCF，个人更常用相对估值 + 情景分析。"
                        "但 DCF 直觉有用："
                        "「长期现金流越好、越稳定、折现率越低 → 估值越高」。"
                    ),
                    key_terms=["DCF", "WACC", "自由现金流", "永续增长", "绝对估值"],
                    practice="不用算公式：若一家公司未来 10 年 FCF 为零，DCF 直觉上值多少？（接近零，除非清算资产）",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="无风险利率上升对 DCF 估值的典型影响？",
                            answer="WACC 上升，未来现金流现值下降，估值承压。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p5-ta-vs-fa",
                    title="技术分析 vs 基本面分析",
                    summary="分别回答「何时买卖」「值不值得持有」——可互补不可偏废",
                    body=(
                        "【基本面】研究公司盈利、行业、估值、宏观——"
                        "回答「做什么生意、赚多少钱、贵不贵」。\n"
                        "【技术面】研究价格、成交量、形态——"
                        "回答「趋势与情绪、支撑阻力、时机」。\n\n"
                        "【有效市场争论】\n"
                        "– 弱式：历史价格已反映，技术分析无效（争议大）；\n"
                        "– 半强式：公开信息已定价；\n"
                        "– 强式：所有信息已定价。\n\n"
                        "【本工具定位】③④以日线与规则信号为主，偏技术面 + 简单因子；"
                        "⑤补充基本面公开摘要。应结合使用，"
                        "且理解任何信号都是历史规律演示，非预测保证。"
                    ),
                    key_terms=["基本面", "技术面", "K线", "有效市场", "因子"],
                    practice="对同一只自选，分别写一句基本面结论与一句技术面观察（来自④信号即可）。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="基本面分析主要回答什么问题？",
                            answer="公司做什么、盈利能力如何、当前价格相对价值是否合理等「值不值得」问题。",
                        ),
                    ],
                ),
            ],
        ),
    ]
