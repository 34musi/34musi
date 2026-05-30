# cython: language_level=3
import secrets
import shutil
import tempfile
from pathlib import Path
from struct import calcsize
from struct import unpack

import pandas as pd

from ..exceptions import ValidationException
from .base_crawler import BaseCrawler

VALUE = "<6s1c1L"


class HistoryFinancialListCrawler(BaseCrawler):
    """
    获取历史财务数据的接口，参考上面issue里面 @datochan 的方案和代码
        https://github.com/rainx/tdxpy/issues/133
    """

    mode = "content"

    def get_url(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        :return:
        """
        return "https://gitee.com/yutiansut/QADATA/raw/master/financial/content.txt"

    def get_content(self, reporthook=None, path_to_download=None, proxies=None, chunksize=1024 * 50, *args, **kwargs):
        """

        :param reporthook:
        :param path_to_download:
        :param proxies:
        :param chunksize:
        :param args:
        :param kwargs:
        :return:
        """
        from tdxpy.hq import TdxHq_API

        api = TdxHq_API()
        api.need_setup = False

        # calc.tdx.com.cn, calc2.tdx.com.cn
        with api.connect(ip="120.76.152.87"):
            content = api.get_report_file_by_size("tdxfin/gpcw.txt")
            download_file = (
                not path_to_download and tempfile.NamedTemporaryFile(delete=True) or open(path_to_download, "wb")
            )
            download_file.write(content)
            download_file.seek(0)

            return download_file

    def parse(self, download_file, *args, **kwargs):
        """

        :param download_file:
        :param args:
        :param kwargs:
        :return:
        """
        content = download_file.read()
        content = content.decode("utf-8")

        def list_to_dict(li):
            return {"filename": li[0], "hash": li[1], "filesize": int(li[2])}

        result = [list_to_dict(x) for x in [line.strip().split(",") for line in content.strip().split("\n")]]

        return result


class HistoryFinancialCrawler(BaseCrawler):
    mode = "content"

    def get_url(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        :return:
        """
        if "filename" not in kwargs:
            raise ValidationException("Param filename is not set")

        filename = kwargs["filename"]

        return f"http://data.yutiansut.com/{filename}"  # noqa

    def get_content(self, reporthook=None, path_to_download=None, proxies=None, chunksize=1024 * 50, *args, **kwargs):
        """

        :param reporthook:
        :param path_to_download:
        :param proxies:
        :param chunksize:
        :param args:
        :param kwargs:
        :return:
        """
        from tdxpy.hq import TdxHq_API

        if "filename" not in kwargs:
            raise ValidationException("Param filename is not set")

        filename = kwargs["filename"]
        file_size = kwargs.get("filesize", 0)

        api = TdxHq_API()
        api.need_setup = False

        with api.connect(ip="120.76.152.87"):
            # calc.tdx.com.cn, calc2.tdx.com.cn
            content = api.get_report_file_by_size(f"tdxfin/{filename}", filesize=file_size, reporthook=reporthook)
            download_file = (
                path_to_download and open(path_to_download, "wb") or tempfile.NamedTemporaryFile(delete=True)
            )
            download_file.write(content)
            download_file.seek(0)

            return download_file

    def parse(self, download_file, *args, **kwargs):
        """

        :param download_file:
        :param args:
        :param kwargs:
        :return:
        """
        tmpdir = None
        header_pack_format = "<1hI1H3L"

        if download_file.name.endswith(".zip"):
            tmpdir_root = tempfile.gettempdir()
            subdir_name = f"tdxpy_{secrets.randbelow(1000000)}"

            tmpdir = Path(tmpdir_root, subdir_name)
            shutil.rmtree(tmpdir, ignore_errors=True)

            tmpdir.mkdir(parents=True, exist_ok=True)
            shutil.unpack_archive(download_file.name, extract_dir=tmpdir)

            # only one file endswith .dat should be in zip archives
            dat_file = None

            for _file in tmpdir.iterdir():
                if str(_file).endswith(".dat"):
                    dat_file = open(str(_file), "rb")

            if not dat_file:
                raise ValidationException("no dat file found in zip archive")
        else:
            dat_file = download_file

        header_size = calcsize(header_pack_format)
        stock_item_size = calcsize(VALUE)

        data_header = dat_file.read(header_size)
        stock_header = unpack(header_pack_format, data_header)

        max_count = stock_header[2]

        report_date = stock_header[1]
        report_size = stock_header[4]

        report_fields_count = int(report_size / 4)
        report_pack_format = f"<{report_fields_count}f"

        results = []

        for stock_idx in range(0, max_count):
            dat_file.seek(header_size + stock_idx * calcsize(VALUE))
            si = dat_file.read(stock_item_size)

            stock_item = unpack("<6s1c1L", si)
            code = stock_item[0].decode("utf-8")

            foa = stock_item[2]
            dat_file.seek(foa)

            info_data = dat_file.read(calcsize(report_pack_format))
            cw_info = unpack(report_pack_format, info_data)

            one_record = (code, report_date) + cw_info
            results.append(one_record)

        if download_file.name.endswith(".zip"):
            dat_file.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

        return results

    @staticmethod
    def to_df(data):
        if not data:
            return None

        col = ["code", "report_date"]
        col += [f"col{str(i).zfill(3)}" for i in range(1, len(data[0]) - 1)]

        df = pd.DataFrame(data=data, columns=col)
        df.set_index("code", inplace=True)

        return df
