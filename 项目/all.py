import pytest

if __name__ == '__main__':
    # pytest.main(['-vs'])   # 全部运行
    # pytest.main(['./testcase/test_测试01.py', '-vsx', '--reruns=2'])   # 指定test_测试02文件运行
    # pytest.main(['-vs', 'test_interface_testcase'])   # 指定文件运行
    # pytest.main(['-vs', './test_interface_testcase/test_interface.py::test_04']) # 使用nodeid来进行运行方法
    # pytest.main(['-vs', './test_interface_testcase/test_interface.py::TestLike::test_03']) # 使用nodeid来进行运行类中的方法
    # pytest.main(['-vs', './testcase', '-n=2'])
