#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:yuless
@file:log.py
@time:2022/3/18
"""
import logging


class Log:

    def __init__(self):
        self.loggor = logging.getLogger('yulessloging')
        self.loggor.setLevel(logging.INFO)

    # 定义控制台处理器
    def consoleHandler(self, leval='INFO'):
        consolehandler = logging.StreamHandler()
        consolehandler.setLevel(leval)
        consolehandler.setFormatter(self.consformat())
        return consolehandler

    def fileconsolHanler(self, filename, level):
        fileHandler = logging.FileHandler(filename, mode='a', encoding='utf-8')
        fileHandler.setLevel(level=level)
        fileHandler.setFormatter(self.consformat())
        return fileHandler

    def consformat(self):
        mesage = logging.Formatter('%(levelname)s---%(asctime)s---%(message)s', datefmt=' %Y-%m-%d %H:%M:%S')
        return mesage

    def get_logger(self):
        self.loggor.addHandler(self.consoleHandler())
        return self.loggor


if __name__ == '__main__':
    obj = Log()
    logger = obj.get_logger()
    logger.info('jokersimachao')
