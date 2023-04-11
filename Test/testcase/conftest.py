import pytest


def read_yaml():
    return ["小李", "小王", "小红"]


@pytest.fixture(scope="class", autouse=True)  # class类 function模块 model模块
def execute_sql():
    print("数据库校验")
    yield
    print("校验完毕,关闭数据库")
