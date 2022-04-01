#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author:yuless
@file:log_new.py
@time:2022/03/22

"""

from colorlog import ColoredFormatter
import logging

LOG_LEVEL = logging.DEBUG
LOGFORMAT = "[%(asctime)s][%(name)s] [%(log_color)s**%(levelname)s**%(reset)s] [%(filename)s:%(funcName)s:%(log_color)s%(lineno)d%(reset)s] %(log_color)s%(message)s%(reset)s"
logging.root.setLevel(LOG_LEVEL)
formatter = ColoredFormatter(LOGFORMAT)
stream = logging.StreamHandler()
stream.setLevel(LOG_LEVEL)
stream.setFormatter(formatter)
log = logging.getLogger('logconfig')
log.setLevel(LOG_LEVEL)
log.addHandler(stream)

def Logging(name):

    log = logging.getLogger(name)
    log.setLevel(LOG_LEVEL)
    log.addHandler(stream)
    return log


if __name__ == '__main__':
    pass
    # logger = Logging("test")
    # logger.info(123123)
    # logger.warning(123123)
    # logger.debug(123123)