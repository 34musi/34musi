import json

import requests
from common.yaml_util import *
from common.yaml_util import *


class RequestUtil:
    """获得session对话"""

    def __init__(self, file_path, base, base_url):
        """获取通用指定的url路径地址"""
        self.base_url = read_yaml(file_path, base, base_url)
        self.last_herders = {}

    # 替换数据的方法
    def replace_value(self, data):
        if data and isinstance(data, dict):  # 如果传入的数据不是空，并且是字典的形式
            str_data = json.dumps(data)
        else:
            str_data = data
        for i in range(1, str_data.count("{{") + 1):
            if "{{" in str_data and "}}" in str:
                star_va = str_data.index("{{")
                end_va = str_data.index("}}")
                old_va = str_data[star_va:end_va + 2]
                new_va = read_yaml(extract_path(), old_va[2:-2])
                str_data.replace(old_va, new_va)
        if data and isinstance(data, dict):
            data = json.loads(str_data)
        else:
            data = str
        return data

    # 发送请求的方法
    def send_requests(self, method, url, headers=None, **kwargs):  # headers=None
        self.url = self.base_url + url  # 需要访问的接口地址
        self.last_method = str(method).lower()  # 处理请求方法的大小写
        # # 判断请求头是否为空
        if headers and isinstance(headers, dict):
            self.last_herders = self.replace_value(headers)

        # 如何替换请求头中，可能会包含params， data， json格式的数据
        for key, value in kwargs.items():
            if key in ["params", "data", "json"]:
                kwargs[key] = self.replace_value(value)
        res = requests.session().request(method=self.last_method, url=self.url, headers=self.last_herders,
                                         **kwargs)  # 发送请求 headers=self.last_herders,
        return res

    def get_cookie(self):
        cook = read_yaml("Cookie", "cookie")
        return cook

    def get_token(self):
        tok = read_yaml("X-Token", "token")
        return tok


re = RequestUtil(yong_li_path(), "base", "xinghe_base")
if __name__ == '__main__':
    res = re.send_requests("post", "/api/user/login", json= {
            "username": "admin",
            "password": "starunion2021"
        } )