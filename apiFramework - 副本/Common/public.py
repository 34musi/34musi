#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author: yuless
@file: public.py
@time: 2021/11/26
"""

import os
import time
import datetime
from faker import Faker


def filePath(fileDir, fileName):
    # param: fileDir:目录
    # param: fileName:文件名称
    filepath = os.path.join(os.path.dirname(os.path.dirname(__file__)), fileDir, fileName)
    return filepath


# 写文件内容
def writeContent(content):
    with open(filePath(fileDir="Data/echo_data", fileName='code'), 'w') as f:
        f.write(str(content))


# 读文件内容
def readContent():
    with open(filePath(fileDir="Data", fileName='code'), 'r') as f:
        return f.read()


def get_cutten_timestr(flag=True):
    """
    获取当前时间字符串
    @return:
    时间格式： 2021-12-14
    """
    if flag:
        return datetime.datetime.now().strftime('%Y-%m-%d')
    return datetime.datetime.now().strftime('%Y-%m-%d')


# 获取当前时间  格式 2022-01-01
def parse_timestr_to_timestamp(time_str, flag=True):
    """
    把时间字符串转换为时间戳格式
    :param time_str: 时间字符串,格式为：2022-01-01 12:12:12 或 2022-01-01
    :param flag: 标志位，决定输入时间字符串的格式
    :return: 时间戳格式
    """
    if flag:
        struct_time = time.strptime(time_str, "%Y-%m-%d")  # 2019-01-01
        return time.mktime(struct_time)


def tiem_ago(n):
    """
    n：时间天数   int
    获取几天前的时间
    """
    threeDayAgo = (datetime.datetime.now() - datetime.timedelta(n))
    # 转换为时间戳:
    timeAgo = int(time.mktime(threeDayAgo.timetuple()))
    # 转换为其他字符串格式:
    otherStyleTime = threeDayAgo.strftime("%Y-%m-%d %H:%M:%S")
    return timeAgo


def tiem_future(n):
    """
    n：时间天数  int
    获取几天前的时间
    """
    threeDayAgo = (datetime.datetime.now() + datetime.timedelta(n))
    # 转换为时间戳:
    timeAgo = int(time.mktime(threeDayAgo.timetuple()))
    # 转换为其他字符串格式:
    otherStyleTime = threeDayAgo.strftime("%Y-%m-%d %H:%M:%S")
    return timeAgo


def time_now():
    timeNow = int(round(time.time()))
    return timeNow


# 生成随机姓名
def random_name():
    fake = Faker('zh_CN')
    name = fake.name()  # 随机生成姓名
    return name


def parse_to_timestamp(timeStr):
    timeArray = time.strptime(timeStr, '%Y-%m-%d %H:%M:%S')  # 按照对应的格式转换为时间数组time.strptime()
    timeStamp = int(time.mktime(timeArray))  # 转换成整形时间戳 time.mktime()
    return timeStamp


if __name__ == '__main__':
    # filePt = filePath('Data\id','fan.png')
    # con  = parse_timestr_to_timestamp('2019-01-01')
    # print(filePt)
    filePt = filePath('Data/id', 'fan.png')
    # generate_timestamp()
    print(filePt)
