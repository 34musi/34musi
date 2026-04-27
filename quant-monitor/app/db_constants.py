"""
数据库相关的“静态数据/常量”集中存放，避免与 ORM / 会话管理混在一起。
"""

WATCHLIST_ORIGIN_MANUAL = "manual"
WATCHLIST_ORIGIN_AUTO_HOT = "auto_hot"
# ⑨ 量化选股（sector-screen）同步写入的自选；与 auto_hot 同属「非手动」
WATCHLIST_ORIGIN_AUTO_QUANT = "auto_quant"


FUNDAMENTAL_SNAPSHOT_SQLITE_ALTER: tuple[tuple[str, str], ...] = (
    ("roe_pct", "REAL"),
    ("roa_pct", "REAL"),
    ("net_margin_pct", "REAL"),
    ("gross_margin_pct", "REAL"),
    ("debt_to_assets_pct", "REAL"),
    ("current_ratio", "REAL"),
    ("quick_ratio", "REAL"),
    ("ocf_per_share", "REAL"),
)

