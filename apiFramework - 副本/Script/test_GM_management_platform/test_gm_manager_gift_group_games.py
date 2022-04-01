#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      test_gm_manager_gift_group_games.py
@time:      2022/3/23 17:32
{中台系统} - {GM管理平台} - {游戏活动管理} - {活动查询}

"""

import pytest
import allure
from apiFramework.Common.public import *
from apiFramework.Lib.BusinessFunc.InterfacePublicMethods import InterfacePublicMethods
from apiFramework.Log.log_new import Logging
logger = Logging("test.py")
from apiFramework.Lib.until.doConfigpar import operation
headers = operation('Config', 'Middle_station_system.ini')
from apiFramework.Lib.until.operationYaml import OperationYaml



@allure.feature("中台系统 - GM管理平台-游戏活动管理-{模块名}")
class TestClassuser():

    def setup_class(self):
        after = time.ctime()
        logger.info("test_gm_manager_gift_group_games after time is : %s " % after)
        self.objyaml = OperationYaml()
        self.interface_info = self.objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'test_gm_manager_gift_group_games')[0]
        self.url = self.interface_info['url']
        self.method = self.interface_info['method'].lower()
        self.dec = self.interface_info['des']
        self.headers = eval(headers.doIni('Middle_system', 'headers'))
        logger.debug(self.dec)

    def test_gm_manager_gift_group_games(self):
        logger.info(self.dec)
        response = InterfacePublicMethods().NormalTestInterface(url=self.url, method=self.method, headers=self.headers)
        # res = MyRequest().sendRequest(url=self.url, method=self.method, json=self.data, headers=self.headers)
        self.assert_method(response)

    #断言
    def assert_method(self, res):
        assert res.status_code == 200
        assert 'Success', res.text
        assert '20000' in res.text

    def teardown_class(self):
        after = time.ctime()
        # self.db.close()
        logger.info("test_gm_manager_gift_group_games after time is : %s " % after)


if __name__ == '__main__':
    pytest.main(['-s', '-v', 'test_gm_manager_gift_group_games.py'])



