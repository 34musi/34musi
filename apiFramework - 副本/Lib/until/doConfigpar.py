#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author: yuless
@file: doConfigpar.py
@time: 2021/11/23
"""
#author: godyu


import configparser
from apiFramework.Common.public import filePath
import json
class operation:

    conUrl = 'url'
    conJson = 'json'
    conMethod = 'method'
    headers = 'headers'
    sql_host = 'host'
    sql_port = 'port'
    sql_username = 'user'
    sql_password = 'password'
    sql_database = 'database'

    def __init__(self,filepath,filename):

        self.filepath = filepath
        self.filename = filename

    def doIni(self,selection,field):

        fip = filePath(self.filepath, self.filename)
        cf = configparser.ConfigParser()
        cf.read(fip,encoding='utf-8')
        confield = cf.get(selection, field)
        return confield

if __name__ == '__main__':

    obj = operation('Config','sqlConnectionTemp.ini')
    res= obj.doIni('test_db',obj.sql_host)
    obj1 = operation('Config','Middle_station_system.ini')
    res1= obj1.doIni('Middle_system','headers')
    res1 =eval(res1)
    print(res1['Connection'])
