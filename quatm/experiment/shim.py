"""Compatibility layer for legacy experiment entry points."""

import sys
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


from quatm.servers import send_info, send_error, send_warning
from quatm.servers.configreader import PROJECT_ROOT
from quatm.experiment import *
import time
import json
import os
from quatm.utils.i18n import tr

class Trap_Shim():
    '''允许“联动”设置阱参数（径向/轴向位置与偏置）。

    把一组“阱参数”（轴向/径向位置与偏置）的换算集中管理：设置某个
    物理量时重算并联动写对应的成对 DAC（如 trap_y1/y2）。构造期间
    （``_initializing``）只更新内部状态、不写硬件，避免打开窗口/Defaults
    面板时把电极初值覆盖掉。
    '''

    def __init__(self, experiment, **kwargs):
        """初始化 Trap_Shim：打开属性库并载入默认阱参数初值。

        Args:
            experiment (Experiment): 所属实验（写 DAC 的目标）。
        """
        self._props = Properties('trap_shim')

        self._experiment = experiment
        # 构造期间禁止硬件写：打开实验窗口 / 构建 Defaults 面板都会实例化
        # Trap_Shim，若构造函数直接写 DAC，硬件会被固定初值覆盖（审计报告
        # 3.1/3.2）。recalculate_* 在该标志下只更新内部状态。
        self._initializing = True

        self.ion_d = 0
        self.ion_d_offs = 0
        self.ion_y = 0
        self.ion_y_offs = 0
        self.ion_ax = 0
        self.ion_ax_offs = 0

        self.ion_d = 0.2885
        self.ion_d_offs = 0.2790
        self.ion_y = -5
        self.ion_y_offs = -5
        self.ion_ax = -0.5948
        self.ion_ax_offs = 8.9052

        # try:
        #     self.ion_d = self._props.get('/Experiments/Defaults/trap_d')
        #     self.ion_d_offs = self._props.get('/Experiments/Defaults/trap_d_offs')
        #     self.ion_y = self._props.get('/Experiments/Defaults/trap_y')
        #     self.ion_y_offs = self._props.get('/Experiments/Defaults/trap_y_offs')
        #     self.ion_ax = self._props.get('/Experiments/Defaults/trap_ax')
        #     self.ion_ax_offs = self._props.get('/Experiments/Defaults/trap_ax_offs')
        # except:
        #     self._props.set('/Experiments/Defaults/trap_d', self.ion_d)
        #     self._props.set('/Experiments/Defaults/trap_d_offs', self.ion_d_offs)
        #     self._props.set('/Experiments/Defaults/trap_y', self.ion_y)
        #     self._props.set('/Experiments/Defaults/trap_y_offs', self.ion_y_offs)
        #     self._props.set('/Experiments/Defaults/trap_ax', self.ion_ax)
        #     self._props.set('/Experiments/Defaults/trap_ax_offs', self.ion_ax_offs)

        self.recalculate_y()
        self.recalculate_d()
        self.recalculate_ax()
        self._initializing = False

    def set_y(self, yposition, **kwargs):
        """设置径向 y 位置并重算成对电极输出。

        Args:
            yposition (float): 目标 y 位置。
        """
        self.ion_y = yposition
        self.recalculate_y()

    def set_y_offs(self, yposition):
        """设置 y 方向偏置并重算。

        Args:
            yposition (float): 偏置值。
        """
        self.ion_y_offs = yposition
        self.recalculate_y()

    def set_d(self, pos, **kwargs):
        """设置对角(diag)位置并重算。

        Args:
            pos (float): 目标位置。
        """
        self.ion_d = pos
        self.recalculate_d()

    def set_d_offs(self, pos, **kwargs):
        """设置对角偏置并重算。

        Args:
            pos (float): 偏置值。
        """
        self.ion_d_offs = pos
        self.recalculate_d()

    def set_ax(self, yposition, **kwargs):
        """设置轴向(ax)位置并重算。

        Args:
            yposition (float): 目标轴向位置。
        """
        self.ion_ax = yposition
        self.recalculate_ax()

    def set_ax_offs(self, yposition):
        """设置轴向偏置并重算。

        Args:
            yposition (float): 偏置值。
        """
        self.ion_ax_offs = yposition
        self.recalculate_ax()

    @staticmethod
    def checkvalue(v, name, minv, maxv):
        """把值钳制到 [minv, maxv]，越界时发送错误并返回边界值。

        Args:
            v (float): 待钳制值。
            name (str): 参数名（用于错误信息）。
            minv (float): 下限。
            maxv (float): 上限。

        Returns:
            float: 钳制后的值。
        """
        if v > maxv:
            send_error(tr('[Trap Shim] ') + name + ' value exceeding {0:} V, putting {0:} V instead'.format(maxv))
            v = maxv
        if v < minv:
            send_error(tr('[Trap Shim] ') + name + ' value exceeding {0:} V, putting {0:} V instead'.format(minv))
            v = minv
        return v

    @staticmethod
    def shim(shim_name, setval, offset):
        """差分 shim：在中心偏置 ``offset`` 两侧各加/减 ``setval``。

        返回 ``(offset + setval, offset - setval)``（均被钳制到 ±10 V），
        供 stray-field 检查等对称驱动一对 shim 电极。

        Args:
            shim_name (str): 参数名（错误信息用）。
            setval (float): 差分幅度。
            offset (float): 中心偏置。

        Returns:
            tuple[float, float]: 一对电极设定值 ``(set1, set2)``。
        """
        set1 = Trap_Shim.checkvalue(offset + setval, name=shim_name, minv=-10, maxv=10)
        set2 = Trap_Shim.checkvalue(offset - setval, name=shim_name, minv=-10, maxv=10)
        return set1, set2

    def recalculate_y(self):
        """按 ion_y 重算成对 y 电极并写 trap_y1/trap_y2（初始化期只算不写）。"""
        # bottom = self.checkvalue(-self.ion_y + self.ion_y_offs, 'bottom', -10, 10)
        # self._experiment.trap_y1 = top
        # self._experiment.trap_y2 = bottom
        # print('top', top, 'bottom', bottom)

        top = self.checkvalue(self.ion_y, 'top', -30, 30)
        bottom = 0
        if not self._initializing:
            self._experiment.trap_y1 = top
            self._experiment.trap_y2 = bottom

    def recalculate_d(self):
        """按 ion_d(+offs) 重算对角电极并写 trap_diag1/trap_diag2。"""
        d1 = self.checkvalue(self.ion_d + self.ion_d_offs, 'diag1', -10, 10)
        d2 = self.checkvalue(-self.ion_d + self.ion_d_offs, 'diag2', -10, 10)
        if not self._initializing:
            self._experiment.trap_diag1 = d1
            self._experiment.trap_diag2 = d2

    def recalculate_ax(self):
        """按 ion_ax(+offs) 重算轴向电极并写 trap_ax1/trap_ax2。"""
        ax_bali = self.checkvalue(self.ion_ax + self.ion_ax_offs, 'ax1', -30, 30)
        ax_bermuda = self.checkvalue(-self.ion_ax + self.ion_ax_offs, 'ax2', -30, 30)
        if not self._initializing:
            self._experiment.trap_ax1 = ax_bali
            self._experiment.trap_ax2 = ax_bermuda

    def save_paras(self, save=False):
        """把当前阱参数（含 Defaults 各值）追加记录到
        ``configuration/trap_paras.json``。

        Args:
            save (bool): True 才真正落盘（由 UI 开关驱动）；False 仅复位开关。
        """
        filename = PROJECT_ROOT / 'configuration' / 'trap_paras.json'
        if save:
            record = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'rf': self._props.get('/Experiments/Defaults/rf', 4),
                'trap_d': self._props.get('/Experiments/Defaults/trap_d'),
                'trap_d_offs': self._props.get('/Experiments/Defaults/trap_d_offs'),
                'trap_y': self._props.get('/Experiments/Defaults/trap_y'),
                'trap_ax': self._props.get('/Experiments/Defaults/trap_ax'),
                'trap_ax_offs': self._props.get('/Experiments/Defaults/trap_ax_offs'),
                'verdi_pi_x': self._props.get('/Experiments/Defaults/verdi_pi_x'),
                'verdi_pi_y': self._props.get('/Experiments/Defaults/verdi_pi_y'),
                'mephisto_pi_x': self._props.get('/Experiments/Defaults/mephisto_pi_x'),
                'mephisto_pi_y': self._props.get('/Experiments/Defaults/mephisto_pi_y'),
            }
            # 处理文件写入
            try:
                # 如果文件不存在，创建并写入第一个记录（带数组括号）
                if not os.path.exists(filename):
                    with open(filename, 'w') as f:
                        json.dump([record], f, indent=4)
                    send_info(f"[Trap Shim] Created new log file: {filename}")
                else:
                    # 文件存在时，读取全部内容，追加新记录，再写回
                    with open(filename, 'r+') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError:
                            data = []  # 如果文件内容损坏，重新开始

                        if not isinstance(data, list):
                            data = [data]  # 如果文件不是数组，转换为数组

                        data.append(record)
                        f.seek(0)
                        json.dump(data, f, indent=4)
                        f.truncate()

                    send_info(f"[Trap Shim] Appended new record to: {filename}")

            except Exception as e:
                print(f"Error saving log: {str(e)}")
                # 创建错误备份
                backup_name = f"{filename}.error_{time.strftime('%Y%m%d_%H%M%S')}"
                os.rename(filename, backup_name)
                send_info(f"[Trap Shim] Created backup: {backup_name}")
                # 重试
                self.save_paras_log(filename)

            save = False
            self._props.set('/Experiments/Defaults/save_paras', False)


    def save_old_paras(self, save=False):
        """把旧版阱参数（成对 DAC 设定值）追加记录到
        ``configuration/trap_old_paras.json``。

        Args:
            save (bool): True 才真正落盘；False 仅复位开关。
        """
        filename = PROJECT_ROOT / 'configuration' / 'trap_old_paras.json'
        if save:
            record = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'rf': self._props.get('/Experiments/Defaults/rf', 4),
                'trap_diag1': self._props.get('/Experiments/Defaults/trap_diag1'),
                'trap_diag2': self._props.get('/Experiments/Defaults/trap_diag2'),
                'trap_y1': self._props.get('/Experiments/Defaults/trap_y1'),
                'trap_y2': self._props.get('/Experiments/Defaults/trap_y2'),
                'trap_ax1': self._props.get('/Experiments/Defaults/trap_ax1'),
                'trap_ax2': self._props.get('/Experiments/Defaults/trap_ax2'),
                'verdi_pi_x': self._props.get('/Experiments/Defaults/verdi_pi_x'),
                'verdi_pi_y': self._props.get('/Experiments/Defaults/verdi_pi_y'),
                'mephisto_pi_x': self._props.get('/Experiments/Defaults/mephisto_pi_x'),
                'mephisto_pi_y': self._props.get('/Experiments/Defaults/mephisto_pi_y'),
            }
            # 处理文件写入
            try:
                # 如果文件不存在，创建并写入第一个记录（带数组括号）
                if not os.path.exists(filename):
                    with open(filename, 'w') as f:
                        json.dump([record], f, indent=4)
                    send_info(f"[Trap Shim] Created new log file: {filename}")
                else:
                    # 文件存在时，读取全部内容，追加新记录，再写回
                    with open(filename, 'r+') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError:
                            data = []  # 如果文件内容损坏，重新开始

                        if not isinstance(data, list):
                            data = [data]  # 如果文件不是数组，转换为数组

                        data.append(record)
                        f.seek(0)
                        json.dump(data, f, indent=4)
                        f.truncate()

                    send_info(f"[Trap Shim] Appended new record to: {filename}")

            except Exception as e:
                print(f"Error saving log: {str(e)}")
                # 创建错误备份
                backup_name = f"{filename}.error_{time.strftime('%Y%m%d_%H%M%S')}"
                os.rename(filename, backup_name)
                send_info(f"[Trap Shim] Created backup: {backup_name}")
                # 重试
                self.save_paras_log(filename)

            save = False
            self._props.set('/Experiments/Defaults/save_old_paras', False)