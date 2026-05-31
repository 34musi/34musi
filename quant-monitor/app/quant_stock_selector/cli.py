"""CLI entry: argparse and main."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .constants import DEFAULT_END_DATE, DEFAULT_START_DATE
from .exceptions import DataSourceError
from .export_io import export_results, print_stock_rankings
from .sectors import print_sector_rankings
from .pipeline import run_analysis


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股热门板块筛选与个股量化评估脚本")
    parser.add_argument(
        "--data-source",
        choices=["akshare", "baostock", "tushare", "mootdx"],
        default="mootdx",
        help="A 股数据源（mootdx 通达信；baostock 日 K+东财板块；akshare 东财全线；tushare 同花顺）",
    )
    parser.add_argument("--tushare-token", help="TuShare token，可选")
    parser.add_argument(
        "--hot-sectors",
        action="store_true",
        help="自动计算并分析近期热门板块；默认不传入口参数时也会启用",
    )
    parser.add_argument("--sector", help="直接分析指定板块名称")
    parser.add_argument("--codes", type=Path, help="自定义股票列表文件，支持 csv/xlsx")
    parser.add_argument("--data-dir", type=Path, help="本地行情目录，可与接口模式混合使用")
    parser.add_argument("--board-type", choices=["all", "concept", "industry"], default="all", help="板块类型")
    parser.add_argument("--top-sectors", type=int, default=5, help="热门板块模式下分析前 N 个板块")
    parser.add_argument("--max-stocks-per-sector", type=int, default=20, help="每个板块最多分析的股票数")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE, help="结束日期，格式 YYYYMMDD")
    parser.add_argument("--adjust", default="qfq", help="复权方式，AkShare 常用 qfq/hfq")
    parser.add_argument("--fast-period", type=int, default=10, help="快速均线周期")
    parser.add_argument("--slow-period", type=int, default=30, help="慢速均线周期")
    parser.add_argument("--initial-cash", type=float, default=100000.0, help="回测初始资金")
    parser.add_argument("--commission", type=float, default=0.001, help="单边交易手续费率")
    parser.add_argument("--stop-loss", type=float, default=0.08, help="止损比例，例如 0.08 表示 8%%")
    parser.add_argument(
        "--scoring-strategy",
        choices=["v2", "v1"],
        default="v2",
        help="综合评分策略：v2（新版，偏个股技术面/回测）/ v1（旧版，板块热度权重更高）",
    )
    parser.add_argument("--only-passed", action="store_true", help="只输出通过技术面初筛的股票")
    parser.add_argument("--top-stocks", type=int, default=20, help="终端展示前 N 只股票")
    parser.add_argument("--output", type=Path, help="结果导出路径，建议 xlsx")
    args = parser.parse_args(argv)
    if not any([args.hot_sectors, args.sector, args.codes]):
        args.hot_sectors = True
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.fast_period >= args.slow_period:
        raise DataSourceError("fast-period 必须小于 slow-period")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        sectors, stocks = run_analysis(args)
    except Exception as exc:
        print(f"执行失败: {exc}")
        return 1

    print_sector_rankings(sectors)
    print_stock_rankings(stocks, args.top_stocks)
    export_results(sectors, stocks, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
