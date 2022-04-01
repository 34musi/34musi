#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      test_gm_manager_gift_group_search.py
@time:      2022/3/23 18:42
{中台系统} - {GM管理平台} - {游戏活动管理} - {活动查询}

"""

import time

import allure
import pytest
from apiFramework.Lib.BusinessFunc.InterfacePublicMethods import InterfacePublicMethods
from apiFramework.Lib.InterfacesFunc.business_method.gm_management_platform_business import gm_manager_method
from apiFramework.Lib.until.doConfigpar import operation
from apiFramework.Lib.until.operationYaml import OperationYaml
from apiFramework.Log.log_new import Logging

logger = Logging("test.py")
headers = operation('Config', 'Middle_station_system.ini')


@allure.feature("中台系统 - GM管理平台-游戏活动管理-操作记录详情")
class TestClassuser():

    def setup_class(self):
        before = time.ctime()
        logger.info(" test_gm_manager_gift_group_search before time is : %s " % before)
        self.objyaml = OperationYaml()
        self.interface_info = \
        self.objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'test_gm_manager_gift_group_search')[0]
        self.url = self.interface_info['url']
        self.method = self.interface_info['method'].lower()
        self.data = self.interface_info['data']
        self.data['relation_id'] = gm_manager_method.gm_manager_activity_search_id()
        self.dec = self.interface_info['des']
        self.headers = eval(headers.doIni('Middle_system', 'headers'))
        logger.debug(self.dec)

    def test_gm_manager_gift_group_search(self):
        res = InterfacePublicMethods().NormalTestInterface(url=self.url, method=self.method, json=self.data,
                                                           headers=self.headers)
        logger.info(res.json())

    def assert_method(self, res):
        assert res.status_code == 200
        assert 'Success', res.text
        assert '20000' in res.text

    def teardown_class(self):
        after = time.ctime()
        logger.debug(" test_gm_manager_gift_group_search after time is : %s " % after)


if __name__ == '__main__':
    pytest.main(['-s', '-v', ' test_gm_manager_gift_group_search.py'])


