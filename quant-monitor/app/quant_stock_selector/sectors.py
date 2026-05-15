"""Sector ranking, constituent loading, console print."""

from __future__ import annotations

import argparse
import re
from dataclasses import asdict
from typing import Dict, List, Sequence

import pandas as pd

from .datasources import BaseAShareDataSource
from .exceptions import DataSourceError
from .hot_pick import is_st_stock_name
from .market_utils import is_listed_a_share_equity, normalize_code, read_codes_file, safe_float
from .models import SectorRecord

# 与 POST /research/sector-screen 的 include_hot_snapshot_stocks 配套：虚拟板块名，避免与真实板块重名。
HOT_SNAPSHOT_SECTOR_NAME = "热门快照·全市场热门股"
# pool_mode=universe：单池虚拟板块名，与 get_stock_universe 截取的成分表 sector_name 一致。
UNIVERSE_POOL_SECTOR_NAME = "全市场股票池"


def _code_universe_tags(code: object) -> set[str]:
    """代码所属板块标签（可多标签，用于与 universe_segments 求交）。"""
    c = normalize_code(str(code))
    out: set[str] = set()
    if len(c) != 6:
        return out
    if re.match(r"^00[0-3]\d{3}$", c):
        out.add("sh_sz_main")
    if c.startswith("60") and not c.startswith("688") and not c.startswith("689"):
        out.add("sh_sz_main")
    if re.match(r"^30[01]\d{3}$", c):
        out.add("cyb")
    if c.startswith("688") or c.startswith("689"):
        out.add("kcb")
    if re.match(r"^(43|83|87|88|92)\d{4}$", c):
        out.add("bj")
    return out


def build_sector_records(frame: pd.DataFrame) -> List[SectorRecord]:
    records: List[SectorRecord] = []
    for row in frame.itertuples():
        records.append(
            SectorRecord(
                sector_name=row.sector_name,
                board_type=row.board_type,
                change_pct=round(safe_float(row.change_pct), 2),
                advancers_ratio=round(safe_float(row.advancers_ratio) * 100.0, 2),
                leader_change_pct=round(safe_float(row.leader_change_pct), 2),
                turnover_rate=round(safe_float(row.turnover_rate), 2),
                hot_score=round(safe_float(row.hot_score), 2),
                source=getattr(row, "source", "unknown"),
            )
        )
    return records


def print_sector_rankings(sectors: Sequence[SectorRecord]) -> None:
    if not sectors:
        print("没有可展示的热门板块")
        return
    frame = pd.DataFrame([asdict(record) for record in sectors])
    display = frame[
        [
            "sector_name",
            "board_type",
            "hot_score",
            "change_pct",
            "advancers_ratio",
            "leader_change_pct",
            "turnover_rate",
        ]
    ]
    print("\n热门板块排名:")
    print(display.to_string(index=False))


def select_target_sectors(datasource: BaseAShareDataSource, args: argparse.Namespace) -> List[SectorRecord]:
    if args.sector:
        constituents = datasource.get_sector_constituents(
            args.sector, args.board_type if args.board_type != "all" else None
        )
        return [
            SectorRecord(
                sector_name=constituents["sector_name"].iloc[0],
                board_type=constituents["board_type"].iloc[0],
                change_pct=0.0,
                advancers_ratio=0.0,
                leader_change_pct=0.0,
                turnover_rate=0.0,
                hot_score=60.0,
                source=args.data_source,
            )
        ]

    if getattr(args, "flat_universe", False):
        return [
            SectorRecord(
                sector_name=UNIVERSE_POOL_SECTOR_NAME,
                board_type="all",
                change_pct=0.0,
                advancers_ratio=0.0,
                leader_change_pct=0.0,
                turnover_rate=0.0,
                hot_score=50.0,
                source="universe",
            )
        ]

    if args.hot_sectors:
        rankings = datasource.get_sector_rankings(args.board_type)
        out = build_sector_records(rankings.head(args.top_sectors))
        if getattr(args, "include_hot_snapshot_stocks", False):
            out.append(
                SectorRecord(
                    sector_name=HOT_SNAPSHOT_SECTOR_NAME,
                    board_type="snapshot",
                    change_pct=0.0,
                    advancers_ratio=0.0,
                    leader_change_pct=0.0,
                    turnover_rate=0.0,
                    hot_score=52.0,
                    source="hot_snapshot",
                )
            )
        return out

    return [
        SectorRecord(
            sector_name="自定义股票池",
            board_type="custom",
            change_pct=0.0,
            advancers_ratio=0.0,
            leader_change_pct=0.0,
            turnover_rate=0.0,
            hot_score=50.0,
            source="custom",
        )
    ]


def _constituents_from_hot_market_snapshot(
    *,
    sector_name: str,
    cap: int,
    exclude_codes: set[str],
) -> pd.DataFrame:
    """从 hot_market_snapshot.json 构建与 get_sector_constituents 列对齐的成分表。"""
    from app.hot_market_snapshot import load_hot_market_snapshot

    snap = load_hot_market_snapshot()
    if snap is None or not snap.stocks:
        return pd.DataFrame(columns=["code", "name", "sector_name", "board_type"])

    cap = max(1, min(500, int(cap)))
    rows: list[dict[str, str]] = []
    seen_local: set[str] = set()
    for st in snap.stocks:
        if len(rows) >= cap:
            break
        if not isinstance(st, dict):
            continue
        raw_code = st.get("code")
        if raw_code is None:
            continue
        code = normalize_code(str(raw_code))
        if not code or len(code) != 6 or not is_listed_a_share_equity(code):
            continue
        if code in exclude_codes or code in seen_local:
            continue
        seen_local.add(code)
        nm_raw = st.get("name")
        name = str(nm_raw).strip() if nm_raw is not None else ""
        if name.lower() == "nan":
            name = ""
        rows.append(
            {
                "code": code,
                "name": name,
                "sector_name": sector_name,
                "board_type": "snapshot",
            }
        )

    if not rows:
        return pd.DataFrame(columns=["code", "name", "sector_name", "board_type"])
    return pd.DataFrame(rows)


def load_sector_constituents(
    datasource: BaseAShareDataSource,
    sectors: Sequence[SectorRecord],
    args: argparse.Namespace,
) -> Dict[str, pd.DataFrame]:
    sector_map: Dict[str, pd.DataFrame] = {}
    if args.codes:
        codes = read_codes_file(args.codes)
        sector_map["自定义股票池"] = codes.assign(sector_name="自定义股票池", board_type="custom")
        return sector_map

    if getattr(args, "flat_universe", False):
        top_target = max(1, min(500, int(getattr(args, "flat_universe_top", 200) or 200)))
        scan_raw = int(getattr(args, "flat_universe_scan", 0) or 0)
        if scan_raw > top_target:
            n = max(top_target, min(8000, scan_raw))
        else:
            n = top_target
        try:
            uni = datasource.get_stock_universe()
        except Exception as exc:
            raise DataSourceError(f"获取全市场股票池失败: {exc}") from exc
        if uni is None or uni.empty:
            raise DataSourceError("全市场股票池为空，无法选股")
        work = uni[["code", "name"]].copy()
        work["code"] = work["code"].map(lambda x: normalize_code(str(x)))
        work = work[work["code"].astype(str).str.len() == 6]
        work = work[work["code"].map(is_listed_a_share_equity)]
        work = work.drop_duplicates(subset=["code"], keep="first")
        seg_list = getattr(args, "universe_segments", None) or ["sh_sz_main"]
        include = set(str(s).strip() for s in seg_list if str(s).strip())
        if not include:
            include = {"sh_sz_main", "cyb", "kcb", "bj"}
        exclude_st = bool(getattr(args, "universe_exclude_st", True))
        picked: list[dict[str, str]] = []
        for row in work.itertuples(index=False):
            c = normalize_code(str(getattr(row, "code", "") or ""))
            nm_raw = getattr(row, "name", "") or ""
            name = str(nm_raw).strip()
            if name.lower() == "nan":
                name = ""
            if len(c) != 6 or not is_listed_a_share_equity(c):
                continue
            if exclude_st and is_st_stock_name(name):
                continue
            tags = _code_universe_tags(c)
            if not (tags & include):
                continue
            picked.append({"code": c, "name": name})
            if len(picked) >= n:
                break
        if not picked:
            raise DataSourceError(
                "全市场股票池在当前「股票类型 / 排除 ST」条件下为空，请放宽多选或取消排除 ST。"
            )
        sn = UNIVERSE_POOL_SECTOR_NAME
        sector_map[sn] = pd.DataFrame(picked).assign(sector_name=sn, board_type="all")
        return sector_map

    seen_codes: set[str] = set()
    for sector in sectors:
        if sector.source == "hot_snapshot" or sector.sector_name == HOT_SNAPSHOT_SECTOR_NAME:
            cap = int(getattr(args, "hot_snapshot_stocks_cap", 80) or 80)
            snap_df = _constituents_from_hot_market_snapshot(
                sector_name=sector.sector_name,
                cap=cap,
                exclude_codes=seen_codes,
            )
            sector_map[sector.sector_name] = snap_df
            for c in snap_df["code"].astype(str):
                cc = normalize_code(c)
                if cc:
                    seen_codes.add(cc)
            continue

        constituents = datasource.get_sector_constituents(
            sector.sector_name, sector.board_type if sector.board_type != "custom" else None
        )
        if args.max_stocks_per_sector:
            constituents = constituents.head(args.max_stocks_per_sector)
        sector_map[sector.sector_name] = constituents
        for c in constituents["code"].astype(str):
            cc = normalize_code(c)
            if cc:
                seen_codes.add(cc)
    return sector_map
