"""Compatibility shim retained for older experiment imports."""

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
import numpy as np
from quatm.utils.i18n import tr

class Trap_Shim():
    '''允许把多个阱参数联合设置（joined setting）的辅助类。'''

    def __init__(self, experiment, **kwargs):
        self._props = Properties('trap_shim')

        self._experiment = experiment

        self.ion_d = 0
        self.ion_d_offs = 0
        self.ion_y = 0
        self.ion_y_offs = 0
        self.ion_ax = 0
        self.ion_ax_offs = 0

        self.ion_d = 0.6430
        self.ion_d_offs = 1.2000
        self.ion_y = -18.1430
        self.ion_y_offs = -5
        self.ion_ax = -0.6530
        self.ion_ax_offs = 12.0000

        # inverse transition matrix for rf=1v from 2025_08_13_shim_projection_measurement/LongerDataSet.ipynb
        self.inv_trans_matrix = np.array([[-1.03704318e-04, -1.61160697e-04,  3.81502349e-06],
                                            [ 1.57568388e-04, -4.00890941e-04, -3.27175601e-05],
                                            [ 7.13970115e-05,  4.57248854e-04, -3.15916465e-05]])
        self.trans_matrix = np.array([[ -9504.41491993,   1151.51609753,  -2340.31370432],
                                        [  -908.95616581,  -1033.46890614,    960.5353095 ],
                                        [-34635.92772509, -12355.72398243, -23040.51276406]])

        self.ion_x_ax_prime = 0
        self.ion_y_ax_prime = 0
        self.ion_x_rad_prime = 0
        posvec = np.array([self.ion_ax, self.ion_d, self.ion_y])
        primeposvec = self.inv_trans_matrix@posvec
        self.ion_x_ax_prime = primeposvec[0]
        self.ion_y_ax_prime = primeposvec[1]
        self.ion_x_rad_prime = primeposvec[2]

        # set maximal changes
        self.maxChange_ax = 30
        self.maxChange_y = 30
        self.maxChange_d = 10
        voltvec = np.array([self.maxChange_ax, self.maxChange_d, self.maxChange_y])
        primevec = self.inv_trans_matrix@voltvec
        self.maxChange_x_ax_prime = primevec[0]
        self.maxChange_y_ax_prime = primevec[1]
        self.maxChange_x_rad_prime = primevec[2]

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
        #self.recalculate_all()

    def set_y(self, yposition, **kwargs):
        self.ion_y = yposition
        self.recalculate_y()

    def set_y_offs(self, yposition):
        self.ion_y_offs = yposition
        self.recalculate_y()

    def set_d(self, pos, **kwargs):
        self.ion_d = pos
        self.recalculate_d()

    def set_d_offs(self, pos, **kwargs):
        self.ion_d_offs = pos
        self.recalculate_d()

    def set_ax(self, yposition, **kwargs):
        self.ion_ax = yposition
        self.recalculate_ax()

    def set_ax_offs(self, yposition):
        self.ion_ax_offs = yposition
        self.recalculate_ax()

    def checkvalue(self, v, name, minv, maxv):
        if v > maxv:
            send_error(tr('[Trap Shim] ') + name + 'value exceeding {0:} V putting {0:} instead'.format(maxv))
            v = maxv
        if v < minv:
            send_error(tr('[Trap Shim] ') + name + 'value exceeding {0:} V putting {0:} instead'.format(minv))
            v = minv
        return v

    def set_ion_x_ax_prime(self, x_ax_prime, **kwargs):
        self.ion_x_ax_prime = x_ax_prime
        self.recalculate_all()

    def set_ion_y_ax_prime(self, y_ax_prime, **kwargs):
        self.ion_y_ax_prime = y_ax_prime
        self.recalculate_all()

    def set_ion_x_rad_prime(self, x_rad_prime, **kwargs):
        self.ion_x_rad_prime = x_rad_prime
        self.recalculate_all()

    def findVoltages(self, change_prime_volt):
        # calculate solution
        voltages = np.linalg.solve(self.trans_matrix, change_prime_volt)
        return voltages

    def recalculate_y_projection(self, changeY):
        self._props.set('/Experiments/Defaults/trap_y', changeY)
        top = self.checkvalue(changeY, 'top', -self.maxChange_ax, self.maxChange_ax)
        bottom = 0
        self._experiment.trap_y1 = top
        self._experiment.trap_y2 = bottom

    def recalculate_d_projection(self, changeD):
        self._props.set('/Experiments/Defaults/trap_d', changeD)
        d1 = self.checkvalue(changeD + self.ion_d_offs, 'diag1', -self.maxChange_d, self.maxChange_d)
        d2 = self.checkvalue(-changeD + self.ion_d_offs, 'diag2', -self.maxChange_d, self.maxChange_d)
        self._experiment.trap_diag1 = d1
        self._experiment.trap_diag2 = d2

    def recalculate_ax_projection(self, changeAx):
        self._props.set('/Experiments/Defaults/trap_ax', changeAx)
        ax_bali = self.checkvalue(changeAx + self.ion_ax_offs, 'ax1', -self.maxChange_y, self.maxChange_y)
        ax_bermuda = self.checkvalue(-changeAx + self.ion_ax_offs, 'ax2', -self.maxChange_y, self.maxChange_y)
        self._experiment.trap_ax1 = ax_bali
        self._experiment.trap_ax2 = ax_bermuda

    def recalculate_all(self):
        changes = np.array([self.ion_x_ax_prime, self.ion_y_ax_prime, self.ion_x_rad_prime])
        voltages = self.findVoltages(changes)
        # look if prime parameters exceed range
        check1 = self.checkvalue(self.ion_x_ax_prime, 'x_ax_prime', -self.maxChange_x_ax_prime, self.maxChange_x_ax_prime)
        check2 = self.checkvalue(self.ion_y_ax_prime, 'y_ax_prime', -self.maxChange_y_ax_prime, self.maxChange_y_ax_prime)
        check3 = self.checkvalue(self.ion_x_rad_prime, 'x_rad_prime', -self.maxChange_x_rad_prime, self.maxChange_x_rad_prime)
        counter = 0
        # readjust prime parameters if needed
        if check1 == self.maxChange_x_ax_prime or check1 == -self.maxChange_x_ax_prime:
            self.ion_x_ax_prime = check1
            counter +=1
        if check2 == self.maxChange_y_ax_prime or check2 == -self.maxChange_y_ax_prime:
            self.ion_y_ax_prime = check2
            counter +=1
        if check3 == self.maxChange_x_rad_prime or check3 == -self.maxChange_x_rad_prime:
            self.ion_x_rad_prime = check3
            counter +=1
        # recalculate matrix if needed
        if counter > 0:
            changes = np.array([self.ion_x_ax_prime, self.ion_y_ax_prime, self.ion_x_rad_prime])
            voltages = self.findVoltages(changes)
        # adujst all "unprimed" voltages
        self.recalculate_y_projection(voltages[2])
        self.recalculate_d_projection(voltages[1])
        self.recalculate_ax_projection(voltages[0])

    def recalculate_y(self):
        # top = self.checkvalue(self.ion_y + self.ion_y_offs, 'top', -10, 10)
        # bottom = self.checkvalue(-self.ion_y + self.ion_y_offs, 'bottom', -10, 10)
        # self._experiment.trap_y1 = top
        # self._experiment.trap_y2 = bottom
        # print('top', top, 'bottom', bottom)

        top = self.checkvalue(self.ion_y, 'top', -30, 30)
        bottom = 0
        self._experiment.trap_y1 = top
        self._experiment.trap_y2 = bottom

    def recalculate_d(self):
        d1 = self.checkvalue(self.ion_d + self.ion_d_offs, 'diag1', -10, 10)
        d2 = self.checkvalue(-self.ion_d + self.ion_d_offs, 'diag2', -10, 10)
        self._experiment.trap_diag1 = d1
        self._experiment.trap_diag2 = d2

    def recalculate_ax(self):
        ax_bali = self.checkvalue(self.ion_ax + self.ion_ax_offs, 'ax1', -30, 30)
        ax_bermuda = self.checkvalue(-self.ion_ax + self.ion_ax_offs, 'ax2', -30, 30)
        self._experiment.trap_ax1 = ax_bali
        self._experiment.trap_ax2 = ax_bermuda

    def save_paras(self, save=False):
        # 获取参数值
        filename = PROJECT_ROOT / 'configuration' / 'trap_paras.json'
        if save:
            record = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'rf': self._props.get('/Experiments/Defaults/rf', 4),
                'trap_d': self._props.get('/Experiments/Defaults/trap_d'),
                'trap_d_offs': self._props.get('/Experiments/Defaults/trap_d_offs'),
                'trap_y': self._props.get('/Experiments/Defaults/trap_y'),
                'trap_ax': self._props.get('/Experiments/Defaults/trap_ax'),
                'trap_ax_offs': self._props.get('/Experiments/Defaults/trap_ax_offs')
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