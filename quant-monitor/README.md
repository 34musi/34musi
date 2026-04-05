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

- **图形控制台（推荐日常使用）**：<http://127.0.0.1:8000/ui> — 按 ①～⑥ 模块分步填写、测试连接与调用接口，无需在 Swagger 里拼 JSON。
- 交互文档：<http://127.0.0.1:8000/docs>（Swagger UI，面向开发者）
- 服务信息（JSON）：<http://127.0.0.1:8000/>（含 `ui`、`docs` 等字段）
- 探活：<http://127.0.0.1:8000/health>

控制台会将 **API Key 存在浏览器 localStorage**（本机），请勿在不可信设备上使用；生产环境请配合 HTTPS。

## 配置（可选）

在项目根目录（`quant-monitor`）放置 `.env`，或通过环境变量覆盖。常用项与 `app/config.py` 中 `Settings` 对应（环境变量名一般为**大写字段名**）：

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | 不设则使用 SQLite：`data/quant_monitor.db` |
| `DATA_DIR` | 数据目录，默认 `quant-monitor/data/` |
| `API_KEY` | 非空时，受保护接口需在请求头携带 `X-API-Key`，值与此完全一致 |
| `API_HOST` / `API_PORT` | 若你用其它方式托管时可参考；`uvicorn` 命令行参数优先生效 |
| `RATE_LIMIT_DEFAULT` | 默认 `60/minute`（slowapi） |

示例 `.env`：

```env
# API_KEY=my-secret-key
```symbol

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

5. **（可选）告警预览**  
   `POST /alerts/preview`  
   对比缓存中的上一版信号与当前计算结果，返回 `new` / `shift` 等事件，并刷新缓存。

6. **免责与数据源说明**  
   `GET /meta/disclaimer`

## 主要 HTTP 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/ui` | 图形控制台（HTML，不在 OpenAPI 中列出） |
| GET | `/` | 服务名、`ui`、`docs`、简要免责 |
| GET | `/health` | 健康检查 |
| GET | `/meta/auth-status` | 是否要求 API Key（`api_key_required`，供控制台探测） |
| GET | `/meta/disclaimer` | 免责与数据源说明 |
| GET | `/watchlist` | 列出自选 |
| POST | `/watchlist` | 添加自选 |
| DELETE | `/watchlist/{symbol}` | 删除自选 |
| GET | `/ingest/test-connection` | 测试本机与 AkShare 数据源连通性（短区间探测） |
| POST | `/ingest/update` | 更新自选日线；Body 可选 `start_date` / `end_date`（区间或增量规则见 `/docs`） |
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
    alerts.py    # 变更检测与 Webhook 占位
    schemas.py   # Pydantic 模型
    static/
      console.html  # /ui 图形控制台静态页
  data/          # 默认数据目录（含 SQLite）
  requirements.txt
  README.md
```
