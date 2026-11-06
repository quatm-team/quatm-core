'''quatm 的属性（Attribute）概念。

实验通过“给属性赋值”来标准化、整洁地控制参数：不用调用函数，而是写
``self.Li_Mot_Power = 3E-3  # Watt`` 或 ``self.image_detuning = 4.5E6  # Hz``
这类语句。

设备种类很多，有通用型 :class:`.genericAttr`，也有更专门的
:class:`.DAC`（模拟输出）、:class:`.TTLOut`（数字输出）。除 TTL 外属性都以
浮点数为输入，并基于 :class:`.AttributeBaseClass`——后者在创建时提供
minval/maxval（限制允许的输入范围）与 multiplier/display_unit（控制 GUI
里的显示方式）等通用选项。
'''


import traceback
import sys
import os
import threading
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

# 诊断消息开关：属性“构造/写入”原本每次都会 send_debug（一个实验窗口有几十上百
# 个属性 → 每次发送要加锁 + ZMQ + 时间格式化，Windows 上累积可达数百 ms；
# 运行时每次 DAC/TTL 写入也会发一条，属于热路径）。默认关闭，排查时设
# QUATM_DEBUG_ATTRIBUTES=1 打开。
_DEBUG_ATTRS = str(os.environ.get('QUATM_DEBUG_ATTRIBUTES', '0')).strip().lower() \
    in ('1', 'true', 'yes', 'on')

from quatm.experiment.experiment import now
from quatm.servers import *
from quatm.servers.configreader import PROJECT_ROOT


def _adwin_driver_error():
    """Resolve the ADwin backend error type lazily.

    The user-project driver ``drivers.adwin.adwin`` is preferred; a repository
    checkout may provide ``quatm.drivers.adwin.adwin``. If neither is available,
    a generic exception type is returned and ``_push_realtime`` still reports
    the failure.

    解析放在函数里是有意的：``quatm.experiment`` 是 AGPL 子包，不能在模块
    导入期依赖 optional driver packages. The same fallback keeps the AGPL
    core usable when no project driver package is installed.

    Returns:
        type: 异常类型（``AdWinDriverError`` 或 ``Exception``）。
    """
    try:
        from drivers.adwin.adwin import AdWinDriverError
        return AdWinDriverError
    except ImportError:
        pass
    try:
        from quatm.drivers.adwin.adwin import AdWinDriverError
        return AdWinDriverError
    except ImportError:
        return Exception


import numpy as np
import os
from quatm.utils.i18n import tr


class ExperimentDeviceError(RuntimeError):
    """设备操作失败（写属性 / 提交实时命令）时抛出的专用异常。

    背景：旧实现里 :meth:`genericAttr.__set__` 与 :func:`_push_realtime` 都是
    ``send_error(...)`` 之后把异常**吞掉**——控制器没在跑/驱动离线时，run()
    照样跑完、任务标 Done，数据却静默错误（最危险的一类故障）。现在统一抛
    本异常，由 :meth:`quatm.experiment.experiment.Experiment._start_run` 走
    ``_cleanup_after_failed_run`` 后原样上抛，队列据此把任务标 Failed。

    继承 ``RuntimeError`` 是有意的：GUI（如 Defaults 面板）原本就捕获
    RuntimeError 做“写入被拒绝”的提示/回滚，新增异常不会打断 Qt 事件。
    """


def _simulation_active(experiment):
    """判断实验当前是否处于模拟运行（Simulate 窗口或全局 /Devices/simulating）。

    模拟运行没有真实硬件，也不产生数据：驱动/实时提交失败不应该把任务标
    Failed（本来就是空跑）。两个来源的判定口径与
    :meth:`quatm.experiment.experiment.Experiment._in_simulation` 保持一致。

    Args:
        experiment (Experiment|None): 实验实例；None 视为真实运行（无法确认
            是模拟时按真实处理，宁可把错误暴露出来）。

    Returns:
        bool: 模拟运行时为 True。
    """
    if experiment is None:
        return False
    try:
        if getattr(experiment, '_simulate_cmd_stack', False):
            return True
        return bool(getattr(experiment, '_simulating', False))
    except Exception:  # noqa: BLE001 - 判定失败按真实运行处理
        return False


# ---------------------------------------------------------------------------
# 设备按名租约（DLL / 本地设备）：厂商 DLL 没有可租的 transport 地址，按
# device_db 设备名做跨进程互斥，防止两个实验窗口/任务同时打开同一台设备。
# 租约本身在 AGPL 的 quatm.servers.device_lease（文件 O_EXCL + 心跳续期）。
# ---------------------------------------------------------------------------
def _device_lease_module():
    """延迟导入设备租约模块（attributes 不能 import 期拉起 servers 的重依赖）。

    Returns:
        module: :mod:`quatm.servers.device_lease`。
    """
    from quatm.servers import device_lease
    return device_lease


def _device_lease_enabled():
    """设备租约是否全局启用（``QUATM_DEVICE_LEASE=0``/configfile 可关）。

    Returns:
        bool: True=构造真实设备前必须拿到按名租约。
    """
    try:
        return bool(_device_lease_module().lease_enabled())
    except Exception:  # noqa: BLE001 - 租约层故障不能挡住设备构造
        return False


class _DeviceNameLease:
    """按名租约 + 心跳续期线程（生命周期由持有者显式 release 管理）。

    只在 ``lease['heartbeat']`` 为真时启动 daemon 续期线程：按名租约的 TTL
    默认 120s（:func:`quatm.servers.device_lease.heartbeat_ttl`），长跑实验
    不会因为没续期被误判陈旧；进程崩溃后最多一个 TTL 就能被回收。

    Args:
        lease (dict): :func:`quatm.servers.device_lease.acquire_name` 的结果。
    """

    def __init__(self, lease):
        """保存租约并按需启动续期线程。"""
        self.lease = lease
        self._stop = threading.Event()
        self._thread = None
        if isinstance(lease, dict) and lease.get('heartbeat'):
            thread = threading.Thread(
                target=self._renew_loop,
                name='quatm-device-name-lease-renew', daemon=True)
            self._thread = thread
            thread.start()

    def _renew_loop(self):
        """后台续期：每 TTL/3 刷新 ``ts``，续期失败（锁被抢/文件没了）即退出。"""
        try:
            module = _device_lease_module()
        except Exception:  # noqa: BLE001 - 租约层不可用：没有可续的租约
            return
        ttl = self.lease.get('ttl_s') or module.heartbeat_ttl()
        interval = module.renew_interval(ttl)
        while not self._stop.wait(interval):
            try:
                ok = module.renew(self.lease)
            except Exception:  # noqa: BLE001 - 续期失败按丢失处理
                ok = False
            if not ok:
                try:
                    send_warning(
                        tr('[Device Lease] lost the lease for %s; the device '
                           'may have been reclaimed by another process.')
                        % self.lease.get('address'))
                except Exception:  # noqa: BLE001 - 告警失败不影响实验
                    pass
                return

    def release(self):
        """停止续期线程并释放租约（幂等；只删本进程+本机+nonce 匹配的）。

        Returns:
            bool: 确实删掉租约时为 True。
        """
        self._stop.set()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(2.0)
        try:
            return bool(_device_lease_module().release_name(self.lease))
        except Exception:  # noqa: BLE001 - 释放失败不能让 close 抛
            return False


def acquire_device_name_lease(device_name, experiment=None, owner='',
                              wait=0.0):
    """为 device_db 设备名取按名心跳租约（供 :class:`_LazyDevice` 构造前调用）。

    语义与 ``open_transport`` 的设备租约一致：

    * 租约全局关闭 / 租约层不可用 → 返回 ``None``（不阻塞设备构造）；
    * 同名设备已被**存活**的其它进程/窗口占用 → 抛
      :class:`ExperimentDeviceError`（信息含 owner/pid/实验名与建议）；
    * 成功 → 返回 :class:`_DeviceNameLease`（调用方负责 ``release()``）。

    Args:
        device_name (str): device_db 里的设备名。
        experiment (Experiment|None): 所属实验（用于 owner/实验名展示）。
        owner (str): 占用者标识；空时用实验名，再退回进程号。
        wait (float): 被占用时最多等待多少秒（默认 0=立刻报错）。

    Returns:
        _DeviceNameLease|None: 成功时的租约句柄；关闭/降级时为 ``None``。

    Raises:
        ExperimentDeviceError: 设备名已被其它存活进程/窗口占用。
    """
    if not device_name:
        return None
    if not _device_lease_enabled():
        return None
    try:
        module = _device_lease_module()
    except Exception:  # noqa: BLE001 - import 失败按“租约不可用”降级
        return None
    try:
        exp_name = str(getattr(experiment, 'expName', '')
                       or getattr(experiment, 'name', '') or '')
    except Exception:  # noqa: BLE001 - 诊断字段拿不到不影响租约
        exp_name = ''
    lease = module.acquire_name(
        device_name, owner=owner or exp_name or str(os.getpid()),
        experiment=exp_name, wait=wait, heartbeat=True)
    if lease is None:
        held = module.is_name_held(device_name)
        if held is not None and not held.get('stale'):
            raise ExperimentDeviceError(
                tr('[Device Lease] device name %s is already in use by %s '
                   '(pid %s, experiment %s); close the experiment window/task '
                   'using it and retry')
                % (device_name, held.get('owner') or '?', held.get('pid'),
                   held.get('experiment') or '-'))
        # 没有可读的现行租约 = 租约层降级（只读盘/权限/陈旧文件清不掉）：
        # acquire 已打印警告，这里继续构造，别让记录问题挡住实验。
        return None
    return _DeviceNameLease(lease)


# 校准曲线缓存：{绝对路径: ((mtime_ns, size), ndarray)}。
# Browser 恢复多个实验窗口时，每个窗口都会重建同名 DAC 属性并重新
# np.loadtxt 同一批 gauge 文件。文件虽小，但“每窗口 × 每个带校准的通道”
# 叠加起来是实打实的磁盘 I/O（在 Windows/杀软扫描或网络盘上尤其明显）。
# 按 (mtime_ns, size) 失效：用户替换校准文件后下一次构建自动重载。
_GAUGE_CACHE = {}


def _load_gaugecurve(gpath):
    """读取（并缓存）DAC 校准曲线。

    旧实现每个 DAC 构造都 ``np.loadtxt(gpath)``；同一文件被多个通道/多个
    窗口使用时重复读盘。这里按路径缓存，并用文件的 (mtime_ns, size) 判断
    是否需要重读（改文件即失效）。读取失败不做任何吞错处理——保持原有
    语义（构建期就该暴露损坏的校准文件），此时缓存不写入。

    Args:
        gpath (str): 校准文件绝对路径。

    Returns:
        numpy.ndarray: 两列数组（物理量与对应 DAC 码）。
    """
    key = None
    try:
        st = os.stat(gpath)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _GAUGE_CACHE.get(gpath)
        if cached is not None and cached[0] == key:
            return cached[1]
    curve = np.loadtxt(gpath)
    if key is not None:
        _GAUGE_CACHE[gpath] = (key, curve)
    return curve


def _require_controller(controller, what):
    """返回可用的实时控制器；没有控制器时抛错并把“怎么办”讲清楚。

    以前 controller 是 None 时，用户看到的是 ``self.controller.push(...)``
    抛出的 ``'NoneType' object has no attribute 'push'`` ——完全看不出“本项目
    还没登记实时控制器（configuration/device_db.py 里没有 controller/adwin
    条目）”。这里 fail fast：不静默丢命令，也不留这种看不懂的报错。

    Args:
        controller: 实验的实时控制器（可能为 None）。
        what (str): 正在写的对象描述（如 ``'DAC 03'`` / ``'TTL 01'``）。

    Returns:
        controller: 原样返回传入的控制器对象。

    Raises:
        RuntimeError: ``controller`` 为 None（项目里没有配置控制器）时。
    """
    if controller is None:
        raise RuntimeError(
            '[Experiment Attributes] %s needs a realtime controller, but this '
            'project has none (configuration/device_db.py has no "controller" '
            'entry, nor the legacy "adwin" one): add one, or run the '
            'experiment in Simulate mode.' % what)
    return controller


def _realtime_submit_failure(error, extra, experiment):
    """上报实时命令提交失败；真实运行再抛 :class:`ExperimentDeviceError`。

    先 ``send_error``（消息监视器/通知里留下完整上下文），随后由
    :func:`_simulation_active` 决定是否上抛：模拟运行只记录、不失败；真实
    运行抛错让 run() 失败并让队列标 Failed（不再“失败仍标 Done”）。

    Args:
        error (BaseException): 控制器/驱动抛出的原始异常。
        extra (dict|None): 结构化附加字段（设备/通道等）。
        experiment (Experiment|None): 所属实验（判定模拟模式用）。

    Raises:
        ExperimentDeviceError: 真实运行中实时命令未能执行时。
    """
    send_error('[Experiment Attributes] %s' % error, extra=extra)
    if _simulation_active(experiment):
        return
    raise ExperimentDeviceError(
        tr('[Experiment Attributes] realtime commands were not executed '
           '(%s): %s') % (type(error).__name__, error)) from error


def _push_realtime(controller, extra=None, experiment=None):
    """把已排入的实时命令提交给控制器；失败时上报并（真实运行时）抛出。

    捕获后端驱动错误（AdWinDriverError，无 adwin 环境退化为通用异常）与
    超长命令表断言（uploadCommands 的 assert 拒绝）。以前这里只
    ``send_error`` 就结束：控制器没在工作时 run() 仍会跑完、任务标 Done，
    数据静默错误。现在真实运行会把失败抛成 :class:`ExperimentDeviceError`，
    让 run() 失败、队列标 Failed；模拟运行（``_simulate_cmd_stack`` /
    ``_simulating``）保持不抛——模拟本来就不提交硬件命令。

    Args:
        controller: 实时控制器对象（实现 start_realtime）。
        extra (dict|None): 结构化附加字段（如 ``{'device': 'xODT_power',
            'channel': 5}``），写入消息库后可按设备/通道过滤。
        experiment (Experiment|None): 所属实验，用于判定模拟模式；None 视为
            真实运行。

    Raises:
        ExperimentDeviceError: 真实运行中控制器拒绝/未能执行实时命令时。
    """
    # 异常类型在调用前解析一次（惰性，避免 AGPL 层 import 期依赖 quatm.drivers）。
    driver_error = _adwin_driver_error()
    try:
        controller.start_realtime(precise=False)
    except AssertionError as e:
        # uploadCommands 对超长命令表用 assert 拒绝：同样不能在真实运行里吞掉
        _realtime_submit_failure(e, extra, experiment)
    except driver_error as e:
        _realtime_submit_failure(e, extra, experiment)

class AttributeBaseClass:
    '''属性基类：提供显示单位/量程/步进等通用元数据。

    所有硬件属性（DAC/TTL/genericAttr 等）都基于本类。创建时可传：

    Args:
        display_unit (str): GUI 中显示的单位字符串（任意文本）。
        multiplier (float): 显示单位→SI 的倍数（如 1E-3 表示 mV→V）。
        maxval (float): 允许的最大值（以显示单位计）。
        minval (float): 允许的最小值（以显示单位计）。
        step (float): 步进。
    '''
    def __init__(self, display_unit='V', maxval=10, minval=-10, multiplier=1, step=0.01, **kwargs):
        '''初始化属性基类并保存通用元数据。

        Args:
            display_unit (str): 显示单位。
            maxval (float): 上限。
            minval (float): 下限。
            multiplier (float): 显示单位→SI 倍数。
            step (float): 步进。
        '''
        self.display_unit = display_unit
        self.maxval = maxval
        self.minval = minval
        self.multiplier = multiplier    #multiply the displayed unit by this factor
        self.step = step

    def __set_name__(self, owner, name):
        """描述符被挂到实验类上时记录属性名（如 ``ba_493_freq``）。

        ``genericAttr.__set__`` 用 ``self.__name__`` 标注命令。
        """
        self.__name__ = name


class CommandAttr(AttributeBaseClass):
    """“命令型”属性：赋值即向某设备发送一条格式化命令字符串。

    使用本属性所在对象需持有 Properties 引用（用于设备寻址）。

    描述符是类属性，但只保存设备名/命令模板与命令客户端，**不保存实验实例
    或控制器绑定**；赋值时的模拟/构建状态从 ``__set__`` 收到的 obj 解析，
    因此同一实验类的两个实例互不影响。

    Args:
        experiment (Experiment): 所属实验（仅为兼容 device_db 的构造约定）。
        device (str): 目标设备名。
        command (str): 命令模板，含 ``{}`` 占位符以插入值。
        defaultval: 默认值。
    """
    def __init__(self, experiment, device='test', command='PASS', defaultval=0.0, **kwargs):
        """构造一个“命令型”属性：赋值即向设备发送一条命令字符串。

        Args:
            experiment (Experiment): 所属实验（仅为兼容构造约定，不保存）。
            device (str): device_db 里的 CommandClient 设备名。
            command (str): 命令模板，应包含 ``{}`` 占位符以插入值
                （经 ``str.format(v)`` 填充）。
            defaultval: 默认值（读取时返回）。
        """
        super().__init__(**kwargs)
        self._comq = CommandClient(device)
        self._default = defaultval
        self._command = command

    def __get__(self, obj, t):
        """读取命令属性恒返回默认值（属性不存储状态）。"""
        return self._default

    def __set__(self, obj, v):
        """赋值即发送命令：模拟/构建期只记录不真发。

        Args:
            obj (Experiment): 实验实例。
            v: 要插入命令模板的值。
        """
        # simulated run (Simulate window / ExperimentParser): record the
        # command ONLY and never send it to the real device
        if obj._simulate_cmd_stack:
            obj._add_simulated_cmd(getattr(self, '__name__', self._command), v)
            return
        if getattr(obj, '_building', False):
            # 构建期间不向真实设备发命令
            return
        self._comq.send(self._command.format(v), {'value': v})

    def valueType(self):
        """返回属性值的 Python 类型。

        Returns:
            type: 默认值的类型。
        """
        return type(self._default)

    def close(self):
        """关闭本属性持有的 ``CommandClient``（幂等；失败只告警不抛）。

        ``CommandAttr`` 是**类属性描述符**，``Experiment.close()`` 遍历实例
        字典时看不到它；每条 CommandAttr 却持有一个 ``CommandClient``（2 个
        ZMQ socket）。反复开合实验窗口时这些连接只能等 pyzmq GC，会把进程
        拖到 “Too many open files”。这里给关闭路径一个显式入口。

        ``CommandClient.close()`` 本身幂等且线程安全（见
        ``GenericClient.close``），所以重复调用（重复 ``Experiment.close()``、
        重新构建描述符后的旧对象）都不会出错；发送/格式化/时序语义不变。
        关闭失败不能阻断其它资源的回收，只发 warning。
        """
        comq = getattr(self, '_comq', None)
        if comq is None:
            return
        try:
            comq.close()
        except Exception as e:  # noqa: BLE001 - 关闭路径不允许抛异常
            name = getattr(self, '__name__', None)
            send_warning(tr('[Experiment Device] closing device %r '
                            'failed: %s: %s')
                         % (name, type(e).__name__, e),
                         extra={'device': name})



class DAC(AttributeBaseClass):
    """DAC（模拟电压输出）属性：赋值即经校准后写控制器。

    见 :meth:`DAC.__init__` 的参数说明；支持 gauge 校准曲线与缺表写保护。
    """
    def __init__(self, experiment, channel=0, defaultval=0.0, gaugefile=None, **kwargs):
        """构造一个 DAC 模拟输出属性。

        若给 ``gaugefile`` 则加载校准文件；文件缺失时进入“写保护”状态
        （真实运行写会被拒绝，除非显式放行）。

        Args:
            experiment (Experiment): 所属实验。
            channel (int): DAC 通道号（0 起）。
            defaultval (float): 默认值（读取时返回）。
            gaugefile (str|None): 相对项目根的校准文件（两列查表）。
            allow_uncalibrated (bool): 经 kwargs 传入——True 时允许缺校准
                文件仍真实下发（谨慎使用）。

        Attributes:
            gaugecurve (numpy.ndarray|None): 已加载校准曲线。
            _gauge_missing (bool): 校准文件是否缺失。
            missing_gaugefile (str|None): 缺失的校准文件绝对路径。
        """
        # 未校准写保护：device_db 该通道 arguments 可写 allow_uncalibrated: True
        # 来显式允许“缺校准表时仍下发”（默认拒绝，防损坏设备）
        self.allow_uncalibrated = bool(kwargs.pop('allow_uncalibrated', False))
        super().__init__(**kwargs)
        # 注意：**不保存** experiment / controller 引用。描述符是类属性
        # （setattr_device 用 setattr(type(self), ...) 挂上去，见
        # experiment.py），同一个实验类开第二个窗口时会重建描述符并覆盖类
        # 属性——若在这里绑定某个实例，两个窗口就会共用“最后一个实例”的
        # controller：窗口 1 的 DAC 写到窗口 2 的控制器上（Browser 多窗口
        # 必现）。写入时从 __set__ 收到的 obj 按实例解析（见 _volt）。
        self.channel = channel
        self.channelname = 'DAC ' + str(channel).zfill(2)
        self._default = defaultval
        self._gauge_missing = False
        self.missing_gaugefile = None
        if gaugefile is not None:
            if _DEBUG_ATTRS:
                send_debug(tr('[Experiment Attributes] loading ')
                           + str(PROJECT_ROOT / gaugefile))
            gpath = str(PROJECT_ROOT / gaugefile)
            if os.path.exists(gpath):
                self.gaugecurve = _load_gaugecurve(gpath)
            else:
                send_error(tr('[Experiment Attributes] gaugefile ') + gaugefile + tr(' does not exist'),
                           extra={'device': getattr(self, '__name__', None),
                                  'channel': channel,
                                  'gaugefile': gaugefile})
                send_error(tr('[Experiment Attributes] no gaugefile loaded for ') + self.channelname
                           + tr(' in ') + getattr(experiment, 'name', '?')
                           + tr(' - write protection enabled: realtime/panel '
                             'writes are blocked until the file is restored '
                             'or writes are explicitly allowed '
                             '(allow_uncalibrated).'),
                           extra={'device': getattr(self, '__name__', None),
                                  'channel': channel,
                                  'gaugefile': gaugefile,
                                  'protection': 'uncalibrated-write-blocked'})
                self.gaugecurve = None
                # 标记 + 记录到 experiment，供 GUI 构建后一次性弹窗提示
                self._gauge_missing = True
                self.missing_gaugefile = gpath
                try:
                    exp_missing = getattr(experiment,
                                          '_missing_gaugefiles', None)
                    if exp_missing is not None:
                        exp_missing.add('%s -> %s'
                                        % (self.channelname, gpath))
                except Exception:
                    pass
        else:
            self.gaugecurve = None

    def __get__(self,obj,t):
        """读取 DAC 属性返回其默认值（属性不存运行时状态）。"""
        #print('CommandAttr get not implemented !!!')
        return self._default

    def __set__(self,obj,v):
        """赋值即输出：先按 gauge 曲线校准，再把换算后的 DAC 码发给控制器。

        模拟期只记录命令；构建期不写硬件；缺校准文件且未放行时拒绝真实写
        （fail-closed，抛 RuntimeError 防止把未校准值按电压下发损坏设备）。

        Args:
            obj (Experiment): 实验实例（提供 _simulate/_building/_props）。
            v (float): 物理量值（显示单位）。

        Raises:
            RuntimeError: 校准文件缺失且未显式放行时；项目没有实时控制器时。
            ExperimentDeviceError: 真实运行中控制器拒绝/未能提交实时命令时。
        """
        # collect all commands in simulation mode (needed for ExperimentParser)
        if self.gaugecurve is not None:
            vold = v
            v = np.interp(v, self.gaugecurve.T[0], self.gaugecurve.T[1])
        if obj._simulate_cmd_stack:
            # simulated run (Simulate window / ExperimentParser): record the
            # command ONLY and never touch the real hardware. Label it with
            # the device_db name (set via __name__ in setattr_device), falling
            # back to the raw channel name.
            obj._add_simulated_cmd(
                getattr(self, '__name__', None) or self.channelname, v)
            return
        if getattr(obj, '_building', False):
            # 实验构建/窗口加载期间绝不写硬件（否则重启 Browser 会用
            # 默认值覆盖真实电极值）
            return
        # ---- 未校准写保护（fail-closed，防静默错发损坏设备）----
        # 通道配置了 gaugefile 但文件缺失：真实运行写入默认拒绝，除非
        # ① device_db 该通道 allow_uncalibrated: True，或
        # ② 属性 /Devices/allow_uncalibrated_dac = True（全局显式放行）。
        if (getattr(self, '_gauge_missing', False)
                and not self.allow_uncalibrated
                and not obj._props.get('/Devices/allow_uncalibrated_dac', False)):
            raise RuntimeError(
                '[Experiment Attributes] DAC %s has no calibration file '
                '(%s): refusing to send an uncalibrated value to the '
                'hardware. Restore the file under configuration/gauge_files/ '
                'or set /Devices/allow_uncalibrated_dac to True to allow it.'
                % (self.channelname,
                   getattr(self, 'missing_gaugefile', '?')))
        self._volt(obj, v)
        if self.channel == 17:
            # print("attributes.py: Control voltage b_homogen = {:.4f} V".format(v))
            pass

    def _volt(self, obj, Voltage):
        """把电压值转换成 DAC 码并 push 到控制器；非实时解析期立即提交。

        Args:
            obj (Experiment): 触发本次写入的实验实例——控制器与解析状态都按
                它解析，描述符本身不持有某个实例的绑定（见 __init__ 说明）。
            Voltage (float): 物理电压值（gauge 已校准）。

        Raises:
            RuntimeError: 项目里没有实时控制器时（见 _require_controller）。
            ExperimentDeviceError: 真实运行中控制器拒绝/未能提交实时命令时。
        """
        ctrl = _require_controller(getattr(obj, 'controller', None),
                                   self.channelname)
        ctrl.push(now(), 'dac_%i' % self.channel, self._dacv(Voltage))
        if not getattr(obj, 'parsing', False):
            if _DEBUG_ATTRS:
                send_debug(tr('[Experiment Attributes] not parsing DAC.py')
                           + repr(getattr(obj, 'parsing', False)))
            _push_realtime(ctrl,
                           extra={'device': getattr(self, '__name__', None),
                                  'channel': self.channel, 'kind': 'dac'},
                           experiment=obj)

    def _dacv(self, v):
        """电压→16 位 DAC 码：±10 V 线性映射到 0..65535。

        Args:
            v (float): 电压（V）。

        Returns:
            int: ``int((v+10)*2**16/20)``（-10V→0，+10V→65535）。
        """
        return int((v + 10) * 2 ** 16 / 20)

class TTLOut(AttributeBaseClass):
    """数字（TTL）输出属性：赋值 True/False 即置位/复位一个数字口。

    Args:
        experiment (Experiment): 所属实验。
        channel (int): 数字口通道号（0 起）。
        logic (str): ``'l'`` 正逻辑 / ``'a'`` 反逻辑。
    """
    def __init__(self, experiment, channel=0, logic='l'):
        """构造 TTL 输出属性。

        Args:
            experiment (Experiment): 所属实验。
            channel (int): 通道号（0 起）。
            logic (str): ``'l'``=正逻辑，``'a'``=反逻辑。
        """
        super().__init__()
        # 与 DAC 相同：**不保存** experiment / controller 引用。描述符是类
        # 属性，同一实验类开第二个窗口会覆盖它；绑定实例就会让两个窗口共用
        # 最后一个实例的控制器。写入时按 __set__ 收到的 obj 解析（见
        # _set_output）。
        self.binchannel = 2**channel
        self.channel = channel
        self.logic = logic
        self.channelname = 'TTL ' + str(channel).zfill(2)

    def _set_output(self, obj, o):
        """把布尔输出写到控制器（按 logic 处理反逻辑）。

        Args:
            obj (Experiment): 触发本次写入的实验实例——控制器与解析状态都按
                它解析（见 __init__ 说明）。
            o (bool): 目标逻辑电平。

        Raises:
            RuntimeError: 项目里没有实时控制器时（见 _require_controller）。
            ExperimentDeviceError: 真实运行中控制器拒绝/未能提交实时命令时。
        """
        ctrl = _require_controller(getattr(obj, 'controller', None),
                                   self.channelname)
        if self.logic == 'a':
            if o:
                ctrl.push(now(), 'clear_bits', self.binchannel)
            else:
                ctrl.push(now(), 'set_bits', self.binchannel)
        else:
            if o:
                ctrl.push(now(), 'set_bits', self.binchannel)
            else:
                ctrl.push(now(), 'clear_bits', self.binchannel)

        if not getattr(obj, 'parsing', False):
            _push_realtime(ctrl,
                           extra={'device': getattr(self, '__name__', None),
                                  'channel': self.channel, 'kind': 'ttl'},
                           experiment=obj)

    def __get__(self, obj, t):
        """读取 TTL 属性恒返回 False（数字输出不存储电平）。"""
        #print('CommandAttr get not implemented !!!')
        return False

    def __set__(self, obj, v):
        """赋值即输出数字电平：模拟期只记录，构建期不写硬件。

        Args:
            obj (Experiment): 实验实例。
            v (bool): 目标电平。

        Raises:
            RuntimeError: 项目里没有实时控制器时。
            ExperimentDeviceError: 真实运行中控制器拒绝/未能提交实时命令时。
        """
        # collect all commands in simulation mode (needed for ExperimentParser)
        if obj._simulate_cmd_stack:
            # simulated run (Simulate window / ExperimentParser): record the
            # command ONLY and never set the real TTL output. Label it with
            # the device_db name (set via __name__ in setattr_device), falling
            # back to the raw channel name.
            obj._add_simulated_cmd(
                getattr(self, '__name__', None) or self.channelname, v)
            return
        if getattr(obj, '_building', False):
            # 构建期间不写硬件（重启 Browser 不得覆盖真实 TTL）
            return
        self._set_output(obj, v)


class genericAttr(AttributeBaseClass):
    """ 把“驱动对象的某函数”做成可赋值属性：赋值即调用该函数。

    经 device_db 的 ``generic_attr`` 条目登记，配合
    :meth:`set_class` / :meth:`set_function` 指定驱动与函数。实时(解析)期
    把赋值压入命令栈延迟执行，非实时期立即调用驱动函数。

    描述符是类属性：**不保存**所属 experiment 实例，驱动对象在赋值时按
    ``__set__`` 收到的 obj 解析（``getattr(obj, self.driver)``），因此同一
    实验类的两个实例互不影响（旧实现保存 self.experiment，会被后建实例覆盖）。

    Args:
        experiment (Experiment): 所属实验（仅为兼容 device_db 的构造约定，
            不再保存引用）。
        function_kwargs (dict): 调用驱动函数时附加的关键字参数。
        attrtype (type): 读取属性时的默认值类型（bool 返回 False，其余 0.0）。

    Example:
        device_db 写法见 :doc:`sdk 手册 06 章`；实验内::

            self.ps_voltage = 1.5   # 触发 mysupply.set_voltage(1.5, ...)
    """

    def __init__(self, experiment, function_kwargs={}, attrtype=float, **kwargs):
        """构造通用属性。

        Args:
            experiment (Experiment): 所属实验（仅为兼容构造约定，不用作
                实例绑定——见类说明）。
            function_kwargs (dict): 调用目标函数时的附加关键字参数。
            attrtype (type): 默认值类型（bool→False，其余→0.0）。
        """
        super().__init__(**kwargs)
        if _DEBUG_ATTRS:
            send_debug(tr('[Experiment Attributes] generic Attribute created'))
        self.kwargs = function_kwargs
        self.attrtype = attrtype

    def __get__(self,obj,t):
        """读取 generic 属性：bool 类型返回 False，其余返回 0.0。"""
        if self.attrtype == bool:
            return False
        return 0.0

    def __set__(self, obj, v):
        """赋值即调用驱动函数；模拟/构建期只记录不调用，实时(解析)期入栈。

        非模拟/非构建/非解析时的驱动调用失败，先 ``send_error`` 记录上下文，
        再抛 :class:`ExperimentDeviceError`：run() 据此失败并由队列标 Failed
        （旧实现 ``except:`` 吞掉异常 → 任务标 Done、数据静默错误）。

        Args:
            obj (Experiment): 实验实例。
            v: 传给驱动函数的值。

        Raises:
            ExperimentDeviceError: 真实运行中驱动函数调用失败时。
        """
        # collect all commands in simulation mode (needed for ExperimentParser)
        name = getattr(self, '__name__', getattr(self, 'function', ''))
        if obj._simulate_cmd_stack:
            obj._add_simulated_cmd(name, v)
            return
        if getattr(obj, '_building', False):
            # 构建期间不调用驱动（不写硬件）
            return
        else:
            if obj.parsing:
                # add this attribute to command list
                obj._add_realtime_cmd(name, v)
                pass
            else:
                driver = getattr(obj, self.driver)
                func = getattr(driver, self.function)
                #print('generic attr',self.kwargs)
                try:
                    func(v, **self.kwargs)
                except Exception as e:  # noqa: BLE001 - 先上报，再抛专用异常
                    traceback.print_exc()
                    send_error(f'[Experiment Attributes] calling driver: {self.driver}, function: {self.function}, {self.kwargs}',
                               extra={'device': self.driver,
                                      'function': self.function,
                                      'channel': self.kwargs.get('channel')})
                    raise ExperimentDeviceError(
                        tr('[Experiment Attributes] driver %s, function %s '
                           'failed: %s: %s')
                        % (self.driver, self.function,
                           type(e).__name__, e)) from e


    def set_class(self, s):
        """设置驱动类（device_db driver 名）——记录到 self.driver。

        Args:
            s (str): 当前实验里驱动对象的名字（需已 setattr_device）。
        """
        self.driver = s

    def set_function(self, s):
        """设置赋值时要调用的驱动方法名——记录到 self.function。

        Args:
            s (str): 驱动上的方法名。
        """
        self.function = s
