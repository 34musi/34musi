# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price
from tdxpy.helper import get_time
from tdxpy.parser.base import BaseParser
from tdxpy.logger import logger

class GetHistoryTransactionData(BaseParser):
    def setParams(self, market, code, start, count, date):
        """

        :param market:
        :param code:
        :param start:
        :param count:
        :param date:
        """
        if type(code) is str:
            code = code.encode("utf-8")

        if type(date) is (type(date) is str) or (type(date) is bytes):
            date = int(date)

        pkg = bytearray.fromhex("0c 01 30 01 00 01 12 00 12 00 b5 0f")
        pkg.extend(struct.pack("<IH6sHH", date, market, code, start, count))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0
        (num,) = struct.unpack("<H", body_buf[:2])
        logger.debug(f'num => {num}')

        pos += 2
        ticks = []

        # skip 4 bytes
        pos += 4
        last_price = 0

        for _ in range(num):
            hour, minute, pos = get_time(body_buf, pos)  # noqa

            price_raw, pos = get_price(body_buf, pos)
            vol, pos = get_price(body_buf, pos)

            buy_or_sell, pos = get_price(body_buf, pos)
            _, pos = get_price(body_buf, pos)

            last_price = last_price + price_raw

            tick = OrderedDict(
                [
                    ("time", "%02d:%02d" % (hour, minute)),
                    ("price", float(last_price) / 100),
                    ("vol", vol),
                    ("buyorsell", buy_or_sell),
                ]
            )

            logger.debug(f'tick => {tick}')
            ticks.append(tick)

        return ticks
