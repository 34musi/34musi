# quant-monitor

A 股日线趋势监控与信号 API：通过 [AkShare](https://github.com/akfamily/akshare) 拉取公开前复权日线，写入本地数据库，再基于均线、动量、波动等规则输出结构化信号（**不构成投资建议**）。

## 环境要求

- Python 3.10+（推荐）
- 网络可访问 AkShare 所需数据源

## 安装

在 **`quant-monitor`** 目录下执行：

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

## 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **图形控制台（推荐日常使用）**：<http://127.0.0.1:8000/ui> — 按 ①～⑦ 模块分步操作，详见下文「图形控制台使用说明」。
- 交互文档：<http://127.0.0.1:8000/docs>（Swagger UI，面向开发者）
- 服务信息（JSON）：<http://127.0.0.1:8000/>（含 `ui`、`docs` 等字段）
- 探活：<http://127.0.0.1:8000/health>

控制台会将 **API Key** 与 **③ 行情路线** 存在浏览器 **localStorage**（仅本机），请勿在不可信设备上使用；生产环境请配合 HTTPS。

## 图形控制台（/ui）使用说明

启动服务后，在浏览器打开 <http://127.0.0.1:8000/ui>（或部署地址下的 `/ui`）。左侧为功能模块 **①～⑦**，右侧为表单与结果区。数据为**公开日线（前复权）**，**非实时**，**不构成投资建议**。

### ① 入门必读

- 若服务端配置了 `API_KEY`，在输入框填入与服务器一致的密钥，点 **保存到本机浏览器**，后续请求会自动带请求头。
- **测试连接**：确认本机能访问 API；**刷新服务信息**：查看根路径返回的 JSON（含 `ui`、`docs` 等）。
- 可点 **加载自用定位摘要** 查看 `GET /meta/self-use` 的说明性 JSON（与 `docs/SELF_USE_GUIDE.md` 配套）。

### ② 管理自选股票

- 输入 6 位 A 股代码（如 `600519`），**确定添加**；**刷新列表**查看当前自选。
- **③ 批量更新行情**、**⑤ 变动预览** 只处理自选里的标的；单只股票在 **④** 也可直接查信号（仍需本地有足够 K 线）。

### ③ 更新行情数据（K 线从哪里来）

- **行情路线**下拉框：决定调用 AkShare / Baostock 时走哪条数据源（`auto` 为新浪→腾讯→Baostock 链式尝试；可选 **仅东方财富日线**，服务端对相邻东财请求有约 **3–5 秒**随机间隔防限流；也可固定「仅新浪」等）。所选值会写入本机并与 **④⑤** 的 `data_source` 联动。
- **K 线落库位置**：拉取成功后写入本机 SQLite（默认 `data/quant_monitor.db` 的 `bars` 表），之后 **④ 信号**、**③ 底部「查询本地 K 线」** 都读这份库。
- **开始 / 结束日期**（均可空）：
  - **都不填**：从库里已有最后交易日增量更新到今天；
  - **只填结束日**：增量更新到该日为止；
  - **只填开始日**：从该日拉到今日；
  - **两个都填**：拉取该闭区间（适合补历史）。
- **测试数据源连接**：短探测，确认网络与数据源是否可用。
- **开始更新自选行情**：对自选中每一只执行入库（需联网，耗时与标的数量有关）。
- **扩展因子（量化 Demo）**：与日线相互独立，拉取估值/财报同比/主力净流入等，供信号里的扩展字段与合成评分使用。
- **查询本地 K 线**：输入代码与根数，读库展示已入库 OHLCV（**不发起网络请求**），用于确认某标的是否已有足够日线（信号计算至少需要约 **30** 根有效 K 线）。

### ④ 查看信号

- 需本地 **K 线充足**；若提示「K 线数据不足」，请回到 **③** 更新行情，或用 **③ 查询本地 K 线** 核对条数。
- **当前路线**与 **③ 下拉框**一致；勾选 **计算前先增量更新** 时，会按该路线先 `incremental_refresh` 再算信号（较慢，防限流会在标的间间隔）。
- **查看自选全部信号**：对自选逐只计算；**单只股票代码** + **查询该只**：只算一只。

### ⑤ 变动预览

- 将当前算出的信号与服务器**上次缓存**对比，产出 `new` / `shift` 等事件，并刷新缓存。路线与 **③** 联动方式同 **④**。

### ⑥ 说明与免责

- 加载 `GET /meta/disclaimer` 全文（数据源说明与免责声明）。

### ⑦ 决策日志（自用）

- 记录周趋势、计划仓位、是否按计划执行等；可选附加当前信号快照（需该代码 K 线足够）。详见 [docs/SELF_USE_GUIDE.md](docs/SELF_USE_GUIDE.md)。

### 建议的首次使用顺序

**① 填 Key（如需）→ ② 添加自选 → ③ 选好路线并「开始更新自选行情」→ ③ 用「查询本地 K 线」确认条数 → ④ 查看信号**。若某路线失败，可换 `auto` 或其它单一源并重试 **③**。

与「自用决策、一周复盘」配套的**精简版**与 **⑦ 决策日志** 说明，另见 [docs/SELF_USE_GUIDE.md](docs/SELF_USE_GUIDE.md) **§5**。

## 配置（可选）

在项目根目录（`quant-monitor`）放置 `.env`，或通过环境变量覆盖。常用项与 `app/config.py` 中 `Settings` 对应（环境变量名一般为**大写字段名**）：

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | 不设则使用 SQLite：`data/quant_monitor.db` |
| `DATA_DIR` | 数据目录，默认 `quant-monitor/data/` |
| `API_KEY` | 非空时，受保护接口需在请求头携带 `X-API-Key`，值与此完全一致 |
| `API_HOST` / `API_PORT` | 若你用其它方式托管时可参考；`uvicorn` 命令行参数优先生效 |
| `RATE_LIMIT_DEFAULT` | 默认 `60/minute`（slowapi） |
| `EASTMONEY_REQUEST_MIN_INTERVAL_SEC` | 东财日线 `stock_zh_a_hist` 相邻请求最小间隔（秒），默认 `3` |
| `EASTMONEY_REQUEST_MAX_INTERVAL_SEC` | 同上最大间隔（秒），默认 `5`（实际间隔在 min～max 间随机） |
| `INGEST_EASTMONEY_BYPASS_PROXY` | 为 `true` 时，东财日线请求前**临时清除** `HTTP_PROXY`/`HTTPS_PROXY` 等，尝试直连（仅适合可直连外网且代理异常时） |

示例 `.env`：

```env
# API_KEY=my-secret-key
```

## 自用决策路线图（小白向）

仓库内说明与脚本与「自用量化决策」计划对齐（**非投资建议**）：

- 图文说明：[docs/SELF_USE_GUIDE.md](docs/SELF_USE_GUIDE.md)（工具定位、风控红线模板、一周复盘节奏、实盘复盘字段）
- 风控字段示例（请复制后改成自己的数字）：[examples/risk_policy.example.json](examples/risk_policy.example.json)
- 接口：`GET /meta/self-use`（自用定位摘要）、`POST /journal` / `GET /journal`（决策日志，存本机 SQLite）
- 冒烟（无需先起 uvicorn）：`python scripts/smoke_self_use.py`
- 单规则历史检验示例（需已 ingest 足够日线）：`python scripts/backtest_sample_rule.py 600519`

图形控制台 **[/ui](/ui)** 已增加 **「⑦ 决策日志」** 模块。

## 推荐使用流程

1. **（可选）** 设置 `API_KEY` 后，以下接口调用需加请求头：`X-API-Key: <你的密钥>`。  
   根路径 `/`、`/health`、`/docs` 等一般无需 Key。

2. **添加自选**  
   `POST /watchlist`  
   Body：`{"symbol": "600519"}`（6 位 A 股代码；可含非数字字符，服务端会规范为 6 位数字）。

3. **拉取行情并入库**  
   `POST /ingest/update`  
   对自选池中每个标的增量拉取前复权日线并写入 SQLite。自选为空会返回 `400`。

4. **查询信号**  
   - `GET /signals`：自选全部标的  
   - `GET /signals/{symbol}`：单个标的，如 `/signals/600519`  
   - 可选查询参数（与③行情路线对齐）：`pre_refresh=true|false`、`data_source=auto|eastmoney|sina|tencent|baostock`。`pre_refresh=true` 时先按该路线对各标的 **incremental_refresh** 再算信号；响应头含 `X-Quant-Data-Source`、`X-Quant-Pre-Refresh`。图形控制台④会随③下拉框自动传参。

5. **（可选）告警预览**  
   `POST /alerts/preview`  
   对比缓存中的上一版信号与当前计算结果，返回 `new` / `shift` 等事件，并刷新缓存。  
   Body 可选：`{"pre_refresh": true, "data_source": "sina"}`（与 `GET /signals` 含义一致）；返回 JSON 中含 `request` 字段回显本次参数。控制台⑤与③路线联动。

6. **免责与数据源说明**  
   `GET /meta/disclaimer`

7. **（推荐）自用摘要与决策日志**  
   `GET /meta/self-use`  
   `POST /journal` / `GET /journal` / `GET /journal/{id}` / `DELETE /journal/{id}`

## 主要 HTTP 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/ui` | 图形控制台（HTML，不在 OpenAPI 中列出） |
| GET | `/` | 服务名、`ui`、`docs`、简要免责 |
| GET | `/health` | 健康检查 |
| GET | `/meta/auth-status` | 是否要求 API Key（`api_key_required`，供控制台探测） |
| GET | `/meta/disclaimer` | 免责与数据源说明 |
| GET | `/meta/self-use` | 自用工具定位与风控检查摘要（无需 Key） |
| POST / GET / DELETE | `/journal` … | 决策日志（需 Key 时同其它受保护接口） |
| GET | `/watchlist` | 列出自选 |
| POST | `/watchlist` | 添加自选 |
| DELETE | `/watchlist/{symbol}` | 删除自选 |
| GET | `/ingest/test-connection` | 测试本机与 AkShare 数据源连通性（短区间探测） |
| POST | `/ingest/update` | 更新自选日线；Body 可选 `start_date` / `end_date`（区间或增量规则见 `/docs`） |
| POST | `/ingest/fundamentals` | （可选）扩展因子入库 |
| GET | `/quotes/{symbol}/bars` | 本地日线 OHLCV（`limit` 查询参数） |
| GET | `/signals` | 批量信号 |
| GET | `/signals/{symbol}` | 单标的信号 |
| POST | `/alerts/preview` | 信号变更预览并更新缓存 |

具体请求体、响应模型以 `/docs` 为准。

## 注意事项

- 展示数据基于**公开来源日线（前复权）**，**非实时行情**，不适合对时效要求极高的决策场景。
- 接口带频率限制（slowapi），`ingest/update` 等路由另有单独限额。
- `requirements.txt` 中包含 `streamlit`，当前仓库以 **FastAPI API** 为主；若未使用 Streamlit 相关脚本，可忽略该依赖或自行精简。

## 项目结构（简要）

```text
quant-monitor/
  app/
    main.py      # FastAPI 路由与启动生命周期
    config.py    # 配置
    db.py        # SQLAlchemy 模型与会话
    ingest.py    # AkShare 拉取与入库
    signals.py   # 信号计算
    fundamentals.py  # 扩展因子（Demo）
    alerts.py    # 变更检测与 Webhook 占位
    schemas.py   # Pydantic 模型
    static/
      console.html  # /ui 图形控制台静态页
  docs/
    SELF_USE_GUIDE.md  # 自用定位与风控模板
  examples/
    risk_policy.example.json
  scripts/
    smoke_self_use.py       # 冒烟测试
    backtest_sample_rule.py # 单规则检验示例
    validate_fundamentals_demo.py
  data/          # 默认数据目录（含 SQLite）
  requirements.txt
  README.md
```
