# -*- coding: utf-8 -*-
"""
Created on Wed Dec  9 15:11:43 2020

@author: Daniel
"""

from scipy import interpolate
import glob
import os
import pandas as pd

class Calib():
    """Verdi 激光功率标定：由标定文件建立 DAC → 功率(W) 插值。

    从 verdi_cals 目录加载指定日期的标定表，构造二次插值函数。

    Args:
        date (str): 标定日期标识，或 'newest' 取最新文件。
    """
    
    def __init__(self, date):
        """加载标定表并建立 DAC → 功率插值器。

        Args:
            date (str): 标定日期或 'newest'。
        """
        self.data = self.load_calib(date)
        self.cal = interpolate.interp1d(self.data['DAC1'],
                                        self.data['Power(mW)']/1000,
                                        kind ='quadratic')
    

    def load_calib(self, date):
        """从 verdi_cals 目录读取指定日期的标定数据表。

        Args:
            date (str): 标定日期或 'newest'（取创建时间最新文件）。

        Returns:
            pandas.DataFrame: 含 DAC1/Power(mW) 等列的标定表。
        """
        directory = os.path.dirname(os.path.abspath(__file__))
        print(glob.glob(os.path.join(directory, '*')))
        if date == 'newest':
            path = max(glob.iglob(os.path.join(directory, 'verdi_cals', '*.txt')),
                       key=os.path.getctime)

        else:
            try:
                path = glob.glob(os.path.join(directory, 'verdi_cals',
                                              f'*{date}.txt'))[0]
            except IndexError:
                raise IndexError('No Calibration found')

        cal_data = pd.read_csv(path, sep='\t')

        return cal_data
    
    def dac2power(self,power):
        """由 DAC 值经标定插值得到功率（W）。

        Args:
            power: DAC 值。

        Returns:
            float: 对应功率（W）。
        """
        return self.cal(power)