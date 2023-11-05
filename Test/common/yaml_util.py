import yaml
from common.path_util import *



def read_yaml(file_path, one_noed=None, two_node=None):
    """读取yaml配置文件"""""
    with open(file_path, encoding="utf-8") as f:
        value = yaml.load(stream=f, Loader=yaml.FullLoader)
        if one_noed and two_node:
            return value[one_noed][two_node]
        elif one_noed:
            return value[one_noed]
        else:
            return value

#
# def read_extract_yaml(node_name):
#     """读取可配置存储文件"""
#     with open(extract_path(), encoding="utf_8") as f:
#         value = yaml.load(stream=f, Loader=yaml.FullLoader)
#         return value[node_name]


def write_extract_yaml(data):
    """写入配置文件"""
    with open(extract_path(), encoding="utf-8", mode="a") as f:
        yaml.dump(data, stream=f, allow_unicode=True)


def clean_extract_yaml():
    """清空配置文件"""
    with open(extract_path(), encoding="utf-8", mode="w") as f:
        f.truncate()


if __name__ == '__main__':
    print(read_yaml(yong_li_path(), "base", "xinghe_base"))
