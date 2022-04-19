"""
methods: 请求的方法
    post
    get
app.rout:是一个静态方法后面跟参数才能访问匹配的地址
endpoint:这个是端点的意思
"""
# 导包
from flask import Flask

app = Flask(__name__)


# 当我的路由都一样是是否能访问?
# 可以访问只要端点正确
@app.route("/hello", methods=['get', 'post'], endpoint='hello')  # 地址后面需要加入hello才能访问,端点是:hello
def hello():
    return "hello world"


@app.route("/hi", methods=['get', 'post'], endpoint='hi')  # 地址后面需要加hi才能访问,端点:是hi
def hello():
    return "hi world"


if __name__ == '__main__':
    app.run()
