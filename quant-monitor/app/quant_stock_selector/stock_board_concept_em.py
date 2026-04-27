#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2025/3/12 17:00
Desc: 沪深板块-概念板块（东方财富为主，兼容雪球/项目内多数据源封装）
https://quote.eastmoney.com/center/boardlist.html#concept_board

说明：
- 本文件原始实现以东方财富（EastMoney）概念板块为主：板块列表、成分、历史等。
- 为了提升稳定性与可扩展性，补充了：
  - **雪球（Xueqiu）**：概念板块列表（可选补齐行情字段）
  - **项目内多数据源封装**：通过 `datasources.get_data_source()` 统一入口（akshare/tushare/mootdx 等）
- 雪球接口受风控影响较大：不带 Cookie 可能被拦截；带 `xq_a_token`（建议同时带 `u`）成功率更高。
"""

import re
from functools import lru_cache

import pandas as pd
import requests

from akshare.utils.func import fetch_paginated_data

try:
    # 优先使用项目内的数据源抽象（CLI/管线会用到）。
    from .datasources import get_data_source  # type: ignore
except Exception:  # noqa: BLE001
    try:
        # 兼容脚本直接运行（python path/to/file.py）时相对导入失败的情况。
        from app.quant_stock_selector.datasources import get_data_source  # type: ignore
    except Exception:  # noqa: BLE001
        get_data_source = None  # type: ignore


@lru_cache()
def __stock_board_concept_name_em() -> pd.DataFrame:
    """
    东方财富网-行情中心-沪深京板块-概念板块-名称
    https://quote.eastmoney.com/center/boardlist.html#concept_board
    :return: 概念板块-名称
    :rtype: pandas.DataFrame
    """
    url = "https://79.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:90 t:3 f:!50",
        "fields": "f2,f3,f4,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,f25,f22,f33,f11,f62,f128,f124,f107,f104,f105,f136",
    }
    temp_df = fetch_paginated_data(url, params)
    temp_df.columns = [
        "排名",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "换手率",
        "_",
        "板块代码",
        "板块名称",
        "_",
        "_",
        "_",
        "_",
        "总市值",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "上涨家数",
        "下跌家数",
        "_",
        "_",
        "领涨股票",
        "_",
        "_",
        "领涨股票-涨跌幅",
    ]
    temp_df = temp_df[
        [
            "排名",
            "板块名称",
            "板块代码",
            "最新价",
            "涨跌额",
            "涨跌幅",
            "总市值",
            "换手率",
            "上涨家数",
            "下跌家数",
            "领涨股票",
            "领涨股票-涨跌幅",
        ]
    ]
    temp_df["最新价"] = pd.to_numeric(temp_df["最新价"], errors="coerce")
    temp_df["涨跌额"] = pd.to_numeric(temp_df["涨跌额"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["总市值"] = pd.to_numeric(temp_df["总市值"], errors="coerce")
    temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
    temp_df["上涨家数"] = pd.to_numeric(temp_df["上涨家数"], errors="coerce")
    temp_df["下跌家数"] = pd.to_numeric(temp_df["下跌家数"], errors="coerce")
    temp_df["领涨股票-涨跌幅"] = pd.to_numeric(
        temp_df["领涨股票-涨跌幅"], errors="coerce"
    )
    return temp_df


def stock_board_concept_name_em() -> pd.DataFrame:
    """
    东方财富网-行情中心-沪深京板块-概念板块-名称
    https://quote.eastmoney.com/center/boardlist.html#concept_board
    :return: 概念板块-名称
    :rtype: pandas.DataFrame
    """
    url = "https://79.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:90 t:3 f:!50",
        "fields": "f2,f3,f4,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,f25,f22,f33,f11,f62,f128,f124,f107,f104,f105,f136",
    }
    temp_df = fetch_paginated_data(url, params)
    temp_df.columns = [
        "排名",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "换手率",
        "_",
        "板块代码",
        "板块名称",
        "_",
        "_",
        "_",
        "_",
        "总市值",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "上涨家数",
        "下跌家数",
        "_",
        "_",
        "领涨股票",
        "_",
        "_",
        "领涨股票-涨跌幅",
    ]
    temp_df = temp_df[
        [
            "排名",
            "板块名称",
            "板块代码",
            "最新价",
            "涨跌额",
            "涨跌幅",
            "总市值",
            "换手率",
            "上涨家数",
            "下跌家数",
            "领涨股票",
            "领涨股票-涨跌幅",
        ]
    ]
    temp_df["最新价"] = pd.to_numeric(temp_df["最新价"], errors="coerce")
    temp_df["涨跌额"] = pd.to_numeric(temp_df["涨跌额"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["总市值"] = pd.to_numeric(temp_df["总市值"], errors="coerce")
    temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
    temp_df["上涨家数"] = pd.to_numeric(temp_df["上涨家数"], errors="coerce")
    temp_df["下跌家数"] = pd.to_numeric(temp_df["下跌家数"], errors="coerce")
    temp_df["领涨股票-涨跌幅"] = pd.to_numeric(
        temp_df["领涨股票-涨跌幅"], errors="coerce"
    )
    return temp_df


def stock_board_concept_spot_em(symbol: str = "可燃冰") -> pd.DataFrame:
    """
    东方财富网-行情中心-沪深京板块-概念板块-实时行情
    https://quote.eastmoney.com/bk/90.BK0818.html
    :param symbol: 概念板块代码
    :type symbol: str
    :return: 概念板块-实时行情
    :rtype: pandas.DataFrame
    """
    url = "https://91.push2.eastmoney.com/api/qt/stock/get"
    field_map = {
        "f43": "最新",
        "f44": "最高",
        "f45": "最低",
        "f46": "开盘",
        "f47": "成交量",
        "f48": "成交额",
        "f170": "涨跌幅",
        "f171": "振幅",
        "f168": "换手率",
        "f169": "涨跌额",
    }

    if re.match(pattern=r"^BK\d+", string=symbol):
        em_code = symbol
    else:
        industry_listing = __stock_board_concept_name_em()
        em_code = industry_listing.query("板块名称 == @symbol")["板块代码"].values[0]
    params = dict(
        fields=",".join(field_map.keys()),
        mpi="1000",
        invt="2",
        fltt="1",
        secid=f"90.{em_code}",
    )
    r = requests.get(url, params=params)
    data_dict = r.json()
    result = pd.DataFrame.from_dict(data_dict["data"], orient="index")
    result.rename(field_map, inplace=True)
    result.reset_index(inplace=True)
    result.columns = ["item", "value"]
    result["value"] = pd.to_numeric(result["value"], errors="coerce")

    # 各项转换成正常单位. 除了成交量与成交额, 原始数据中已是正常单位(元)
    result["value"] = result["value"] * 1e-2
    result.iloc[4, 1] = result.iloc[4, 1] * 1e2
    result.iloc[5, 1] = result.iloc[5, 1] * 1e2
    return result


def stock_board_concept_hist_em(
    symbol: str = "绿色电力",
    period: str = "daily",
    start_date: str = "20220101",
    end_date: str = "20221128",
    adjust: str = "",
) -> pd.DataFrame:
    """
    东方财富网-沪深板块-概念板块-历史行情
    https://quote.eastmoney.com/bk/90.BK0715.html
    :param symbol: 板块名称
    :type symbol: str
    :type period: 周期; choice of {"daily", "weekly", "monthly"}
    :param period: 板块名称
    :param start_date: 开始时间
    :type start_date: str
    :param end_date: 结束时间
    :type end_date: str
    :param adjust: choice of {'': 不复权, "qfq": 前复权, "hfq": 后复权}
    :type adjust: str
    :return: 历史行情
    :rtype: pandas.DataFrame
    """
    period_map = {
        "daily": "101",
        "weekly": "102",
        "monthly": "103",
    }
    stock_board_concept_em_map = __stock_board_concept_name_em()
    stock_board_code = stock_board_concept_em_map[
        stock_board_concept_em_map["板块名称"] == symbol
    ]["板块代码"].values[0]
    adjust_map = {"": "0", "qfq": "1", "hfq": "2"}
    url = "https://91.push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"90.{stock_board_code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": period_map[period],
        "fqt": adjust_map[adjust],
        "beg": start_date,
        "end": end_date,
        "smplmt": "10000",
        "lmt": "1000000",
    }
    r = requests.get(url, params=params)
    data_json = r.json()
    temp_df = pd.DataFrame([item.split(",") for item in data_json["data"]["klines"]])
    temp_df.columns = [
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
        "振幅",
        "涨跌幅",
        "涨跌额",
        "换手率",
    ]
    temp_df = temp_df[
        [
            "日期",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "换手率",
        ]
    ]
    temp_df["开盘"] = pd.to_numeric(temp_df["开盘"], errors="coerce")
    temp_df["收盘"] = pd.to_numeric(temp_df["收盘"], errors="coerce")
    temp_df["最高"] = pd.to_numeric(temp_df["最高"], errors="coerce")
    temp_df["最低"] = pd.to_numeric(temp_df["最低"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["涨跌额"] = pd.to_numeric(temp_df["涨跌额"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    temp_df["振幅"] = pd.to_numeric(temp_df["振幅"], errors="coerce")
    temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
    return temp_df


def stock_board_concept_hist_min_em(
    symbol: str = "长寿药", period: str = "5"
) -> pd.DataFrame:
    """
    东方财富网-沪深板块-概念板块-分时历史行情
    https://quote.eastmoney.com/bk/90.BK0715.html
    :param symbol: 板块名称
    :type symbol: str
    :param period: choice of {"1", "5", "15", "30", "60"}
    :type period: str
    :return: 分时历史行情
    :rtype: pandas.DataFrame
    """
    stock_board_concept_em_map = __stock_board_concept_name_em()
    stock_board_code = stock_board_concept_em_map[
        stock_board_concept_em_map["板块名称"] == symbol
    ]["板块代码"].values[0]
    if period == "1":
        url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "iscr": "0",
            "ndays": "1",
            "secid": f"90.{stock_board_code}",
        }
        r = requests.get(url, params=params)
        data_json = r.json()
        temp_df = pd.DataFrame(
            [item.split(",") for item in data_json["data"]["trends"]]
        )
        temp_df.columns = [
            "日期时间",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "最新价",
        ]
        temp_df["开盘"] = pd.to_numeric(temp_df["开盘"], errors="coerce")
        temp_df["收盘"] = pd.to_numeric(temp_df["收盘"], errors="coerce")
        temp_df["最高"] = pd.to_numeric(temp_df["最高"], errors="coerce")
        temp_df["最低"] = pd.to_numeric(temp_df["最低"], errors="coerce")
        temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
        temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
        temp_df["最新价"] = pd.to_numeric(temp_df["最新价"], errors="coerce")
        return temp_df
    else:
        url = "https://91.push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": f"90.{stock_board_code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": period,
            "fqt": "1",
            "end": "20500101",
            "lmt": "1000000",
        }
        r = requests.get(url, params=params)
        data_json = r.json()
        temp_df = pd.DataFrame(
            [item.split(",") for item in data_json["data"]["klines"]]
        )
        temp_df.columns = [
            "日期时间",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "振幅",
            "涨跌幅",
            "涨跌额",
            "换手率",
        ]
        temp_df = temp_df[
            [
                "日期时间",
                "开盘",
                "收盘",
                "最高",
                "最低",
                "涨跌幅",
                "涨跌额",
                "成交量",
                "成交额",
                "振幅",
                "换手率",
            ]
        ]
        temp_df["开盘"] = pd.to_numeric(temp_df["开盘"], errors="coerce")
        temp_df["收盘"] = pd.to_numeric(temp_df["收盘"], errors="coerce")
        temp_df["最高"] = pd.to_numeric(temp_df["最高"], errors="coerce")
        temp_df["最低"] = pd.to_numeric(temp_df["最低"], errors="coerce")
        temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
        temp_df["涨跌额"] = pd.to_numeric(temp_df["涨跌额"], errors="coerce")
        temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
        temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
        temp_df["振幅"] = pd.to_numeric(temp_df["振幅"], errors="coerce")
        temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
        return temp_df


def stock_board_concept_cons_em(symbol: str = "融资融券") -> pd.DataFrame:
    """
    东方财富-沪深板块-概念板块-板块成份
    https://quote.eastmoney.com/center/boardlist.html#boards-BK06551
    :param symbol: 板块名称或者板块代码
    :type symbol: str
    :return: 板块成份
    :rtype: pandas.DataFrame
    """
    if re.match(pattern=r"^BK\d+", string=symbol):
        stock_board_code = symbol
    else:
        stock_board_concept_em_map = __stock_board_concept_name_em()
        stock_board_code = stock_board_concept_em_map[
            stock_board_concept_em_map["板块名称"] == symbol
        ]["板块代码"].values[0]
    url = "https://29.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": f"b:{stock_board_code} f:!50",
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,"
        "f24,f25,f22,f11,f62,f128,f136,f115,f152,f45",
    }
    temp_df = fetch_paginated_data(url, params)
    temp_df.columns = [
        "序号",
        "_",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交量",
        "成交额",
        "振幅",
        "换手率",
        "市盈率-动态",
        "_",
        "_",
        "代码",
        "_",
        "名称",
        "最高",
        "最低",
        "今开",
        "昨收",
        "_",
        "_",
        "_",
        "市净率",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    temp_df = temp_df[
        [
            "序号",
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "昨收",
            "换手率",
            "市盈率-动态",
            "市净率",
        ]
    ]
    temp_df["最新价"] = pd.to_numeric(temp_df["最新价"], errors="coerce")
    temp_df["涨跌幅"] = pd.to_numeric(temp_df["涨跌幅"], errors="coerce")
    temp_df["涨跌额"] = pd.to_numeric(temp_df["涨跌额"], errors="coerce")
    temp_df["成交量"] = pd.to_numeric(temp_df["成交量"], errors="coerce")
    temp_df["成交额"] = pd.to_numeric(temp_df["成交额"], errors="coerce")
    temp_df["振幅"] = pd.to_numeric(temp_df["振幅"], errors="coerce")
    temp_df["最高"] = pd.to_numeric(temp_df["最高"], errors="coerce")
    temp_df["最低"] = pd.to_numeric(temp_df["最低"], errors="coerce")
    temp_df["今开"] = pd.to_numeric(temp_df["今开"], errors="coerce")
    temp_df["昨收"] = pd.to_numeric(temp_df["昨收"], errors="coerce")
    temp_df["换手率"] = pd.to_numeric(temp_df["换手率"], errors="coerce")
    temp_df["市盈率-动态"] = pd.to_numeric(temp_df["市盈率-动态"], errors="coerce")
    temp_df["市净率"] = pd.to_numeric(temp_df["市净率"], errors="coerce")
    return temp_df


def _normalize_xueqiu_cookie(token: str) -> str:
    """
    将用户传入的雪球登录态信息规范成 Cookie 字符串（用于请求雪球 v5 接口）。

    兼容输入形式：
    - 推荐：完整 Cookie 片段，例如 "xq_a_token=...; u=...; ..."
    - 兼容：仅传 xq_a_token 的值（会自动拼成 "xq_a_token=..."）
    """
    raw = (token or "").strip()
    if not raw:
        raise ValueError("雪球 token 不能为空；请传入形如 'xq_a_token=...;u=...' 的 Cookie")
    if "xq_a_token=" in raw:
        return raw
    # 允许只传 xq_a_token 的值
    return f"xq_a_token={raw}"


def _xueqiu_batch_quote(session: requests.Session, symbols: list[str]) -> dict[str, dict]:
    """
    雪球-批量行情（batch quote）：将 symbol 列表映射为 quote 字典。

    返回格式（按雪球常见结构做“尽力解析”）：
    {
      "SH600000": {"current": ..., "percent": ..., ...},
      ...
    }
    """
    if not symbols:
        return {}
    url = "https://stock.xueqiu.com/v5/stock/batch/quote.json"
    params = {"symbol": ",".join(symbols)}
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    items = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("quote") or data.get("quotes") or []
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        quote = it.get("quote") if isinstance(it.get("quote"), dict) else it
        sym = quote.get("symbol") if isinstance(quote, dict) else None
        if sym:
            out[str(sym)] = quote
    return out


def stock_board_concept_name_xq(token: str, enrich_quote: bool = True) -> pd.DataFrame:
    """
    雪球-概念板块列表（可选补齐行情字段；返回结构对齐东财概念板块）

    说明：
    - 雪球 `stock.xueqiu.com/v5/...` 多数情况下需要登录态 Cookie：
      - 至少包含 `xq_a_token`
      - 建议同时带 `u`，以提高成功率
    - 雪球返回字段与可用的 symbol/code 可能随时间变化；本函数采用“尽力解析”策略：
      - 能解析到的字段直接填充
      - 解析不到的字段用 NaN 占位
    - enrich_quote=True 时：若 `sector/list` 未返回行情字段，但板块条目里包含可用于行情查询的 symbol，
      则会调用 `batch/quote` 批量补齐：最新价/涨跌幅/涨跌额/换手率/总市值。
    - 返回列名与顺序与 `stock_board_concept_name_em()` 保持一致，方便下游复用同一套处理逻辑。
    """
    url = "https://stock.xueqiu.com/v5/stock/sector/list.json"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://xueqiu.com/",
        "Origin": "https://xueqiu.com",
    }
    session = requests.Session()
    session.headers.update(headers)
    session.headers["Cookie"] = _normalize_xueqiu_cookie(token)

    r = session.get(url, timeout=15)
    r.raise_for_status()
    payload = r.json()

    data = payload.get("data") if isinstance(payload, dict) else None
    items = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("list") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload.get("items")

    # 对齐东财概念板块字段结构
    cols = [
        "排名",
        "板块名称",
        "板块代码",
        "最新价",
        "涨跌额",
        "涨跌幅",
        "总市值",
        "换手率",
        "上涨家数",
        "下跌家数",
        "领涨股票",
        "领涨股票-涨跌幅",
    ]
    if not items:
        return pd.DataFrame(columns=cols)

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (
            it.get("name")
            or it.get("sector_name")
            or it.get("title")
            or it.get("industry_name")
            or it.get("concept_name")
            or ""
        )
        code = (
            it.get("code")
            or it.get("id")
            or it.get("sector_code")
            or it.get("symbol")
            or it.get("key")
            or ""
        )
        chg_pct = (
            it.get("percent")
            or it.get("pct")
            or it.get("change_pct")
            or it.get("chg_percent")
            or it.get("changePercent")
        )
        chg_amt = it.get("chg") or it.get("change") or it.get("change_amount")
        last = it.get("current") or it.get("price") or it.get("last") or it.get("quote")
        turnover = it.get("turnover_rate") or it.get("turnoverRate")
        market_cap = it.get("market_capital") or it.get("marketCap")
        leader = it.get("leader") or it.get("leader_stock") or it.get("leaderStock") or pd.NA
        leader_pct = it.get("leader_percent") or it.get("leader_change_pct") or it.get("leaderChangePct")
        up_cnt = it.get("up_count") or it.get("upCount")
        down_cnt = it.get("down_count") or it.get("downCount")

        rows.append(
            {
                "板块名称": str(name).strip(),
                "板块代码": str(code).strip(),
                "最新价": last,
                "涨跌额": chg_amt,
                "涨跌幅": chg_pct,
                "总市值": market_cap,
                "换手率": turnover,
                "上涨家数": up_cnt,
                "下跌家数": down_cnt,
                "领涨股票": leader,
                "领涨股票-涨跌幅": leader_pct,
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    out.insert(0, "排名", range(1, len(out) + 1))

    # 若 sector/list 未带行情字段，则尝试用 batch/quote 补齐（避免循环单个请求）
    if enrich_quote:
        need_quote = (
            ("最新价" in out.columns and out["最新价"].isna().all())
            or ("涨跌幅" in out.columns and out["涨跌幅"].isna().all())
            or ("涨跌额" in out.columns and out["涨跌额"].isna().all())
        )
        if need_quote and "板块代码" in out.columns:
            syms = (
                out["板块代码"]
                .astype(str)
                .map(lambda x: x.strip())
                .tolist()
            )
            # 粗过滤：雪球 quote 常见 symbol 类似 "SH600000"/"SZ000001"/"BKxxxx"/"SH000001"
            syms = [s for s in syms if re.match(r"^[A-Z]{1,4}\d{3,}$", s)]
            # 去重并限制批量长度（避免过大 URL）
            uniq: list[str] = []
            seen = set()
            for s in syms:
                if s not in seen:
                    uniq.append(s)
                    seen.add(s)
                if len(uniq) >= 500:
                    break
            quote_map = _xueqiu_batch_quote(session, uniq)
            if quote_map:
                def _fill(col: str, key: str):
                    if col not in out.columns:
                        return
                    out[col] = out.apply(
                        lambda row: row[col]
                        if not pd.isna(row[col])
                        else quote_map.get(str(row["板块代码"]), {}).get(key),
                        axis=1,
                    )

                _fill("最新价", "current")
                _fill("涨跌幅", "percent")
                _fill("涨跌额", "chg")
                _fill("换手率", "turnover_rate")
                _fill("总市值", "market_capital")

    for c in ["最新价", "涨跌额", "涨跌幅", "总市值", "换手率", "上涨家数", "下跌家数", "领涨股票-涨跌幅"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    # 补齐缺失列并对齐顺序
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out[cols].copy()


def stock_board_concept_list_xq(xueqiu_token: str | None = None) -> pd.DataFrame:
    """
    雪球-板块列表（仅用于展示：代码 + 名称，优先使用不登录接口）

    设计目标：尽量贴近你提供的“三方示例”风格：
    - 默认不强制 token（不带 Cookie 也尝试请求）
    - 若被雪球风控拦截，可传入 `xueqiu_token`（Cookie 或 xq_a_token 值）提高成功率

    :param xueqiu_token: 可选；支持 "xq_a_token=...;u=..." 或仅 token 值
    :return: DataFrame columns = ["板块代码", "板块名称"]
    """
    # 说明：
    # - `stock.xueqiu.com/v5/stock/sector/list.json` 在多数环境下会要求登录态 Cookie（否则 403）
    # - `xueqiu.com/stock/screener/industries.json` 属于雪球选股器的公共数据入口，通常不需要 token，
    #   能稳定返回“行业板块”列表（可用于满足“展示板块”的需求）。
    url_public = "https://xueqiu.com/stock/screener/industries.json"
    url_private = "https://stock.xueqiu.com/v5/stock/sector/list.json"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://xueqiu.com/hq",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    session = requests.Session()
    session.headers.update(headers)
    if xueqiu_token:
        session.headers["Cookie"] = _normalize_xueqiu_cookie(xueqiu_token)

    rows: list[dict] = []

    # 1) 优先走“公共”行业板块列表（不登录更稳定）
    try:
        r = session.get(url_public, params={"category": "SH"}, timeout=15)
        if r.status_code == 200:
            payload = r.json()
            items = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    # 常见字段：level2code/level2name（有些版本也可能是 code/name）
                    code = it.get("level2code") or it.get("code") or it.get("indcode") or ""
                    name = it.get("level2name") or it.get("name") or it.get("indname") or ""
                    rows.append({"板块代码": str(code).strip(), "板块名称": str(name).strip()})
    except Exception:
        # 公共入口失败时再尝试私有 v5（若无 token 可能 403）
        rows = []

    # 2) 若公共入口未拿到任何数据，再尝试 v5 sector/list（通常需要 token；无 token 很可能 403）
    if not rows:
        if not xueqiu_token:
            raise RuntimeError(
                "雪球当前已对板块列表接口加强风控：不带登录态 Cookie 时，"
                "常见返回 403/400016，无法获取板块列表。\n"
                "—— 处理方案：\n"
                "1) 传入 xueqiu_token（建议 Cookie：'xq_a_token=...;u=...'）；或\n"
                "2) 改用东财/通达信等不需要登录的板块数据源。"
            )
        r = session.get(url_private, timeout=15)
        r.raise_for_status()
        payload = r.json()

        data = payload.get("data") if isinstance(payload, dict) else None
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("list") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload.get("items")

        for it in items or []:
            if not isinstance(it, dict):
                continue
            name = (
                it.get("name")
                or it.get("sector_name")
                or it.get("title")
                or it.get("industry_name")
                or it.get("concept_name")
                or ""
            )
            code = (
                it.get("code")
                or it.get("id")
                or it.get("sector_code")
                or it.get("symbol")
                or it.get("key")
                or ""
            )
            rows.append({"板块代码": str(code).strip(), "板块名称": str(name).strip()})

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["板块代码", "板块名称"])
    out = out.drop_duplicates(subset=["板块代码", "板块名称"]).reset_index(drop=True)
    return out[["板块代码", "板块名称"]].copy()


def _to_em_concept_name_frame(rankings: pd.DataFrame) -> pd.DataFrame:
    """
    将 `datasources.BaseAShareDataSource.get_sector_rankings(board_types="concept")`
    的标准列映射为东财概念板块 `stock_board_concept_name_em()` 的列结构。

    注意：非东财数据源通常无法提供 总市值/上涨家数/领涨股 等细分字段，这里用 NaN 填充，
    但保证列名与顺序一致，便于下游复用同一套处理逻辑。
    """
    cols = [
        "排名",
        "板块名称",
        "板块代码",
        "最新价",
        "涨跌额",
        "涨跌幅",
        "总市值",
        "换手率",
        "上涨家数",
        "下跌家数",
        "领涨股票",
        "领涨股票-涨跌幅",
    ]
    if rankings is None or rankings.empty:
        return pd.DataFrame(columns=cols)

    src = str(rankings.get("source", pd.Series(["unknown"])).iloc[0] if "source" in rankings.columns else "unknown")
    out = pd.DataFrame(index=rankings.index)
    out["排名"] = range(1, len(rankings) + 1)
    out["板块名称"] = rankings.get("sector_name", "").astype(str)

    # 用可追溯的“板块代码”占位，避免与东财 BKxxxx 混淆
    if "ts_code" in rankings.columns:
        out["板块代码"] = rankings["ts_code"].astype(str)
    else:
        # mootdx 没有 ts_code；用 source + 序号 保证唯一性
        out["板块代码"] = [f"{src}:{i:04d}" for i in range(1, len(rankings) + 1)]

    out["最新价"] = pd.NA
    out["涨跌额"] = pd.NA
    out["涨跌幅"] = pd.to_numeric(rankings.get("change_pct", 0.0), errors="coerce")
    out["总市值"] = pd.NA
    out["换手率"] = pd.to_numeric(rankings.get("turnover_rate", 0.0), errors="coerce")
    out["上涨家数"] = pd.NA
    out["下跌家数"] = pd.NA
    out["领涨股票"] = pd.NA
    out["领涨股票-涨跌幅"] = pd.to_numeric(rankings.get("leader_change_pct", pd.NA), errors="coerce")
    return out[cols].copy()


def stock_board_concept_name(
    data_source: str = "akshare",
    tushare_token: str | None = None,
    xueqiu_token: str | None = None,
) -> pd.DataFrame:
    """
    多数据源版：概念板块-名称（列结构与 `stock_board_concept_name_em` 对齐）

    - data_source="akshare": 走东财（优先本仓库自定义接口，失败回退 AkShare 内置实现）
    - data_source="tushare": 走 TuShare 同花顺板块（ths_index/ths_daily），需账号权限/积分
    - data_source="mootdx": 走通达信 block_gn.dat（概念覆盖较少，但连接较稳定）
    - data_source="xueqiu": 走雪球概念板块接口（需登录态 token/Cookie；字段不全时会用 NaN 占位）

    备注：
    - 若你只想“展示板块代码+名称”，推荐使用 `stock_board_concept_list_xq()`（可不带 token）。
    """
    source = (data_source or "akshare").strip().lower()
    if source in {"em", "eastmoney"}:
        source = "akshare"

    if source in {"xueqiu", "snowball", "xq"}:
        if not xueqiu_token:
            raise ValueError(
                "使用雪球数据源需要传入 xueqiu_token（建议直接传 Cookie：'xq_a_token=...;u=...'）"
            )
        return stock_board_concept_name_xq(token=xueqiu_token)

    if get_data_source is None:
        # 兼容脚本单文件运行：仅允许 akshare 退回东财；其他数据源需要包导入正常才可用
        if source == "akshare":
            return stock_board_concept_name_em()
        raise RuntimeError(
            "当前以脚本方式直接运行，无法导入项目内 datasources，"
            "因此不能使用 tushare/mootdx。请改用以下方式之一运行：\n"
            "1) 在项目根目录执行：python -m app.quant_stock_selector.stock_board_concept_em\n"
            "2) 或通过 CLI：python -m app.quant_stock_selector.cli --data-source mootdx --hot-sectors --board-type concept\n"
        )

    ds = get_data_source(source, tushare_token=tushare_token)
    rankings = ds.get_sector_rankings(board_types="concept")
    return _to_em_concept_name_frame(rankings)


def stock_board_concept_cons(
    symbol: str = "融资融券",
    data_source: str = "akshare",
    tushare_token: str | None = None,
) -> pd.DataFrame:
    """
    多数据源版：概念板块-板块成份

    - akshare: 使用东财接口（返回字段最全）
    - tushare/mootdx: 仅能稳定提供 code/name（其余列用 NaN 占位，保证与东财列对齐）
    """
    source = (data_source or "akshare").strip().lower()
    if source in {"em", "eastmoney"}:
        source = "akshare"

    if source == "akshare":
        return stock_board_concept_cons_em(symbol=symbol)

    if get_data_source is None:
        raise RuntimeError(
            "当前以脚本方式直接运行，无法导入项目内 datasources，"
            "因此不能使用 tushare/mootdx 获取板块成分。请在项目根目录执行：\n"
            "python -m app.quant_stock_selector.stock_board_concept_em\n"
            "或使用 CLI 入口运行。"
        )

    ds = get_data_source(source, tushare_token=tushare_token)
    cons = ds.get_sector_constituents(sector_name=str(symbol), board_type="concept")
    if cons is None or cons.empty:
        return pd.DataFrame(
            columns=[
                "序号",
                "代码",
                "名称",
                "最新价",
                "涨跌幅",
                "涨跌额",
                "成交量",
                "成交额",
                "振幅",
                "最高",
                "最低",
                "今开",
                "昨收",
                "换手率",
                "市盈率-动态",
                "市净率",
            ]
        )

    base = cons.rename(columns={"code": "代码", "name": "名称"}).copy()
    base["序号"] = range(1, len(base) + 1)

    # 对齐东财的列结构（非东财源一般拿不到这些字段）
    for col in [
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交量",
        "成交额",
        "振幅",
        "最高",
        "最低",
        "今开",
        "昨收",
        "换手率",
        "市盈率-动态",
        "市净率",
    ]:
        base[col] = pd.NA

    return base[
        [
            "序号",
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "昨收",
            "换手率",
            "市盈率-动态",
            "市净率",
        ]
    ].copy()


if __name__ == "__main__":
    print(stock_board_concept_name(data_source="mootdx").head())
    print(stock_board_concept_cons("一带一路", data_source="mootdx").head())
    # stock_board_concept_em_df = stock_board_concept_name_em()
    # print(stock_board_concept_em_df)

    # stock_board_concept_spot_em_df = stock_board_concept_spot_em(symbol="可燃冰")
    # print(stock_board_concept_spot_em_df)

    # stock_board_concept_hist_em_df = stock_board_concept_hist_em(
    #     symbol="绿色电力",
    #     period="daily",
    #     start_date="20220101",
    #     end_date="20250227",
    #     adjust="",
    # )
    # print(stock_board_concept_hist_em_df)

    # stock_board_concept_hist_min_em_df = stock_board_concept_hist_min_em(
    #     symbol="长寿药", period="5"
    # )
    # print(stock_board_concept_hist_min_em_df)

    # stock_board_concept_cons_em_df = stock_board_concept_cons_em(symbol="BK0655")
    # print(stock_board_concept_cons_em_df)
