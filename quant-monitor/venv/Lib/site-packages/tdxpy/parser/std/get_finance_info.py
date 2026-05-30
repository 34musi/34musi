# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser


class GetFinanceInfo(BaseParser):
    def setParams(self, market, code):
        """

        :param market:
        :param code:
        """
        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("0c 1f 18 76 00 01 0b 00 0b 00 10 00 01 00")
        pkg.extend(struct.pack("<B6s", market, code))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0
        pos += 2  # skip num ,we only query 1 in this case

        market, code = struct.unpack("<B6s", body_buf[pos : pos + 7])

        pos += 7

        (
            liutongguben,
            province,
            industry,
            updated_date,
            ipo_date,
            zongguben,
            guojiagu,
            faqirenfarengu,
            farengu,
            bgu,
            hgu,
            zhigonggu,
            zongzichan,
            liudongzichan,
            gudingzichan,
            wuxingzichan,
            gudongrenshu,
            liudongfuzhai,
            changqifuzhai,
            zibengongjijin,
            jingzichan,
            zhuyingshouru,
            zhuyinglirun,
            yingshouzhangkuan,
            yingyelirun,
            touzishouyu,
            jingyingxianjinliu,
            zongxianjinliu,
            cunhuo,
            lirunzonghe,
            shuihoulirun,
            jinglirun,
            weifenlirun,
            baoliu1,
            baoliu2,
        ) = struct.unpack("<fHHIIffffffffffffffffffffffffffffff", body_buf[pos:])

        def _get_v(v):
            return v

        result = OrderedDict(
            [
                ("market", market),
                ("code", code.decode("utf-8")),
                ("liutongguben", _get_v(liutongguben) * 10000),
                ("province", province),
                ("industry", industry),
                ("updated_date", updated_date),
                ("ipo_date", ipo_date),
                ("zongguben", _get_v(zongguben) * 10000),
                ("guojiagu", _get_v(guojiagu) * 10000),
                ("faqirenfarengu", _get_v(faqirenfarengu) * 10000),
                ("farengu", _get_v(farengu) * 10000),
                ("bgu", _get_v(bgu) * 10000),
                ("hgu", _get_v(hgu) * 10000),
                ("zhigonggu", _get_v(zhigonggu) * 10000),
                ("zongzichan", _get_v(zongzichan) * 10000),
                ("liudongzichan", _get_v(liudongzichan) * 10000),
                ("gudingzichan", _get_v(gudingzichan) * 10000),
                ("wuxingzichan", _get_v(wuxingzichan) * 10000),
                ("gudongrenshu", _get_v(gudongrenshu)),
                ("liudongfuzhai", _get_v(liudongfuzhai) * 10000),
                ("changqifuzhai", _get_v(changqifuzhai) * 10000),
                ("zibengongjijin", _get_v(zibengongjijin) * 10000),
                ("jingzichan", _get_v(jingzichan) * 10000),
                ("zhuyingshouru", _get_v(zhuyingshouru) * 10000),
                ("zhuyinglirun", _get_v(zhuyinglirun) * 10000),
                ("yingshouzhangkuan", _get_v(yingshouzhangkuan) * 10000),
                ("yingyelirun", _get_v(yingyelirun) * 10000),
                ("touzishouyu", _get_v(touzishouyu) * 10000),
                ("jingyingxianjinliu", _get_v(jingyingxianjinliu) * 10000),
                ("zongxianjinliu", _get_v(zongxianjinliu) * 10000),
                ("cunhuo", _get_v(cunhuo) * 10000),
                ("lirunzonghe", _get_v(lirunzonghe) * 10000),
                ("shuihoulirun", _get_v(shuihoulirun) * 10000),
                ("jinglirun", _get_v(jinglirun) * 10000),
                ("weifenpeilirun", _get_v(weifenlirun) * 10000),
                ("meigujingzichan", _get_v(baoliu1)),
                ("baoliu2", _get_v(baoliu2)),
            ]
        )

        return result
