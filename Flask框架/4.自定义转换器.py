"""
<>: 提取器
<int:id>:其中冒号左边的int就是转换器
"""

# 需要导自定义转换器的包
from werkzeug.routing import BaseConverter
# 导flask的包
from flask import Flask

app = Flask(__name__)  # 实例化对象


class RegexConverter(BaseConverter):  # 自定义转换器需要继承BaseConverter
    """ 自定义转换器类 """

    def __init__(self, url_map, regex):
        # 要继承父类的方法
        super(RegexConverter, self).__init__(url_map)
        self.regex = regex  # 初始化对象

    def to_python(self, value):
        print("to_python方法被调用")
        return value


# 最后将自定义转换器添加到flask应用中
app.url_map.converters['re'] = RegexConverter


@app.route('/hello/<re("1\d{5}"):value>')
def hello(value):
    print(value)
    return "hello world "


if __name__ == '__main__':
    app.run()
