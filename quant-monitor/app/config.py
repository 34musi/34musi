"""
应用运行时配置：从环境变量与可选 `.env` 加载，供全项目统一读取。

## 功能作用

本模块定义 `Settings`（Pydantic Settings）及两个入口函数，集中管理 quant-monitor
的服务端参数，避免在业务代码中散落 `os.environ` 读取。

加载顺序（Pydantic Settings 默认行为）：

1. 字段默认值（代码内）
2. 项目根目录 `.env` 文件（若存在）
3. 进程环境变量（优先级最高，覆盖 `.env`）

环境变量名与字段名对应，一般为**大写**（如 `API_KEY`、`INGEST_DATA_SOURCE`）。

## 主要配置分组

| 分组 | 代表字段 | 用途 |
|------|----------|------|
| 基础 | `app_name`, `data_dir` | 应用标识与本地数据目录 |
| 数据库 | `database_url` | SQLAlchemy 连接串；未设则用 `data_dir/quant_monitor.db` |
| API 服务 | `api_host`, `api_port`, `api_key` | 监听地址与可选的 `X-API-Key` 鉴权 |
| 限流 | `rate_limit_default` | slowapi 默认桶（如 `60/minute`） |
| 行情拉取 | `ingest_data_source`, `akshare_*`, `eastmoney_*` | 默认路线、重试、间隔、代理绕过 |
| TuShare | `tushare_token` | ingest 路线 `tushare` 时使用的 token |
| 自选 | `watchlist_auto_ingest_days` | 添加自选后自动拉取的大致日历天数 |
| 基本面 | `fundamentals_spot_cache_ttl_sec` | 东财全 A spot 估值表内存缓存 TTL |
| 合规文案 | `data_source_note`, `disclaimer_short` | API / 控制台展示的数据来源与免责声明 |

## 对外接口

| 函数 | 用途 |
|------|------|
| `get_settings()` | 读取配置单例；并确保 `data_dir` 目录存在 |
| `get_database_url()` | 返回 SQLAlchemy URL（显式 `DATABASE_URL` 或 SQLite 文件路径） |

## 使用约定

- 业务模块应通过 `get_settings()` / `get_database_url()` 访问配置，勿直接实例化 `Settings()`。
- `ingest_data_source` 可被单次请求的 Body / Query 覆盖；未传时回退到本配置。
- Windows 上 SQLite 路径使用 `as_posix()`，**不要**对路径做 URL 编码（`%20` 会导致无法打开文件）。
"""

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    应用级配置项。

    字段名对应环境变量（大写）；详见模块顶部说明与 README「配置」一节。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 基础 ---
    app_name: str = "quant-monitor"
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"

    # --- 数据库 ---
    # 若需覆盖（如 Postgres），可设置环境变量 DATABASE_URL
    database_url: str | None = None

    # --- API 服务 ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # 非空则启用：客户端须在 Header 中携带 X-API-Key，与下列值完全一致
    api_key: str = ""

    # --- 限流（slowapi） ---
    rate_limit_default: str = "60/minute"

    # --- AkShare / 批量 ingest 通用 ---
    # 拉日线：对连接被远端断开、超时等做重试（指数退避）
    akshare_fetch_retries: int = 4
    akshare_retry_base_delay_sec: float = 1.0
    # 批量 ingest 时两只标的之间的间隔，减轻数据源限流概率
    akshare_pause_between_symbols_sec: float = 1.5
    # /ingest/test-connection 探测用的 6 位代码（默认平安银行，仅测连通性）
    akshare_test_symbol: str = "000001"

    # --- 东财日线（stock_zh_a_hist）请求节流 ---
    # 相邻两次请求之间的随机间隔（秒），全局串行，减轻限流
    eastmoney_request_min_interval_sec: float = 3.0
    eastmoney_request_max_interval_sec: float = 5.0
    # 为 True 时，调用东财日线前临时清除 HTTP(S)_PROXY 等环境变量，尝试直连
    # （仅当本机可直连外网且代理损坏时使用）
    ingest_eastmoney_bypass_proxy: bool = False

    # --- 基本面 / 扩展因子 ---
    # 东财全 A spot 估值表内存缓存秒数（扩展基本面批量更新时共用）
    fundamentals_spot_cache_ttl_sec: float = 90.0

    # --- 默认行情路线 ---
    # auto=新浪失败后依次腾讯/Baostock；eastmoney=仅东财日线（带请求间隔）；
    # akshare 与 eastmoney 等价；mootdx / tushare 见文档
    ingest_data_source: str = "auto"
    # TuShare ingest 路线用；亦可仅设环境变量 TUSHARE_TOKEN
    tushare_token: str = ""

    # --- 自选 ---
    # POST /watchlist 添加自选后自动拉取日线的大致日历天数（约近 N 日，非精确交易日）
    watchlist_auto_ingest_days: int = 30

    # --- AI 潜力测算（⑦ OpenAI 兼容接口） ---
    ai_api_key: str = ""
    ai_api_base: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_timeout_sec: float = 90.0
    ai_json_mode: bool = True

    # --- 合规与展示文案 ---
    # AkShare 聚合公开页，非交易所实时推送，可能有延迟或缺数
    data_source_note: str = "AkShare 聚合公开数据源，非交易所实时行情，可能存在延时与缺失。"
    disclaimer_short: str = (
        "本工具仅提供基于历史行情的技术性数据分析与展示，不构成投资建议。"
        "市场有风险，决策需谨慎。"
    )

    @field_validator("ingest_data_source")
    @classmethod
    def _validate_ingest_data_source(cls, v: str) -> str:
        """校验并规范化行情路线字符串（小写、去空白）。"""
        allowed = frozenset(
            {"auto", "eastmoney", "akshare", "sina", "tencent", "baostock", "mootdx", "tushare"}
        )
        x = (v or "auto").strip().lower()
        if x not in allowed:
            raise ValueError(f"ingest_data_source / INGEST_DATA_SOURCE 须为 {sorted(allowed)} 之一")
        return x

    @model_validator(mode="after")
    def _eastmoney_interval_order(self):
        """东财请求间隔：max 不得小于 min。"""
        if self.eastmoney_request_max_interval_sec < self.eastmoney_request_min_interval_sec:
            raise ValueError("eastmoney_request_max_interval_sec 须 >= eastmoney_request_min_interval_sec")
        return self


def get_settings() -> Settings:
    """
    读取应用配置。

    每次调用会重新解析环境变量与 `.env`；并确保 `data_dir` 目录存在（含父级）。
    业务代码中广泛使用，相当于轻量「按需加载」而非严格进程级单例。
    """
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def get_database_url() -> str:
    """
    返回 SQLAlchemy 使用的数据库 URL。

    若 `database_url` 已配置则直接返回；否则使用 SQLite 文件
    `{data_dir}/quant_monitor.db`。
    """
    s = get_settings()
    if s.database_url:
        return s.database_url
    db_path = (s.data_dir / "quant_monitor.db").resolve()
    # 注意：不要对路径做 quote(%20)，Windows 上 sqlite3 会把 %20 当字面量导致无法打开文件
    return f"sqlite:///{db_path.as_posix()}"
