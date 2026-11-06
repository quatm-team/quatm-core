"""Optical-dipole-trap utility calculations."""

#from decimal import *
from scipy import constants as const
import numpy as np
from quatm.physics.odt.lightmatterint import LMinteraction

from quatm.physics.odt.lasers import SuperField, Laser


class Trap(object):
    """光偶极 + DC 曲率 + 杂散电场构成的阱模型：势深与势形计算。

    Args:
        laser (Laser|SuperField): 光偶极激光。
        ion (Atom): 离子。
        efields: DC 电场计算器（efields 或 DCTrap）。
        laseroffset (np.ndarray): 光束相对原点的偏移 [delx, dely]。

    Attributes:
        LMint (LMinteraction): 光-物质相互作用计算器。
        delx/dely: 光束偏移。
    """
    def __init__(self,laser,ion,efields,laseroffset=np.array([0,0])):
        """构造阱模型。

        Args:
            laser (Laser|SuperField): 光偶极激光。
            ion (Atom): 离子。
            efields: DC 电场计算器。
            laseroffset (np.ndarray): 光束偏移。
        """
        #    self.name = name
        self.laser = laser
        self.ion = ion
        self.efields = efields
        self.LMint = LMinteraction(ion,laser)
        self.delx = laseroffset[0]
        self.dely = laseroffset[1]


    def AnalTrapDepth(self):
        """无线性杂散场（但有散焦静电曲率）时的解析阱深表达式。

        对 U = -a x² - b·exp(-2(x/w)²) 求导并令 dU/dx=0 得到极值点；
        阱深取 max(U(xmax)-U(0), 0)。假定束腰对称。

        Returns:
            float: 阱深（mK）。
        """
        print(self.efields.curvx())
        b = - self.laser.I0() * self.LMint.S12shift()
        if self.efields.curvx() >= 0:
            depth = 1e3*b/const.k
        else:
            a = -self.ion.mass/2*(4. * const.pi**2 * self.efields.curvx()**2)
            depth = 0
            print(a)
            if -2.*b/(a*self.laser.waist**2)>1:
                Uxmax = a*self.laser.waist**2 / 2. * (1. + np.log(-2.*b/(a*self.laser.waist**2)) )
                #print Uxmax/const.k*1e3
                depth = 1e3*(b+Uxmax)/const.k
        return depth

    def FullPotential2D(self,x,y):
        """计算 (x,y) 处总势能（光偶极 + 线性/四极电场，含光束偏移）。

        Args:
            x: x 坐标。
            y: y 坐标。

        Returns:
            float: 势能（K）。
        """
        maxshift = self.laser.I0() * self.LMint.S12shift()
        gauss = np.exp(-2*(((x-self.delx)/self.laser.waistx)**2 + ((y-self.dely)/self.laser.waisty)**2))
        linEfield = const.e*(self.efields.strayx()*x+self.efields.strayy()*y)
        quadEfield = self.ion.mass/2 * (2*const.pi)**2 *  (self.efields.curv*abs(self.efields.curv)).dot(np.array([x**2,y**2,0]))
        return 1/const.k*(maxshift*gauss - linEfield + quadEfield)

    def FullPotential3D(self,x,y,z):
        """计算 (x,y,z) 处总势能（含束传播随 z 的束宽变化）。

        Args:
            x: x 坐标。
            y: y 坐标。
            z: z 坐标。

        Returns:
            float: 势能（K）。
        """
        maxshift = self.laser.I0()*self.LMint.S12shift()*self.laser.waistx*self.laser.waisty/(self.laser.beamsizex(z)*self.laser.beamsizey(z))
        gauss = np.exp(-2*(((x-self.delx)/self.laser.beamsizex(z))**2 + ((y-self.dely)/self.laser.beamsizey(z))**2))
        linEfield = const.e*(self.efields.strayx()*x+self.efields.strayy()*y)
        omega_squared =(2*const.pi)**2 * self.efields.curv*abs(self.efields.curv)
        dist_squared = np.array([x**2, y**2, z**2])
        quadEfield = self.ion.mass/2*np.einsum('i, i...', omega_squared, dist_squared) 
        return 1/const.k*(maxshift*gauss - linEfield + quadEfield)
        
    
    def FullPotential(self, point, ion_pos=None):
        """计算空间某点的完整势能（偶极束 + DC 曲率 + 杂散场 + 可选离子）。

        Args:
            point (np.ndarray): 求值点坐标。
            ion_pos (list[np.ndarray]|None): 阱内附加离子位置；默认 None。

        Returns:
            float: 该点势能（K）。
        """
        optical = self.laser.intensity(point)*self.LMint.S12shift()
        electronic = self.efields.potential(point, ion_pos)
        
        potential = 1/const.k*(optical+electronic)
        return potential



    def CreatePotentialData(self,xmax=0,Npoints=1000):
        """生成一维势数据：返回 x 轴与对应势能曲线 U(x)。

        x=0 为原点；范围默认 ±2.5 倍束腰（当 xmax=0 时按束腰/子束自动取）。

        Args:
            xmax (float): 距阱轴的最大求值距离；0 表示按束腰自动取。
            Npoints (int): 势能求值点数。

        Returns:
            (xaxis, PotentialData): x 数组与势能数组。
        """
        
        if xmax == 0:
            if isinstance(self.laser, Laser):
                xmax = 2.5*self.laser.waistx
            elif isinstance(self.laser, SuperField):
                xmax = max([child.waistx for child in self.laser.children])
        # create xaxis
        xaxis = np.linspace(-xmax,xmax,Npoints+1)

        # create potential data
        #maxshift = self.laser.I0()*self.LMint.S12shift()
        #gauss = np.exp(-2*(((xaxis-self.delx)/self.laser.waistx)**2))
        #linEfield = const.e*self.efields.strayx()*xaxis
        #quadEfield = np.sign(self.efields.curv[0])*self.ion.mass/2*(2*const.pi)**2*self.efields.curv[0]**2*xaxis**2 

        #PotentialData =  np.array(1/const.k*(maxshift*gauss - linEfield + quadEfield))
        PotentialData = np.array([self.FullPotential([x,0,0]) for x in xaxis])
        return xaxis, PotentialData



    def TrapDepth(self,PotentialData):
        """从一维势数据计算阱深（偶极凹陷谷底与最近势垒之差）。

        Args:
            PotentialData (list|np.ndarray): 一维势能数据。

        Returns:
            float: 阱深（势能单位）。
        """
        depth = 0
        # first find the turning points of the potential (maxima / minima)
        dx = np.diff(PotentialData) # get numerical derivative (point by point, no interpolation)
        turningpoints = np.where(np.diff(np.sign(dx) >= 0))[0]+2  # get turning points i.e. zeros of derivative
        #turningpoints = [i+2 for i, x in enumerate(dx[1:] * dx[:-1] < 0) if x] # get turning points i.e. zeros of derivative
        if len(turningpoints) == 0:
            # if there are no turning points, there can be no potential minimum => depth = 0
            depth = 0
        elif len(turningpoints)==1:
            # if there is one turning point, two possibilities:
            # - the potential is concave at the turning point, 
            #   and PotentialData[0] is LARGER than
            #   the Potential at the turning point
            # EXAMPLE: no defocussing E-Field, simple Gaussian shape, only turning point is in the center
            # - the potential is convex at the turning point,
            #   and PotentialData[0] is SMALLER than
            #   the Potential at the turning point => 0
            # EXAMPLE : defocussing E-Field stronger than confinement
                if PotentialData[turningpoints[0]]<PotentialData[0]:
                    depth = abs(PotentialData[turningpoints[0]-1])
                else:
                    depth = 0
        elif len(turningpoints)==2:
            # if there are two turning points, we probably have the situation 
            # of a linear E-Field that tilts the potential:
            # the higher turning point is the 'barrier' and the
            # lower turning point is the 'minimum'
            # the trapdepth is the difference between these two
            depth = abs(max([PotentialData[turningpoints[0]-1],PotentialData[turningpoints[1]-1]])-min([PotentialData[turningpoints[0]-1],PotentialData[turningpoints[1]-1]]))
        elif len(turningpoints)==3:
            # if there are three turning points, we probably have the situation
            # of a defocussing E-Field with a 'dimple' from the laser
            # the trap depth then is the difference between the Potential
            # at the second turning point and and the smaller of the other two turning points
            depth = abs(PotentialData[turningpoints[1]-1]-min([PotentialData[turningpoints[0]-1],PotentialData[turningpoints[2]-1]]))
        return depth

    def QuickieTrapDepth(self):
        """快速估算阱深：构造 1D 势数据并返回以 mK 为单位的阱深。

        Returns:
            float: 阱深（mK）。
        """
        x,U = self.CreatePotentialData(xmax=0,Npoints=1000)
        return 1e3*self.TrapDepth(U)

