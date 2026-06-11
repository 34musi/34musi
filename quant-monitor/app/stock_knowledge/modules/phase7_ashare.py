"""阶段 7：A 股实务。"""

from __future__ import annotations

from app.schemas import StockKnowledgeLesson, StockKnowledgeModule, StockKnowledgeQuiz

PHASE = "阶段 7 · A 股实务"


def modules() -> list[StockKnowledgeModule]:
    return [
        StockKnowledgeModule(
            id="p7-rules",
            phase=PHASE,
            title="交易制度与板块",
            description="T+1、涨跌停、注册制——在 A 股生存的基本规则",
            lessons=[
                StockKnowledgeLesson(
                    id="p7-t1-limit",
                    title="T+1、涨跌停与 ST",
                    summary="制度塑造流动性与博弈方式，与港股、美股不同",
                    body=(
                        "【T+1】当日买入，次一交易日方可卖出。"
                        "计划止损须提前考虑：今天买，最早明天卖。\n"
                        "【涨跌幅限制】主板常见 ±10%，ST ±5%，"
                        "创业板/科创板 ±20%，北交所有独立规则（以交易所最新规定为准）。\n"
                        "【ST/*ST】连续亏损等风险警示，涨跌幅更严，"
                        "存在退市风险，不宜当普通股研究。\n\n"
                        "【一字涨跌停】订单堆积，无法成交 → 流动性风险。"
                        "小市值、题材炒作时更常见。"
                    ),
                    key_terms=["T+1", "涨跌停", "ST", "退市", "流动性"],
                    practice="统计自选里 ST 与非 ST 数量，确认是否误加了高风险标的。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="T+1 对止损计划的主要影响？",
                            answer="买入当日无法卖出，无法对 intraday 下跌立即止损。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p7-boards",
                    title="主板、创业板、科创板、北交所",
                    summary="上市标准、投资者门槛、涨跌幅各不相同",
                    body=(
                        "【主板】大型成熟企业，60/000/001/003 等，±10%。\n"
                        "【创业板】成长型，300/301，±20%，注册制下发行更市场化。\n"
                        "【科创板】硬科技，688/689，±20%，需 50 万资产 + 24 个月经验等门槛。\n"
                        "【北交所】创新型中小企业，独立代码段，不同投资者门槛。\n\n"
                        "【注册制】强调信息披露，退市常态化，"
                        "「壳资源」逻辑弱化，研究应更回归基本面。\n\n"
                        "本工具⑨热门选股等可能过滤板块，添加自选前请确认代码属性。"
                    ),
                    key_terms=["注册制", "科创板", "北交所", "投资者门槛", "壳价值"],
                    practice="各板块选一只自选，对比近一年最大回撤与波动（定性即可）。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="688 开头股票属于哪个板块？",
                            answer="科创板（上海）。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p7-costs",
                    title="交易成本与税费",
                    summary="佣金、印花税、过户费——频繁交易会侵蚀 edge",
                    body=(
                        "A 股交易费用（大致，以券商为准）：\n"
                        "– 佣金：买卖双向，有最低 5 元等规则；\n"
                        "– 印花税：卖出时征收（税率以财政部为准，历史上曾调整）；\n"
                        "– 过户费：极小。\n\n"
                        "【影响】\n"
                        "– 短线频繁交易，成本 + 滑点可能吃掉大部分「alpha」；\n"
                        "– 回测若未扣费，会高估策略表现（③ walk-forward 应注意）。\n\n"
                        "【融资融券】额外利息成本，杠杆放大盈亏与强平风险。"
                    ),
                    key_terms=["佣金", "印花税", "滑点", "融资融券", "交易成本"],
                    practice="估算：买卖各一次、10 万元本金，印花税+佣金大约占几 ‰？",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="A 股印花税通常在哪一侧收取？",
                            answer="卖出方（政策可能调整，以现行规定为准）。",
                        ),
                    ],
                ),
            ],
        ),
        StockKnowledgeModule(
            id="p7-ashare-special",
            phase=PHASE,
            title="A 股特色与资金",
            description="政策、北向、两融、产业主题——理解「A 股逻辑」",
            lessons=[
                StockKnowledgeLesson(
                    id="p7-policy-theme",
                    title="政策与主题投资",
                    summary="五年规划、产业政策、监管表态——A 股对政策敏感度高",
                    body=(
                        "A 股历史上对政策与主题反应往往较快：\n"
                        "– 产业规划（新能源、半导体、信创等）；\n"
                        "– 监管变化（教培、游戏版号、地产融资）；\n"
                        "– 资本市场改革（注册制、北交所、互联互通）。\n\n"
                        "【主题 vs 业绩】\n"
                        "主题炒作可能脱离短期基本面，靠叙事与资金推动；"
                        "持续行情最终需业绩验证，否则回落。\n\n"
                        "【研究习惯】\n"
                        "– ⑤新闻与概念板块；\n"
                        "– 区分「政策催化」与「长期竞争力」；\n"
                        "– 决策日志记录你依据的是主题还是财报。"
                    ),
                    key_terms=["产业政策", "主题投资", "监管", "叙事", "业绩验证"],
                    practice="选近期一个政策热点，追踪相关板块一周后表现，写一句因果反思。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="主题行情若要持续，通常还需什么支撑？",
                            answer="后续业绩或订单等基本面验证，否则可能仅是短期资金博弈。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p7-northbound",
                    title="北向资金与机构行为",
                    summary="陆股通是观察外资风险偏好的一扇窗，非「聪明钱」保证",
                    body=(
                        "【北向资金】香港投资者经沪深股通买卖 A 股，"
                        "日度净流入/流出常上新闻。\n\n"
                        "【如何解读】\n"
                        "– 持续净流入 + 偏好龙头 → 有时伴随核心资产行情；\n"
                        "– 大幅净流出 → 可能反映全球流动性或地缘风险，"
                        "不必然意味着 A 股长期走熊；\n"
                        "– 单日数据噪音大，宜看趋势与结构。\n\n"
                        "【其他机构】公募仓位、私募备案、"
                        "国家队（汇金等）在极端行情时有稳定预期作用，"
                        "但细节不透明，勿过度解读单笔动作。"
                    ),
                    key_terms=["北向资金", "沪深股通", "核心资产", "净流入", "机构"],
                    practice="查最近一周北向资金累计流向新闻，对照沪深 300 同期涨跌。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="北向单日大幅净流出是否必然预示 A 股下跌？",
                            answer="否；单日噪音大，需结合全球流动性、汇率与更长趋势，非确定性信号。",
                        ),
                    ],
                ),
                StockKnowledgeLesson(
                    id="p7-kline-reading",
                    title="K 线、成交量与趋势",
                    summary="OHLC、均线、量价关系——④信号的技术基础",
                    body=(
                        "【K 线】开高低收四价，反映一段时期多空博弈结果。\n"
                        "【成交量】价涨量增有时表示趋势确认；"
                        "价涨量缩可能动能不足（非绝对）。\n"
                        "【均线 MA】过去 N 日均价，常用 5/10/20/60/120/250 日，"
                        "观察趋势与支撑阻力。\n\n"
                        "【本工具④】基于日线计算趋势（bullish/sideways/bearish）、"
                        "强度等，属于规则化 Demo。\n\n"
                        "【局限】\n"
                        "– 历史形态不保证未来；\n"
                        "– 除权除息需复权（本工具③使用前复权）；\n"
                        "– 应结合基本面与仓位，而非单看 K 线。"
                    ),
                    key_terms=["K线", "成交量", "均线", "前复权", "趋势"],
                    practice="在③查询一只自选本地 K 线条数，在④看同只信号，对照 MA 方向。",
                    quiz=[
                        StockKnowledgeQuiz(
                            question="为何研究历史 K 线要用复权价？",
                            answer="除权除息会使价格跳空，不复权会扭曲均线与涨跌幅计算。",
                        ),
                    ],
                ),
            ],
        ),
    ]
