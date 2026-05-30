# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price
from tdxpy.helper import get_time
from tdxpy.parser.base import BaseParser


class GetTransactionData(BaseParser):
    def setParams(self, market, code, start, count):
        """
        设置请求参数
        :param market:
        :param code:
        :param start:
        :param count:
        """
        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("0c 17 08 01 01 01 0e 00 0e 00 c5 0f")
        pkg.extend(struct.pack("<H6sHH", market, code, start, count))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """
        解析返回结果
        :param body_buf:
        :return:
        """
        pos = 0

        (num,) = struct.unpack("<H", body_buf[:2])

        pos += 2

        ticks = []
        last_price = 0

        for _ in range(num):
            hour, minute, pos = get_time(body_buf, pos)
            price_raw, pos = get_price(body_buf, pos)

            vol, pos = get_price(body_buf, pos)
            num, pos = get_price(body_buf, pos)

            buy_or_sell, pos = get_price(body_buf, pos)
            _, pos = get_price(body_buf, pos)

            last_price = last_price + price_raw

            tick = OrderedDict(
                [
                    ("time", "%02d:%02d" % (hour, minute)),
                    ("price", float(last_price) / 100),
                    ("vol", vol),
                    ("num", num),
                    ("buyorsell", buy_or_sell),  # noqa
                ]
            )

            ticks.append(tick)

        return ticks
