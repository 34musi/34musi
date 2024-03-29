# import json
#
# import allure
# import pytest
# import requests
# import yaml
#
#
# @allure.epic("项目名称：正在学习")
# @allure.feature("模块名称：test——api ")
# @allure.link(name="接口地址", url="www.baidu.ocm")
# def read_yaml(yaml_path):
#     with open(yaml_path, mode='r', encoding='utf-8') as f:
#         value = yaml.load(f, Loader=yaml.FullLoader)
#         return value
#
#
# class TestApi:
#     list = ["百里", "星耀", "王者"]
#
#     @allure.story("接口测试名称： 测试百里接口")
#     @pytest.mark.parametrize("args_name", list)
#     def test_bai(self, args_name):
#         return args_name
#
#     @allure.issue(name="bug地址", url="https://www.jd.com/")
#     @allure.severity(allure.severity_level.BLOCKER)  # 严重的bug
#     def test_li(self):
#         print("效力夏利")
#         # web报告
#         with open("D:\\Program Files\\项目\\Project\\34musi\\Test\\错误截图.png", mode="rb") as f:
#             allure.attach(body=f.read(), name="错误截图", attachment_type=allure.attachment_type.PNG)
#
#     @pytest.mark.parametrize("date", read_yaml("./get_yaml.yaml"))
#     @allure.severity(allure.severity_level.CRITICAL)  # 一般bug
#     @allure.description("描述：这个答案是0")
#     def test_num(self, date):
#         # print(
#         #     1 - 1
#         # )
#         # 接口报告
#         allure.attach(body=date["request"]["url"], name="请求地址：")
#         allure.attach(body=date["request"]["method"], name="请求方式:", attachment_type=allure.attachment_type.TEXT)
#         data = date["request"]["data"]
#         allure.attach(body=json.dumps(data), name="请求数据", attachment_type=allure.attachment_type.TEXT)
#         rep = requests.get(url=date["request"]["url"], params=data)
#         allure.attach(body=rep.text, name="响应数据", attachment_type=allure.attachment_type.TEXT)
#
#     #     # for i in range(1, 6):
#     #     #     with allure.step(f"用例步骤{i}"):
#     #     #         print(
#     #     #             f"这个是我执行的用例步骤{i}"
#     #     #         )
#
#     def setup_class(self):
#         print(
#             "在执行类之前之前一次"
#         )
#
# def test_send(self):
#     url = "/api/roles_log/gamelog/log_item"
#     method = "Post"
#     headers = {
#         "authority": "platform-release.staruniongame.com",
#         "accept": "pplication / json, text / plain, * / *",
#         "content-type": "application/json;charset=UTF-8",
#         "cookie": re.get_cookie(),
#         "X-Token": re.get_token()
#     }
#     data = {
#         "player_info": {
#             "role_id": "",
#             "role_name": "",
#             "acc_id": ""
#         },
#         "time_range": {
#             "from_time": 1696118400,
#             "to_time": 1698789428,
#             "time_zone": 0
#         },
#         "exc_internal": False,
#         "category": 1,
#         "order_by": {
#             "desc": True,
#             "field_choose": 1,
#             "field_name": "created_at"
#         },
#         "page": 1,
#         "size": 20,
#         "table_prefix": "log_item"
#     }
#     res = re.send_requests(method=method, url=url, headers=headers, data=data)
#     res_data = res.json()
#     write_extract_yaml(res_data)
#
#     return res
# if __name__ == '__main__':
#     t = TestApi()