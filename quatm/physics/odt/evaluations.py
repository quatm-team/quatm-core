# -*- coding: utf-8 -*-
"""
Created on Mon Dec  7 19:57:06 2020

@author: Daniel
"""

import copy
import numpy as np
import pandas as pd

# 重量级依赖改为惰性导入：本模块是数值评估工具，很多调用方（CI、离屏环境、
# 只做拟合不做图的脚本）不应该因为 import 就拉起绘图栈。详见
# quatm/physics/_lazy.py。``models`` 仍会在导入期加载 lmfit（它是 lmfit 模型
# 定义），这是有意保留的天花板。
from quatm.physics._lazy import lazy_attr, lazy_module
plt = lazy_module('matplotlib.pyplot')
Parameters = lazy_attr('lmfit', 'Parameters')
tnrange = lazy_attr('tqdm', 'tnrange')
tqdm_notebook = lazy_attr('tqdm', 'tqdm_notebook')

from quatm.physics.odt import (atomlib, lasers, efields,
                         opticaltraputils, statistics)
from quatm.physics.odt.utils import wilson_err


def _default_model():
    """惰性构造默认概率模型（``lmfit`` 只在真正做拟合时才加载）。

    ``models`` 用 lmfit 作基类，导入它就会拉起 lmfit（以及 matplotlib）；
    而评估器的默认参数如果写成 ``model=models.RadialCutoffModel()``，会在
    **模块 import 期**求值。改成 ``model=None`` + 调用时构造后，纯数值使用
    方（论文脚本、CI）不需要付出绘图栈的导入代价。

    Returns:
        lmfit.Model: 默认的 ``RadialCutoffModel`` 实例。
    """
    from quatm.physics.odt import models
    return models.RadialCutoffModel()

class Result():
    """温度评估结果基类：统一提供结果打印与绘图的接口（子类实现）。"""
    def __init__(self):
        """构造结果对象（基类为空实现）。"""
        pass

    def print_result(self):
        """把拟合结果与关键数值打印到控制台（基类占位接口，由子类实现）。"""
        pass

    def plot_result(self):
        """把拟合结果绘制成图形并展示（基类占位接口，由具体子类实现）。"""
        pass



class TempDataLoader():
    """温度测量数据加载器基类：把测量文件读入统一 DataFrame。

    数据列：power、N_succ、N_tot、mask。
    """
    def __init__(self):
        """初始化空的数据表 DataFrame（预设各数据列）。"""
        self.data = pd.DataFrame(columns = ['power','N_succ','N_tot','mask'])

    def load_data(self):
        """从文件加载数据到 self.data（基类占位，子类实现读取）。"""
        pass



class TiamoTempLoader(TempDataLoader):
    """Tiamo 温度测量数据加载器：读取文件并把 DAC 值换算成功率。

    Args:
        path (str): 数据文件路径。
        calib: 功率标定（Calib），用于 DAC→功率；None 则原样使用。
        legacy (bool): 是否为旧格式测量文件（向后兼容）。
        power_scaling (float): 功率缩放因子。
    """
    def __init__(self, path, calib = None, legacy=False , power_scaling = 1):
        """构造加载器。

        Args:
            path (str): 数据文件路径。
            calib: 标定对象或 None。
            legacy (bool): 旧格式标志。
            power_scaling (float): 功率缩放。
        """
        super().__init__()
        self.legacy = legacy
        self.path = path
        self.calib = calib
        self.power_scaling = power_scaling

    def load_data(self):
        '''
        loads data from file.

        calib: Calibration to use to convert DAC values into Laser Powers

        legacy: True if needed to load old measurement files (added for
                backwards compatibility)

        '''
        if self.legacy:
            raw_data = np.loadtxt(self.path,skiprows = 1)
            if self.calib is not None:
                self.data['power'] = (self.power_scaling*
                                      self.calib.dac2power(raw_data[:,0]))
            else:
                self.data['power'] = raw_data[:,0]
            self.data['mask'] = raw_data[:,1]
            self.data['N_succ'] = raw_data[:,2]
            self.data['N_tot'] = raw_data[:,3]

        else:
            self.data = pd.read_csv(self.path)
            if 'DAC1' in self.data.columns:
                if self.calib  is not None:
                    self.data['power'] = (self.power_scaling *
                                          self.calib.dac2power(
                                              self.data['DAC1']))
                else:
                    self.data['power'] = self.power_scaling*self.data['DAC1']
        return self.data



class TiamoTempLoaderTwobm(TempDataLoader):
    """Tiamo 双束阱温度测量数据加载器（含 p_in/p_r 两路功率）。

    Args:
        path (str): 数据文件路径。
        calib: 功率标定或 None。
        legacy (bool): 旧格式标志。
        power_scaling (float): 功率缩放因子。
    """
    def __init__(self, path, calib = None, legacy=False , power_scaling = 1):
        """构造加载器。

        Args:
            path (str): 数据文件路径。
            calib: 标定或 None。
            legacy (bool): 旧格式标志。
            power_scaling (float): 功率缩放。
        """
        super().__init__()
        self.legacy = legacy
        self.path = path
        self.calib = calib
        self.power_scaling = power_scaling

    def load_data(self):
        '''
        loads data from file.

        calib: Calibration to use to convert DAC values into Laser Powers

        legacy: True if needed to load old measurement files (added for
                backwards compatibility)

        '''
        if self.legacy:
            raw_data = np.loadtxt(self.path,skiprows = 1)
            if self.calib is not None:
                self.data['p_in'] = self.calib.dac2power(raw_data[:,0])
            else:
                self.data['p_in'] = raw_data[:,0]
            
            self.data['p_r'] = self.power_scaling*self.data['p_in']
            self.data['mask'] = raw_data[:,1]
            self.data['N_succ'] = raw_data[:,2]
            self.data['N_tot'] = raw_data[:,3]

        else:
            self.data = pd.read_csv(self.path)
            if 'DAC1' in self.data.columns:
                if self.calib  is not None:
                    self.data['p_in'] = self.calib.dac2power(self.data['DAC1'])
                else:
                    self.data['p_in'] = self.data['DAC1']
                self.data['p_r'] = self.power_scaling*self.data['p_in']
        return self.data




class TempEvaluation():
    """光学阱温度测量评估：由加载数据计算阱深并拟合温度模型。

    可被子类化以适配不同光势/概率模型或具体实验（如 TIAMO 的 ODT）。

    Args:
        data_loader (TempDataLoader): 数据加载器。
        ion (Atom): 离子。
        laser (Laser): 光阱激光。
        dc_trap (DCTrap): 直流阱。
        model: lmfit 概率模型（默认 RadialCutoffModel）。
        params: 拟合参数（None 则用模型默认）。
    """

    def __init__(self, data_loader,
                 ion,
                 laser,
                 dc_trap,
                 model=None,
                 params = None):
        """构造评估器：加载数据并计算概率。

        Args:
            data_loader: 数据加载器。
            ion: 离子。laser: 激光。dc_trap: 直流阱。
            model: 概率模型（None 时用 :func:`_default_model`）。
            params: 拟合初值参数。
        """

        if model is None:
            model = _default_model()
        self.data = data_loader.load_data()
        self.model = model
        self.ion = ion
        self.dc_trap = dc_trap
        self.laser = laser
        self.params = params
        self.calc_probabilites()


    def simple_fit(self):
        """单次拟合评估：计算各数据点阱深并对概率做一次模型拟合。

        Returns:
            SingleFitTempResult: 拟合结果（含数据与推断值）。
        """

        avg_dc = self.dc_trap.avg_trap
        self.data['depths'] = self.calc_depths(self.data,avg_dc,self.laser)

        #Filter data for points being used in the fit and perform the fit
        include_data = self.data[self.data['mask'] == 1]
        fit_result = self.fit_model(include_data)

        result = SingleFitTempResult(self.data, fit_result, model = self.model)
        return result


    def bootstrap(self, repetitions):
        """用自助抽样（bootstrap）评估，考虑 x 不确定度与 Wilson 非对称误差。

        Args:
            repetitions (int): 抽样次数。

        Returns:
            (BootstrappingTempResult, T_array): 结果对象与温度样本数组。
        """
        ### Calculate the results for an average trap
        avg_dc = self.dc_trap.avg_trap
        self.data['depths'] = self.calc_depths(self.data,avg_dc,self.laser)

        ### Filter data for included points pre evaluation since expensive
        include_data = self.data[self.data['mask'] == 1]


        ### creating copies of the  laser s.t. we don't influence the initial data
        bs_laser = copy.deepcopy(self.laser)
        bs_laser.power = include_data['power']
        #initialise E-field
        bs_efield = efields.efields(self.ion, [0,0,0], [0,0,0])

        ### setup the arrays for the mean and standard deviation of the measured
        ### values, and generate a normal distribution for them
        means =[0, self.laser.waistx,self.laser.waisty,self.dc_trap.curv[0]]
        errs = np.diag([self.dc_trap.sig_stray[0],self.laser.wx_err,self.laser.wy_err, 0.01e3 ])**2

        randew = np.random.multivariate_normal(means,errs,size=repetitions)
        T_array = np.zeros(repetitions)
        pSat_array = np.zeros(repetitions)


        ###generate cloud of y data according to the probability distribution
        ycloud = np.array([statistics.getrandbincont(n,k,repetitions)
                                     for n,k in zip(include_data['N_tot'],
                                                    include_data['N_succ'])]).T

        ### generate an array to store all the calculated depths during the
        ### evaluation
        xcloud = np.empty_like(ycloud)

        for ns in tnrange(repetitions, desc = 'Evaluating'):
            # create random x data (trap depths) from beam waist error

            bs_efield.stray[0], bs_laser.xwaist, bs_laser.ywaist, bs_efield.curv[0] = randew[ns,:]
            depths = self.calc_depths(include_data,bs_efield, bs_laser)

            ydata = ycloud[ns,:]

            # initialise fitting parameters
            if self.params is None:
                p0 = self.model.make_params()
            else:
                p0 = Parameters()
                p0.add_many(self.params)

            # Perform least-squares optimization
            fit_results = self.model.fit(ydata,
                                          depth=depths,
                                          weights=np.ones(len(ydata)),
                                          params=p0,
                                          nan_policy='propagate')
            p1 = fit_results.params
            T_array[ns] = p1['temp'].value
            pSat_array[ns] = p1['p_sat'].value
            xcloud[ns] = depths

        ###calculate mean and std_deviation from the sampled values
        T_mean = np.average(T_array)
        T_err = np.std(T_array)

        pSat_mean = np.average(pSat_array)
        pSat_err = np.std(pSat_array)


        ### building result parameter object
        p1['temp'].value = T_mean
        p1['temp'].stderr = T_err

        p1['p_sat'].value = pSat_mean
        p1['p_sat'].stderr = pSat_err

        result = BootstrappingTempResult(self.data,xcloud = xcloud,ycloud = ycloud,
                                         result = p1, model = self.model,
                                         repetitions = repetitions)
        return result, T_array


    def calc_probabilites(self):
        """计算 Wilson 分数区间概率并存入 self.data（prob/lerr/uerr）。"""
        self.data[['prob','lerr','uerr']] = np.array(wilson_err(
            self.data['N_tot'],
            self.data['N_succ'])).T


    def calc_depths(self,data, efield, laser):
        """为给定功率与阱配置计算每个数据点的阱深。

        Args:
            data: 数据表（含 power 列）。
            efield: 电场计算器。
            laser: 光阱激光。

        Returns:
            np.ndarray: 各点的阱深。
        """

        #updating the laser power. The power needs to be an array of shape
        #(n,1) to allow for vectorization of the calculation for the
        #potential data
        laser.power = data['power'].to_numpy().reshape(-1,1)

        #initialise trap and generate potential data
        full_trap = opticaltraputils.Trap(laser, self.ion,
                                               efield)
        x,U = full_trap.CreatePotentialData()

        #calculate potential depths
        depths = np.zeros(U.shape[0])
        for i in range(U.shape[0]):
            depths[i] = full_trap.TrapDepth(U[i])

        return depths


    def fit_model(self,data):
        """把概率模型拟合到（含权重的）数据上。

        Args:
            data: 含 prob/depths/lerr/uerr 列的数据子集。

        Returns:
            lmfit 拟合结果对象。
        """

        weigths =1/(0.5*(data['uerr']+data['lerr']))


        if self.params is None:
            p0 = self.model.make_params()
        else:
            p0 = Parameters()
            p0.add_many(self.params)

        fit_results = self.model.fit(
            data['prob'],
            depth=data['depths'],
            weights=weigths,
            params=p0,
            scale_covar=False)

        return fit_results




class TiamoTempEval(TempEvaluation):
    """适配 Tiamo 工作流的温度评估子类（单光束 ODT）。

    Args:
        path (str): 数据文件路径。
        calib: 功率标定。
        legacy (bool): 旧格式标志。
        ion (Atom): 离子（默认 Ba）。
        laser: 光阱激光（默认 532 nm）。
        sig_Es: 杂散场标准差（V/m）。
        f_ax: 轴向囚禁频率。
        model: 概率模型。
        power_scaling (float): 功率缩放。
        params: 拟合初值参数。
    """
    def __init__(self, path, calib, legacy=False, ion=atomlib.Ba,
                 laser=lasers.Laser(wavelength=532e-9),#
                 sig_Es=8e-3, f_ax = 12e3,
                 model=None,
                 power_scaling =1, params = None):
        """构造 Tiamo 温度评估器。

        Args:
            path (str): 数据文件路径。
            calib: 功率标定。
            legacy (bool): 旧格式标志。
            ion/laser/sig_Es/f_ax/model/power_scaling/params: 见类说明。
        """

        if model is None:
            model = _default_model()
        data_loader = TiamoTempLoader(path, calib, legacy, power_scaling)
        sig_stray = np.array([sig_Es,0,0])
        curv = np.array([-f_ax,0,f_ax])
        dc_trap = efields.DCTrap(ion,sig_stray=sig_stray,curv=curv)
        super().__init__(data_loader, ion=ion,
                         dc_trap=dc_trap,laser=laser, model=model, params=params)


class TiamoTempEvalTwobm(TempEvaluation):
    """适配 Tiamo 双光束 ODT 工作流的温度评估子类。

    Args:
        path (str): 数据文件路径。
        calib: 功率标定。
        light_field: 双光束叠加场（SuperField）。
        legacy (bool): 旧格式标志。
        ion (Atom): 离子（默认 Ba）。
        sig_Es: 杂散场标准差。
        f_ax: 轴向囚禁频率。
        power_scaling/model/params: 功率缩放/模型/参数。
    """

    def __init__(self, path, calib, light_field, legacy=False, ion=atomlib.Ba,
                sig_Es=8e-3, f_ax=12e3, power_scaling=1,model=None,
                params=None):
        """构造双光束温度评估器。

        Args:
            path (str): 数据路径。calib: 标定。light_field: 双光束叠加场。
            legacy/ion/sig_Es/f_ax/power_scaling/model/params: 见类说明。
        """
        
        if model is None:
            model = _default_model()
        data_loader = TiamoTempLoaderTwobm (path, calib, legacy, power_scaling)
        sig_stray = np.array([sig_Es,0,0])
        curv = np.array([-f_ax,0,f_ax])
        dc_trap = efields.DCTrap(ion,sig_stray=sig_stray,curv=curv)

        super().__init__(data_loader, ion, light_field, dc_trap, model, params)

    
    def calc_depths(self,data, efield, laser):
        """双光束情形下为给定功率与阱配置计算每个数据点的阱深。

        Args:
            data: 数据表（含 p_in/p_r 列）。
            efield: 电场计算器。
            laser: 双光束叠加场。

        Returns:
            np.ndarray: 各点的阱深。
        """

        #updating the laser power. The power needs to be an array of shape
        #(n,1) to allow for vectorization of the calculation for the
        #potential data
        laser.children[0].power = data['p_in'].to_numpy().reshape(-1,1)
        laser.children[1].power = data['p_r'].to_numpy().reshape(-1,1)

        #initialise trap and generate potential data
        full_trap = opticaltraputils.Trap(laser, self.ion,
                                               efield)
        x,U = full_trap.CreatePotentialData()

        #calculate potential depths
        depths = np.zeros(U.shape[0])
        for i in range(U.shape[0]):
            depths[i] = full_trap.TrapDepth(U[i])

        return depths



class TempResult():
    """温度评估结果基类：保存数据/拟合结果与模型，提供绘图与保存能力。

    Args:
        data: 数据表。
        result: 拟合参数结果。
        model: lmfit 模型。
    """
    def __init__(self, data, result, model):
        """构造结果对象。

        Args:
            data: 数据表。
            result: 拟合参数。
            model: lmfit 模型。
        """
        self.data = data
        self.result = result
        self.model = model
        
        
    def save_figure(self, path):
        """把当前绘图保存到文件。

        Args:
            path (str): 保存路径。
        """
        self.fig.savefig(path)

    def plot(self, ax, color, show_confidence):
        """绘制主图（基类占位，由子类覆写）。

        Args:
            ax: matplotlib 坐标轴。
            color: 颜色。
            show_confidence (bool): 是否显示置信区间。
        """
        'placeholder to be overridden by subclass'
        pass


    def plot_residuals(self, figsize = (10,2), ax = None, color = 'C0'):
        """绘制拟合残差（误差棒形式）。

        Args:
            figsize (tuple): 图形尺寸。
            ax: 坐标轴；None 时新建。
            color: 颜色。
        """
        if ax is None:
            fig, ax = plt.subplots(figsize=figsize)
            ax.set_title('Residuals')
            ax.set_xlabel("$U_0$ (mK)")
            ax.set_ylabel('Residuals')
        

        residuals = (self.model.eval(self.result, depth = self.data['depths'])-
                     self.data['prob'])
        ax.errorbar(self.data['depths']*1e3,residuals,
                    yerr =[self.data['lerr'],self.data['uerr']],
                    linewidth = 3,marker='s',markersize=10,
                    linestyle='none',ecolor = 'gray', color = color)
        ax.axhline(y=0, color = color )
        
        self.fig = ax.figure


    def plot_full(self, figsize = (10,8), color = 'C0',
                  show_confidence = False):
        """绘制完整图：上（残差）+ 下（数据与拟合）两联。

        Args:
            figsize (tuple): 图形尺寸。
            color: 颜色。
            show_confidence (bool): 是否显示置信区间。
        """

        fig, (ax1, ax2) = plt.subplots(2,1 ,sharex=True, figsize = figsize,
                                       gridspec_kw={'height_ratios':[2,6]})

        self.plot_residuals(ax=ax1, color=color)
        self.plot(ax=ax2, color= color, show_confidence=show_confidence)
        self.fig = fig

class SingleFitTempResult(TempResult):
    """单次拟合的结果对象：数据、拟合结果与模型，附完整绘图方法。

    Args:
        data: 数据表。
        fit_result: lmfit 单次拟合结果。
        model: lmfit 模型。
    """
    def __init__(self, data, fit_result, model):
        """构造单次拟合结果（深拷贝数据）。

        Args:
            data: 数据表。
            fit_result: lmfit 拟合结果。
            model: lmfit 模型。
        """
        self.fit_result = fit_result
        result = fit_result.result.params
        self.fit_report = fit_result.result
        data = copy.deepcopy(data)

        super().__init__(data, result, model)


    def plot(self, title='', show_confidence=False, show_fit=True,
             show_temp = True, show_data=True, figsize=(10,6), ax=None,
             color='C0', label=None, xrange=None, include_masked='mark'):
        """绘制数据与单次拟合结果（含可选的置信带/温度标注）。

        Args:
            title (str): 图标题。
            show_confidence (bool): 是否画 ±1σ 参数带。
            show_fit/show_data/show_temp (bool): 显示拟合/数据/温度标注开关。
            figsize (tuple): 图形尺寸。
            ax: 坐标轴；None 时新建。
            color (str): 颜色。
            label (str|None): 数据图例。
            xrange: x 显示范围；None 自动。
            include_masked (str): 'mark'/'include'/'exclude' 处理掩膜点。
        """

        styledict = {'caption':{'fontsize': 18},
                     'label':{'fontsize':16},
                     'lineplot':{'linewidth':3},
                     'errorbar':{'linewidth':3,'marker':'s',
                                 'markersize':10, 'linestyle':'none',
                                 'ecolor':'gray'},
                     'alt_errorbar':{'linewidth':3,'marker':'s',
                                     'markersize':10,
                                     'markerfacecolor':'none',
                                     'markeredgewidth':2,
                                     'linestyle':'none','ecolor':'gray'}}


        #self.fit_result.plot_fit()
        T = self.result['temp'].value
        T_err = self.result['temp'].stderr

        if ax is None:
            fig, ax = plt.subplots(1,figsize=figsize)
            standalone = True
        else: standalone = False

        ### Plot best fit
        if show_fit:
            if xrange is None:
                xrange = np.linspace(0, max(self.data['depths']), 1000)

            if show_temp:
                t_label = 'T = {:.3f} +/- {:.3f} mK'.format(T*1e3, T_err*1e3)
            else: t_label = None

            ax.plot(xrange*1e3,self.model.eval(self.result, depth=xrange),
                    **styledict['lineplot'], label = t_label,color=color)


            if show_confidence:
                upb_par = Parameters()
                upb_par.add_many(('temp', T+T_err),
                                 ('p_sat', self.result['p_sat'].value))
                lob_par = Parameters()
                lob_par.add_many(('temp', T-T_err),
                                 ('p_sat', self.result['p_sat'].value))

                ax.plot(xrange*1000, self.model.eval(upb_par, depth=xrange),
                        **styledict['lineplot'], color=color,
                        linestyle='dashed')
                ax.plot(xrange*1000, self.model.eval(lob_par, depth=xrange),
                        **styledict['lineplot'], color=color,
                        linestyle='dashed')

        if show_data:
            assert include_masked in ['mark','include','exclude'], """
            include_masked parameter must be one of the following:
                'mark','include','exclude'
            """
            #Plot Data
            if include_masked == 'include':
                ax.errorbar(self.data['depths']*1000, self.data['prob'],
                            yerr =[self.data['lerr'],self.data['uerr']],
                            **styledict['errorbar'], color = color,
                            label=label)
            else:
                incl=self.data[self.data['mask'] == 1]
                ax.errorbar(incl['depths']*1000, incl['prob'],
                            yerr =[incl['lerr'],incl['uerr']],
                            **styledict['errorbar'], color = color,
                            label=label)
                if include_masked == 'mark':
                    excl=self.data[self.data['mask'] == 0]
                    ax.errorbar(excl['depths']*1000, excl['prob'],
                            yerr =[excl['lerr'],excl['uerr']],
                            **styledict['alt_errorbar'], color = color)

        if standalone:
            ax.legend()
            ax.set_xlabel("$U_0$ (mK)", fontsize = 16)
            ax.set_ylabel(r"$p_{opt}$", fontsize = 16)
            ax.set_title(title, fontsize = 18)
            ax.set_ylim(0,1)
            
        self.fig = ax.figure







class BootstrappingTempResult(TempResult):
    """自助抽样（bootstrap）温度评估的结果对象。

    Args:
        data: 数据表。
        xcloud: bootstrap 生成的阱深云点。
        ycloud: bootstrap 生成的概率云点。
        result: 拟合参数。
        model: lmfit 模型。
        repetitions (int): 抽样次数。
    """
    def __init__(self, data, xcloud, ycloud, result, model, repetitions):
        """构造 bootstrap 结果对象。

        Args:
            data: 数据表。xcloud/ycloud: 抽样云点。
            result: 拟合参数。model: 模型。repetitions: 抽样次数。
        """

        data = copy.deepcopy(data)
        super().__init__(data, result, model)

        self.xcloud = xcloud
        self.ycloud = ycloud
        self.result = result
        self.repetitions = repetitions

    def plot(self, title='', color='C0', show_confidence=False,
             show_cloud=False, show_fit=True, show_data=True, show_temp=True,
             figsize = (10,6), label=None, ax=None, include_masked='mark'):
        """绘制数据、误差棒与 bootstrap 拟合结果（可选显示抽样云点）。

        Args:
            title (str): 标题。
            color (str): 颜色。
            show_confidence (bool): 显示 ±1σ 带。
            show_cloud (bool): 显示抽样云点。
            show_fit/show_data/show_temp (bool): 显示拟合/数据/温度。
            figsize (tuple): 尺寸。
            label (str|None): 图例。
            ax: 坐标轴；None 新建。
            include_masked (str): 'mark'/'include'/'exclude'。

        Returns:
            (fig, ax): matplotlib 图形与坐标轴。
        """


        styledict = {'caption':{'fontsize': 18},
                     'label':{'fontsize':16},
                     'errorbar':{'linewidth':3,'marker':'s',
                                 'markersize':10, 'linestyle':'none',
                                 'ecolor':'gray'},
                     'lineplot':{'linewidth':3},
                     'alt_errorbar':{'linewidth':3,'marker':'s',
                                     'markersize':10,
                                     'markerfacecolor':'none',
                                     'markeredgewidth':2,
                                     'linestyle':'none','ecolor':'gray'}}



        T = self.result['temp'].value
        T_err = self.result['temp'].stderr

        # create a 'continous' array of x values
        xrange = np.linspace(0,np.max(self.xcloud)*1.4,1000)

        # create the figure
        if ax is None:
            fig, ax = plt.subplots(1,figsize=figsize)
            standalone = True
        else: standalone = False

        if show_cloud:
            #plot point clouds
            ax.scatter(self.xcloud.flatten()*1000,self.ycloud.flatten(),
                       s=1,alpha=max(min(0.2,100/self.repetitions),1/255),
                       marker=',', color=color )


        #plot the best fit
        if show_fit:
            if show_temp:
                t_label = 'T = {:.3f} +/- {:.3f} mK'.format(T*1e3, T_err*1e3)
            else: t_label = None

            ax.plot(xrange*1000, self.model.eval(self.result, depth=xrange),
                    label = t_label,color=color, **styledict['lineplot'])


            if show_confidence:
                upb_par = Parameters()
                upb_par.add_many(('temp', T+T_err),
                                 ('p_sat', self.result['p_sat'].value))
                lob_par = Parameters()
                lob_par.add_many(('temp', T-T_err),
                                 ('p_sat', self.result['p_sat'].value))

                ax.plot(xrange*1000, self.model.eval(upb_par, depth=xrange),
                        color=color, linestyle='dashed',
                        **styledict['lineplot'])
                ax.plot(xrange*1000, self.model.eval(lob_par, depth=xrange),
                        color=color, linestyle='dashed',
                        **styledict['lineplot'])


        # plot data plus errorbars
        if show_data:
            assert include_masked in ['mark','include','exclude'], """
            include_masked parameter must be one of the following:
                'mark','include','exclude'
            """
            incl=self.data[self.data['mask'] == 1]
            excl=self.data[self.data['mask'] == 0]
            
            errorXL = incl['depths']-np.percentile(self.xcloud,16,axis=0)
            errorXU = np.percentile(self.xcloud,84,axis=0)-incl['depths']

            ax.errorbar(incl['depths']*1000, incl['prob'],
                        yerr=[incl['lerr'],incl['uerr']],
                        xerr=[errorXL*1000,errorXU*1000],
                        **styledict['errorbar'], color=color,
                        label=label)
            
            if include_masked == 'include':
                ax.errorbar(excl['depths']*1000, excl['prob'], 
                            yerr=[excl['lerr'],excl['uerr']],
                            **styledict['errorbar'], color=color,
                            label=None)
                
            elif include_masked == 'mark':
                 ax.errorbar(excl['depths']*1000, excl['prob'], 
                            yerr=[excl['lerr'],excl['uerr']],
                            **styledict['alt_errorbar'], color=color,
                            label=None)
                


        if standalone:
            ax.legend(**styledict['label'])
            ax.set_xlabel("$U_0$ (mK)", **styledict['label'])
            ax.set_ylabel(r"$p_{opt}$", **styledict['label'])
            ax.set_title(title, **styledict['caption'])
            
        self.fig = ax.figure
