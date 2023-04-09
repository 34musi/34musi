import json

import allure
import pytest
import requests


@allure.epic("项目名称：正在学习")
@allure.feature("模块名称：test——api ")
@allure.link(name="接口地址", url="www.baidu.ocm")
class TestApi:
    list = ["百里", "星耀", "王者"]

    @allure.story("接口测试名称： 测试百里接口")
    @pytest.mark.parametrize("args_name", list)
    def test_bai(self, args_name):
        print("百里")
        print(args_name)

    @allure.issue(name="bug地址", url="https://www.jd.com/")
    @allure.severity(allure.severity_level.BLOCKER)  # 严重的bug
    def test_li(self):
        print("效力夏利")
        # web报告
        with open("D:\\Program Files\\项目\\Project\\34musi\\Test\\错误截图.png", mode="rb") as f:
            allure.attach(body=f.read(), name="错误截图", attachment_type=allure.attachment_type.PNG)

    @allure.severity(allure.severity_level.CRITICAL)  # 一般bug
    @allure.description("描述：这个答案是0")
    def test_num(self):
        print(
            1 - 1
        )
        # 接口报告
        allure.attach(body="https://api.weixin.qq.com/cgi-bin/token", name="请求地址：")
        allure.attach(body="get", name="请求方式:", attachment_type=allure.attachment_type.TEXT)
        data = {
            "grant_type": "client_credential",
            "appid": "wx6b11b3efd1cdc290",
            "secret": "106a9c6157c4bd5f6029918738f9529d"
        }
        allure.attach(body=json.dumps(data), name="请求数据", attachment_type=allure.attachment_type.TEXT)
        rep = requests.get(url="https://api.weixin.qq.com/cgi-bin/token", params=data)
        allure.attach(body=rep.text, name="响应数据", attachment_type=allure.attachment_type.TEXT)
        for i in range(1, 6):
            with allure.step(f"用例步骤{i}"):
                print(
                    f"这个是我执行的用例步骤{i}"
                )

    def setup_class(self):
        print(
            "在执行类之前之前一次"
        )
