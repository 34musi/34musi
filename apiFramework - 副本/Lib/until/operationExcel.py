#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
@author: yuless
@file: operationExcel.py
@time: 2021/11/26
"""
import xlrd
from apiFramework.Lib.until.operationYaml import OperationYaml
# from apiFramework.Common.public import *

from apiFramework.Common.public import filePath


class ExcelValues:
    caseId= 0
    des = 1
    url = 2
    method= 3
    data = 4
    expect = 5

    @property
    def getcaseID(self):
        return self.caseId
    @property
    def getdes(self):
        return self.des
    @property
    def geturl(self):
        return self.url

    @property
    def getmethod(self):
        return self.method

    @property
    def getdata(self):
        return self.data

    @property
    def getexpect(self):
        return self.expect


class OperationExcel:

    def getSheet(self):
        book = xlrd.open_workbook_xls(filePath('Data','temp.xls'))
        return book.sheet_by_index(0)
    # 获取总行数
    @property
    def getRows(self):
        return self.getSheet().nrows
    #获取总列数
    @property
    def getCols(self):
        return self.getSheet().ncols

    def getValue(self,row,col):
        return self.getSheet().cell_value(row,col)

    def getcaseId(self,row):
        return self.getValue(row=row,col=ExcelValues().caseId)

    def getdes(self,row):
        return self.getValue(row=row,col=ExcelValues().getdes)

    def geturl(self,row):
        url = self.getValue(row=row,col=ExcelValues().geturl)
        if '{bookID}' in url:
            url = str(url).replace('{bookID}','')
            return url
        else:
            return url
    def getmethod(self,row):
        return self.getValue(row=row,col=ExcelValues().getmethod)

    def getdata(self,row):
        return self.getValue(row=row,col=ExcelValues().getdata)

    def getexpect(self,row):
        return self.getValue(row=row,col=ExcelValues().getexpect)

    def getExcleDate(self,row):
        return OperationYaml().dictYaml()[self.getcaseId(row)]
    #获取所有数据
    def getExcelDates(self):
        # for item in self.getRows:
        datas= list()
        title = self.getSheet().row_values(0)
        for row in range(1,self.getSheet().nrows):
            row_value  = self.getSheet().row_values(row)
            datas.append((dict(zip(title,row_value))))
        return datas


if __name__ == '__main__':
    obj = OperationExcel()
    res = obj.getExcelDates()
    for item in res:
        print(item)
