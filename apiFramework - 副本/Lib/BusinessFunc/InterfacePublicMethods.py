#!/usr/bin/python
# -*- coding: UTF-8 -*-

"""
@author:yuless
@file:InterfacePublicMethods.py
@time:2022/3/21
"""

import random
import requests
from retrying import retry
from apiFramework.Lib.until.operationYaml import OperationYaml
from apiFramework.Log.log_new import Logging
logger = Logging("test.py")
objamli = OperationYaml()

class InterfacePublicMethods(object):


    def initRequestUrl(self):
        #拼接接口方法
        interface_info = objamli.aloneReadYaml('Config', 'middleEnv.yaml', 'middle_env')[0]
        theInitialAddress = interface_info.get('middle_host').get("test") + ":" + str(interface_info.get('middle_prod').get("test"))
        return theInitialAddress

    @retry(stop_max_attempt_number=3, retry_on_result=lambda x: x is None, wait_fixed=2000)
    def NormalTestInterface(self,url,method,headers,**kwargs):

        url = self.initRequestUrl() + url
        logger.info("正在发送get请求，请求地址：{}， 请求参数{}".format(url, kwargs))
        responseResult = requests.request(url=url,method=method, headers=headers, **kwargs)
        logger.info("正在发送get请求， 请求结果{}".format(responseResult.text))
        return responseResult


    def NormalTestInterface_lostObject(self, url,method,data,headers):
        '''Body缺少可选参数，接口请求后，判断请求是否正常，判断返回是否成功'''
        logger.info('*** Body缺少可选参数，请求成功 ***')
        for k,v in list(data.items()):
            data[k] = ''
            logger.info('参数{}为空执行接口情况'.format(k))
            try:
                res = self.NormalTestInterface(url=url, method=method, json=data, headers=headers)
                data.pop(k)
                data[k] = v
                assert 'S100' in res.text
                logger.info('{}参数不为必填项目，可以为空'.format(k), )
            except  AssertionError as e:
                logger.info('{}参数为必填项目，不能为空 断言报错{}'.format(k,e),)
                # exceptions = str(e) + '%s参数为必填项目，不能为空 断言报错'.format(e),k + '\n'
                # raise Exception(exceptions)

    #重复请求
    def NormalTestInterface_ReTry(self, url,method,data,headers):
        '''Body重新请求，接口请求后，判断请求是否正常，判断返回是否成功'''
        logger.info('*** Body缺少可选参数，请求成功 ***')
        for k,v in list(data.items()):
            data[k] = ''
            logger.info('参数{}为空执行接口情况'.format(k))
            try:
                res =  self.NormalTestInterface(url=url, method=method, json=data, headers=headers)
                data.pop(k)
                data[k] = v
                assert 'S100' in res.text
                logger.info('{}参数不为必填项目，可以为空'.format(k), )
            except  AssertionError as e:
                logger.info('{}参数为必填项目，不能为空 断言报错{}'.format(k,e),)
                # exceptions = str(e) + '%s参数为必填项目，不能为空 断言报错'.format(e),k + '\n'
                # raise Exception(exceptions)


    def MoreTestInterface_lostParamsObject(self, url,method,data,headers):
        '''Params长度超长，接口请求后，判断请求是否正常，判断返回是否成功'''
        logger.info('*** Params缺少可选参数，请求成功 ***')
        for k, v in list(data.items()):
            new_char = ['',99999999999999999,'']
            replace_value = random.choice(new_char)
            data[k] = replace_value
            logger.info('参数超长{}执行接口情况'.format(k))
            try:
                res =  self.NormalTestInterface(url=url, method=method, json=data, headers=headers)
                data.pop(k)
                data[k] = v
                assert 'S100' in res.text
                logger.info('{}参数没做校验，可以超长'.format(k), )
            except  AssertionError as e:
                logger.info('{}参数做了限制，不能超长 断言报错{}'.format(k, e), )




if __name__ == '__main__':

    objyaml = OperationYaml()
    interface_info = objyaml.aloneReadYaml('Config', 'middleEnv.yaml', 'middle_env')[0]
    interface_info1 = objyaml.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'gm_manager_activity_search_test')[0]
    res = InterfacePublicMethods().initRequestUrl()


