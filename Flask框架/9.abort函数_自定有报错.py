"""
1.raise:主动抛出代码异常
2.abort:主动抛出网页异常
"""
from flask import Flask, request, render_template, abort, jsonify

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


@app.route('/abort', methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template('index.html')
    elif request.method == "POST":
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'musi' and password == '123456':
            print(username, password)
            return 'login success'
        else:
            abort(404)
            return None


# 自定义处理方法

@app.errorhandler(404)
def handle_404_error(err):
    return render_template('404报错.html')


if __name__ == '__main__':
    app.run()
