# -*- coding: utf-8 -*-
# @Time    : 2021/8/18 15:42
# @Author  : Dell
# @File    : getmassage.py
# @Software: PyCharm
# -*- coding:utf-8 -*-
# __author__ == 'chenmingle'
import requests
import websocket
import time
import threading
import json
import multiprocessing
import uuid
from threadpool import ThreadPool, makeRequests

# 修改成自己的websocket地址
WS_URL = "wss://im.service.ovivas.cn/web?Token="
# 定义进程数
processes = 1
# 定义线程数（每个文件可能限制1024个，可以修改fs.file等参数）
thread_num = 700
index = 1


def on_message(ws, message):  # 服务器有数据更新时，主动推送过来的数据
    print(message)



def on_error(ws, error):
    print(error)


def on_close(ws, code, masg):
    print("### closed ###")


def on_open(ws):
    global index
    index = index + 1

    def send_thread():
        # 设置你websocket的内容
        # 每隔10秒发送一下数据使链接不中断
        while True:
            ws.send(u'hello服务器')
            time.sleep(10)

    t = threading.Thread(target=send_thread)
    t.start()


def on_start(token):
    # time.sleep(5)
    websocket.enableTrace(True)
    ws = websocket.WebSocketApp(WS_URL + token,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.on_open = on_open
    ws.run_forever(ping_interval=20, ping_timeout=10)


def thread_web_socket():
    # 线程池
    pool_list = ThreadPool(thread_num)
    token = list()
    # 设置开启线程的数量
    for username in range(1, 1):
        tk = fs_login('YUTEST01', '12345a')
        print(tk)
        token.append(tk)

    requests = makeRequests(on_start, token)
    [pool_list.putRequest(req) for req in requests]
    pool_list.wait()


def fs_login(username, password):
    print(username)
    url = "https://im.api.ovivas.cn/rest/v1/im/login"
    payload = f"{{\"loginAccount\":\"{username}\",\"password\":\"{password}\"}}"
    headers = {
        'authority': 'im.api.ovivas.cn',
        'pragma': 'no-cache',
        'cache-control': 'no-cache',
        'sec-ch-ua': '" Not;A Brand";v="99", "Google Chrome";v="97", "Chromium";v="97"',
        'accept': 'text/plain',
        'appId': '61e54035583612881c9983e3',
        'content-type': 'application/json',
        'sec-ch-ua-mobile': '?0',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36',
        'sec-ch-ua-platform': '"Windows"',
        'origin': 'https://im.api.ovivas.cn',
        'sec-fetch-site': 'same-origin',
        'sec-fetch-mode': 'cors',
        'sec-fetch-dest': 'empty',
        'referer': 'https://im.api.ovivas.cn/swagger/index.html?urls.primaryName=login',
        'accept-language': 'zh-CN,zh;q=0.9',
        'authType': '1'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    RE = json.loads(response.text)

    return  RE['data']['access_Token']


if __name__ == "__main__":

    # 进程池
    pool = multiprocessing.Pool(processes=processes)
    # 设置开启进程的数量
    for i in range(processes):
        pool.apply_async(thread_web_socket)
    pool.close()
    pool.join()
    re = fs_login('YUTEST01','12345a')
    on_start(re)
    print(re)