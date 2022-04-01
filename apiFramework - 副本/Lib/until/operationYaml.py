#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author: yuless
@file: operationYaml.py
@time: 2021/11/26
"""
import yaml
from apiFramework.Common.public import filePath


class OperationYaml:
    def readYaml(self,fileDir,fileName):
        with open(filePath(fileDir,fileName),'r',encoding='utf-8') as f:
            return list(yaml.safe_load_all(f))

    def dictYaml(self,fileDir='Config',fileName='book.yaml'):
        with open(filePath(fileDir=fileDir,fileName=fileName),'r',encoding='utf-8') as f:
            return yaml.safe_load(f)

    #列表嵌套  可以当做字典传参   也可以用pytest的装饰器传参
    def aloneReadYaml(self,fileDir,fileName,casename):
        with open(filePath(fileDir, fileName), 'r', encoding='utf-8') as f:
             list_casename=[]
             casename=list(yaml.safe_load_all(f))[0][casename]
             list_casename.append(casename)
             return list_casename



if __name__ == '__main__':
    obj1 = OperationYaml()
    tes=obj1.aloneReadYaml('Data', 'test_gm_management_platform.yaml', 'test_gm_manager_change_status')[0]
    print(tes)

