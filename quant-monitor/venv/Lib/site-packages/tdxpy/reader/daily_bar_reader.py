# cython: language_level=3
from pathlib import Path

import pandas as pd

from tdxpy.logger import logger
from tdxpy.reader.base_reader import BaseReader
from tdxpy.reader.base_reader import TdxFileNotFoundException
from tdxpy.reader.base_reader import TdxNotAssignVipdocPathException


class TdxDailyBarReader(BaseReader):
    """
    读取通达信日线数据
    """

    # 交易所
    SECURITY_EXCHANGE = ["sz", "sh"]

    # 股票类型
    SECURITY_TYPE = [
        "SH_A_STOCK",
        "SH_B_STOCK",
        "SH_INDEX",
        "SH_FUND",
        "SH_BOND",
        "SZ_A_STOCK",
        "SZ_B_STOCK",
        "SZ_INDEX",
        "SZ_FUND",
        "SZ_BOND",
    ]

    # todo 股票系数
    SECURITY_COEFFICIENT = {
        "SH_A_STOCK": [0.01, 0.01],
        "SH_B_STOCK": [0.001, 0.01],
        "SH_INDEX": [0.01, 1.0],
        "SH_FUND": [0.001, 1.0],
        "SH_BOND": [0.001, 1.0],
        "SZ_A_STOCK": [0.01, 0.01],
        "SZ_B_STOCK": [0.01, 0.01],
        "SZ_INDEX": [0.01, 1.0],
        "SZ_FUND": [0.001, 0.01],
        "SZ_BOND": [0.001, 0.01],
    }

    def generate_filename(self, code, exchange):
        """

        :param code:
        :param exchange:
        :return:
        """
        if not self.vipdoc_path:
            raise TdxNotAssignVipdocPathException(r"Please provide a vipdoc path , such as c:\\newtdx\\vipdoc")  # noqa

        filename = Path(self.vipdoc_path, exchange, "lday", f"{exchange}{code}.day")  # noqa

        return str(filename)

    def get_kline_by_code(self, code, exchange):
        """

        :param code:
        :param exchange:
        :return:
        """
        filename = self.generate_filename(code, exchange)

        return self.parse_data_by_file(filename)

    def parse_data_by_file(self, filename):
        """

        :param filename:
        :return:
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException("no tdx kline data, please check path %s", filename)

        content = Path(filename).read_bytes()
        content = self.unpack_records("<IIIIIfII", content)

        return content

    def get_df(self, code_or_file, exchange=None):
        """

        :param code_or_file:
        :param exchange:
        :return:
        """
        if not exchange:
            return self.get_df_by_file(code_or_file)

        return self.get_df_by_code(code_or_file, exchange)

    def get_df_by_file(self, filename):
        """
        通过文件名读数据
        :param filename:
        :return:
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException("no tdx kline data, please check path %s", filename)

        security_type = self.get_security_type(filename)

        if security_type not in self.SECURITY_TYPE:
            logger.exception("Unknown security type !\n")
            raise NotImplementedError

        coefficient = self.SECURITY_COEFFICIENT[security_type]
        data = [self._df_convert(row_, coefficient) for row_ in self.parse_data_by_file(filename)]

        df = pd.DataFrame(data=data, columns=["date", "open", "high", "low", "close", "amount", "volume"])
        df.index = pd.to_datetime(df.date, errors="coerce")
        # df.index = pd.to_datetime(df.date)

        return df[["open", "high", "low", "close", "amount", "volume"]]

    def get_df_by_code(self, code, exchange):
        """
        通过股票代码生成文件名
        :param code: 股票代码
        :param exchange: 交易所
        :return:
        """
        name = self.generate_filename(code, exchange)

        return self.get_df_by_file(name)

    @staticmethod
    def _df_convert(row_, coefficient):
        """
        源数据转换

        :param row_:
        :param coefficient:
        :return:
        """
        t_date = str(row_[0])
        datestr = t_date[:4] + "-" + t_date[4:6] + "-" + t_date[6:]

        new_row = (
            datestr,
            row_[1] * coefficient[0],  # * 0.01 * 1000 , zipline need 1000 times to original price
            row_[2] * coefficient[0],
            row_[3] * coefficient[0],
            row_[4] * coefficient[0],
            row_[5],
            row_[6] * coefficient[1],
        )

        return new_row

    def get_security_type(self, filename):
        """
        获取股票类型, A股, B股, 指数等

        :param filename:
        :return:
        """
        logger.debug(f"get_security_type name: {filename}")
        basename = Path(filename).stem

        # exchange = str(name[-12:-10]).lower()
        # code_head = name[-10:-8]

        exchange = str(basename[:2]).lower()
        code_head = basename[2:4]

        if exchange == self.SECURITY_EXCHANGE[0]:
            if code_head in ["00", "30"]:
                return "SZ_A_STOCK"

            if code_head in ["20"]:
                return "SZ_B_STOCK"

            if code_head in ["39"]:
                return "SZ_INDEX"

            if code_head in ["15", "16"]:
                return "SZ_FUND"

            if code_head in ["10", "11", "12", "13", "14"]:
                return "SZ_BOND"

        if exchange == self.SECURITY_EXCHANGE[1]:
            if code_head in ["60", "68"]:  # 688XXX科创板
                return "SH_A_STOCK"

            if code_head in ["90"]:
                return "SH_B_STOCK"

            if code_head in ["00", "88", "99"]:
                return "SH_INDEX"

            if code_head in ["50", "51"]:
                return "SH_FUND"

            if code_head in ["01", "10", "11", "12", "13", "14", "20"]:
                return "SH_BOND"

        logger.debug("Unknown security exchange !")
        raise NotImplementedError
