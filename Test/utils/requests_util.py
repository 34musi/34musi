import requests


class RequestUtil:
    """获得session对话"""

    session = requests.session()

    def method_request(self, method, url, **kwargs):
        method = str(method).lower()
        if method == "post":
            res = RequestUtil.session.request("post", url, **kwargs)
            return res
        if method == "put":
            res = RequestUtil.session.request("put", url, **kwargs)
            return res
        if method == "delete":
            res = RequestUtil.session.request("delete", url, **kwargs)
            return res
        if method == "get":
            res = RequestUtil.session.request("get", url, **kwargs)
            return res
