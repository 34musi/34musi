import pytest
from common.requests_util import re
from common.enum_util import en
from common.yaml_util import *
from common.path_util import *


class TestWanJiaDj:
    headers = {
        "merchant-id": 4,
        "content - type": "application /json;charset = UTF-8",
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJFbWFpbCI6Im11c2lAc3RhcnVuaW9uZ2FtZS5jb20iLCJJcCI6IjE4My4yMjEuMTcuMTA4IiwiSXNTdXBlciI6ZmFsc2UsIk1lcmNoYW50SWQiOjQsIlVzZXJJZCI6NTQyLCJVc2VyTmFtZSI6IuaFleaWryIsImV4cCI6MTczNjg1OTg3OH0.2afKRAwDlv55dJDcTpTWTGWUVhUVdjXGNtPp-CXvbdo"

    }

    @pytest.mark.parametrize("data", rf.read_yaml(examine_path_data()))
    def test_token(self, data):
        re.send_requests(url=en.URL.value, method=en.POST.value, headers=self.headers, json=data)


if __name__ == '__main__':
    T = TestWanJiaDj()
