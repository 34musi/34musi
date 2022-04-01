#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      gm_management_platform_business.py
@time:      2022/3/22 10:50

"""
from apiFramework.Lib.BusinessFunc.InterfacePublicMethods import InterfacePublicMethods
from apiFramework.Lib.until.doConfigpar import operation
from apiFramework.Lib.until.operationYaml import OperationYaml

obj = operation('Config', 'Middle_station_system.ini')
headers = obj.doIni('Middle_system', 'headers')


# 游戏活动业务方法   地址：http://platform-develop.outer.staruniongame.com:32141/game-manager/activity/index
# GM平台  游戏活动管理 -

class gm_manager_method:

    @classmethod
    def gm_manager_activity_search_id(cls):
        interface_info = \
        OperationYaml().aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'gm_manager_activity_search')[0]
        url = interface_info['url']
        method = interface_info['method'].lower()
        data = interface_info['data']

        res = InterfacePublicMethods().NormalTestInterface(url=url, method=method, json=data, headers=eval(headers))
        id = res.json().get('data').get('items')[0]['id']
        # 返回 GM平台  游戏活动管理 - 活动页面ID
        return id


if __name__ == '__main__':
    gm_manager_method.gm_manager_activity_search_id()
