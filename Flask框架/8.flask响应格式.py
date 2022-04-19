from flask import Flask, make_response, json, jsonify

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # 关闭json中加密的形式,主要返回给前端展示的信息


@app.route('/response')
def index():
    data = {
        'name': '张三',
        'sex': '男',
        'age': '18'
    }
    # return make_response(json.dumps(data, ensure_ascii=False))  # 这样在前端返回的是文本格式,我们需要转成json
    # response = make_response(json.dumps(data, ensure_ascii=False))  # 将文本类型的赋值给一个变量
    # response.mimetype = 'application/json'  # 将变量转成json格式的方法
    # return response  # 返回转成json格式的变量
    return jsonify(data)  # 直接转成json数据格式,但在前端展示是加密形式的,我们需要关闭ascii才能显示我们能读懂的值


if __name__ == '__main__':
    app.run()
