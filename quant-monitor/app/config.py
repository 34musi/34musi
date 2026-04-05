"""
运行时配置：从环境变量与可选的 .env 加载。

- data_dir：本地数据目录（默认项目根下 data/），SQLite 文件默认落在此目录。
- database_url：显式数据库连接串；未设置时使用 SQLite 文件。
- api_key：非空时，受保护路由需请求头 X-API-Key 与之完全一致。
- ingest_data_source：默认行情路线（auto / eastmoney / sina / tencent / baostock），可由请求 Body 覆盖。
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用级配置项，字段名对应环境变量（大写）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "quant-monitor"
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    # 若需覆盖（如 Postgres），可设置环境变量 DATABASE_URL
    database_url: str | None = None
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # 非空则启用：客户端须在 Header 中携带 X-API-Key，与下列值完全一致
    api_key: str = ""
    # slowapi 限流表达式，用于大部分读接口的默认桶
    rate_limit_default: str = "60/minute"
    # AkShare 拉日线：对连接被远端断开、超时等做重试（指数退避）
    akshare_fetch_retries: int = 4
    akshare_retry_base_delay_sec: float = 1.0
    # 批量 ingest 时两只标的之间的间隔，减轻东方财富等源限流概率
    akshare_pause_between_symbols_sec: float = 1.5
    # 东财全 A spot 估值表内存缓存秒数（扩展基本面批量更新时共用）
    fundamentals_spot_cache_ttl_sec: float = 90.0
    # /ingest/test-connection 探测用的 6 位代码（默认平安银行，仅测连通性）
    akshare_test_symbol: str = "000001"
    # 行情路线：auto=东财失败后依次新浪/腾讯/Baostock；亦可固定单一源（见 IngestDataSource）
    ingest_data_source: str = "auto"

    @field_validator("ingest_data_source")
    @classmethod
    def _validate_ingest_data_source(cls, v: str) -> str:
        allowed = frozenset({"auto", "eastmoney", "sina", "tencent", "baostock"})
        x = (v or "auto").strip().lower()
        if x not in allowed:
            raise ValueError(f"ingest_data_source / INGEST_DATA_SOURCE 须为 {sorted(allowed)} 之一")
        return x
    # AkShare 聚合公开页，非交易所实时推送，可能有延迟或缺数
    data_source_note: str = "AkShare 聚合公开数据源，非交易所实时行情，可能存在延时与缺失。"
    disclaimer_short: str = (
        "本工具仅提供基于历史行情的技术性数据分析与展示，不构成投资建议。"
        "市场有风险，决策需谨慎。"
    )


def get_settings() -> Settings:
    """单例式读取配置，并确保 data_dir 目录存在。"""
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def get_database_url() -> str:
    """返回 SQLAlchemy 使用的数据库 URL；未配置 DATABASE_URL 时用 SQLite 文件路径。"""
    s = get_settings()
    if s.database_url:
        return s.database_url
    db_path = (s.data_dir / "quant_monitor.db").resolve()
    # 注意：不要对路径做 quote(%20)，Windows 上 sqlite3 会把 %20 当字面量导致无法打开文件
    return f"sqlite:///{db_path.as_posix()}"
