# cython: language_level=3
import secrets
import warnings

import pandas as pd

from tdxpy.base_socket_client import BaseSocketClient
from tdxpy.base_socket_client import last_ack_time
from tdxpy.constants import TDXParams
from tdxpy.helper import time_frame
from tdxpy.parser.setup_commands import SetupCmd1
from tdxpy.parser.setup_commands import SetupCmd2
from tdxpy.parser.setup_commands import SetupCmd3
from tdxpy.parser.std.get_block_info import GetBlockInfo
from tdxpy.parser.std.get_block_info import GetBlockInfoMeta
from tdxpy.parser.std.get_block_info import get_and_parse_block_info
from tdxpy.parser.std.get_company_info_category import GetCompanyInfoCategory
from tdxpy.parser.std.get_company_info_content import GetCompanyInfoContent
from tdxpy.parser.std.get_finance_info import GetFinanceInfo
from tdxpy.parser.std.get_history_minute_time_data import GetHistoryMinuteTimeData
from tdxpy.parser.std.get_history_transaction_data import GetHistoryTransactionData
from tdxpy.parser.std.get_index_bars import GetIndexBarsCmd
from tdxpy.parser.std.get_minute_time_data import GetMinuteTimeData
from tdxpy.parser.std.get_report_file import GetReportFile
from tdxpy.parser.std.get_security_bars import GetSecurityBarsCmd
from tdxpy.parser.std.get_security_count import GetSecurityCountCmd
from tdxpy.parser.std.get_security_list import GetSecurityList
from tdxpy.parser.std.get_security_quotes import GetSecurityQuotesCmd
from tdxpy.parser.std.get_transaction_data import GetTransactionData
from tdxpy.parser.std.get_xdxr_info import GetXdXrInfo


class TdxHq_API(BaseSocketClient):  # noqa
    def setup(self):
        SetupCmd1(self.client).call_api()
        SetupCmd2(self.client).call_api()
        SetupCmd3(self.client).call_api()

    @last_ack_time
    def get_security_bars(self, category, market, code, start, count):
        """
        # Notice：，如果一个股票当天停牌，那天的K线还是能取到，成交量为0
        :param category:
        :param market:
        :param code:
        :param start:
        :param count:
        :return:
        """
        cmd = GetSecurityBarsCmd(self.client, lock=self.lock)
        cmd.setParams(category, market, code, start, count)

        return cmd.call_api()

    @last_ack_time
    def get_index_bars(self, category, market, code, start, count):
        """
        指数时间线
        :param category:
        :param market:
        :param code:
        :param start:
        :param count:
        :return:
        """
        cmd = GetIndexBarsCmd(self.client, lock=self.lock)
        cmd.setParams(category, market, code, start, count)

        return cmd.call_api()

    @last_ack_time
    def get_security_quotes(self, all_stock=None, code=None):
        """
        支持三种形式的参数
        get_security_quotes(market, code )
        get_security_quotes((market, code))
        get_security_quotes([(market1, code1), (market2, code2)] )

        :rtype: object
        :param all_stock （market, code) 的数组
        :param code{optional} code to query
        :return:
        """

        # if not all_stock:
        #     raise ValidationException('参数错误')

        if isinstance(all_stock, list) and TDXParams.MARKET_BJ in [x[0] for x in all_stock]:
            warnings.warn("此接口暂时不支持北证A股", DeprecationWarning)
            return None

        if all_stock == TDXParams.MARKET_BJ:
            warnings.warn("此接口暂时不支持北证A股", DeprecationWarning)
            return None

        if code:
            all_stock = [(all_stock, code)]
        elif (
            (isinstance(all_stock, list) or isinstance(all_stock, tuple))
            and len(all_stock) == 2
            and type(all_stock[0]) is int
        ):
            all_stock = [all_stock]

        cmd = GetSecurityQuotesCmd(self.client, lock=self.lock)
        cmd.setParams(all_stock)

        return cmd.call_api()

    @last_ack_time
    def get_security_count(self, market=1):
        """
        获取证券数量
        :param market:
        :return:
        """
        cmd = GetSecurityCountCmd(self.client, lock=self.lock)
        cmd.setParams(market)

        return cmd.call_api()

    @last_ack_time
    def get_security_list(self, market, start):
        """
        获取证券列表
        :param market:
        :param start:
        :return:
        """
        if market == TDXParams.MARKET_BJ:
            warnings.warn("此接口暂时不支持北证A股", DeprecationWarning)
            return None

        cmd = GetSecurityList(self.client, lock=self.lock)
        cmd.setParams(market, start)

        return cmd.call_api()

    @last_ack_time
    def get_minute_time_data(self, market, code):
        """
        获取分钟线
        :param market:
        :param code:
        :return:
        """
        cmd = GetMinuteTimeData(self.client, lock=self.lock)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_history_minute_time_data(self, market, code, date):
        """
        获取分钟线历史数据
        :param market:
        :param code:
        :param date:
        :return:
        """
        cmd = GetHistoryMinuteTimeData(self.client, lock=self.lock)
        cmd.setParams(market, code, date)

        return cmd.call_api()

    @last_ack_time
    def get_transaction_data(self, market, code, start, count):
        """
        获取分笔数据
        :param market:
        :param code:
        :param start:
        :param count:
        :return:
        """
        if not time_frame():
            warnings.warn("目前不是交易时间段, 此接口必须在交易时间才能使用", DeprecationWarning)
            return None

        cmd = GetTransactionData(self.client, lock=self.lock)
        cmd.setParams(market, code, start, count)

        return cmd.call_api()

    @last_ack_time
    def get_history_transaction_data(self, market, code, start, count, date):
        """
        分笔历史数据
        :param market:
        :param code:
        :param start:
        :param count:
        :param date:
        :return:
        """
        cmd = GetHistoryTransactionData(self.client, lock=self.lock)
        cmd.setParams(market, code, start, count, date)

        return cmd.call_api()

    @last_ack_time
    def get_company_info_category(self, market, code):
        """
        公司信息类别
        :param market:
        :param code:
        :return:
        """
        if market == TDXParams.MARKET_BJ:
            warnings.warn("此接口暂时不支持北证A股", DeprecationWarning)
            return None

        cmd = GetCompanyInfoCategory(self.client, lock=self.lock)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_company_info_content(self, market, code, filename, start, length):
        """
        公司信息内容
        :param market:
        :param code:
        :param filename:
        :param start:
        :param length:
        :return:
        """
        if market == TDXParams.MARKET_BJ:
            warnings.warn("此接口暂时不支持北证A股", DeprecationWarning)
            return None

        cmd = GetCompanyInfoContent(self.client, lock=self.lock)
        cmd.setParams(market, code, filename, start, length)

        return cmd.call_api()

    @last_ack_time
    def get_xdxr_info(self, market, code):
        """
        除息除权信息
        :param market:
        :param code:
        :return:
        """
        cmd = GetXdXrInfo(self.client, lock=self.lock)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_finance_info(self, market, code):
        """
        财务信息
        :param market:
        :param code:
        :return:
        """
        cmd = GetFinanceInfo(self.client, lock=self.lock)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_block_info_meta(self, block_file):
        """
        板块元信息
        :param block_file:
        :return:
        """
        cmd = GetBlockInfoMeta(self.client, lock=self.lock)
        cmd.setParams(block_file)

        return cmd.call_api()

    @last_ack_time
    def get_block_info(self, block_file, start, size):
        """
        板块内容
        :param block_file:
        :param start:
        :param size:
        :return:
        """
        cmd = GetBlockInfo(self.client, lock=self.lock)
        cmd.setParams(block_file, start, size)

        return cmd.call_api()

    def get_and_parse_block_info(self, block_file):
        """
        下载并解析板块
        :param block_file:
        :return:
        """
        return get_and_parse_block_info(self, block_file)

    @last_ack_time
    def get_report_file(self, filename, offset):
        """
        下载财务报表文件
        :param filename:
        :param offset:
        :return:
        """
        cmd = GetReportFile(self.client, lock=self.lock)
        cmd.setParams(filename, offset)

        return cmd.call_api()

    def get_report_file_by_size(self, filename, filesize=0, reporthook=None):
        """
        Download file from proxy server
        :param reporthook:
        :param filename the filename to download
        :param filesize the file_size to download , if you do not know the actually file_size, leave this value 0
        """
        file_content = bytearray(filesize)
        get_zero_length_package_times = 0
        current_downloaded_size = 0

        while current_downloaded_size < filesize or filesize == 0:
            response = self.get_report_file(filename, current_downloaded_size)

            if response["chunksize"] > 0:
                current_downloaded_size = current_downloaded_size + response["chunksize"]
                file_content.extend(response["chunkdata"])

                reporthook and reporthook(current_downloaded_size, filesize)
            else:
                get_zero_length_package_times = get_zero_length_package_times + 1

                if filesize == 0 or get_zero_length_package_times > 2:
                    break

        return file_content

    def do_heartbeat(self):
        self.get_security_count(secrets.randbelow(1))

    def get_k_data(self, code, start_date, end_date):
        """
        巨 ugly 代码
        :param code:
        :param start_date:
        :param end_date:
        :return:
        """

        # 具体详情参见 https://github.com/rainx/pytdx/issues/5
        # 具体详情参见 https://github.com/rainx/pytdx/issues/21
        def __select_market_code(symbol):
            symbol = str(symbol)

            if symbol[0] in ["5", "6", "9"] or symbol[:3] in ["009", "126", "110", "201", "202", "203", "204"]:
                return 1

            return 0

        # 新版一劳永逸偷懒写法zzz
        # market_code = 1 if str(code)[0] == '6' else 0

        # https://github.com/rainx/pytdx/issues/33
        # 0 - 深圳， 1 - 上海

        result = pd.concat(
            [
                self.to_df(self.get_security_bars(9, __select_market_code(code), code, (9 - i) * 800, 800))
                for i in range(10)
            ],
            axis=0,
        )
        result = (
            result.assign(date=result["datetime"].apply(lambda x: str(x)[0:10]))
            .assign(code=str(code))
            .set_index("date", drop=False, inplace=False)
            .drop(["year", "month", "day", "hour", "minute", "datetime"], axis=1)[start_date:end_date]
        )

        result = result.assign(date=result["date"].apply(lambda x: str(x)[0:10]))

        return result
