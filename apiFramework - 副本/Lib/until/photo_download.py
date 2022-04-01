# -*- coding: utf-8 -*-
# @Time    : 2021/8/19 15:34
# @Author  : yuless
# @File    : photo_download.py
# @Software: PyCharm
# 获取图片url连接
# 百度批量下载图片
from multiprocessing import Pool,Process
from threading import Thread
from PIL import Image
import requests, time, re, os, json
from faker import Faker


class Baidu_batch_downloads_photo:
    def __init__(self):
        name = input('请输入你要下载的关键词：')
        pn = input('你想下载前几页（1页有60张）:')
        self.get_parse_page(pn, name)
        #self.img_Clipping(r'.\清纯\0-0.jpg')

    def get_parse_page(self, pn, name):
        pool = Pool(8)
        process_list=[]
        for i in range(int(pn)):
            # 1.获取网页
            print('正在获取第{}页'.format(i + 1))
            # 百度图片首页的url
            # name是你要搜索的关键词
            # pn是你想下载的页数
            url = 'https://image.baidu.com/search/flip?tn=baiduimage&ie=utf-8&word=%s&pn=%d' % (name, i * 20)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.104 Safari/537.36 Core/1.53.4843.400 QQBrowser/9.7.13021.400'}
            # 发送请求，获取相应
            response = requests.get(url, headers=headers)
            html = response.content.decode()
            # print(html)

            # 2.正则表达式解析网页
            # "objURL":"http://n.sinaimg.cn/sports/transform/20170406/dHEk-fycxmks5842687.jpg"
            results = re.findall('"objURL":"(.*?)",', html)  # 返回一个列表
            # 根据获取到的图片链接，把图片保存到本地
            pool.apply_async(self.save_to_txt, (results, name, i))
        pool.close()
        pool.join()

    def save_to_txt(self, results, name, i):
        j = 0
        # 在当目录下创建文件夹
        if not os.path.exists('./' + name):
            os.makedirs('./' + name)
        # 下载图片
        for result in results:
            print('正在保存第{}个'.format(j))
            try:
                pic = requests.get(result, timeout=10)
                # time.sleep(1)
            except:
                print('当前图片无法下载')
                j += 1
            # 把图片保存到文件夹
            file_full_name = './' + name + '/' + str(i) + '-' + str(j) + '.jpg'
            with open(file_full_name, 'wb') as f:
                f.write(pic.content)
            j += 1
            self.img_Clipping(file_full_name)


    def img_Clipping(self, path):
        # 图片裁剪
        img = Image.open(path)
        width,height=img.size
        cropped = img.crop((0,0 ,width , width+width*0.7))  # (left, upper, right, lower;左、上、右、下)
        cropped.save(path)


if __name__ == '__main__':
    Baidu_batch_downloads_photo()
