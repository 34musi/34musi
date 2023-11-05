import os


def get_jd_path():
    return os.getcwd().split("common")[0]


def gen_path():
    """这个是根目录"""
    Dir = os.path.dirname(os.path.dirname(__file__))
    return Dir


def yong_li_path():
    yongli = gen_path() + "\get_yaml.yaml"
    return yongli


def extract_path():
    """获取配置路径"""
    PeiZ = gen_path() + "/extract.yaml"
    return PeiZ


if __name__ == '__main__':
    print(extract_path())
