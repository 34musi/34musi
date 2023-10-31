import requests


class TestRequest():
    # def __init__(self):
    #     self.url = "https://www.wanandroid.com/user/login"
    #     self.params = {"username": 60,
    #                    "password": 123456}
    #     self.res = requests.post(self.url, self.params)

    def test_request(self):
        url = "https://www.wanandroid.com/user/login"
        params = {"username": "小李子呀",
                       "password": 123456}
        res = requests.post(url, params)
        Json = res.json()
        # TestRequest
        nickname = Json["data"]["nickname"]
        return nickname

        # print(res.text)
        # print(res.content)
        # print(res.json())
        # print(res.status_code)
        # print(res.headers)
        # requests.get()
        # requests.post()
        # requests.delete()
        # requests.patch()
        # requests.session()
        # requests.put()

    def test_post(self):
        url = "https://www.wanandroid.com/user/login"
        data = {
            "username" :self.test_request(),
            "password": 123456
        }
        res = requests.post(url=url, json=data)
        print(res.json()
              )
        print(res.request.headers)

if __name__ == '__main__':
    test = TestRequest()
    print(test.test_post())
