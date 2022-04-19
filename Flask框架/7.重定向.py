"""
redirect 是利用重定向
url_for 指定的路由
"""
from flask import Flask, redirect, url_for

app = Flask(__name__)


@app.route('/index')
def index():
    return redirect(url_for("anzi"))


@app.route('/hanzi')
def anzi():
    return "这是汉字的拼音:hanzi"


if __name__ == '__main__':
    app.run()
