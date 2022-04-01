#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:yuless
@file:test_search_family_list.py
@time:2021/12/27

"""

import pytest
import allure
from apiFramework.Lib.BusinessFunc.InterfacePublicMethods import InterfacePublicMethods
from apiFramework.Lib.until.doConfigpar import operation
from apiFramework.Lib.until.operationYaml import OperationYaml
from apiFramework.Log.log_new import  Logging
logger = Logging("test.py")
headers = operation('Config', 'Middle_station_system.ini')


#
@allure.feature("中台系统 - GM管理平台-游戏活动管理-活动查询")
class TestClassuser():

    def setup(self):
        self.objyaml = OperationYaml()
        self.interface_info = self.objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'gm_manager_activity_search')[0]
        self.url = self.interface_info['url']
        self.method = self.interface_info['method'].lower()
        self.data = self.interface_info['data']
        self.dec = self.interface_info['des']
        self.headers = eval(headers.doIni('Middle_system','headers'))

    @allure.description('活动查询')
    def test_gm_manager_activity_search(self):
        res = InterfacePublicMethods().NormalTestInterface(url=self.url, method=self.method, json=self.data, headers=self.headers)
        logger.debug(res.json()['data']['items'][0])
        self.assert_method(res)

    def assert_method(self,res):
        assert res.status_code == 200
        assert 'Success', res.text
        assert '20000' in res.text


if __name__ == '__main__':
    # test_SearchSystemReviewsListV2()
    pytest.main([ '-s','-v','test_search_family_list.py'])


