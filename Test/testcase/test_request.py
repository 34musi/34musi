import requests

from common.requests_util import re
from common.yaml_util import write_extract_yaml


class TestWanJiaDj:

    def test_login(self):
        url = "/api/user/login"
        method = "post"
        data = {
            "username": "admin",
            "password": "starunion2021"
        }
        res = re.send_requests(url=url, method=method, headers=None, json=data)
        res_token = res.json()
        write_extract_yaml(res_token)

    # def test_shishi(self):
    #     # url = "api/real_time_online"
    #     headers = {}
    #     method = "post"
    #     data = {
    #         "base_param": {
    #             "player_info": {},
    #             "time_range": {
    #                 "from_time": 1698969600,
    #                 "to_time": 1699055999,
    #                 "time_zone": 0
    #             },
    #             "group": {
    #                 "group_by_date": 7
    #             },
    #             "page": 1,
    #             "size": 20
    #         },
    #         "payment_param": {},
    #         "role_param": {},
    #         "account_param": {},
    #         "device_param": {},
    #         "log_base_param": {},
    #         "activity_param": {}
    #     }
    #     res = re.send_requests(url=url, method=method, data=data)
    #     return res

if __name__ == '__main__':
    T = TestWanJiaDj()
