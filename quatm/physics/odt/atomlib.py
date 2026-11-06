"""Atomic data used by the optical-dipole-trap models."""

# some ion properties in SI
# clebsch gordan coefficients are squared

from scipy import constants as const

from decimal import *
from numpy.linalg import norm
import numpy as np
import struct

class Atom(object):
    """原子对象：以键值字典构造，保存元素的各项物理性质（SI 单位）。

    常用实例为模块底部定义的 Ba/Mg/Rb/Ca/Li（来自对应 *_Dict）。

    Attributes:
        name (str): 元素名。
        charge (float): 电荷（C）。
        mass (float): 质量（kg）。
        SP_frequencies/SP_decays/DP_frequencies/...: 光谱频率与衰变率等。
    """
    def __init__(self, **entries):
        """用键值对初始化原子属性。

        Args:
            **entries: 属性名 → 值（写入 self.__dict__）。
        """
        self.__dict__.update(entries)

BariumDict = {'name':'Barium',
      'charge': const.e, # why????? SOURCE NEEDED!!!!
      'mass': 137.905*const.physical_constants['unified atomic mass unit'][0], # literatur Wiki
      'SP_frequencies': [2*const.pi * const.lambda2nu(493.4e-9),2*const.pi * const.lambda2nu(455.4e-9)], # literatur NIST database
      'SP_decays': [2*const.pi*15.2e6 ,  2*const.pi*17.7e6], # literatur NIST database
      'SP_linpolcoupling':[1./3, 2./3],
      'DP_frequencies': [2*const.pi * const.lambda2nu(649.7e-9),2*const.pi *  const.lambda2nu(614.2e-9), 2*const.pi * const.lambda2nu(585.4e-9)], # literatur NIST database
      'DP_decays': [2*const.pi*4.9e6, 2*const.pi*6.6e6, 2*const.pi*0.95e6], # literatur NIST database
      'DP_linpolcoupling':[1,1,1],}

LithiumDict = {'name':'Lithium6',
      'charge': 0, 
      'mass': 6.0151214*const.physical_constants['unified atomic mass unit'][0], # gehm 2003
      'SP_frequencies': [2*const.pi * const.lambda2nu(670.992421e-9),2*const.pi * const.lambda2nu(670.977338e-9)], # gehm 2003
      'SP_decays': [2*const.pi*5.8724e6 ,  2*const.pi*5.8724e6], # gehm 2003
      'SP_linpolcoupling':[1/3, 1/3], # calculated for linear polarization and population only in the F=1/2
      #levels of the ground state, using transition matrix elements in gehm 2003
      'DP_frequencies': [], # literatur NIST database
      'DP_decays': [], # literatur NIST database
      'DP_linpolcoupling':[],}

MagnesiumDict = {'name':'Magnesium',
      'charge': const.e,
      'mass': 24*const.m_p,
      'SP_frequencies': [2*const.pi * const.lambda2nu(280.4e-9),2*const.pi * const.lambda2nu(279.6e-9)],
      'SP_decays': [2*const.pi*41.8e6 ,  2*const.pi*41.3e6],
      'SP_linpolcoupling':[1./3, 2./3]}

RubidiumDict = {'name':'Rubidium',
      'charge': 0,
      'mass': 87*const.m_p,
      'SP_frequencies': [2*const.pi * const.lambda2nu(780.4e-9),2*const.pi * const.lambda2nu(795.4e-9)],
      'SP_decays': [2*const.pi*6.07e6,2*const.pi*5.75e6],
      'SP_linpolcoupling':[1./3,2./3],
      'GS_Pol':const.h*0.0794*1e-4}

CalciumDict = {'name':'Calcium',
      'charge': const.e,
      'mass': 40*const.physical_constants['unified atomic mass unit'][0],
      'SP_frequencies': [2*const.pi * const.lambda2nu(396.847e-9),2*const.pi * const.lambda2nu(393.366e-9)],
      'SP_decays': [2*const.pi*20.669e6 ,  2*const.pi*21.507e6],
      'SP_linpolcoupling':[1./3, 2./3],
      'DP_frequencies': [2*const.pi * const.lambda2nu(649.7e-9),2*const.pi *  const.lambda2nu(614.2e-9), 2*const.pi * const.lambda2nu(585.4e-9)],
      'DP_decays': [2*const.pi*4.9e6, 2*const.pi*6.6e6, 2*const.pi*0.95e6],
      'DP_linpolcoupling':[1,1,1],}

Ba = Atom(**BariumDict)

Mg = Atom(**MagnesiumDict)

Rb = Atom(**RubidiumDict)

Ca = Atom(**CalciumDict)

Li = Atom(**LithiumDict)
