# `app.quant_stock_selector` 用法手册

本文档按当前代码实现整理，基于：

- `app/quant_stock_selector/cli.py`
- `app/quant_stock_selector/pipeline.py`
- `app/quant_stock_selector/histories.py`
- `app/quant_stock_selector/evaluation.py`

也就是说，以下内容以当前包入口 `python -m app.quant_stock_selector` 为准，不再混入旧版独立脚本里那些已经不在这个包中生效的参数。

## 1. 推荐入口

在仓库根目录 `quant-monitor` 下执行：

```bash
python -m app.quant_stock_selector --help
```

如果你的 `PYTHONPATH` 只包含 `app/`，也可以这样执行：

```bash
python -m quant_stock_selector --help
```

## 2. 当前代码的实际执行流程

当前包的运行链路是：

1. 解析参数。
2. 根据 `--hot-sectors`、`--sector`、`--codes` 选择分析对象。
3. 拉取板块成分股。
4. 对每只股票优先尝试读取本地历史数据；如果没命中本地文件，再走数据源接口拉历史行情。
5. 对每只股票执行技术面初筛与双均线回测。
6. 在终端打印板块排名与股票排名。
7. 如果传了 `--output`，导出 Excel。

这里最容易和旧文档混淆的一点是：

- 当前包 **没有** `--history-source`
- 当前包 **没有** `--leader-stocks-per-sector`
- 当前包 **没有** `--slippage`
- 当前包 **没有** `--allow-st`
- 当前包 **没有** 企业微信通知、iTick、次日统计、板块快照等增强参数

## 3. 默认行为

如果你没有传 `--hot-sectors`、`--sector`、`--codes` 中任何一个，程序会自动启用：

```bash
--hot-sectors
```

也就是默认跑“热门板块分析”。

## 4. 当前版本支持的参数

### 4.1 入口选择

- `--hot-sectors`：分析热门板块
- `--sector`：分析指定板块
- `--codes`：分析自定义股票池文件，支持 `csv/xlsx`

### 4.2 数据源与行情

- `--data-source akshare|tushare|mootdx`
- `--tushare-token <token>`
- `--data-dir <目录>`
- `--start-date YYYYMMDD`
- `--end-date YYYYMMDD`
- `--adjust qfq|hfq`

说明：

- `--data-dir` 在当前包里的真实行为是“**启用本地优先**”。只要传了这个目录，程序会先在目录里找股票历史文件；如果找不到，再调用远端数据源。
- 当前包 **不会** 通过参数强制切换成“只本地”或“只 API”；这和旧版独立脚本不同。
- 使用 `tushare` 时，需要传 `--tushare-token` 或提前设置 `TUSHARE_TOKEN`。

### 4.3 板块范围

- `--board-type all|concept|industry`
- `--top-sectors N`
- `--max-stocks-per-sector N`

说明：

- `--top-sectors` 只在热门板块模式下生效。
- `--max-stocks-per-sector` 用来限制每个板块最多分析多少只股票。

### 4.4 回测与筛选

- `--fast-period N`
- `--slow-period N`
- `--initial-cash 金额`
- `--commission 比例`
- `--stop-loss 比例`
- `--only-passed`

说明：

- `--fast-period` 必须小于 `--slow-period`。
- 技术面初筛要求历史数据足够长，当前实现里建议至少准备 **120 个交易日以上** 的有效日线。

### 4.5 展示与导出

- `--top-stocks N`
- `--output 结果文件.xlsx`

说明：

- 终端默认展示综合排序前 `N` 只股票。
- 导出时当前包会生成两个工作表：
  - `hot_sectors`
  - `candidate_stocks`

## 5. 常用命令

### 5.1 查看帮助

```bash
python -m app.quant_stock_selector --help
```

### 5.2 默认运行热门板块分析

```bash
python -m app.quant_stock_selector
```

等价于显式写法：

```bash
python -m app.quant_stock_selector --hot-sectors
```

### 5.3 分析前 10 个热门板块

```bash
python -m app.quant_stock_selector --hot-sectors --top-sectors 10
```

### 5.4 只分析概念板块

```bash
python -m app.quant_stock_selector --hot-sectors --board-type concept
```

### 5.5 分析指定板块

```bash
python -m app.quant_stock_selector --sector "机器人"
```

### 5.6 分析自定义股票池

```bash
python -m app.quant_stock_selector --codes "my_codes.csv"
```

或：

```bash
python -m app.quant_stock_selector --codes "my_codes.xlsx"
```

### 5.7 使用本地目录做历史行情补充

```bash
python -m app.quant_stock_selector --codes "my_codes.csv" --data-dir "D:\stock_data"
```

这条命令的真实含义是：

- 先在 `D:\stock_data` 中查找对应股票历史文件
- 没找到再走 `--data-source` 对应的远端接口

### 5.8 使用 `akshare`

```bash
python -m app.quant_stock_selector --data-source akshare --hot-sectors
```

### 5.9 使用 `mootdx`

```bash
python -m app.quant_stock_selector --data-source mootdx --hot-sectors
```

### 5.10 使用 `tushare`

```bash
python -m app.quant_stock_selector --data-source tushare --tushare-token "你的token" --hot-sectors
```

PowerShell 也可以先设置环境变量：

```powershell
$env:TUSHARE_TOKEN="你的token"
python -m app.quant_stock_selector --data-source tushare --hot-sectors
```

### 5.11 指定历史区间

```bash
python -m app.quant_stock_selector --start-date 20240101 --end-date 20241231
```

### 5.12 调整双均线参数

```bash
python -m app.quant_stock_selector --fast-period 5 --slow-period 20
```

### 5.13 只输出通过初筛的股票

```bash
python -m app.quant_stock_selector --only-passed
```

### 5.14 只在终端展示前 50 只股票

```bash
python -m app.quant_stock_selector --top-stocks 50
```

### 5.15 导出结果到 Excel

```bash
python -m app.quant_stock_selector --output "result.xlsx"
```

### 5.16 指定板块并导出结果

```bash
python -m app.quant_stock_selector --sector "算力" --output "sector_result.xlsx"
```

## 6. 自定义股票池文件格式

`--codes` 对应的文件至少需要有一列代码。下面这些列名都可以被识别：

- `code`
- `代码`

如果还带名称列，也会一起读取：

- `name`
- `名称`

一个最简单的 `csv` 例子：

```csv
code,name
600519,贵州茅台
300750,宁德时代
002594,比亚迪
```

## 7. 结果里会看到什么

当前包的终端输出主要分两部分：

### 7.1 热门板块

会打印板块热度相关字段，例如：

- `sector_name`
- `board_type`
- `hot_score`
- `change_pct`
- `advancers_ratio`
- `leader_change_pct`
- `turnover_rate`

### 7.2 候选股票

会打印股票评分结果，例如：

- `sector_name`
- `code`
- `name`
- `screen_passed`
- `sector_hot_score`
- `screen_score`
- `annual_return_pct`
- `max_drawdown_pct`
- `sharpe_ratio`
- `final_score`

## 8. 几个重要的真实限制

### 8.1 本地行情读取不是“纯本地模式”

当前 `app/quant_stock_selector/histories.py` 的逻辑是：

- 命中本地文件就优先使用
- 没命中就回退到远端数据源

所以传了 `--data-dir` 也不代表一定不会联网。

### 8.2 `tushare` 在当前包里不是所有能力都和 `akshare` 一样

当前数据源抽象允许选择 `tushare`，但实际可用性还取决于：

- 是否提供了 token
- 对应接口是否可用
- 板块数据权限是否满足

如果你主要看热门板块覆盖，通常 `akshare` 更稳妥。

### 8.3 历史数据太短会被跳过

当前包里：

- 技术面初筛需要较长历史
- 双均线回测也需要至少慢均线周期以上数据

所以某只股票历史不足时，会被直接跳过。

## 9. 当前包不支持的旧参数

如果你看到旧文档或历史聊天里出现以下参数，请注意它们 **不是当前 `app/quant_stock_selector` 包入口支持的参数**：

- `--history-source`
- `--leader-stocks-per-sector`
- `--slippage`
- `--next-day-stop-loss`
- `--next-day-target-pct`
- `--allow-st`
- `--min-avg-turnover-20d`
- `--max-5d-return`
- `--max-close-above-ma20`
- `--use-sector-snapshot`
- `--sector-snapshot-path`
- `--notify-webhook-url`
- `--subscribe-itick`
- `--itick-*`

这些参数属于另外一套历史增强脚本能力，不能直接套到当前包入口上。

## 10. 一句话总结

如果你现在只是想按当前包代码稳定使用，最安全的记法就是：

```bash
python -m app.quant_stock_selector --data-source akshare --hot-sectors --top-sectors 5 --output result.xlsx
```

如果要混合本地行情：

```bash
python -m app.quant_stock_selector --codes "my_codes.csv" --data-dir "D:\stock_data" --output result.xlsx
```
