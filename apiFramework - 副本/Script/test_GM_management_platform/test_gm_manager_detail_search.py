#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      test_gm_manager_detail_search.py
@time:      2022/3/22 16:13
{中台系统} - {GM管理平台} - {游戏活动管理} - {活动详情查询}
"""
import allure
import pytest
from apiFramework.Common.public import *
from apiFramework.Lib.BusinessFunc.InterfacePublicMethods import InterfacePublicMethods
from apiFramework.Lib.until.doConfigpar import operation
from apiFramework.Lib.until.operationYaml import OperationYaml
from apiFramework.Log.log_new import Logging
logger = Logging("test.py")
headers = operation('Config', 'Middle_station_system.ini')
from apiFramework.Lib.InterfacesFunc.business_method.gm_management_platform_business import gm_manager_method


@allure.feature("中台系统 - GM管理平台-游戏活动管理-活动详情查询")
class TestClassuser():

    def setup_class(self):
        after = time.ctime()
        logger.info("test_gm_manager_detail_search after time is : %s " % after)
        self.objyaml = OperationYaml()
        self.interface_info = self.objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'test_gm_manager_detail_search')[0]
        self.url = self.interface_info['url']
        self.method = self.interface_info['method'].lower()
        self.data = self.interface_info['data']
        self.data['activity_id'] = gm_manager_method.gm_manager_activity_search_id()
        self.dec = self.interface_info['des']
        self.headers = eval(headers.doIni('Middle_system', 'headers'))
        logger.debug(self.dec)

    def test_gm_manager_add_activity(self):
        logger.info(self.dec)
        res = InterfacePublicMethods().NormalTestInterface(url=self.url, method=self.method, json=self.data, headers=self.headers)
        logger.debug(res.json())
        self.assert_method(res)

    #断言
    def assert_method(self, res):
        assert res.status_code == 200
        assert 'Success', res.text
        assert '20000' in res.text
        assert  self.data['activity_id'] , res.text


    def teardown_class(self):
        after = time.ctime()
        #后置操作
        logger.info("test_gm_manager_detail_search after time is : %s " % after)


if __name__ == '__main__':
    pytest.main(['-s', '-v', 'test_gm_manager_detail_search.py'])



