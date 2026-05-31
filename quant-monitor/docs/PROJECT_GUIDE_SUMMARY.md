# quant-monitor 文档总览

这份文档把下面 3 份说明合并整理成一份便于快速阅读的总览：

- `README.md`
- `docs/SELF_USE_GUIDE.md`
- `quant_stock_selector_commands.md`

如果你只想快速知道“这个项目是干什么的、先看哪个入口、命令怎么跑、我自己怎么用”，看这一份就够了。

## 1. 这个项目是做什么的

`quant-monitor` 是一个面向 A 股日线数据的辅助分析项目，主要用途是：

- 管理自选股票
- 拉取并更新本地行情数据
- 计算趋势/量能/风险等信号
- 通过图形界面或接口查看结果
- 通过量化选股模块做板块筛选、个股评分和回测
- 记录自己的决策日志，做复盘

它的定位是：

- **辅助决策**
- **不自动下单**
- **不构成投资建议**

## 2. 这 3 份原始文档分别负责什么

### `README.md`

这是项目总说明，适合第一次接触仓库时先看。

它主要讲：

- 项目整体结构
- 怎么安装依赖
- 怎么启动 FastAPI 服务
- `/ui` 图形控制台怎么用
- 常见接口有哪些
- 推荐的整体使用流程

一句话理解：

> `README.md` 负责回答“整个项目怎么跑起来、能做什么”。

### `docs/SELF_USE_GUIDE.md`

这是“自用场景”的说明，适合你已经知道项目功能以后，再看自己该怎么用。

它主要讲：

- 自用定位
- 风控红线
- 一周复盘节奏
- 决策日志怎么配合使用
- 常用脚本怎么跑
- 自用情况下推荐的使用顺序

一句话理解：

> `SELF_USE_GUIDE.md` 负责回答“我自己拿这套工具辅助决策时，怎么更稳地用”。

### `quant_stock_selector_commands.md`

这是量化选股命令的专项说明，适合你只想跑 CLI，不想看整套项目。

它主要讲：

- `python -m app.quant_stock_selector` 怎么运行
- 参数有哪些
- 常用命令怎么写
- 自定义股票池文件怎么准备
- 输出结果大概包含哪些字段

一句话理解：

> `quant_stock_selector_commands.md` 负责回答“量化选股命令具体怎么写”。

## 3. 你应该按什么顺序看

如果你是第一次接触这个项目，建议按下面顺序：

1. 先看 `README.md`
2. 再看 `docs/SELF_USE_GUIDE.md`
3. 最后按需要看 `quant_stock_selector_commands.md`

原因很简单：

- `README.md` 先帮你建立全局认识
- `SELF_USE_GUIDE.md` 再告诉你怎么按自用节奏落地
- `quant_stock_selector_commands.md` 最后负责命令细节

## 4. 项目里有两条主要使用方式

这个仓库实际可以从两条主线去理解。

### 主线 A：Web 服务 / 图形控制台

适合想通过浏览器界面操作的人。

这条线的核心是：

- 启动服务
- 打开 `/ui`
- 管理自选
- 更新行情
- 查看信号
- 做变动预览
- 写决策日志

常用启动命令：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后常用地址：

- `http://127.0.0.1:8000/ui`
- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/health`

这条线主要对应：

- `README.md`
- `docs/SELF_USE_GUIDE.md`

### 主线 B：量化选股命令行

适合你想直接跑板块筛选和个股分析。

推荐入口：

```bash
python -m app.quant_stock_selector --help
```

这条线主要对应：

- `quant_stock_selector_commands.md`

## 5. 脚本怎么运行

这个项目里，常见会接触到两种“运行方式”。

### 方式 1：运行 Web 服务

这是整套项目最完整的运行方式，适合日常使用。

运行命令：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

运行后你可以通过浏览器访问：

- `/ui`：图形控制台
- `/docs`：接口文档
- `/health`：健康检查

这条方式适合：

- 管理自选
- 更新本地行情
- 查看信号
- 预览变动
- 写决策日志

你可以把它理解为：

> 这是“整个项目”的运行方式。

### 方式 2：运行量化选股脚本

这是只跑筛股逻辑的方式，不需要先进入 `/ui`。

推荐运行命令：

```bash
python -m app.quant_stock_selector --help
```

常见直接运行方式：

```bash
python -m app.quant_stock_selector
```

这条方式适合：

- 分析热门板块
- 分析指定板块
- 分析自定义股票池
- 导出候选股票结果

你可以把它理解为：

> 这是“项目里的量化选股子模块”的运行方式。

### 历史脚本说明

仓库根目录还保留了一个历史脚本：

```bash
python quant_stock_selector.py
```

但当前更推荐使用：

```bash
python -m app.quant_stock_selector
```

原因是后者对应当前模块化实现，和现在仓库里的包结构更一致。

## 6. 脚本有哪些运行模式

这里说的“模式”，主要是指 `app.quant_stock_selector` 这套脚本在不同参数下会怎么运行。

### 模式 1：热门板块模式

触发方式：

```bash
python -m app.quant_stock_selector --hot-sectors
```

如果你什么都不传，默认也会进入这个模式：

```bash
python -m app.quant_stock_selector
```

它的运行过程是：

1. 从数据源获取热门板块排名
2. 取前 `N` 个板块
3. 拉每个板块的成分股
4. 获取每只股票的历史行情
5. 做技术面初筛和双均线回测
6. 按综合得分排序输出

适合场景：

- 每天快速看当前强势板块
- 从热门板块里找候选股票

### 模式 2：指定板块模式

触发方式：

```bash
python -m app.quant_stock_selector --sector "机器人"
```

它的运行过程是：

1. 直接按板块名查成分股
2. 不再先做热门板块筛选
3. 对该板块里的股票逐只拉历史数据
4. 做筛选、回测、评分
5. 输出结果

适合场景：

- 你已经明确想研究某个板块
- 不想先从热门板块排序开始

### 模式 3：自定义股票池模式

触发方式：

```bash
python -m app.quant_stock_selector --codes "my_codes.csv"
```

它的运行过程是：

1. 直接读取你给的股票列表文件
2. 不再去按板块选股
3. 对文件中的股票逐只拉历史数据
4. 做筛选、回测、评分
5. 输出结果

适合场景：

- 你已经有自己的观察池
- 只想评估一组指定股票

### 模式 4：本地行情优先模式

这不是独立入口模式，而是一个“数据读取模式”。

触发方式：

```bash
python -m app.quant_stock_selector --codes "my_codes.csv" --data-dir "D:\stock_data"
```

它的真实行为是：

1. 先去 `--data-dir` 目录中寻找本地历史文件
2. 找到就优先使用本地数据
3. 找不到再回退到远端数据源

注意：

- 这不是“纯本地模式”
- 当前包里没有通过参数强制指定“只用本地”或“只用 API”的开关

### 模式 5：不同数据源模式

这也是“数据获取模式”，由 `--data-source` 控制。

#### `akshare`

```bash
python -m app.quant_stock_selector --data-source akshare --hot-sectors
```

特点：

- 板块覆盖通常更全
- 更适合热门板块筛选

#### `mootdx`

```bash
python -m app.quant_stock_selector --data-source mootdx --hot-sectors
```

特点：

- 通达信协议
- 在一些环境下更稳定
- 但板块覆盖通常没有 `akshare` 全

#### `tushare`

```bash
python -m app.quant_stock_selector --data-source tushare --tushare-token "你的token"
```

特点：

- 需要 token
- 适合已经有 TuShare 使用条件的场景
- 能不能顺利使用还取决于 token 和接口权限

## 7. 量化选股模块到底做什么

`app.quant_stock_selector` 这套模块的核心流程是：

1. 选择分析对象
2. 获取板块或股票池
3. 拉取历史行情
4. 执行技术面初筛
5. 执行双均线回测
6. 对股票做综合评分
7. 打印或导出结果

它适合的场景：

- 看热门板块
- 看指定板块
- 跑自定义股票池
- 导出候选结果做人工复核

## 8. 量化选股命令最常用的几种写法

### 查看帮助

```bash
python -m app.quant_stock_selector --help
```

### 默认运行热门板块分析

```bash
python -m app.quant_stock_selector
```

### 指定数据源并分析热门板块

```bash
python -m app.quant_stock_selector --data-source akshare --hot-sectors --top-sectors 5
```

### 分析指定板块

```bash
python -m app.quant_stock_selector --sector "机器人"
```

### 分析自定义股票池

```bash
python -m app.quant_stock_selector --codes "my_codes.csv"
```

### 导出结果

```bash
python -m app.quant_stock_selector --output "result.xlsx"
```

### 使用本地行情目录做补充

```bash
python -m app.quant_stock_selector --codes "my_codes.csv" --data-dir "D:\stock_data"
```

这里要特别注意：

- 当前包里，`--data-dir` 的含义是“**先找本地，再回退接口**”
- 它不是“强制只用本地文件”的意思

## 9. 当前量化选股命令真正支持什么

当前 `app.quant_stock_selector` 包入口，重点支持这些参数类别：

### 入口参数

- `--hot-sectors`
- `--sector`
- `--codes`

### 数据源参数

- `--data-source akshare|tushare|mootdx`
- `--tushare-token`
- `--data-dir`
- `--start-date`
- `--end-date`
- `--adjust`

### 板块范围参数

- `--board-type`
- `--top-sectors`
- `--max-stocks-per-sector`

### 回测与输出参数

- `--fast-period`
- `--slow-period`
- `--initial-cash`
- `--commission`
- `--stop-loss`
- `--only-passed`
- `--top-stocks`
- `--output`

## 10. 最容易混淆的地方

这个项目里容易混淆的点，主要有下面几个。

### 不是所有文档都在讲同一层东西

- `README.md` 讲整个项目
- `SELF_USE_GUIDE.md` 讲自用方法
- `quant_stock_selector_commands.md` 讲量化选股命令

所以看到重复内容时，不代表它们冲突，很多只是站在不同角度写的。

### 量化选股命令不等于整个项目

`python -m app.quant_stock_selector` 只是项目里的一个命令行模块。

它不是整个 `quant-monitor` 的全部功能，也不等于 `/ui` 控制台。

### 根目录历史脚本和包入口不是一回事

仓库里还有根目录的历史脚本 `quant_stock_selector.py`，但现在更推荐使用：

```bash
python -m app.quant_stock_selector
```

也就是说：

- 想稳定跟着当前模块化实现走，用 `app.quant_stock_selector`
- 历史脚本保留着，但不是优先入口

## 11. 如果你只是想自用，最简单的理解方式

你可以把整个项目拆成 3 个动作：

### 第一步：把项目跑起来

看 `README.md`，完成：

- 安装依赖
- 启动服务
- 打开 `/ui`

### 第二步：按自己的节奏使用

看 `docs/SELF_USE_GUIDE.md`，明确：

- 你的风控底线
- 你的复盘节奏
- 你的决策日志怎么写

### 第三步：需要批量选股时再用 CLI

看 `quant_stock_selector_commands.md`，跑：

```bash
python -m app.quant_stock_selector ...
```

## 12. 推荐的实际使用路径

如果你平时是“自用辅助决策”为主，建议这么用：

1. 用 `README.md` 把服务先跑起来
2. 进入 `/ui` 管理自选、更新行情、看信号
3. 用 `docs/SELF_USE_GUIDE.md` 约束自己的风控和复盘节奏
4. 需要批量筛板块或导出候选时，再使用 `app.quant_stock_selector`

如果你主要就是跑命令筛选股票，建议这么用：

1. 先确认依赖和数据源可用
2. 直接看 `quant_stock_selector_commands.md`
3. 优先使用：

```bash
python -m app.quant_stock_selector --data-source akshare --hot-sectors --top-sectors 5 --output result.xlsx
```

## 13. 一页记住

如果你只记一句话，可以这样记：

- `README.md`：整个项目怎么跑
- `SELF_USE_GUIDE.md`：我自己怎么稳妥地用
- `quant_stock_selector_commands.md`：量化选股命令怎么写

如果你只看这一份总览，建议之后按下面方式继续：

- 想启动项目：回到 `README.md`
- 想按自用方式落地：看 `docs/SELF_USE_GUIDE.md`
- 想直接跑筛股命令：看 `quant_stock_selector_commands.md`
