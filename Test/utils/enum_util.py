from enum import Enum, unique

from utils.yaml_util import rf


@unique
class EnumMethod(Enum):
    POST = "post"
    PUT = "put"
    DELETE = "delete"
    GET = "get"
    URL = rf.read_yaml(examine_path_url())


en = EnumMethod
