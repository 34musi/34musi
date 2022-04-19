import pytest, os


class TestLogin(object):
    name = "棉袄"
    age = 18
    Report = os.path.dirname(__file__)
    report = os.path.join(Report, 'report')

    @pytest.mark.run(order=2)
    def test_04_huahua(self):
        print("花花喜欢的小动物是狗")

    @pytest.mark.run(order=1)
    @pytest.mark.skip(reason="慕斯的小猫现在不在身边")  # reason:必须写跳过的原因
    def test_02_musi(self):
        print("慕斯喜欢的小动物是猫")

    @pytest.mark.run(order=4)
    @pytest.mark.smoke
    @pytest.mark.skipif(name="夹克", reason="他的名字不叫棉袄是夹克")
    def test_01_canyue(self):
        print("残月喜欢的小动物是小鸟")
        assert 1 == 1  # 断言

    @pytest.mark.run(order=3)
    @pytest.mark.usermanger
    def test_03_xiaoyue(self):
        print("小月喜欢的小动物是猴子")

    @pytest.mark.run(order=5)
    @pytest.mark.skipif(age >= 18, reason="小明年龄16岁")
    def test_05_xiaoming(self):
        print("小明今年16岁了")


if __name__ == '__main__':
    tl = TestLogin()
    print(tl.report)

    # pytest.main(['-vs', '--html', 'report/report.html'])
