# -*- coding: utf-8 -*-
# @Time    : 2021/12/14 12:30
# @Author  : yuless
# @File    : data_comparison.py
# @Software: PyCharm
import difflib
import json
import os

from apiFramework.Common.public import filePath
from apiFramework.Lib.until.SendMessage import send_message

agreement = 'https'
port = ''
backstage = 'dfuse2-console.ovivas.cn'
login_path = "/Account/Login"


def ip_builder(agreement, port, backstage, path):
    """
    IP组装
    :param agreement: 协议
    :param port:端口
    :param backstage:IP
    :param path:路径
    :return: 返回组装字符串
    """
    return agreement + '://' + backstage + port + path

def json_formatting(data):
    """
    json字符串格式化,只取keys
    :param text:
    :return:
    """
    def __builder_dict(data):
        for value in data.keys():
            data[value]= __builder_dict(data[value])if isinstance(data[value], dict) else __builder_list(data[value])if isinstance(data[value], list)else None
        return data

    def __builder_list(data):
        for value in range(len(data)):
            data[value]=__builder_dict(data[value]) if isinstance(data[value], dict)else __builder_list(data[value])if isinstance(data[value], list) else None
        return data

    return __builder_dict(data)if isinstance(data, dict)else __builder_list(data)if isinstance(data, list)else data


def formatToJsonStr(dataStr):
    """
    格式化json
    :param dataStr:
    :return:
    """
    if not isinstance(dataStr, str):
        dataStr = str(dataStr)
    newDataStr = dataStr.strip().replace("'", '"')
    dataJson = json.loads(newDataStr)
    return json.dumps(dataJson, indent=4, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def diff_text_html(actual_result, expected_result, savepath):
    """
    比对文本数据
    :param actual_result:
    :param expected_result:
    :param savepath:
    :return:
    """
    htmldiff = difflib.HtmlDiff()
    html = htmldiff.make_file(
        json.dumps(actual_result, indent=4, sort_keys=True, ensure_ascii=False, separators=(',', ':')).splitlines(),
        json.dumps(expected_result, indent=4, sort_keys=True, ensure_ascii=False, separators=(',', ':')).splitlines(),
        context=False,
        fromdesc='预期结果', todesc='实际结果')
    with open(savepath, 'w+b') as file:
        file.write(html.encode())
    return True if os.path.exists(savepath) else False

def witre_message():
    write_mes = send_message('Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuIjoiemF1dG8iLCJzIjoiUGFzc3dvcmQiLCJuaSI6IjYxZWUxMTgwMWYzMjQwNWM5ZGRiNjYwNyIsImFpIjoiNjFlNTQwMzU1ODM2MTI4ODFjOTk4M2UzIiwiaW1wIjoiMSIsIm5iZiI6MTY0Mjk5MjkxMiwiZXhwIjoxNjQ1NTg0OTEyLCJpc3MiOiJJTU1TIiwiYXVkIjoiSU1NUyJ9.Hubctta6xCmP280W2A05kmFAlipdGk_cCun46ROj960')
    with open(filePath('data','yuless_write.txt'),'w',encoding='utf-8')as f:
        f.write(json.dumps(write_mes))

def read_message():
    with open(filePath('data','yuless_read.txt'),'r',encoding='utf-8') as f:
        red_content =json.loads(f.read())
        return red_content

def read_message_wr():
    with open(filePath('data', 'yuless_write.txt'), 'r', encoding='utf-8') as fi:
        write_content = json.loads(fi.read())
        return write_content

if __name__ == '__main__':


    res = witre_message()
    # wr= read_message_wr()
    # read= read_message()
    # isok = diff_text_html(wr, read[-100:], './demo.html')
    # # jsonstr = json_formatting(wr)
    # # jsonstr_end = json_formatting(read)
    # print(isok)

