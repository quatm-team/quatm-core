"""Light-matter interaction models for optical traps."""

#from decimal import *
from scipy import constants as const


class LMinteraction(object):
    """光-物质相互作用计算：对给定的离子与激光求 Stark 位移与散射率。

    Args:
        ion (Atom): 离子/原子对象（含 SP/DP 频率与衰变等）。
        laser (Laser): 激光对象（frequency、I0 等）。
    """
    def __init__(self, ion, laser):
        """构造相互作用计算器。

        Args:
            ion (Atom): 离子对象。
            laser (Laser): 激光对象。
        """
        #self.name = name
        self.ion = ion
        self.laser = laser

    def SPStarkShift(self,transition):
        """计算某个 S→P 跃迁的 Stark 位移（Metcalf 公式，含旋转波两项）。

        Args:
            transition (int): 跃迁序号（索引 SP_decays/SP_frequencies）。

        Returns:
            Stark 位移。
        """
        gamma = self.ion.SP_decays[transition]
        omega_0 = self.ion.SP_frequencies[transition]
        omega = self.laser.frequency

        pref = -3 * const.pi * const.c**2
        pref2 = gamma / (2 * omega_0**3)
        rotwav = 1 / (omega_0 - omega) + 1/(omega_0 + omega)

        return pref * pref2 * rotwav

    def S12shift(self):
        """计算 S 态的总 Stark 位移（对各 S→P 跃迁按耦合系数加权求和）。

        Returns:
            总 Stark 位移。
        """
        return sum(p*q for p,q in zip(self.ion.SP_linpolcoupling,[self.SPStarkShift(i) for i in [0,1]]))

    def DPStarkShift(self, transition):
        """计算某个 D→P 跃迁的 Stark 位移。

        Args:
            transition (int): 跃迁序号。

        Returns:
            Stark 位移。
        """
        return -3 * const.pi*const.c**2 / (2 * self.ion.DP_frequencies[transition]**3) * (self.ion.DP_decays[transition]/(self.ion.DP_frequencies[transition] - self.laser.frequency) + self.ion.DP_decays[transition]/(self.ion.DP_frequencies[transition] + self.laser.frequency) )

    # summed and weighted with coupling coefficients
    def Dshift(self,level):
        """计算 D 态能级的总 Stark 位移（按耦合系数加权）。

        Args:
            level (str): '3/2' 或 '5/2'。

        Returns:
            总位移；其他 level 返回 0。
        """
        if level == '3/2':
            return sum(p*q for p,q in zip([1,1],[self.DPStarkShift(self.laser.frequency,i) for i in [0,2]]))
        elif level == '5/2':
            return sum(p*q for p,q in zip([1],[self.DPStarkShift(self.laser.frequency,i) for i in [1]]))
        else:
            return 0

    # off-res scattering rate (weidemueller / altes mathematica nb)
    def ScattRate(self,transition):
        """计算某 S→P 跃迁的离共振散射率。

        Args:
            transition (int): 跃迁序号。

        Returns:
            散射率。
        """
        return self.laser.I0()*(self.laser.frequency/self.ion.SP_frequencies[transition])**3 *3*const.pi*const.c**2/(2*const.hbar*self.ion.SP_frequencies[transition]**3)*(self.ion.SP_decays[transition]/(self.ion.SP_frequencies[transition]-self.laser.frequency)+self.ion.SP_decays[transition]/(self.ion.SP_frequencies[transition]+self.laser.frequency))**2

    # summed and weighted with coupling coefficients
    def TotalScattRate(self):
        """S 态总散射率（按耦合系数加权求和）。

        Returns:
            总散射率。
        """
        return sum(p*q for p,q in zip(self.ion['SP_linpolcoupling'],[self.ScattRate(transition) for transition in [0,1]]))
   
    # off-res scattering rate (weidemueller / altes mathematica nb)
    def DScattRate(self,transition):
        """计算某 D→P 跃迁的离共振散射率。

        Args:
            transition (int): 跃迁序号。

        Returns:
            散射率。
        """
        #self.laser.frequency = 2*const.pi * const.lambda2nu(wavelength*1e-9)
        return self.laser.I0()*(self.laser.frequency/self.ion.DP_frequencies[transition])**3 *3*const.pi*const.c**2/(2*const.hbar*self.ion.DP_frequencies[transition]**3)*(self.ion.DP_decays[transition]/(self.ion.DP_frequencies[transition]-self.laser.frequency)+self.ion.DP_decays[transition]/(self.ion.DP_frequencies[transition]+self.laser.frequency))**2

    # summed and weighted with coupling coefficients
    def DTotalScattRate(self, level):
        """D 态某能级的总散射率（按耦合系数加权）。

        Args:
            level (str): '3/2' 或 '5/2'。

        Returns:
            总散射率；其他 level 返回 0。
        """
        if level == '3/2':
            return sum(p*q for p,q in zip([1,1],[self.DScattRate(i) for i in [0,2]]))
        elif level == '5/2':
            return sum(p*q for p,q in zip([1],[self.DScattRate(i) for i in [1]]))
        else:
            return 0
