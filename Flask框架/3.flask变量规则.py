"""
string:接收任何不包含斜杠的文本
path:接收包含斜杠的文本
int:接收正整数
float:接收正浮点数
"""


from flask import Flask     # 导包

app = Flask(__name__)   # 实例化对象


@app.route('/hello/<id>', methods=['get', 'post'], endpoint='hello')
# /:根目录 methods:请求的方法 endpoint:端点 '/hello/<id>':这个没有做限制需要在规则中写成字符串
def index_hello(id):
    # 变量规则(对规则进行限制很麻烦)
    if id == '1':
        return 'python'
    elif id == '2':
        return 'django'
    elif id == '3':
        return 'flask'
    return '<h1>hello world</h1>'


@app.route('/hi/<int:id>', methods=['get', 'post'], endpoint='hi')
# /:根目录 methods:请求的方法 endpoint:端点 /hi/<int:id>:这个是直接对id进行限制,只能输入整数
def index_hi(id):
    if id == 1:
        return 'python'
    elif id == 2:
        return 'django'
    elif id == 3:
        return 'flask'
    return 'hi world'


@app.route('/nice/<name>')
def index(name):
    return f'hi {name}'


if __name__ == '__main__':
    app.run()
