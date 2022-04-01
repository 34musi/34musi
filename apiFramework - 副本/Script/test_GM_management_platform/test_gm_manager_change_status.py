#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      test_gm_manager_change_status.py
@time:      2022/3/22 17:57
{中台系统} - {GM管理平台} - {游戏活动管理} - {活动删除}

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
        logger.info("test_gm_manager_change_status after time is : %s " % after)
        self.objyaml = OperationYaml()
        self.interface_info = \
        self.objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'test_gm_manager_change_status')[0]
        self.url = self.interface_info['url']
        self.method = self.interface_info['method'].lower()
        self.data = self.interface_info['data']
        self.dec = self.interface_info['des']
        self.headers = eval(headers.doIni('Middle_system', 'headers'))
        del self.headers['Content-Type']
        logger.debug(self.headers)


    def test_gm_manager_change_status(self):
        logger.info(self.dec)
        logger.info(self.method)
        res = InterfacePublicMethods().NormalTestInterface(url=self.url, method=self.method, data=self.data, headers=self.headers)
        logger.debug(res.json())
        self.assert_method(res)

    #断言
    def assert_method(self, res):
        assert res.status_code == 200
        assert 'request_id' in res.text
        # assert '20000' in res.text

    def teardown_class(self):
        after = time.ctime()
        # self.db.close()
        logger.info("test_gm_manager_change_status after time is : %s " % after)


if __name__ == '__main__':
    pytest.main(['-s', '-v', 'test_gm_manager_change_status.py'])



