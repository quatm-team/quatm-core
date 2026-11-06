"""兼容模块：历史实验脚本使用 ``from quatm.experiment.trap_shim import Trap_Shim``。

``Trap_Shim`` 的实现位于 :mod:`quatm.experiment.shim`（原模块名）。
本模块保持旧导入路径可用，避免逐一修改各用户项目中的实验脚本。
"""

from .shim import Trap_Shim

__all__ = ['Trap_Shim']
