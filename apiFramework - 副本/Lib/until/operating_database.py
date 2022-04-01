#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:yuless
@file:operating_database.py
@time:2022/01/06
"""


import pymysql
from apiFramework.Lib.until.doConfigpar import operation

class HandleMysql:

    def __init__(self,host,port,user,password,datebase):

        self.host= host
        self.port= int(port)
        self.user= user
        self.password= password
        self.datebase= datebase

        """初始化方法中，连接到数据库"""

        self.con = pymysql.connect(host = host,
                                   port = self.port,
                                   user = user,
                                   password = password,
                                   charset = "utf8",
                                   database = datebase)
        # 创建一个游标对象
        self.cur = self.con.cursor()
    def find_all(self, sql):
        """
        查询sql语句返回的所有数据
        :param sql: 查询的sql
        :return: 查询到的所有数据
        """
        self.cur.execute(sql)
        self.cur.close()
        return self.cur.fetchall()

    def find_one(self, sql):
        """
        查询sql语句返回的第一条数据
        :param sql: 查询的sql
        :type sql:str
        :return: sql语句查询到的第一条数据
        """
        self.cur.execute(sql)
        return self.cur.fetchone()

    def update(self, sql):
        """
        增删改操作的方法
        :param sql: 增删改的sql语句
        :return:
        """
        self.cur.execute(sql)
        self.con.commit()



if __name__ == '__main__':

    sdatabase_config = operation('Config', 'sqlConnectionTemp.ini')
    host = sdatabase_config.doIni('test_db', sdatabase_config.sql_host)
    port = sdatabase_config.doIni('test_db', sdatabase_config.sql_port)
    user = sdatabase_config.doIni('test_db', sdatabase_config.sql_username)
    password = sdatabase_config.doIni('test_db', sdatabase_config.sql_password)
    datebase = sdatabase_config.doIni('test_db', sdatabase_config.sql_database)
    obj = HandleMysql(host,port,user,password,datebase)
    res=obj.find_all('SELECT * from student.runoob_tbl where runoob_id =1')
    print(res)


