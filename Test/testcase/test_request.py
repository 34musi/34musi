from utils.requests_util import RequestUtil
import allure


@allure.epic()
class TestWanZhuan:

    def test_shouye(self):
        data = {
            "superChapterId": 153,
            "superChapterName": "framework"
        }
        res = RequestUtil().method_request("get", "https://www.wanandroid.com/article/list/1/json", json=data)
        return res.json()

    def test_shouye_banner(self):
        res = RequestUtil().method_request("get", "https://www.wanandroid.com/banner/json")
        return res.text
