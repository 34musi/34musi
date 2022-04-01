#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:    yuless
@file:      runall.py
@time:      2022/3/21 19:37
"""
import os
import allure
import pytest



if __name__ == '__main__':
    # test_SearchSystemReviewsListV2()

    pytest.main(["-s","-v"])

    pytest.main(['--alluredir', './temp'])
    os.system('allure generate ./temp -o ./report --clean')