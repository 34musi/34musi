# `quant_stock_selector.py` 执行命令手册

本文档整理了 `quant_stock_selector.py` 的常用执行命令，覆盖：

- 热门板块分析
- 指定板块分析
- 自定义股票池分析
- 本地文件 / API 历史行情切换
- 结果导出
- 企业微信通知
- iTick 订阅

默认在当前目录执行：

```bash
python quant_stock_selector.py
```

## 1. 最基础命令

### 1.1 默认运行

不传 `--hot-sectors`、`--sector`、`--codes` 时，脚本默认启用热门板块分析。

```bash
python quant_stock_selector.py
```

### 1.2 查看帮助

```bash
python quant_stock_selector.py --help
```

## 2. 历史行情来源模式

脚本支持 3 种历史行情来源模式：

- `auto`：优先读取本地文件，找不到时再走 API
- `local`：只使用本地文件
- `api`：忽略本地文件，直接通过 API 拉取最新数据

### 2.1 自动模式，优先本地

```bash
python quant_stock_selector.py --data-dir "D:\stock_data" --history-source auto
```

### 2.2 只使用本地文件

注意：`local` 模式必须配合 `--data-dir`。

```bash
python quant_stock_selector.py --data-dir "D:\stock_data" --history-source local
```

### 2.3 忽略本地文件，直接走 API

这是你现在需要的“不要本地文件，直接拉最新数据展示”的模式。

```bash
python quant_stock_selector.py --history-source api
```

即使传了本地目录，也会忽略：

```bash
python quant_stock_selector.py --data-dir "D:\stock_data" --history-source api
```

## 3. 热门板块分析

### 3.1 分析热门板块

```bash
python quant_stock_selector.py --hot-sectors
```

### 3.2 分析前 10 个热门板块

```bash
python quant_stock_selector.py --hot-sectors --top-sectors 10
```

### 3.3 每个板块只分析前 30 只股票

```bash
python quant_stock_selector.py --hot-sectors --max-stocks-per-sector 30
```

### 3.4 每个板块只保留前 3 只龙头

```bash
python quant_stock_selector.py --hot-sectors --leader-stocks-per-sector 3
```

### 3.5 指定板块类型

分析概念板块：

```bash
python quant_stock_selector.py --hot-sectors --board-type concept
```

分析行业板块：

```bash
python quant_stock_selector.py --hot-sectors --board-type industry
```

## 4. 指定板块分析

### 4.1 分析指定板块

```bash
python quant_stock_selector.py --sector "机器人"
```

### 4.2 指定板块并强制走 API

```bash
python quant_stock_selector.py --sector "机器人" --history-source api
```

### 4.3 指定板块并限制股票数量

```bash
python quant_stock_selector.py --sector "算力" --max-stocks-per-sector 15
```

## 5. 自定义股票池分析

### 5.1 使用 CSV 股票池

```bash
python quant_stock_selector.py --codes "my_codes.csv"
```

### 5.2 使用 Excel 股票池

```bash
python quant_stock_selector.py --codes "my_codes.xlsx"
```

### 5.3 自定义股票池 + 只走 API

```bash
python quant_stock_selector.py --codes "my_codes.csv" --history-source api
```

### 5.4 自定义股票池 + 本地文件优先

```bash
python quant_stock_selector.py --codes "my_codes.csv" --data-dir "D:\stock_data" --history-source auto
```

## 6. 数据源切换

### 6.1 使用默认数据源 `mootdx`

```bash
python quant_stock_selector.py --history-source api
```

### 6.2 使用 `akshare`

```bash
python quant_stock_selector.py --data-source akshare --history-source api
```

### 6.3 使用 `tushare`

```bash
python quant_stock_selector.py --data-source tushare --tushare-token "你的token" --history-source api
```

### 6.4 使用环境变量方式提供 `tushare` token

PowerShell：

```powershell
$env:TUSHARE_TOKEN="你的token"
python quant_stock_selector.py --data-source tushare --history-source api
```

## 7. 时间范围和回测参数

### 7.1 指定历史区间

```bash
python quant_stock_selector.py --start-date 20240101 --end-date 20241231 --history-source api
```

### 7.2 调整双均线周期

```bash
python quant_stock_selector.py --fast-period 5 --slow-period 20 --history-source api
```

### 7.3 调整止损和滑点

```bash
python quant_stock_selector.py --stop-loss 0.06 --slippage 0.002 --commission 0.001 --history-source api
```

### 7.4 调整次日策略参数

```bash
python quant_stock_selector.py --next-day-stop-loss 0.015 --next-day-target-pct 0.025 --history-source api
```

## 8. 技术筛选参数

### 8.1 调整成交额门槛

```bash
python quant_stock_selector.py --min-avg-turnover-20d 50000000 --history-source api
```

### 8.2 调整近 5 日涨幅容忍度

```bash
python quant_stock_selector.py --max-5d-return 0.12 --history-source api
```

### 8.3 调整股价偏离 20 日线的容忍度

```bash
python quant_stock_selector.py --max-close-above-ma20 0.08 --history-source api
```

### 8.4 允许 ST 股票

```bash
python quant_stock_selector.py --allow-st --history-source api
```

## 9. 结果展示和导出

### 9.1 只显示初筛通过的股票

```bash
python quant_stock_selector.py --only-passed --history-source api
```

### 9.2 展示前 50 只股票

```bash
python quant_stock_selector.py --top-stocks 50 --history-source api
```

### 9.3 导出结果到 Excel

```bash
python quant_stock_selector.py --output "result.xlsx" --history-source api
```

### 9.4 热门板块分析并导出

```bash
python quant_stock_selector.py --hot-sectors --top-sectors 10 --output "hot_sector_result.xlsx" --history-source api
```

### 9.5 当前结果新增的短线样本提示字段

当前脚本已经在终端输出和 Excel 导出结果里加入下面 3 个字段：

- `短线样本等级`
- `是否建议参考次日统计`
- `短线可信度提示`

这些字段用于辅助判断 `next_day_pattern_count` 是否足够支撑短线参考，不需要你自己再手动估计。

### 9.6 短线样本字段的含义

#### `短线样本等级`

按 `次日样本数` 自动分级：

- `< 10`：`很低`
- `10-19`：`偏低`
- `20-49`：`中等`
- `50-79`：`较高`
- `>= 80`：`高`

#### `是否建议参考次日统计`

- `< 20`：`否`
- `20-29`：`谨慎参考`
- `>= 30`：`是`

#### `短线可信度提示`

脚本会自动给出中文提示，例如：

- `样本过少，次日统计仅作观察`
- `样本偏少，建议以技术面和回测为主`
- `样本一般，可辅助参考次日统计`
- `样本尚可，次日统计已有一定参考价值`
- `样本较充足，次日统计参考价值较高`

### 9.7 如何理解这些字段

- 如果 `次日样本数 < 20`，一般不要太依赖次日上涨概率
- 如果 `次日样本数 >= 30`，短线统计开始有一定实用性
- 如果 `次日样本数 >= 50`，短线统计参考价值会明显更高
- 即使样本较多，也建议和 `技术面得分`、`年化收益率`、`最大回撤`、`夏普比率` 一起看

### 9.8 结果字段解释

下面这些字段是终端输出和 Excel 结果里最常看的指标。

#### `初筛通过`

表示是否通过技术面硬门槛筛选，不是看综合分决定的。

主要会检查：

- 当前价格是否站上 `20 日线`
- `20 日线` 是否站上 `60 日线`
- 近 `20` 日收益是否为正
- 是否离 `60 日高点` 不太远
- 近 `60` 日最大回撤是否过大
- 近 `20` 日平均成交额是否达到门槛
- 近 `5` 日涨幅是否过热
- 当前价格是否离 `20 日线` 太远

如果 `初筛通过=False`，说明这只股票技术面并没有完全达到脚本定义的强势条件。

#### `技术面得分`

这是技术面综合评分，不是是否通过的布尔值。

它综合考虑：

- 趋势
- 量能
- 风险
- 流动性
- 过热惩罚

一般理解：

- 分数越高，当前走势结构越接近脚本偏好的形态
- 但即使分数高，也不代表一定 `初筛通过`

#### `次日样本数`

不是历史总天数，而是这只股票历史里，满足“强势龙头次日打法”那组条件的次数。

样本数越少，次日上涨概率、次日平均涨跌幅这些统计就越不稳定。

#### `次日上涨概率(折扣后%)`

表示这只股票在历史上出现相同信号后，次日上涨的概率。

这里已经做过：

- 次日开盘可成交口径修正
- 手续费和滑点处理
- 小样本折扣

所以它比简单的历史胜率更保守一些。

#### `次日平均涨跌幅(折扣后%)`

表示历史上出现同类信号后，次日平均收益表现。

同样做过小样本折扣，所以样本少的时候不会看起来过于夸张。

#### `建议出票阈值(%)`

这是脚本根据历史次日回撤情况给出的短线止损/出票参考阈值。

可以理解成：

- 如果你按这套“次日策略”去做
- 次日盘中跌破这个比例附近
- 脚本倾向于认为应该优先止损或出票

#### `近一月走势`

这是脚本根据最近约 `1` 个月（大约 `22` 个交易日）的收盘价变化，自动生成的短期走势摘要。

这个字段通常会同时展示：

- 最近一月涨跌幅
- 一个简短的走势字符线
- 当前趋势标签

例如可能会看到：

- `强势上行 +12.35% ._-==^^##`
- `震荡整理 +1.28% .-~-==--__`
- `明显下行 -16.40% ##^^=--__..`

你可以把它理解成“最近一个月的压缩版 K 线走势提示”。

一般理解：

- `强势上行`：最近一月整体偏强，股价结构更主动
- `震荡上行`：有上涨，但过程不是单边
- `震荡整理`：最近一月偏横盘
- `震荡走弱`：最近一月偏弱
- `明显下行`：最近一月下跌趋势比较清楚

这个字段适合用来快速判断：

- 当前股票是不是还在延续强势
- 最近一月是不是已经明显走弱
- 短线尝试时，走势背景是否还算配合

#### `年化收益率(%)`

来自脚本内置的双均线回测结果，表示按这套回测规则跑历史后，大致折算成年化的收益水平。

一般理解：

- 越高越好
- 但不能单独看，必须结合回撤和夏普率一起看

#### `最大回撤(%)`

表示回测过程中，从某个阶段高点回落到低点的最大跌幅。

一般理解：

- 越低越稳
- 如果这个值过大，说明虽然可能赚钱，但过程会很难持有

#### `夏普比率`

表示单位波动风险对应的收益效率。

一般理解：

- 越高越好
- 夏普率高，说明策略收益相对更稳定
- 夏普率低，说明可能涨得不差，但过程波动太大

#### `综合得分`

这是最终排序时用到的重要分数，综合考虑：

- 板块热度
- 技术面得分
- 回测得分
- 次日统计信号分

所以 `综合得分` 更像是一个最终排序值，而不是单一维度指标。

### 9.9 实际看结果时的建议顺序

如果你是做短线，建议按这个顺序看：

1. 先看 `初筛通过`
2. 再看 `次日样本数`
3. 再看 `短线样本等级` 和 `是否建议参考次日统计`
4. 再看 `近一月走势`
5. 再看 `次日上涨概率(折扣后%)` 和 `次日平均涨跌幅(折扣后%)`
6. 最后结合 `技术面得分`、`最大回撤(%)`、`夏普比率` 和 `综合得分`

一个简单原则：

- 样本不够时，优先信技术面和回测
- 样本足够时，再提高次日统计的权重

## 10. 板块快照

### 10.1 使用板块快照

```bash
python quant_stock_selector.py --hot-sectors --use-sector-snapshot
```

### 10.2 指定快照文件

```bash
python quant_stock_selector.py --hot-sectors --use-sector-snapshot --sector-snapshot-path "sector_snapshot.csv"
```

### 10.3 拉取最新板块数据并更新快照

```bash
python quant_stock_selector.py --hot-sectors --sector-snapshot-path "sector_snapshot.csv"
```

## 11. 企业微信通知

### 11.1 推送选股结果到企业微信

```bash
python quant_stock_selector.py --history-source api --notify-webhook-url "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
```

### 11.2 热门板块分析后推送通知

```bash
python quant_stock_selector.py --hot-sectors --top-sectors 10 --history-source api --notify-webhook-url "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
```

## 12. iTick 订阅

### 12.1 开启 iTick WebSocket 订阅

```bash
python quant_stock_selector.py --subscribe-itick --itick-token "你的token" --history-source api
```

### 12.2 指定订阅股票数量和持续时间

```bash
python quant_stock_selector.py --subscribe-itick --itick-token "你的token" --itick-max-symbols 20 --itick-duration 120 --history-source api
```

### 12.3 订阅多个类型

```bash
python quant_stock_selector.py --subscribe-itick --itick-token "你的token" --itick-types "tick,quote" --history-source api
```

### 12.4 打印非 tick 原始消息

```bash
python quant_stock_selector.py --subscribe-itick --itick-token "你的token" --itick-print-raw --history-source api
```

### 12.5 iTick + 企业微信推送

```bash
python quant_stock_selector.py --subscribe-itick --itick-token "你的token" --notify-webhook-url "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key" --history-source api
```

## 13. 常用组合命令

### 13.1 直接拉最新热门板块并导出结果

```bash
python quant_stock_selector.py --hot-sectors --history-source api --output "latest_hot_sectors.xlsx"
```

### 13.2 分析机器人板块，直接走 API，只显示通过初筛的股票

```bash
python quant_stock_selector.py --sector "机器人" --history-source api --only-passed
```

### 13.3 自定义股票池，本地优先，结果导出

```bash
python quant_stock_selector.py --codes "my_codes.csv" --data-dir "D:\stock_data" --history-source auto --output "my_pool_result.xlsx"
```

### 13.4 自定义股票池，只用本地数据

```bash
python quant_stock_selector.py --codes "my_codes.csv" --data-dir "D:\stock_data" --history-source local
```

### 13.5 热门板块 + API 最新数据 + 导出 + 企业微信通知

```bash
python quant_stock_selector.py --hot-sectors --top-sectors 10 --history-source api --output "result.xlsx" --notify-webhook-url "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
```

## 14. 参数速查

### 14.1 入口选择

- `--hot-sectors`：热门板块分析
- `--sector "板块名"`：分析指定板块
- `--codes "文件路径"`：分析自定义股票池

### 14.2 数据相关

- `--data-source mootdx|akshare|tushare`
- `--data-dir "目录"`
- `--history-source auto|local|api`
- `--start-date YYYYMMDD`
- `--end-date YYYYMMDD`
- `--adjust qfq|hfq`

### 14.3 板块相关

- `--board-type all|concept|industry`
- `--top-sectors N`
- `--max-stocks-per-sector N`
- `--leader-stocks-per-sector N`
- `--use-sector-snapshot`
- `--sector-snapshot-path "xxx.csv"`

### 14.4 筛选和回测相关

- `--fast-period N`
- `--slow-period N`
- `--initial-cash 金额`
- `--commission 比例`
- `--slippage 比例`
- `--stop-loss 比例`
- `--next-day-stop-loss 比例`
- `--next-day-target-pct 比例`
- `--min-avg-turnover-20d 金额`
- `--max-5d-return 比例`
- `--max-close-above-ma20 比例`
- `--allow-st`

### 14.5 展示和导出

- `--only-passed`
- `--top-stocks N`
- `--output "结果文件.xlsx"`

### 14.6 通知和订阅

- `--notify-webhook-url "企业微信webhook"`
- `--subscribe-itick`
- `--itick-token "token"`
- `--itick-max-symbols N`
- `--itick-duration 秒`
- `--itick-types "tick,quote"`
- `--itick-ping-interval 秒`
- `--itick-ws-url "wss://..."`
- `--itick-print-raw`

## 15. 注意事项

- `--history-source local` 必须配合 `--data-dir`
- `--fast-period` 必须小于 `--slow-period`
- `--sector-snapshot-path` 必须是 `.csv`
- `--notify-webhook-url` 必须是有效的企业微信机器人地址
- 如果本地行情目录中同一股票命中多个同优先级文件，脚本会直接报错，避免误读错误文件
- 如果你想强制忽略本地行情，统一拉最新数据，请始终加上 `--history-source api`
