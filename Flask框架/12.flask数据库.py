from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)


class Config():
    """ 参数配置 """
    SQLALCHEMY_DATABASE_URI = 'mysql://root:root@127.0.0.1:3306/flaskdb'
    SQLALCHEMY_TRACK_MODIFICATIONS = True


app.config.from_object(Config)

# SQLAlchemy要继承app
db = SQLAlchemy(app)


# 创建数据库模型类
class Role(db.Model):
    """ 角色表 """
    __tablename__ = 'role'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True)


class User(db.Model):
    """ 用户表 """
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True)
    password = db.Column(db.String(128))
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))  # ForeignKey是用来关联到另外一张表+


if __name__ == '__main__':
    db.create_all()  # 创建所有的表
    r = Role(name='admin')  # 创建对象 插入数据
    db.session.add(r)  # 记录到对象任务中
    db.session.commit()  # 提交任务
    r1 = Role(name='admin1')
    db.session.add(r1)
    db.session.commit()
    user1 = User(name='zhangsan', password='123', role_id=r.id)
    user2 = User(name='lisi', password='456', role_id=r.id)
    user3 = User(name='wangwu', password='456', rold_id=r1.id)  # 关联第二个角色
    db.session.add_all([user1, user2, user3])
    db.session.commit()
