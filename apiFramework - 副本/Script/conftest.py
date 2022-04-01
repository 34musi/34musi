# #!/usr/bin/python
# # -*- coding: UTF-8 -*-
# """
# @Time   : 2022/01/07
# @Author : zhuzhuxia_yuless
# @file   : conftest.py.py
# @desc   : conftest.py
#
# 使用插槽  用来写业务方法
# """
#
# import time
# import pytest
#
#
# from apiFramework.Lib.until.operationYaml import OperationYaml
# from apiFramework.Base.method import MyRequest
# obj = MyRequest()
#
#
# @pytest.fixture(scope='session')
# def defuse_login():
#     objyaml = OperationYaml()
#     interface_info = objyaml.aloneReadYaml('Data', 'py_study', 'defuse_login')[0]
#     url = interface_info['url']
#     nowtime = str((time.time()))
#     nowtime = nowtime.replace(".", '')[0:14]
#     url = str(url).replace('{time}', nowtime)
#     method = interface_info['method'].lower()
#     data = interface_info['data']
#     obj = MyRequest()
#     res = obj.sendRequest(url=url, method= method, json= data)
#     defHeader = {
#         'authority': 'dfuse2-api.ovivas.cn',
#         'pragma': 'no-cache',
#         'cache-control': 'no-cache',
#         'sec-ch-ua': '" Not A;Brand";v="99", "Chromium";v="96", "Google Chrome";v="96"',
#         'accept': '*/*',
#         'authorization': 'Bearer {}'.format(res.json()['data']['token']),
#         'sec-ch-ua-mobile': '?0',
#         'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36',
#         'sec-ch-ua-platform': '"Windows"',
#         'origin': 'https://dfuse2-console.ovivas.cn',
#         'sec-fetch-site': 'same-site',
#         'sec-fetch-mode': 'cors',
#         'sec-fetch-dest': 'empty',
#         'referer': 'https://dfuse2-console.ovivas.cn/',
#         'accept-language': 'zh-CN,zh;q=0.9'
#     }
#     yield defHeader
#
#
#     #消息管理 - 消息列表ID获取
# @pytest.fixture(scope='session')
# def SearchSystemMessage_Id():
#     objyaml = OperationYaml()
#     interface_info = objyaml.aloneReadYaml('Data', 'test_msg_fam_user_day', 'SearchSystemMessage')[0]
#     url = interface_info['url']
#     method = interface_info['method'].lower()
#     data = interface_info['data']
#     res = obj.sendRequest(url=url, method=method, json=data, headers=headers)
#     id = res.json().get('data').get('rows')[-1]['id']
#
#     yield id
#
# if __name__ == '__main__':
#     SearchSystemMessage_Id()
