'''
1.flask是一个轻量级后端框架
    1.flask路由:用来匹配url
    2.requests对象 abort函数
    3.模板
    4.flask数据库
    5.表单
    6.ajax
    7.管理系统小案例
2.官网地址:https://dormousehole.readthedocs.io/en/latest/(学习地址)
3.下载方式:pip install flask (-i https://pypi.douban.com)--> 换源地址
'''

# 导flask包
from flask import Flask

app = Flask(__name__)  # 实例化对象其中__name__必传


@app.route("/")  # 使用魔法方法
def index():  # 方法
    return "<h1>hello world</h1>"


app.run()
