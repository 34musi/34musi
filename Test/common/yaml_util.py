import yaml
from common.path_util import *


class File_Method:
    """读取yaml配置文件"""""

    def read_yaml(self, file_path, one_node=None, two_node=None):
        with open(file_path, mode="r", encoding="utf-8") as f:
            value = yaml.load(stream=f, Loader=yaml.FullLoader)
            if one_node and two_node:
                return value[one_node][two_node]
            elif one_node or two_node:
                return value[one_node]
            else:
                return value

    #
    # def read_extract_yaml(node_name):
    #     """读取可配置存储文件"""
    #     with open(extract_path(), encoding="utf_8") as f:
    #         value = yaml.load(stream=f, Loader=yaml.FullLoader)
    #         return value[node_name]

    def write_extract_yaml(self, file_path, data):
        """写入配置文件"""
        with open(file_path, encoding="utf-8", mode="a") as f:
            yaml.dump(data, stream=f, allow_unicode=True)

    def clean_extract_yaml(self, file_path):
        """清空配置文件"""
        with open(file_path, encoding="utf-8", mode="w") as f:
            f.truncate()


rf = File_Method()

if __name__ == '__main__':
    print(rf.read_yaml(examine_path_url(), "audit_policy", "examine_url"))