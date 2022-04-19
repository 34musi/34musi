from flask import Flask, render_template, request
from wtforms import StringField, PasswordField, SubmitField  # 类型
from wtforms.validators import DataRequired, EqualTo  # 验证数据不能为空,验证数据是否相同
from flask_wtf import FlaskForm

app = Flask(__name__)

app.config['SECRET_KEY'] = 'ASGDFDFFG'


# 定义表单模板类
class Register(FlaskForm):
    user_name = StringField(label='用户名', validators=[DataRequired('用户名不能为空')])
    pass_word = PasswordField(label='密码', validators=[DataRequired('密码不能为空')])
    pass_word1 = PasswordField(label='再次提交密码', validators=[DataRequired('重复输入密码不能为空')]), EqualTo('pass_word')
    submit = SubmitField(label='提交')


@app.route('/register', methods=['GET', 'POST'])
def register():
    form = Register()
    if request.method == "GET":
        return render_template('register.html', form=form)
    if request.method == 'POST':
        username = form.user_name.data
        password = form.pass_word.data
        print(username)
        print(password)
        return render_template('register.html', form=form)


if __name__ == '__main__':
    app.run()
