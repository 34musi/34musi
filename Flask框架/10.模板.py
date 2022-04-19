"""
通过模板将后端的数据返回到前端页面
"""
from flask import Flask, render_template

app = Flask(__name__)


@app.route('/template')
def index():
    data = {
        'name': '李四',
        'age': 18,
        'list': [1, 2, 3, 4, 5, 6, 7, 8, 9]
    }
    return render_template('index2.html', data=data)


def list_step(li):
    """自定义过滤器"""
    return li[::2]


# 注册过滤器
app.add_template_filter(list_step, 'list')


if __name__ == '__main__':
    app.run(debug=True)
