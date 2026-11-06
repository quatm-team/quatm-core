"""Experiment helper for flip-mirror control sequences."""

import sys, time
import os
# robust import bootstrap (independent of CWD / interpreter): add the repo
# root and <repo>/quatm to sys.path, derived from this file's own location
_here = os.path.dirname(os.path.abspath(__file__))
_repo = _here
while not os.path.isdir(os.path.join(_repo, 'quatm')):
    _parent = os.path.dirname(_repo)
    if _parent == _repo:
        break
    _repo = _parent
for _p in (_repo, os.path.join(_repo, 'quatm')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _here, _repo, _p


#from quatm.experiment import Properties
from quatm.experiment import *

class Flip_Mirror:
    """翻转镜(flip-mirror)与相关光路的联动控制。

    通过共享属性库 ``/Experiments/Defaults`` 里的 TTL/功率开关协调各进程
    （Dashboard/实验）的激光路由，例如在 Verdi 与 Mephisto 光路之间切换。

    Args:
        experiment (Experiment): 所属实验（当前未直接使用，保留接口）。
    """

    def __init__(self, experiment):
        """打开控制属性库 ``Flip_Mirror_Control``。

        Args:
            experiment (Experiment): 所属实验。
        """
        self._props = Properties('Flip_Mirror_Control')

    def set_verdi_aom(self, status):
        """设置 Verdi 光路：开时若翻转镜已入位则不开 AOM 会关闭，否则打开。

        Args:
            status (bool): 期望状态。
        """
        flip_mirror_status = self._props.get('/Experiments/Defaults/flip_mirror_ttl')
        if status:
            if flip_mirror_status:
                self._props.set('/Experiments/Defaults/flip_mirror_ttl', False)
            else:
                self._props.set('/Experiments/Defaults/verdi_aom_ttl', True)
        else:
            self._props.set('/Experiments/Defaults/verdi_aom_ttl', False)


    def set_mephisto_aom(self, status):
        """设置 Mephisto 光路（联动翻转镜与 Mephisto AOM）。

        Args:
            status (bool): 期望状态。
        """
        flip_mirror_status = self._props.get('/Experiments/Defaults/flip_mirror_ttl')
        mephisto_power = self._props.get('/Experiments/Defaults/Mephisto_Power')
        if status:
            if flip_mirror_status or mephisto_power <= 0.1:
                self._props.set('/Experiments/Defaults/Mephisto_AOM_ttl', True)
            else:
                self._props.set('/Experiments/Defaults/flip_mirror_ttl', True)
        else:
            self._props.set('/Experiments/Defaults/Mephisto_AOM_ttl', False)

    def set_photon(self, status):
        """设置“荧光测量”路径：联动翻转镜与 photon 快门。

        Args:
            status (bool): 期望状态。
        """
        if status:
            self._props.set('/Experiments/Defaults/flip_mirror_ttl', True)
            self._props.set('/Experiments/Defaults/photon_shutter', True)
        else:
            self._props.set('/Experiments/Defaults/photon_shutter', False)
            self._props.set('/Experiments/Defaults/flip_mirror_ttl', False)