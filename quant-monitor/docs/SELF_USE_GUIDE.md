# 小白自用量化决策：工具定位与风控模板

本仓库 **quant-monitor** 设计为 **自用辅助决策**，**不提供自动下单**，也不构成投资建议。

## 1. 工具定位（请先写进自己的笔记）

| 选项 | 本工具默认对齐 |
|------|----------------|
| 仅辅助看盘、记录、信号参考 | **是**（推荐） |
| 自动下单 / 程序化交易 | **否**（需自行接券商 API 与合规流程，不在本仓库范围） |

## 2. 风控红线（复制到笔记或 `examples/risk_policy.example.json` 后改成你的数字）

建议在开始用信号辅助决策前，手写或打印：

1. **单笔最大亏损**：占账户总资产不超过 ____%。  
2. **单标的 / 总仓位上限**：不超过 ____%。  
3. **停机条件**（满足任一条即停止按信号开新仓）：  
   - 连续亏损 ____ 笔；  
   - 单日回撤超过 ____%；  
   - 连续 ____ 周信号与结果严重背离（由你定义「背离」）。  
4. **决策频率**：例如「以一周为窗口判断趋势」→ 固定 **每周 ____** 复盘，避免盘中情绪化改计划。

## 3. 与「一周趋势」节奏的配合

- 主看 **日线**；指标需要更长历史时，保持本地 **至少约 60～120 个交易日** 的日线数据。  
- 每周固定时间：更新行情 → 看 `GET /signals` → 在 **决策日志**（`POST /journal`）写 3 条以内依据。  
- 若涉及实盘：在日志里填 **计划仓位 %** 与事后 **是否按计划执行**，便于复盘「信号 vs 手」。

## 4. 仓库内相关能力

| 能力 | 说明 |
|------|------|
| `GET /ui` | **图形控制台**（浏览器操作自选、拉行情、看信号、热门填充、⑨ 量化选股、⑦ 决策日志等）；详细步骤见下文 §5 与根目录 README |
| `GET /meta/self-use` | 返回自用定位与风控检查项摘要（JSON） |
| `POST /journal` / `GET /journal` | 决策与执行记录 |
| `scripts/smoke_self_use.py` | 本地冒烟：验证服务路由与数据库 |
| `scripts/backtest_sample_rule.py` | 单规则历史检验示例（需本地已有 K 线） |
| `examples/risk_policy.example.json` | 风控字段模板 |

## 5. 图形控制台（/ui）— 与 README 同步摘要

完整按模块说明（①～⑦、⑨、日期规则、扩展因子、本地 K 线查询、与「K 线不足」排查）见仓库根目录 **[README.md](../README.md)** 中的章节 **「图形控制台（/ui）使用说明」**。

自用场景下的**建议顺序**（与 README 一致）：

1. **①** 若启用了 API Key，先保存到本机浏览器。  
2. **②** 添加自选标的。  
3. **③** 选择行情路线后 **开始更新自选行情**；可用同页 **查询本地 K 线** 确认条数（信号侧约需 **30** 根以上有效日线）。  
4. **④** 查看信号；**⑦** 写决策日志（与上文 §3「一周趋势」节奏配合）。

**数据从哪来**：日线经 AkShare 按 **③ 下拉「行情路线」** 拉取后写入本机 SQLite；**④⑤** 与 **③** 路线联动。控制台会把 Key 与路线存在浏览器 localStorage（仅本机）。

## 6. 常用命令行（在仓库根目录 `quant-monitor` 下执行）

以下路径以 **`quant-monitor`** 为当前工作目录（本文件在 `docs/`，可先 `cd ..` 回到根目录）。与 [README.md](../README.md) 中的启动方式一致。

### 6.1 启动 Web 服务（FastAPI）

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后浏览器可打开：`http://127.0.0.1:8000/ui`（控制台）、`/docs`（接口说明）。

### 6.2 单元测试

```bash
python -m pytest
```

（配置见根目录 `pytest.ini`：`pythonpath = app`，测试文件在 `app/test_*.py`。若在个别环境下出现 `quant_stock_selector` 导入与根目录单文件 `quant_stock_selector.py` 冲突，可临时改名该文件或仅在保证 `app` 包优先于根目录脚本时运行。）

### 6.3 `scripts/` 脚本

| 文件 | 命令示例 |
|------|----------|
| [scripts/smoke_self_use.py](../scripts/smoke_self_use.py) | `python scripts/smoke_self_use.py` |
| [scripts/run_forecast_validate.py](../scripts/run_forecast_validate.py) | `python scripts/run_forecast_validate.py 600519` |
| | `python scripts/run_forecast_validate.py 600519 --horizon 10 --min-train 150 --json` |
| [scripts/backtest_sample_rule.py](../scripts/backtest_sample_rule.py) | `python scripts/backtest_sample_rule.py 600519` |
| [scripts/validate_fundamentals_demo.py](../scripts/validate_fundamentals_demo.py) | `python scripts/validate_fundamentals_demo.py` |
| | `python scripts/validate_fundamentals_demo.py 600519` |

说明：`run_forecast_validate` / `backtest_sample_rule` **依赖本地 SQLite 已入库日线**（需先通过 API 或控制台 **③** 拉行情）。`validate_fundamentals_demo` **需联网**（AkShare 拉扩展因子示例）。

### 6.4 量化选股 CLI（与包 `app/quant_stock_selector` 同构）

推荐用 **模块方式** 调用（避免与根目录 `quant_stock_selector.py` 文件名混淆时优先用此写法）：

```bash
python -m app.quant_stock_selector --help
python -m app.quant_stock_selector --data-source akshare --hot-sectors --top-sectors 5
```

根目录另有历史单文件 **[quant_stock_selector.py](../quant_stock_selector.py)**，但它和当前包入口的参数集不完全一致；日常使用请优先以上面的模块入口为准。

如果你确实要运行那个历史单文件，入口一般为：

```bash
python quant_stock_selector.py --help
```

当前 `app/quant_stock_selector` 包入口的完整参数说明见 **[quant_stock_selector_commands.md](../quant_stock_selector_commands.md)**。与控制台 **⑨ 量化选股** 对应的 HTTP 接口为 **`POST /research/sector-screen`**（需服务已启动且按需配置 API Key）。

市场有风险，决策需谨慎。
