# cython: language_level=3
import abc
import tempfile
from urllib.request import Request
from urllib.request import urlopen

from tdxpy.logger import logger


def fetch_report_hook(downloaded, total_size):
    logger.debug(f"Downloaded {downloaded}, Total is {total_size}")


class BaseCrawler:
    mode = "http"

    def fetch_and_parse(
        self, reporthook=None, path_to_download=None, proxies=None, chunksize=1024 * 50, *args, **kwargs
    ):
        """
        function to get data ,
        :param chunksize:
        :param args:
        :param kwargs:
        :return:
        :param reporthook 使用urllib.request 的report_hook 来汇报下载进度 \
                    参考 https://docs.python.org/3/library/urllib.request.html#module-urllib.request
        :param path_to_download 数据文件下载的地址，如果没有提供，则下载到临时文件中，并在解析之后删除
        :param proxies urllib格式的代理服务器设置
        :return: 解析之后的数据结果
        """

        method = ("get_content", "fetch_via_http")[self.mode == "http"]
        download_file = getattr(self, method)(
            reporthook=reporthook,
            path_to_download=path_to_download,
            proxies=proxies,
            chunksize=chunksize,
            *args,
            **kwargs,
        )
        result = self.parse(download_file, *args, **kwargs)
        download_file.close()

        return result

    def fetch_via_http(self, reporthook=None, path_to_download=None, chunksize=1024 * 50, *args, **kwargs):
        """

        :param reporthook:
        :param path_to_download:
        :param chunksize:
        :param args:
        :param kwargs:
        :return:
        """
        download_file = path_to_download and open(path_to_download, "wb") or tempfile.NamedTemporaryFile(delete=True)
        url = self.get_url(*args, **kwargs)

        request = Request(url)
        request.add_header("Referer", url)
        request.add_header(
            "User-Agent",
            r"Mozilla/5.0 (Macintosh Intel Mac OS X 10_14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/76.0.3809.100 Safari/537.36",
        )
        response = urlopen(request)

        if response.getheader("Content-Length") is not None:
            total_size = int(response.getheader("Content-Length").strip())
            downloaded = 0

            while True:
                chunk = response.read(chunksize)
                downloaded += len(chunk)

                reporthook and reporthook(downloaded, total_size)

                if not chunk:
                    break

                download_file.write(chunk)
        else:
            content = response.read()
            download_file.write(content)

        download_file.seek(0)
        return download_file

    @abc.abstractmethod
    def get_url(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        """
        raise NotImplementedError("will impl in subclass")

    @abc.abstractmethod
    def get_content(self, reporthook=None, path_to_download=None, proxies=None, chunksize=1024 * 50, *args, **kwargs):
        """

        :param reporthook:
        :param path_to_download:
        :param proxies:
        :param chunksize:
        :param args:
        :param kwargs:
        """
        raise NotImplementedError("will impl in subclass")

    @abc.abstractmethod
    def parse(self, download_file, *args, **kwargs):
        """

        :param download_file:
        :param args:
        :param kwargs:
        """
        raise NotImplementedError("will impl in subclass")
