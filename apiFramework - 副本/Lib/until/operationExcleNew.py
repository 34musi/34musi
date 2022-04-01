#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author: yuless
@file: operationExcel.py
@time: 2021/11/27
"""
import json
import xlrd
from jinja2 import Template

from apiFramework.Common.public import *

class ExcelValues:

    caseId= "caseID"
    des = "描述"
    caseUrl = "请求地址"
    caseMethod=  "请求方法"
    caseJson =  "请求参数"
    caseExpect = "期望结果"
    isRun = "isrun"
    headers= 'headers'
    casePre = '前置条件'
    caseCode = 'code'

class OperationExcel:
    #
    GlobalVariable = {}
    def getSheet(self):
        book = xlrd.open_workbook_xls(filePath('Data','yuless.xls'))
        return book.sheet_by_index(0)
    # 获取总行数

    def getRows(self):
        return self.getSheet().nrows
    #获取总列数

    def getCols(self):
        return self.getSheet().ncols

    def getValue(self,row,col):
        return self.getSheet().cell_value(row,col)

    #获取所有数据
    def getExcelDates(self,data):
        # for item in self.getRows:
        datas = list()
        if data== None:

            title = self.getSheet().row_values(0)
            # title = ['no', 'skip', 'caseno', 'module', 'method', 'url', 'headers', 'payload', 'expected_result',
            #          'set_global']
            for row in range(1, self.getSheet().nrows):
                row_value = self.getSheet().row_values(row)
                datas.append((dict(zip(title, row_value))))
            return datas
        else:
            title = ['no', 'skip', 'caseno', 'module', 'method', 'url', 'headers', 'payload', 'expected_result','set_global']
            for row in range(1,self.getSheet().nrows):
                row_value  = self.getSheet().row_values(row)
                datas.append((dict(zip(title,row_value))))
            return datas

    def runs(self):
        run_list= []
        for item in self.getExcelDates():
            isRun = item[ExcelValues.isRun]
            if isRun=='y':
                run_list.append(item)
            else:pass
        return run_list

    def case_list(self):
        case_list = []
        for item in self.getExcelDates():
            case_list.append(item)
        return  case_list

    def params(self):
        '''队请求参数为空进行处理'''
        params_list = []
        for item in self.runs():
            params = item[ExcelValues.caseJson]
            if len(str(params).strip())==0:pass
            elif len(str(params.strip()))>=0:
                params=json.loads(params)
                return params_list.append(params)

    def case_prev(self,case_prev):
        #根据前置测试条件找到相关的前置测试用例
        #:parms casePrev:前置测试条件
        for item in self.runs():
            if case_prev in item.values():
                return item
        return None

    def prevHeaders(self,prevResult):
        '''
        替换被关联测试点的请求头
        :param prevResult:
        :return:
        '''
        for item in self.runs():
            headers = item[ExcelValues.headers]
            if '{token}'in headers:
                headers = str(headers).replace('{token}',prevResult)
                return headers

    def get_gloabal( self,model, **kwargs):  # 全局变量
        if not isinstance(model, str):
            model = str(model)
        if not isinstance(kwargs, dict):
            raise ValueError('参数错误')
        template = Template(model)
        sult = template.render(**kwargs)
        return sult

    def collect(self,response, kwargs=''):
        """
        全局变量响应指定路径取值
        :param response: 请求响应
        :param args: 寻找深度,支持格式str,list,tuple,dict(value)
        :return:
        """
        if not isinstance(kwargs, tuple):
            if isinstance(kwargs, list):
                kwargs = tuple(kwargs)
            if isinstance(kwargs, str):
                if kwargs == '':
                    return response
                else:
                    kwargs = tuple([kwargs])
            if isinstance(kwargs, dict):
                kwargs = kwargs.values()
        # if not isinstance(response, dict):
        #     response = json.loads(response)
        for key in kwargs:
            try:
                response = response[key]
            except KeyError:
                return response
        return response


    def set_global(self, response, args):
        global_args = {"key":"headers","values":"Set-Cookie","variable":"Cookie"}
        if args != '':
            for data in eval(args):

                if data['key'].lower() == 'headers':
                    global_args[data['variable']] = self.collect(response.headers, data['values'])
                elif data['key'].lower() in ['body', 'json']:
                    global_args[data['variable']] = self.collect(response.json(), data['values'])
                else:
                    raise ValueError("暂时不支持此参数")
        return [global_args]
    #把两个
    # def dic_excle(self):
    #     datas = []
    #     for data in range(1, self.getRows()):
    #         title = ['no', 'skip', 'caseno', 'module', 'method', 'url', 'headers', 'payload', 'expected_result',
    #                  'set_global']
    #         datas.append(dict(zip(title, self.getExcelDates())))
    #     return datas
    #





if __name__ == '__main__':
    model = '[{{http}}{{url}}{{login_path}}..{{url}}]'
    words_dict = {
        "http": "http://",
        "url": "www.baidu.com",
        "login_path": "kw=132"
    }
    #

    obj = OperationExcel()
    # headers = obj.case_prev("")[ExcelValues.caseJson]
    # login = obj.case_prev('loge')[ExcelValues.caseId]
    # case = obj.case_list()
    # res = obj.get_gloabal(model, **words_dict)
    # print(case)
    # print(type(json.loads(headers)))
    # obj = OperationExcel()
    # res = obj.getExcelDates(1)
    key_name ='[{"key":"headers","values":"Set-Cookie","variable":"Cookie"}]'
    json = {"key":"headers","values":"Set-Cookieewqrqewrqw","variable":"Cookieqwerqwerq"}
    res =obj.set_global(json,key_name)
    print(res)