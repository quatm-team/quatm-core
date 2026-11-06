"""实时/时序控制器（Realtime Controller）—— 统一契约层。

背景
----
历史上 quatm 的“即时写 + 实时时序”能力直接绑定 ADwin：
``Experiment`` 无条件装配 ``adwin``，``attributes.py`` 的 DAC/TTL 通道直接
调用 ``experiment.adwin.push(...)/start_realtime(...)``。

为了让框架可以对接**任意实时硬件**（ADwin、NI DAQ、FPGA/Zynq、其它板卡），
我们引入 ``controller`` 这一中性概念：

* 实验只与 ``experiment.controller`` 交互（DAC/TTL 属性、``with realtime()``）；
* ``controller`` 是什么硬件，由 **device_db** 里那条引擎条目决定：条目键用
  ``'controller'``（新写法，推荐），旧配置仍可用 ``'adwin'``（自动兼容）；
* 每个硬件厂商各自写一个 “client” 驱动（如 ``drivers.adwin.client``、
  ``drivers.ni.xxxclient``、``drivers.zynq.xxxclient``），实现下面定义的方法
  契约即可被框架使用 —— **无需继承本模块的任何类**（鸭子类型即可，继承
  ``RealtimeController`` 仅为文档与 IDE 提示）。

契约方法
--------
实现者必须提供：

``push(t, channel, value)``
    排入一条“在时刻 t 生效”的通道命令。通道 ``channel`` 是**字符串**，取值
    与语义见下；该命令在 ``start_realtime()`` 被调用时一次性提交执行。
    （历史：ADwin 模型是“预下载命令 → 板卡高速循环按时间戳执行”。）

``start_realtime(repetitions=1, precise=False)``
    触发执行自上次以来 ``push`` 的全部命令；``repetitions`` 为重复次数。
    （NI/FPGA 实现中，“触发”可以是硬件缓冲开始 / 寄存器序列下发等，语义
    由实现者映射；attributes.py 只调用它，不关心具体机制。）

``stop()``
    停止/复位控制器（实验结束清理用）。

``discard()``（可选）
    丢弃已 ``push`` 但尚未 ``start_realtime`` 提交的命令。实时块中途失败
    （如缺校准文件的 DAC 被写保护拒绝）时，框架会调用它以丢弃中止批次的
    残留命令，避免它们被下一次 ``start_realtime`` 误执行。未实现时框架
    跳过该调用（仅靠 stop 清理）。

通道字符串语义（当前约定，供各后端映射）
----------------------------------------
* ``'dac_<n>'`` ：第 n 路模拟输出。**当前约定 value 为 ±10 V / 16 bit 编码**
  （attributes.py 的 ``DAC._dacv`` 负责换算：``int((V+10)*2**16/20)``，0x0000
  ≈ -10 V，0xFFFF ≈ +10 V）。NI/FPGA 后端若位宽/量程不同，可在此换算基础上
  做自己的二次映射（契约层面保留 ±10V/16bit 语义）。
* ``'set_bits'`` / ``'clear_bits'`` ：按位掩码把数字输出口置 1 / 清 0
  （attributes.py 的 ``TTLOut`` 以 ``2**channel`` 作为掩码）。后端把位号映射
  到自己的数字端口。
* 时刻 ``t`` 的单位为秒（相对实验起点），由 attributes/experiment 传入
  （``now()`` / 命令栈时间）。

向后兼容
--------
* device_db 仍写 ``"adwin": {...}`` 的老项目：``Experiment`` 自动回退装配该
  条目为控制器，无需任何改动。
* ``experiment.adwin`` 作为兼容别名保留（指向同一个 controller），旧的第三方
  实验/驱动若直接读 ``experiment.adwin`` 仍然可用（新代码请用 ``controller``）。
* 找不到引擎条目时：这不是故障——空项目（新机器）本来就没有控制器，实验
  可以正常打开/编辑/扫描/模拟，只在用到 DAC/TTL 写入或 ``realtime()`` 时序
  时才需要它；构造函数对这种情况按进程只提示一次 warning（``Experiment``
  的 ``_no_realtime_engine`` 标记该状态），不再记为“设备加载失败”。

参考实现
--------
``quatm/drivers/adwin/client.py``（AdWinClient）即当前参考实现 —— 它已经满足
上述方法集；后续 NI/FPGA 后端照此实现即可。
"""

from abc import ABC, abstractmethod

__all__ = ['RealtimeController', 'find_engine_key', 'ENGINE_KEYS']

#: device_db 中“实时/时序控制器”条目的候选键，按优先级排列。
#: 新配置推荐用 ``'controller'``；旧项目是 ``'adwin'``。
ENGINE_KEYS = ('controller', 'adwin')


def find_engine_key(device_db):
    """返回 device_db 中第一个引擎条目键（'controller' 优先，'adwin' 兼容）。

    Args:
        device_db: 用户项目 device_db.py 的 dict

    Returns:
        键名（str），或 None（项目里没有配置任何控制器）。
    """
    if not isinstance(device_db, dict):
        return None
    for key in ENGINE_KEYS:
        if key in device_db:
            return key
    return None


class RealtimeController(ABC):
    """实时/时序控制器协议（文档与显式实现的参考基类）。

    不强制继承：任何实现了 :meth:`push` / :meth:`start_realtime` /
    :meth:`stop` 的对象（如 ``AdWinClient``）都可作为 ``experiment.controller``
    使用。继承本类可获得方法签名的静态校验与 IDE 提示。
    """

    #: 控制器名称，仅用于日志/面板显示。
    name = 'controller'

    @abstractmethod
    def push(self, t, channel, value):
        """排入一条在时刻 ``t``（秒）生效的通道命令。

        Args:
            t: 相对实验起点的时刻（秒）
            channel: 通道字符串，见模块 docstring（'dac_<n>' / 'set_bits' /
                'clear_bits'）
            value: 命令值（DAC 为 ±10V/16bit 整数编码；TTL 为位掩码）
        """

    @abstractmethod
    def start_realtime(self, repetitions=1, precise=False):
        """提交并触发执行已排入的命令序列。

        Args:
            repetitions: 序列重复次数
            precise: 是否要求硬件精确时序（ADwin 语义：False 可容差）
        """

    @abstractmethod
    def stop(self):
        """停止/复位控制器（实验结束或失败清理时由框架调用，须中止未完成序列）。"""

    def discard(self):
        """丢弃已 push 但尚未提交执行的命令（可选实现，默认无操作）。

        实时块中途失败时框架会调用它，防止中止批次的残留命令被下一次
        ``start_realtime`` 合并执行。有内部命令缓冲的后端（AdWin 的
        ``cmdlist``、软件控制器的 ``_buffer`` 等）应实现为清空缓冲。
        """

    # 便捷：判断一个设备对象能否充当控制器（鸭子类型检查）
    @staticmethod
    def usable(obj):
        """判断对象是否实现了控制器契约（push/start_realtime/stop）。

        Args:
            obj: 待检查对象（如 device_db 装配出的设备）。

        Returns:
            bool: 三方法均可调用时为 True。
        """
        return (obj is not None
                and callable(getattr(obj, 'push', None))
                and callable(getattr(obj, 'start_realtime', None))
                and callable(getattr(obj, 'stop', None)))
