"""Laser models used by optical-dipole-trap calculations."""

# some ion properties in SI
# clebsch gordan coefficients are squared

from scipy import constants as const
import numpy as np
from scipy.interpolate import interp1d
from quatm.physics.odt import lightmatterint as lmi

class Laser(object):
    '''该对象汇总相干光源（高斯强度分布）的全部定义性参数。

    提供不同空间点处束腰与强度的计算方法；波长/频率/波数三者保持同步。

    Args:
        wavelength/frequency/wavenumber: 三者给一个即可，其余自动换算。
        waistx/waisty: 两方向束腰。
        wx_err/wy_err: 束腰不确定度。
        power: 光功率。
        disp: 束腰相对原点的位移。
        phase: 相位。
        theta: 光束相对 z 轴的角度。
        pol: Jones 形式偏振。
    ''' 
    
    def __init__(self, wavelength=None, frequency=None, wavenumber=None,
                 waistx=4e-6, waisty=4e-6, wx_err=0, wy_err=0, power=0,
                 disp=np.array([0,0,0]), phase=0, theta=0, pol=np.array([1,0]),
                 **kwargs):
        """构造激光对象：波长/频率/波数三者给一个即自动换算其余。

        Args:
            wavelength/frequency/wavenumber: 光频参数（任选其一）。
            waistx/waisty: 束腰。wx_err/wy_err: 束腰不确定度。
            power: 光功率。disp: 束腰位移。phase: 相位。
            theta: 光束角。pol: 偏振（Jones 形式）。
            **kwargs: 可含 'waist'（同时设置两方向束腰）。
        """
        
        if isinstance(wavelength, dict):
            raise TypeError(
                'The legacy dict form for wavelength is no longer supported; '
                'pass wavelength, frequency or wavenumber as a keyword argument.')
        
        if wavelength is not None:    
            self._wavelength = wavelength
            self._frequency = self.wl_2_angfreq(wavelength)
            self._wavenumber = self.wl_2_wn(wavelength)
            
        elif frequency is not None:
            self._frequency = frequency
            self._wavelength = self.angfreq_2_wl(frequency)
            self._wavenumber = self.wl_2_wn(self._wavelength)
            
        elif wavenumber is not None:
            self._wavenumber = wavenumber
            self._wavelength = self.wn_2_wl(wavenumber)
            self._frequency = self.wl_2_angfreq(self._wavelength)
        
        self._waistx = waistx #beamwaist in x direction
        self._waisty = waisty #beamwaist in y direction
        self.wx_err = wx_err #uncertainty on beamwaist in x direction
        self.wy_err = wy_err #uncertainty on beam waist in y direction
        self.power = power #power of the beam
        self.disp = disp #displacement of beamwaist away from zero
        self.pol = pol #polarization of beam in jones formalism
        self.phase = phase #phase
        self.theta = theta #azimuthal angle of beam relative to z axis
        
        if 'waist' in kwargs:
            self._waist = kwargs['waist']
            self._waistx = kwargs['waist']
            self._waisty = kwargs['waist']
        else: self._waist = np.sqrt(self._waistx*self._waisty) #geometric mean
    
    
    # To always keep wavenumber, wavelength and angular frequency synchronized
    # we need to redefine the default setter functions. For this the property
    # decorator approach is being used
    @property
    def wavenumber(self):
        """返回波数（经换算与波长、角频率保持同步；改动请通过 setter）。"""
        return self._wavenumber
    
    @property
    def wavelength(self):
        """返回波长（经换算与波数、角频率保持同步；改动请通过 setter）。"""
        return self._wavelength
    
    @property
    def frequency(self):
        """返回角频率（经换算与波长、波数保持同步；改动请通过 setter）。"""
        return self._frequency
        
    @wavenumber.setter   
    def wavenumber(self, wavenumber):
        """设置波数并同步更新波长与角频率。

        Args:
            wavenumber: 新波数。
        """
        self._wavenumber = wavenumber
        self._wavelength = self.wn_2_wl(wavenumber)
        self._frequency = self.wl_2_angfreq(self._wavelength)
        
    @wavelength.setter
    def wavelength(self,wavelength):
        """设置波长并同步更新角频率与波数。

        Args:
            wavelength: 新波长。
        """
        self._wavelength = wavelength
        self._frequency = self.wl_2_angfreq(wavelength)
        self._wavenumber = self.wl_2_wn(wavelength)
    
    @frequency.setter
    def frequency(self, frequency):
        """设置角频率并同步更新波长与波数。

        Args:
            frequency: 新角频率。
        """
        self._frequency = frequency
        self._wavelength = self.angfreq_2_wl(frequency)
        self._wavenumber = self.wl_2_wn(self._wavelength)
        
    @property
    def waist(self):
        """返回几何平均束腰 sqrt(waistx·waisty)，用于各向同性近似计算。"""
        return self._waist
    
    @waist.setter
    def waist(self, waist):
        """设置平均束腰（两方向同时设为该值）。

        Args:
            waist: 新束腰。
        """
        self._waist = waist
        self._waistx = waist
        self._waisty = waist
        
    @property
    def waistx(self):
        """返回 x 方向束腰（高斯束在 x 方向上的 1/e² 半宽）。"""
        return self._waistx
    
    @property
    def waisty(self):
        """返回 y 方向束腰（高斯束在 y 方向上的 1/e² 半宽）。"""
        return self._waisty
    
    @waistx.setter
    def waistx(self, waistx):
        """设置 x 方向束腰并更新几何平均。

        Args:
            waistx: 新 x 束腰。
        """
        self._waistx = waistx
        self._waist = np.sqrt(waistx*self._waisty)
    
    
    @waisty.setter
    def waisty(self, waisty):
        """设置 y 方向束腰并更新几何平均。

        Args:
            waisty: 新 y 束腰。
        """
        self._waisty = waisty
        self._waist = np.sqrt(waisty*self._waistx)
    
    
    def I0(self):
        """按高斯分布计算峰值强度（I0 = 2P/(π·w²)）。

        Returns:
            float: 峰值强度。
        """
        #waist  = np.sqrt(self.waistx*self.waisty)
        return 2*self.power/(np.pi*self._waist**2)
    
    def intensity(self, r):
        """计算光场在空间某点的高斯强度（含束传播与位移）。

        Args:
            r (np.ndarray): 空间位置坐标。

        Returns:
            float: 该点的光强。
        """
        gauss = np.exp(-2*(((r[0]-self.disp[0])/self.beamsizex(r[2])-self.disp[2])**2
                           +((r[1]-self.disp[1])/self.beamsizey(r[2])-self.disp[2])**2))
        
        pre = self.I0()*self.waistx*self.waisty/(self.beamsizex(r[2]-self.disp[2])
                                                    *self.beamsizey(r[2]-self.disp[2]))
        return pre*gauss
    
    def scaled_E_field(self, r):
        """计算归一化复电场（含高斯束传播相位、波前曲率与 Gouy 相）。

        Args:
            r (np.ndarray): 空间位置坐标。

        Returns:
            复电场（含偏振）。
        """
        
        E0 = np.sqrt(self.I0()) #scaled maximmum electric field (divided by 0.5cne_0)
        r_1 = r-self.disp
        x_rot = np.cos(self.theta)*r_1[0] - np.sin(self.theta)*r_1[2]
        z_rot = np.sin(self.theta)*r_1[0] + np.cos(self.theta)*r_1[2]
    
        # calculate parameters of gaussian beam
        zR = self.rayleigh()
        wz = self.beamsize(z_rot)
        Rcurv = self.R_curv(z_rot)
        guoy = np.arctan(z_rot/zR)
        rsquare = x_rot**2 + r_1[1]**2
        k = self.wavenumber
    
        pre = E0 * self.waist/wz        
        radial = np.exp(-rsquare/wz**2)
        phase = np.exp(-1j*(k*z_rot + k*rsquare/2/Rcurv - guoy + self.phase))
        return pre * radial * phase * self.pol


    #def potential(self, ion, r):
        


    def R_curv(self, z):
        """返回高斯束在距离 z 处的波前曲率半径。

        Args:
            z: 距束腰的距离。

        Returns:
            float: 波前曲率半径。
        """
        #Wavefront Curvature
        if z == 0:
            curv = np.infty
        else:
            curv = z*(1+(self.rayleigh()/z)**2)
        
        return curv
    def Int(self, r):
        """束腰处按高斯分布给出的强度（径向 r）。

        Args:
            r: 距轴距离（可数组）。

        Returns:
            强度值。
        """
        # beam profiler
        waist = np.sqrt(self.waistx*self.waisty)
        return self.I0()*np.exp(-2*(r**2)/(waist**2))

    def beamsizex(self, z):
        """返回 x 方向距束腰 z 处的束宽（含瑞利传播）。

        Args:
            z: 距束腰距离。

        Returns:
            float: x 方向束宽。
        """
        # beam profiler
        return self.waistx*np.sqrt(1.+(z/self.rayleighx())**2)

    def beamsizey(self, z):
        """返回 y 方向距束腰 z 处的束宽（含瑞利传播）。

        Args:
            z: 距束腰距离。

        Returns:
            float: y 方向束宽。
        """
        # beam profiler
        return self.waisty*np.sqrt(1.+(z/self.rayleighy())**2)
    
    def beamsize(self, z):
        """返回距束腰 z 处的平均束宽（几何平均束腰 + 瑞利传播）。

        Args:
            z: 距束腰距离。

        Returns:
            float: 平均束宽。
        """
        return self.waist*np.sqrt(1+(z/self.rayleighx())**2)

    def rayleigh(self):
        """返回平均束腰对应的瑞利长度。

        Returns:
            float: 瑞利长度。
        """
        return np.pi*(self.waist)**2/self.wavelength

    def rayleighx(self):
        """返回 x 方向束腰对应的瑞利长度。

        Returns:
            float: x 瑞利长度。
        """
        return np.pi*(self.waistx)**2/self.wavelength

    def rayleighy(self):
        """返回 y 方向束腰对应的瑞利长度。

        Returns:
            float: y 瑞利长度。
        """
        return np.pi*(self.waisty)**2/self.wavelength
    
    def angfreq_2_wl(self, angfreq):
        """由角频率换算波长。

        Args:
            angfreq: 角频率。

        Returns:
            float: 波长。
        """
        return const.c/(angfreq/(2*np.pi))
    
    def wl_2_angfreq(self, wavelength):
        """由波长换算角频率。

        Args:
            wavelength: 波长。

        Returns:
            float: 角频率。
        """
        return  2*np.pi*const.c/wavelength
    
    def wl_2_wn(self, wavelength):
        """由波长换算波数。

        Args:
            wavelength: 波长。

        Returns:
            float: 波数。
        """
        return 2*np.pi/wavelength
    
    def wn_2_wl(self, wavenumber):
        """由波数换算波长。

        Args:
            wavenumber: 波数。

        Returns:
            float: 波长。
        """
        return 2*np.pi/wavenumber
    
    def wavevector(self):
        """返回传播方向的波矢（2π/λ × 方向）。

        Returns:
            np.ndarray: 波矢。
        """
        return self.direction * 2.*np.pi/np.float64(self.wavelength)
    
    def ctrlPower(self):
        """用标定数据二次插值由控制值得到功率。

        Returns:
            float: 功率。
        """
        # requires input of a Calibration data array
        calfunc = interp1d(self.Calibrationdata[0],self.Calibrationdata[1],
                           kind='quadratic')
        return calfunc(self.ctrlval)



class LightField(object):
    """光场抽象基类：子类须实现 scaled_E_field 与 intensity。"""
    def __init__(self):
        """构造基类（空）。"""
        pass
    
    def scaled_E_field(self):
        """返回归一化复电场分布（由具体光场子类实现；此处仅占位约定接口）。"""
        pass
    
    def intensity(self):
        """返回该光场在给定点的光强（由具体光场子类实现；此处仅占位）。"""
        pass




class SuperField(object):
    """多个同波长光场（LightField）的叠加场。

    Args:
        children (list[LightField]): 要叠加的子光场列表。
    """
    
    
    def __init__(self, children):
        """构造叠加场。

        Args:
            children (list[LightField]): 子光场列表。
        """

        self.children = children
        self.frequency = children[0].frequency
        self.wavelength = children[0].wavelength
        self.wavenumber = children[0].wavenumber
        
    def add_fields(self, *fields):
        '''
        add fields to the children of this instance

        Parameters
        ----------
        fields : LightField or list of LightFields
            fields that should be added to the list of children.
        '''
        self.children.extend(fields)
    
    def scaled_E_field(self, r):
        """叠加场复电场：各子场复电场求和。

        Args:
            r (np.ndarray): 空间位置。

        Returns:
            叠加后的复电场。
        """
        #docstring inherited
        E = np.sum(np.array([child.scaled_E_field(r) for child in self.children]),axis=0)
        return E
    
    def intensity(self, r):
        """叠加场强度：复电场模方。

        Args:
            r (np.ndarray): 空间位置。

        Returns:
            float: 光强。
        """
        #docstring inherited
        E = self.scaled_E_field(r)
        return np.linalg.norm(E)**2
        