# -*- coding: utf-8 -*-
"""
Created on Tue Dec  8 18:10:15 2020

@author: Daniel
"""

from lmfit import Model
import numpy as np

class RadialCutoffModel(Model):
    """径向截断（radial cutoff）拟合模型：描述势深截断下的存活率。

    基于 lmfit.Model，包装 :func:`radial_cutoff`，预置 temp 与 p_sat 参数提示。
    """
    def __init__(self, *args, **kwargs):
        """构造模型。

        Args:
            *args: 传给 lmfit.Model 的位置参数。
            **kwargs: 传给 lmfit.Model 的关键字参数。
        """
        super().__init__(radial_cutoff, *args, **kwargs)
        self._set_paramhints_prefix()
        
    def _set_paramhints_prefix(self):
        """设置拟合参数的初值/是否变化（temp 可变、p_sat 固定为 1）。"""
        self.set_param_hint('temp', value=0.0015, vary = True),
        self.set_param_hint('p_sat', value=1, vary = False)
        
def radial_cutoff(depth, p_sat, temp):
    """径向截断存活率模型函数。

    Args:
        depth: 势深。
        p_sat: 饱和系数。
        temp: 温度。

    Returns:
        存活率：p_sat·(1-exp(-2·depth/temp) - 2·depth/temp·exp(-depth/temp))。
    """
    return p_sat*(1-np.exp(-2*depth/temp)
                  -2*depth/temp*np.exp(-depth/temp))

class TrapProbMultions(Model):
    """多离子阱概率模型：用于计算多离子情形下的占据/存活概率（占位待实现）。"""
    ### To be implemented
    pass

