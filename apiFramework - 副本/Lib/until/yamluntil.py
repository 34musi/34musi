#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      yamluntil.py
@time:      2022/3/23 10:51
"""
import requests
import yaml
import cchardet
from retrying import retry
from apiFramework.Common.public import filePath
from apiFramework.Log.log_new import Logging
from requests import request, RequestException
logger = Logging("test.py")
from ruamel import yaml



class YamlLoader:

    def __init__(self, file):
        self.file = file

    def file_load(self):
        with open(self.file, 'r', encoding='utf-8') as f:
            data = f.read()
        return yaml.load(data, Loader=yaml.RoundTripLoader)


    def file_dump(self,case,url,method,des,json):

        """
        :param case: 用例名称
        :param url: 地址
        :param des: 模块描述
        :param json: 请求参数
        :return:
        """
        data = {case: {'url': '{}', 'method': '{}}','des': '{}}','data': '{}'}}
        data[case]['url']=url
        data[case]['method']=method
        data[case]['des']=des
        data[case]['data']=json
        #生成测试用例方法，向yaml文件里追加测点
        with open(filePath('Data', self.file) , 'a',encoding='utf-8') as f:
            f.write('\n')
            yaml.dump(data, f,Dumper=yaml.RoundTripDumper,allow_unicode=True)


def yamlUntil():

    env = {'test_gm_manager_change_status':{'url': '/api/gm_manager/activity/change_status','method': 'PUT' ,'des': '{中台系统} - {GM管理平台} - {游戏活动管理} - {删除活动}', 'data': {'ids': [127], 'is_delete': True, 'category': 3}}}
    with open('middleEnv.yaml','w',encoding='utf-8') as f:
        yaml.safe_dump(data=env,stream=f,allow_unicode=True)


def post(url, **kwargs):
    """封装post方法"""
    # 获取请求参数
    params = kwargs.get("params")
    data = kwargs.get("data")
    json = kwargs.get("json")
    try:
        result = requests.post(url, params=params, data=data, json=json)
        return result
    except Exception as e:
        print("post请求错误: %s" % e)

@retry(stop_max_attempt_number=3, retry_on_result=lambda x: x is None, wait_fixed=2000)
def downloader(url, method=None, header=None, timeout=None, binary=False, **kwargs):
    logger.info(f'Scraping {url}')
    _header = {'User-Agent': ""}
    _maxTimeout = timeout if timeout else 5
    _headers = header if header else _header
    _method = "GET" if not method else method
    try:
        response = request(method=_method, url=url, headers=_headers, **kwargs)
        encoding = cchardet.detect(response.content)['encoding']
        if response.status_code == 200:
            return response.content if binary else response.content.decode(encoding)
        elif 200 < response.status_code < 400:
            logger.info(f"Redirect_URL: {response.url}")
        logger.error('Get invalid status code %s while scraping %s', response.status_code, url)
    except RequestException as e:
        logger.error(f'Error occurred while scraping {url}, Msg: {e}', exc_info=True)

if __name__ == '__main__':

    case = 'test_gm_manager_gift_group_add'
    url = "/api/gm_manager/gift_group"
    des = '新增'
    json = {
    "items": [
        {
            "start_time": 1648166400,
            "end_time": 1648339199,
            "server_ids": [
                "4"
            ],
            "refresh": 2,
            "popup": 0,
            "conf_id": 29003
        }
    ]
}
    method = 'post'
    yml = YamlLoader('test_gm_management_platform.yaml')
    yml.file_dump(case,url,method,des,json)

