import os


def get_jd_path():
    return os.getcwd().split("common")[0]


def gen_path():
    """这个是根目录"""
    Dir = os.path.dirname(os.path.dirname(__file__))
    return Dir


# 审核策略地址
def examine_path_url():
    examine = gen_path() + "/Data/data_ai_url.yaml"
    return examine


def examine_path_data():
    """获取审核策略查询数据配置"""
    PeiZ = gen_path() + "/Data/data_ai_examine.yaml"
    return PeiZ


if __name__ == '__main__':
    print(examine_path_url())