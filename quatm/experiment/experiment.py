"""Experiment base class, device resolution and run/scan lifecycle."""

import os
import importlib
from importlib import util
import inspect
import time
from contextlib import contextmanager
import sys
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

from quatm.servers import *
from quatm.experiment.simulation_decorator import simulation_class_decorator
from quatm.servers import send_info, send_error, send_warning, send_debug
from quatm.servers.properties import shared_snapshot_source
from quatm.experiment.controller import find_engine_key, RealtimeController
from quatm.utils.i18n import tr


globaltime = 0  # time index for the experimental run
starttime = time.time()  # start of the experimental run
_experiment = None

# device_db 解析缓存：{(绝对路径, mtime_ns, 大小): device_db 字典}。
# Browser 启动会为每个恢复的实验窗口各加载一次 device_db，缓存避免重复 exec。
_DEVICE_DB_CACHE = {}

# “这个项目还没有 configuration/device_db.py”只提示一次（多窗口/多实验否则刷屏）。
_DEVICE_DB_MISSING_REPORTED = False

# “这个项目还没有实时控制器（device_db 里没有 controller/adwin 条目）”只提示
# 一次。空项目本来就没有控制器，那是**正常**的（参数/扫描/队列/Simulate 都不需要
# 它）；但构造函数每个实验窗口都会走一遍，逐窗口报到消息监视器里就是“新机器上
# 一打开模板满屏红字”。
_NO_ENGINE_REPORTED = False

# “实时块没有控制器可用”只提示一次（同上，按进程一次，见 realtime()）。
_REALTIME_NO_ENGINE_REPORTED = False

# 设备类解析缓存：{(已解析模块名, 类名): 类对象}。
# setattr_device 原本每个设备都做一次 inspect.getmembers(module, isclass)——
# 对导入了 numpy 等大模块的驱动约 7 ms/次；一个实验几十个设备、多个恢复窗口
# 重复执行会累积数秒（实测 build()/devices 数秒级）。
_DEVICE_CLASS_CACHE = {}

# resolve_device_module() 结果缓存：{device_db 里的 module 字符串: 实际模块名}
_RESOLVED_MODULE_CACHE = {}

# 惰性驱动实例化开关：默认开启；QUATM_LAZY_DEVICES=0 回退到“窗口打开即构造”。
_LAZY_DEVICES = str(os.environ.get('QUATM_LAZY_DEVICES', '1')).strip().lower() \
    not in ('0', 'false', 'no', 'off')


def _message_count():
    """返回本进程已发出的消息条数（取不到时为 0）。

    Returns:
        int: ``messageclient.sent_count()`` 的值。
    """
    try:
        from quatm.servers.messageclient import sent_count
        return sent_count()
    except Exception:  # noqa: BLE001 - 纯诊断，失败不影响实验
        return 0


def _report_no_engine_once():
    """就“项目里还没有实时控制器”提示一次（每个进程只提示一次）。

    新机器上刚建好的项目 device_db 是空的：没有 DAC/TTL、也没有实时控制器，
    但参数、扫描、队列、Simulate 都不需要控制器，实验照常能开能跑。构造函数
    会被每个实验窗口（含 Browser 启动时恢复的窗口）调用一次，逐窗口报错会把
    消息监视器刷成一片红、提交任务时还会被“有些设备没有加载”弹窗拦一道，而
    真相只是“这个项目还没登记控制器”。

    Returns:
        bool: True 表示本次真的发出了提示（此前没提示过）。
    """
    global _NO_ENGINE_REPORTED
    if _NO_ENGINE_REPORTED:
        return False
    _NO_ENGINE_REPORTED = True
    send_warning(tr('[Experiment] this project configures no realtime '
                    'controller (configuration/device_db.py has no '
                    '"controller" entry, nor the legacy "adwin" one): '
                    'experiments can still be opened, edited, scanned, '
                    'queued and simulated, but TTL/DAC writes and '
                    'realtime() timing need one.'))
    return True


def _report_realtime_no_engine_once():
    """就“实时块没有控制器可用”提示一次（每个进程只提示一次）。

    ``with realtime():`` 里的时序本该由硬件按时间戳执行；项目里没有控制器时
    它退化成普通 Python 计时（仍在跑，但精度没有硬件保证）。这类提示只出现在
    实验真的用到 ``realtime()`` 时，且不重复刷屏。

    Returns:
        bool: True 表示本次真的发出了提示（此前没提示过）。
    """
    global _REALTIME_NO_ENGINE_REPORTED
    if _REALTIME_NO_ENGINE_REPORTED:
        return False
    _REALTIME_NO_ENGINE_REPORTED = True
    send_warning(tr('[Experiment RealTime] this project has no realtime '
                    'controller, so the realtime block runs as plain Python '
                    'timing (no hardware precision). Add a "controller" '
                    'entry in configuration/device_db.py for precise '
                    'timing.'))
    return True


class _BuildProfile:
    """``build()`` 期间的分项耗时累计（用于定位“开窗慢到底慢在哪”）。

    背景：Browser 恢复多个窗口时 ``build()`` 是最大单项，但它在不同机器上
    可能相差十几倍（实测 macOS 0.03 s vs Windows 0.49 s，同一份 device_db）
    ——差异来自每次设备加载里的文件系统/消息发送开销被 OS 放大。只有把
    “模块导入 / 类解析 / 构造 / 参数”拆开才能判断该往哪一步优化。

    只保留总量，不存每个设备的明细；开销为每个设备几次 ``perf_counter``。
    """

    __slots__ = ('n_dev', 't_import', 't_class', 't_ctor', 'n_args', 't_args',
                 'msgs0', 'by_type')

    def __init__(self, msgs0=0):
        """初始化计数器。

        Args:
            msgs0 (int): 起始“已发送消息条数”（用于算差值）。
        """
        self.n_dev = 0
        self.t_import = 0.0
        self.t_class = 0.0
        self.t_ctor = 0.0
        self.n_args = 0
        self.t_args = 0.0
        self.msgs0 = msgs0
        self.by_type = {}  # {device_db 的 type: 构造累计秒}

    def line(self, name, n_fail=0, n_gauge_missing=0):
        """生成一行摘要（供 ``[startup]`` 输出）。

        Args:
            name (str): 实验名。
            n_fail (int): 构建失败的设备数。
            n_gauge_missing (int): 缺校准文件的 DAC 数（每个都会发 2 条
                ``send_error``，在慢机上本身就很贵，且需要用户补文件）。

        Returns:
            str: 摘要文本。
        """
        msgs = _message_count() - self.msgs0
        by_type = ', '.join(
            '%s %.3f' % (k, v) for k, v in
            sorted(self.by_type.items(), key=lambda kv: -kv[1])[:4])
        return ('%s: build 细分 = 模块导入 %.3f + 类解析 %.3f + 构造 %.3f s'
                '（设备 %d 个, 失败 %d, 缺校准 %d）；参数 %d 个 %.3f s；'
                '消息 %d 条；构造按类型: %s'
                % (name, self.t_import, self.t_class, self.t_ctor,
                   self.n_dev, n_fail, n_gauge_missing, self.n_args,
                   self.t_args, msgs, by_type or '-'))


def _wants_simulation(experiment):
    """惰性设备解析时是否应构造“模拟占位对象”而不是真实驱动。

    判定顺序与 ``setattr_device`` 原有关闭惰性的条件一致：

    * ``_simulate_cmd_stack``（Simulate 窗口 / ExperimentParser）永远模拟——
      模拟窗口绝不能构造真实驱动（会真的拨 TTL/DAC）；
    * 否则看 ``_simulating``（``/Devices/simulating``，默认 True）。

    两者都在**解析时**读取（而不是构造惰性代理时），所以构造期不需要 import
    驱动模块，窗口打开后再切换 simulating 也能得到正确结果。

    Args:
        experiment (Experiment): 所属实验（可能没有这两个属性）。

    Returns:
        bool: 应构造模拟占位对象时为 True。
    """
    if getattr(experiment, '_simulate_cmd_stack', False):
        return True
    return bool(getattr(experiment, '_simulating', False))


class _LazyDevice:
    """惰性驱动代理：首次访问属性/调用时才真正构造驱动实例。

    背景：``setattr_device`` 对 ``driver`` 类型原本在实验窗口打开时就构造驱动
    （USB / 串口 / VISA 枚举，单个可能几百毫秒；Browser 恢复多个窗口叠加可达
    数秒，实测 ``build()/devices`` 数秒）。这里把构造推迟到“真正用到”的那一刻：
    没被运行的实验窗口几乎零成本。

    失败语义：构造失败时按原有格式记录错误、写入
    ``experiment._device_failures``，并重新抛出原始异常（首次使用时可见）。

    也用于 ``type: local`` 的设备（如 device_db 的 ``adwin`` 控制器）：这类
    设备构造函数第一个参数是 ``experiment`` 本身，通过 ``prepend`` 传入，
    因此同样可以推迟到首次使用时再构造——AdWinDriver 会建立 ADwin 连接、
    额外再开一个 Properties 客户端并起保活线程，多窗口恢复时会叠加成数秒。

    Args:
        cls: 驱动类；None 表示连模块导入也延迟（首次使用时才 import）。
        args (dict): 传给驱动构造函数的参数。
        device_name (str): device_db 中的设备名（用于错误信息）。
        experiment (Experiment): 所属实验（用于记录失败）。
        prepend: 构造时插在最前面的位置参数（``type: local`` 设备传 experiment）。
        module_name (str|None): 延迟导入时用的模块名（已解析，可直接 import）。
        class_name (str|None): 延迟导入时要取的类名。
        follow_simulation (bool): True 表示本代理由 ``setattr_device`` 在模拟/
            真实模式下自动创建，解析时按**当时的** ``_simulating`` 决定构造
            模拟占位对象还是真实驱动（性能修复 7 的模拟惰性路径）。手工直接
            构造 ``_LazyDevice``（测试/特殊代码）默认 False，保持旧语义：
            解析即真实构造并取设备名租约。
    """

    _INTERNAL = ('_cls', '_args', '_name', '_experiment', '_real', '_resolving',
                 '_prepend', '_module', '_clsname', '_lease', '_closed',
                 '_simflag')

    def __init__(self, cls, args, device_name, experiment, prepend=None,
                 module_name=None, class_name=None, follow_simulation=False):
        """保存构造所需信息，暂不实例化驱动。

        Args:
            cls: 驱动类；None 时改为记住 ``module_name``/``class_name``，
                首次使用时才 import 并取类（驱动模块常在导入期拖入 pyvisa/
                usbtmc 等重库，实测单个 60~110 ms，而窗口打开时未必用得到）。
            args (dict): 构造参数。
            device_name (str): 设备名。
            experiment (Experiment): 所属实验。
            prepend: 构造时插在最前面的位置参数（``local`` 设备为 experiment）。
            module_name (str|None): 延迟导入的模块名（已解析）。
            class_name (str|None): 延迟导入时要取的类名。
            follow_simulation (bool): True 时解析按当时的 ``_simulating``
                选择模拟占位对象/真实驱动（见类 docstring）。
        """
        object.__setattr__(self, '_cls', cls)
        object.__setattr__(self, '_args', dict(args or {}))
        object.__setattr__(self, '_name', device_name)
        object.__setattr__(self, '_experiment', experiment)
        object.__setattr__(self, '_real', None)
        object.__setattr__(self, '_resolving', False)
        object.__setattr__(self, '_prepend', prepend)
        object.__setattr__(self, '_module', module_name)
        object.__setattr__(self, '_clsname', class_name)
        #: 按名设备租约句柄（:class:`quatm.experiment.attributes._DeviceNameLease`）；
        #: 首次真正构造驱动前获取、close() 时释放（幂等）。
        object.__setattr__(self, '_lease', None)
        object.__setattr__(self, '_closed', False)
        object.__setattr__(self, '_simflag', bool(follow_simulation))

    def _resolve_cls(self):
        """返回驱动类，必要时先 import 它的模块（结果缓存到 ``_cls``）。

        Raises:
            ImportError: 模块导入失败。
            AttributeError: 模块里没有该类（如 device_db 的 class 名写错）。
        """
        cls = object.__getattribute__(self, '_cls')
        if cls is not None:
            return cls
        mod_name = object.__getattribute__(self, '_module')
        cls_name = object.__getattribute__(self, '_clsname')
        name = object.__getattribute__(self, '_name')
        try:
            module = sys.modules.get(mod_name)
            if module is None:
                module = importlib.import_module(mod_name)
            cls = getattr(module, cls_name, None)
            if cls is None or not inspect.isclass(cls):
                # 回退：兼容“类名与模块属性名不一致”的旧写法
                for _n, _o in inspect.getmembers(module, inspect.isclass):
                    if _n == cls_name:
                        cls = _o
                        break
            if cls is None or not inspect.isclass(cls):
                raise AttributeError(
                    'class %r not found in module %r (device %r)'
                    % (cls_name, mod_name, name))
        except Exception as e:  # noqa: BLE001 - 按原有格式报错后上抛
            send_error(tr('[Experiment Device] Experiment loading driver '
                       'failed: %s') % name,
                       extra={'device': name, 'module': mod_name,
                              'class': cls_name})
            send_error(tr('[Experiment Device]   error: %s: %s')
                       % (type(e).__name__, e), extra={'device': name})
            try:
                object.__getattribute__(self, '_experiment')\
                    ._record_device_failure(name, e)
            except Exception:  # noqa: BLE001
                pass
            raise
        object.__setattr__(self, '_cls', cls)
        return cls

    def _resolve(self):
        """返回真实驱动实例，必要时先导入模块再构造它（失败会抛出）。

        模拟模式（``_simulating`` / ``_simulate_cmd_stack``）下改为构造“模拟
        占位对象”（见 :func:`_simulated_device`）：真实驱动类仍然需要 import
        才能取到（模拟子类要继承它），但**这个 import 被推迟到首次真正使用**，
        窗口打开时不再为用不到的驱动付出导入代价（模拟项目里一个驱动模块就能
        拉进 matplotlib/pint 十几秒）。标志在解析时读取，构造期不决定。

        Returns:
            驱动实例。
        """
        real = object.__getattribute__(self, '_real')
        if real is not None:
            return real
        if object.__getattribute__(self, '_resolving'):
            raise RuntimeError('recursive resolution of lazy device %r'
                               % object.__getattribute__(self, '_name'))
        object.__setattr__(self, '_resolving', True)
        try:
            cls = self._resolve_cls()
            args = object.__getattribute__(self, '_args')
            exp = object.__getattribute__(self, '_experiment')
            name = object.__getattribute__(self, '_name')
            prepend = object.__getattribute__(self, '_prepend')
            # 模拟标志在**首次使用**时读取（不是构造时）：这样既能把驱动模块
            # 导入推迟到真正用到的那一刻，也能正确处理窗口打开后才切换的
            # /Devices/simulating。模拟分支不取设备名租约——占位对象不碰硬件，
            # 与旧逻辑（模拟模式直接构造模拟子类）一致。
            if object.__getattribute__(self, '_simflag') \
                    and _wants_simulation(exp):
                real = _simulated_device(
                    cls, args,
                    experiment=(prepend if prepend is not None else None))
                object.__setattr__(self, '_real', real)
                return real
            # 顺序固定：先拿按名设备租约（DLL/本地设备没有 transport 地址），
            # 再构造驱动；驱动内部再拿地址租约/总线锁。构造失败必须释放，
            # 不能把设备名锁泄漏出去。
            lease = self._acquire_name_lease(exp, name)
            try:
                if prepend is not None:
                    real = cls(prepend, **args)
                else:
                    real = cls(**args)
            except Exception as e:  # noqa: BLE001 - 按原有格式报错后上抛
                if lease is not None:
                    try:
                        lease.release()
                    except Exception:  # noqa: BLE001 - 释放失败不覆盖原始错误
                        pass
                send_error(tr('[Experiment Device] Experiment loading driver '
                           'failed: %s') % name, extra={'device': name})
                send_error(tr('[Experiment Device]   arguments: %r') % (args,),
                           extra={'device': name})
                send_error(tr('[Experiment Device]   error: %s: %s')
                           % (type(e).__name__, e), extra={'device': name})
                try:
                    exp._record_device_failure(name, e)
                except Exception:  # noqa: BLE001
                    pass
                raise
            object.__setattr__(self, '_lease', lease)
            object.__setattr__(self, '_real', real)
            return real
        finally:
            object.__setattr__(self, '_resolving', False)

    def _acquire_name_lease(self, experiment, name):
        """在真正构造驱动前取按 device_db 设备名的跨进程租约。

        设备名被其它存活进程/窗口占用时抛
        :class:`quatm.experiment.attributes.ExperimentDeviceError`（由调用方
        看到“被谁占用”）；租约层不可用/全局关闭时返回 ``None``（不阻塞）。

        Args:
            experiment (Experiment): 所属实验（用于 owner/实验名展示）。
            name (str): device_db 设备名。

        Returns:
            object|None: 租约句柄（需要 ``release()``）；降级时为 ``None``。

        Raises:
            ExperimentDeviceError: 设备名已被其它存活进程/窗口占用。
        """
        try:
            from quatm.experiment.attributes import (
                ExperimentDeviceError, acquire_device_name_lease)
        except Exception:  # noqa: BLE001 - 租约层 import 失败按降级处理
            return None
        try:
            return acquire_device_name_lease(name, experiment=experiment)
        except ExperimentDeviceError as exc:
            send_error(str(exc), extra={'device': name})
            try:
                experiment._record_device_failure(name, exc)
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception:  # noqa: BLE001 - 租约层故障不能挡住设备构造
            return None

    def is_resolved(self):
        """驱动是否已被真正构造（供测试/诊断）。

        Returns:
            bool: 已构造返回 True。
        """
        return object.__getattribute__(self, '_real') is not None

    def resolve(self):
        """显式构造并返回真实驱动实例（等价于首次使用）。

        Returns:
            驱动实例。
        """
        return self._resolve()

    def close(self):
        """关闭真实设备并释放按名租约（幂等；未构造时不触发构造）。

        顺序与获取相反：先 ``close()`` 真实设备（驱动会关闭 transport，释放
        地址租约），再释放按名租约（``name → address/bus`` 的逆序），避免任何
        交叉等待。未解析的惰性设备直接释放句柄即可，绝不为了关闭去构造驱动。
        """
        if object.__getattribute__(self, '_closed'):
            return
        object.__setattr__(self, '_closed', True)
        real = object.__getattribute__(self, '_real')
        try:
            if real is not None:
                closer = getattr(real, 'close', None)
                if callable(closer):
                    closer()
        finally:
            lease = object.__getattribute__(self, '_lease')
            object.__setattr__(self, '_lease', None)
            if lease is not None:
                try:
                    lease.release()
                except Exception:  # noqa: BLE001 - 释放失败不能让 close 抛
                    pass

    def provides(self, *names):
        """按“类成员”判断是否实现了给定方法，且不触发构造。

        用途：装配实时控制器时框架需要做鸭子类型检查（push/start_realtime/
        stop）。若直接对代理做 ``callable(getattr(proxy, ...))``，会立刻把驱动
        构造出来（``adwin`` 控制器 = ADwin 连接 + 一份 Properties 客户端 +
        保活线程），惰性化就白做了。方法定义在类上时无需构造即可判定；
        只有判定不出来才回退到构造后再检查。

        Args:
            *names (str): 需要存在且可调用的方法名。

        Returns:
            bool: 全部命中返回 True；任一缺失或类上不可调用返回 False
            （调用方应回退到构造后的常规鸭子检查）。
        """
        cls = object.__getattribute__(self, '_cls')
        if cls is None:
            # 延迟导入模式：需要类才能判定——这会触发一次模块导入（不构造
            # 实例）。控制器（adwin）走这条路，而它的模块本来就要在装配时
            # 导入；解析失败时按“不满足契约”处理（错误已由 _resolve_cls 报出）。
            try:
                cls = self._resolve_cls()
            except Exception:  # noqa: BLE001 - 报错已在 _resolve_cls 内完成
                return False
        for name in names:
            try:
                if not callable(getattr(cls, name, None)):
                    return False
            except Exception:  # noqa: BLE001 - 类上描述符取属性失败视为未实现
                return False
        return True

    def __getattr__(self, item):
        """读取未在代理上定义的属性：先构造驱动再转发。

        Args:
            item (str): 属性名。

        Returns:
            驱动实例上的同名属性。
        """
        return getattr(self._resolve(), item)

    def __setattr__(self, item, value):
        """写入属性：内部字段直接设置，其余转发给（必要时构造的）驱动。

        Args:
            item (str): 属性名。
            value: 新值。
        """
        if item in _LazyDevice._INTERNAL:
            object.__setattr__(self, item, value)
        else:
            setattr(self._resolve(), item, value)

    def __delattr__(self, item):
        """删除驱动上的属性。

        Args:
            item (str): 属性名。
        """
        delattr(self._resolve(), item)

    def __call__(self, *args, **kwargs):
        """把调用转发给（必要时构造的）驱动。

        Args:
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            驱动的调用结果。
        """
        return self._resolve()(*args, **kwargs)

    def __enter__(self):
        """把 with 进入转发给驱动。

        Returns:
            驱动 ``__enter__`` 的返回值。
        """
        return self._resolve().__enter__()

    def __exit__(self, *exc):
        """把 with 退出转发给驱动。

        Args:
            *exc: 异常三元组。

        Returns:
            驱动 ``__exit__`` 的返回值。
        """
        return self._resolve().__exit__(*exc)

    def __iter__(self):
        """把迭代转发给驱动。

        Returns:
            驱动上的迭代器。
        """
        return iter(self._resolve())

    def __len__(self):
        """把 len() 转发给驱动。

        Returns:
            驱动 ``__len__`` 的结果。
        """
        return len(self._resolve())

    def __getitem__(self, key):
        """把下标读取转发给驱动。

        Args:
            key: 下标。

        Returns:
            驱动对应元素。
        """
        return self._resolve()[key]

    def __setitem__(self, key, value):
        """把下标写入转发给驱动。

        Args:
            key: 下标。
            value: 新值。
        """
        self._resolve()[key] = value

    def __contains__(self, key):
        """把成员判断转发给驱动。

        Args:
            key: 待判断的键。

        Returns:
            bool: 是否包含。
        """
        return key in self._resolve()

    def __bool__(self):
        """把真值判断转发给驱动（会触发构造）。

        Returns:
            bool: 驱动的真值。
        """
        return bool(self._resolve())

    def __repr__(self):
        """返回可读表示（不触发构造）。

        Returns:
            str: 形如 <lazy device 'name' (resolved=True/False)>。
        """
        return '<lazy device %r (resolved=%s)>' % (
            object.__getattribute__(self, '_name'), self.is_resolved())


def _startup_mark(name, seconds):
    """把一段耗时追加到 Browser 的启动计时器（未启用时零开销）。

    通过 ``sys.modules`` 取计时模块：核心/服务器进程（没有 GUI、更没装
    PySide6）不会因为 ``import quatm.GUI.infra.startup_timing`` 而把 Qt 拉
    进来；Browser 已在运行时，这几条细分计时会直接出现在同一份
    ``[startup]`` 输出里。

    Args:
        name (str): 阶段名。
        seconds (float): 耗时（秒）。
    """
    mod = sys.modules.get('quatm.GUI.infra.startup_timing')
    if mod is None:
        return
    try:
        mod.mark(name, seconds)
    except Exception:  # noqa: BLE001 - 计时绝不影响主流程
        pass


def _engine_usable(engine):
    """判断实时控制器是否满足契约；惰性代理由类成员判定，不触发构造。

    ``RealtimeController.usable`` 会 ``getattr`` 三个方法，对 ``_LazyDevice``
    来说就是立即构造驱动（adwin = ADwin 连接 + 一份 Properties 客户端 +
    保活线程），会让 local 设备的惰性化失效。类上已定义这三个方法时无需
    构造即可判定；否则回退到构造后的常规鸭子检查。

    Args:
        engine: device_db 装配出的控制器对象（可能是 :class:`_LazyDevice`）。

    Returns:
        bool: 可用时为 True。
    """
    if isinstance(engine, _LazyDevice) \
            and engine.provides('push', 'start_realtime', 'stop'):
        return True
    return RealtimeController.usable(engine)


def _simulated_device(cls, arguments, experiment=None):
    """为模拟模式构造一个绝不触碰硬件的“占位设备”实例。

    ``driver`` 与 ``local`` 两种 device_db 条目共用这一条路径：先用一个
    不调用真实构造函数的模拟子类包住 device_db 指向的类，再交给
    :func:`~quatm.experiment.simulation_decorator.simulation_class_decorator`
    做方法级代理（所有实例方法空实现）。因此模拟期既不会打开串口/VISA
    会话，也不会连接板卡、Boot ADwin 或启动保活线程。

    选择“占位对象”而不是“给真实构造函数传 ``simulating=True``”的原因：
    ``simulating`` 只是各驱动自己解释的约定参数，并不由实验框架强制——
    例如 BaLi 的 ``local`` 条目 ``adwin``（AdWinClient → AdWinDriver）读的是
    ``/ADwin/simulating`` 属性，与实验的模拟标志无关；任何驱动漏实现这个
    参数就会真的连上板卡。占位对象把“模拟绝不触硬件”放在框架层保证，且与
    原来的 ``driver`` 分支口径完全一致。

    Args:
        cls (type): device_db 条目指向的真实设备类。
        arguments (dict|None): device_db 里的构造参数。
        experiment (Experiment|None): ``local`` 类型的第一个构造参数（实验
            对象）；``driver`` 类型不传（None）。

    Returns:
        object: 模拟占位设备（方法调用一律空实现）。
    """
    send_info(tr('[Experiment Device] experiment in simulation mode'))

    @simulation_class_decorator
    class simulation_cls(cls):
        def __init__(self, *args, **kwargs):
            send_debug(tr('[Experiment Manager] simulation class created'))

    call_args = () if experiment is None else (experiment,)
    return simulation_cls(*call_args, **(arguments or {}))


def _project_local_only():
    """configfile 的 local_only 开关：true 时 quatm.drivers.* 必须由项目
    drivers/ 副本提供，缺失时报清晰错误，不静默回退到包内驱动。"""
    try:
        from quatm.servers.configreader import ConfigReader
        return bool(ConfigReader.getConfiguration().get('local_only'))
    except Exception:
        return False


def resolve_device_module(module_name):
    """把 device_db 的 ``module`` 条目解析成真正可 import 的模块名。

    项目可覆盖内置驱动：条目写 ``quatm.drivers.xxx`` 时，先查用户项目
    ``drivers/xxx``；项目存在同名文件/目录则用项目版（返回 ``drivers.xxx``），
    否则回退到内置 ``quatm.drivers.xxx``。项目查找直接查文件系统（不依赖
    ``sys.path``），避免 ``quatm/`` 源码树冒充项目驱动包。

    Args:
        module_name (str): device_db 里的 module 值（可为 None/空）。

    Returns:
        str: 真正要 import 的模块名。

    Raises:
        ImportError: ``local_only`` 开启且项目没有对应本地驱动时。
    """
    name = (module_name or '').strip()
    if name.startswith('quatm.drivers.'):
        from quatm.servers.configreader import PROJECT_ROOT
        rest = name[len('quatm.drivers.'):]
        rel = rest.replace('.', os.sep)
        base = PROJECT_ROOT / 'drivers'
        if (base / (rel + '.py')).is_file() or (base / rel).is_dir():
            return 'drivers.' + rest
        if _project_local_only():
            raise ImportError(
                'local_only: no project-local driver for %s (expected %s)'
                % (name, base / (rel + '.py')))
    return name


@contextmanager
def realtime(repetitions=1):
    """实时块上下文管理器：块内赋值只“排命令”，退出时提交给控制器执行。

    项目里没有配置控制器（device_db 里既没有 ``controller`` 也没有旧的
    ``adwin``；新机器上的空项目就是这样）时，实时块**不会失败**，但它退化
    成普通 Python 计时：这里只提示一次“没有硬件精度”，让用户知道拿到的不是
    精确实时序；真正需要控制器的动作（DAC/TTL 写入）会在写入那一刻报错。

    Args:
        repetitions (int): 实时序列重复次数。
    """
    global _experiment
    _experiment.parsing = True
    # 没有控制器可用：提示一次（模拟期不算——Simulate 本来就跑在纯仿真下，
    # 不需要控制器）。放在进入块时而不是 _start_realtime 里，是因为块内直接
    # 写 DAC/TTL 会在中途就依赖控制器。
    if (getattr(_experiment, 'controller', None) is None
            and not getattr(_experiment, '_simulate_cmd_stack', False)
            and not getattr(_experiment, '_simulating', True)):
        _report_realtime_no_engine_once()
    # print('enter context: parsing')
    try:
        yield
    except BaseException:
        # 实时体中途抛异常（典型：缺失校准文件的 DAC 写保护拒绝下发）：
        # 绝不能让模块级 parsing 卡死在 True——否则该进程之后所有实验的
        # delay() 都不再 sleep、非实时 DAC/TTL 写永不提交、实时命令永不
        # 真正执行，表现为实验“假跑/卡住/连续失败”。这里复位解析状态并
        # 丢弃本批尚未提交的实时命令，再原样上抛由上层标记失败。
        if _experiment is not None:
            try:
                _experiment.parsing = False
            except Exception:  # noqa: BLE001
                pass
            try:
                abort = getattr(_experiment, '_abort_realtime', None)
                if abort is not None:
                    abort()
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        # print('leave context: parsing')
        if _experiment is not None:
            _experiment.parsing = False
            _experiment._start_realtime(repetitions=repetitions)
        else:
            send_error(tr('[Experiment RealTime] experiment not found'))


class _UnregisteredAttr:
    """模拟模式下“设备加载失败”时注册的记录型占位属性。

    ``setattr_device`` 在 device_db 条目缺失或模块导入失败时注册本占位符
    （仅在 ``_simulate_cmd_stack`` 下）；对它的赋值会被记入模拟命令栈并以
    设备名标注，保证 Simulate 时序图不丢通道。

    Args:
        name (str): 设备在 device_db 中的名字（用于命令标注与 UI 显示）。

    Attributes:
        display_unit (str): 空字符串。
        minval / maxval / multiplier / step: 缺省元数据（None/1.0 等），供
            Defaults 面板渲染占位。
        attrtype (type): ``float``。
    """

    def __init__(self, name):
        """初始化占位属性。

        Args:
            name (str): device_db 设备名，同时写入 ``__name__`` 供命令标注。
        """
        self.__name__ = name
        # minimal attribute metadata so the Defaults panel can render the
        # entry without crashing (the panel wraps reads in try/except and
        # falls back to a plain label)
        self.display_unit = ''
        self.maxval = None
        self.minval = None
        self.multiplier = 1.0
        self.step = None
        self.attrtype = float

    def __get__(self, obj, t):
        """读取占位属性恒返回 0（模拟期没有真实硬件值）。"""
        return 0

    def __set__(self, obj, v):
        """写入占位属性：把命令记入模拟栈而不是硬件。

        Args:
            obj (Experiment): 实验实例。
            v: 写入值（仅记录，不落硬件）。
        """
        obj._add_simulated_cmd(self.__name__, v)


def _resolve_device_module_name(experiment, deviceName):
    """解析 device_db 条目的模块名（**不 import**）。

    供惰性设备路径使用：延迟导入需要知道“要导入什么”，但不需要真的导入。
    同时把 ``<PROJECT_ROOT>`` 提到 ``sys.path`` 最前——实验文件里的旧
    bootstrap 会往头部插 ``<repo>/quatm``，使核心 ``quatm.utils`` 等正规包
    抢先遮蔽项目同名包（项目 ``utils`` 缺少 ``__init__.py`` 时必现
    ``No module named 'utils.xxx'``）。

    写成模块级函数（而非 Experiment 方法）的原因：``setattr_device`` 也可能
    被 duck-typed 对象调用（测试里的 Stub 只实现 ``_device_db`` 等最小接口），
    那时方法调用会抛 AttributeError——早先版本就因此静默跳过了设备。

    Args:
        experiment (Experiment): 提供 ``_device_db`` 的实验/替身对象。
        deviceName (str): device_db 中的设备名。

    Returns:
        str: 可直接 import 的模块名。
    """
    _root = str(PROJECT_ROOT)
    if sys.path[:1] != [_root]:
        try:
            sys.path.remove(_root)
        except ValueError:
            pass
        sys.path.insert(0, _root)
    _mod_str = experiment._device_db[deviceName]['module']
    device_module = _RESOLVED_MODULE_CACHE.get(_mod_str)
    if device_module is None:
        device_module = resolve_device_module(_mod_str)
        _RESOLVED_MODULE_CACHE[_mod_str] = device_module
    return device_module


class Experiment:
    """所有实验脚本的基类。

    加载实验文件时框架会调用 :meth:`build()`；随后每次运行调用
    :meth:`run()`（扫描时会以不同参数多次调用）。测量/重复/运行的三层结构：

    * measurement（测量）：由若干重复组成；前后调用 start_/end_measurement；
    * sequence/scan（重复或扫描）：由若干 run 组成，前后调用
      start_/end_sequence；
    * run（运行）：一次 :meth:`run()`。

    子类请覆写 :meth:`build`（声明设备与参数）与 :meth:`run`（主逻辑）。

    Attributes:
        _gui_columns (int): 实验窗口参数网格的最小列数（默认 4）。
    """
    _task = PropertyAttribute('/Experiments/_task', 0)  # task number (a task contains multiple repetitions of scans)
    _repetition = PropertyAttribute('/Experiments/_repetition', 0)  # repetition index
    _run = PropertyAttribute('/Experiments/_run', 0)  # run number in a scan (a scan has multiple experiment runs)
    _starttime = PropertyAttribute('/Experiments/_starttime', 0)  # time the experiment starts
    _endtime = PropertyAttribute('/Experiments/_endtime', 0)  # timestamp the experiment has ended
    _totalrun = PropertyAttribute('_totalrun', 0)  # total number of runs of this experiment
    _simulating = PropertyAttribute('/Devices/simulating', True)
    _gui_columns = 4  # columns in the gui

    #: Python 实时回放（:meth:`_run_realtime_stack`）的抖动告警阈值（秒）。
    #: 回放是 ``time.sleep`` 到点再 setattr，唤醒精度受 OS 调度影响（ms 级），
    #: **不是硬实时**；单次 run 内“实际执行 - 目标时刻”的最大滞后超过该值时，
    #: 发一条 warning（每次 run 最多一条）。子类/实例可覆盖（测试注入用）。
    REALTIME_JITTER_WARN_S = 0.002

    def __init__(self, simulate_cmd_stack=False):
        """初始化一个实验实例（通常由 Browser/实验窗口在加载时调用）。

        主要工作：打开实验自己的属性库 ``Experiments/<类名>`` 与数据通道
        ``_dataq``；从用户项目加载 ``device_db``；按 ``controller``/``adwin``
        键装配实时控制器到 ``self.controller``（并保留 ``self.adwin`` 兼容
        别名）；置 ``_building=True``（构建期禁止硬件写）。

        Args:
            simulate_cmd_stack (bool): True 表示“模拟/解析模式”——属性赋值只
                进模拟命令栈，绝不写硬件（Simulate 窗口用）。

        Attributes:
            name (str): 类名。
            _props (Properties): 实验属性库（窗口关闭时经 :meth:`close` 释放）。
            _dataq (DataClient): 数据通道（发 .start/.end）。
            _arguments (list): setattr_argument 登记的参数对象。
            _declared_arguments (dict): setattr_argument 登记的参数名 → 类型对象
                （队列/扫描提交前的 schema 校验用）。
            _attributes (dict): setattr_device 登记的属性名表。
            _device_failures (list): 构建失败的设备 (name, reason)。
            _missing_gaugefiles (set): 缺校准文件的 DAC 描述串。
            _no_realtime_engine (bool): 项目里没有配置控制器条目（正常状态，
                只提示一次 warning；DAC/TTL/realtime() 才需要控制器）。
            controller / adwin: 实时控制器（或 None）。
        """
        self.name = self.__class__.__name__
        # lightweight Properties: no socket monitors (2 threads + 2 sockets) per
        # experiment window - they are pure diagnostics for the servers
        #
        # 快照复用：直接复制进程内已有实例的属性树，省掉与 Property Logger
        # 的 INIT? 往返（服务端每个请求固定 10 ms 延迟 + 序列化整棵树；实测
        # 快照 JSON ~330 KB，Browser 恢复十几个窗口时这些往返串行叠加成秒级）。
        # 复用来源的订阅线程一直在收更新，因此副本与“新做一次 INIT?”等新鲜。
        _t_ctor0 = time.perf_counter()
        try:
            _snapshot_src = shared_snapshot_source()
        except Exception:  # noqa: BLE001 - 快照复用只是优化，失败不影响正确性
            _snapshot_src = None
        self._props = Properties('Experiments/' + self.name, enable_monitors=False,
                                 snapshot_source=_snapshot_src)
        _t_ctor1 = time.perf_counter()
        self._dataq = DataClient('Experiment')
        self._arguments = []
        # 已声明参数的登记表：name -> argtype（setattr_argument 逐个登记）。
        # 队列/扫描提交时用它校验参数名，避免“扫了一个实验根本没声明的参数 →
        # 每次 run 都写同一个值、数据照样落盘、实验读到的还是默认值”这种
        # 静默错误（Tiamo ion_ac_stark.scan_test 扫 P_532 的真实案例）。
        self._declared_arguments = {}
        self._attributes = {}
        self._device_failures = []  # [(name, detail)] build 期间无法创建的设备
        self._missing_gaugefiles = set()  # 构建期发现缺失校准文件的 DAC（供 GUI 一次性提示）
        self._profile = _BuildProfile(_message_count())  # build() 分项诊断
        self._cmd_stack = []  # stack for cmds parsed in parsing mode
        self._simulated_cmd_stack = []  # stack to store simulated commands during the WHOLE experiment
        self._simulate_cmd_stack = simulate_cmd_stack
        # 构建期间禁止硬件写：实验窗口/Defaults 面板在 build() 时会用
        # 代码默认值设置 DAC/TTL 等属性，若直接推送到 ADwin 会把真实
        # 电极值覆盖掉（审计报告 3.2：Browser 崩溃重启后电极值错误，
        # 而存活中的 Dashboard 仍是正确值）。直到首次真正运行
        # （start_measurement/_start_run）才允许写硬件。
        self._building = True
        self.task = None

        self._load_device_db()
        _t_ctor2 = time.perf_counter()
        # ---- 实时/时序控制器装配 ----
        # 控制器承载 DAC/TTL/realtime 的硬件语义（协议见
        # quatm/experiment/controller.py）。device_db 键 'controller'（推荐，
        # 未来可指向 NI/FPGA client）或旧键 'adwin'（自动兼容）都可用；
        # Experiment 统一暴露 self.controller，并保留 self.adwin 兼容别名
        # （deprecated，供旧代码/驱动/面板读取）。
        engine_key = find_engine_key(getattr(self, '_device_db', None))
        #: 项目里没有控制器条目时为 True（正常状态，不是故障；见下面的提示）。
        self._no_realtime_engine = engine_key is None
        if engine_key is not None:
            self.setattr_device(engine_key)
            engine = getattr(self, engine_key, None)
            if engine is None:
                # setattr_device 对 driver 加载失败会记录 failure 并继续
                send_error(tr('[Experiment] realtime controller %r failed to '
                           'assemble: the device could not be loaded '
                           '(see the device error above)') % engine_key,
                           extra={'device': engine_key,
                                  'role': 'controller'})
            elif not _engine_usable(engine):
                send_error(tr('[Experiment] device %r is not a usable realtime '
                           'controller (must implement push/start_realtime/'
                           'stop; see controller.py)') % engine_key,
                           extra={'device': engine_key,
                                  'role': 'controller'})
                engine = None
            self.controller = engine
            # 兼容别名：无论后端是谁，旧代码读 experiment.adwin 仍可用
            if engine is not None and engine_key != 'adwin':
                self.adwin = engine
        else:
            self.controller = None
            # 空项目（新机器）里没有控制器条目是**正常**的，不是故障：参数、
            # 扫描、队列、Simulate 都不需要它。以前这里每个实验窗口都报一条
            # ERROR，还把它记成“设备加载失败”，于是新机器上刚打开模板就看到
            # 一片红字、提交任务又被“有些设备没有加载”弹窗拦一道（用户实测
            # 反馈）。现在收敛为每进程一条 warning；实验真的用到实时/硬件时
            # （DAC/TTL 写入、realtime() 时序）才在那一刻给出可操作的报错。
            _report_no_engine_once()
        _t_ctor3 = time.perf_counter()
        # 构造函数细分计时：Browser 恢复多窗口时 ctor 是最大单项，需要区分
        # “属性客户端 / device_db / 控制器装配”各自占比（复用快照前后对比）。
        _startup_mark('      %s: ctor/props client' % self.name, _t_ctor1 - _t_ctor0)
        _startup_mark('      %s: ctor/device_db' % self.name, _t_ctor2 - _t_ctor1)
        _startup_mark('      %s: ctor/engine' % self.name, _t_ctor3 - _t_ctor2)
        self.parsing = False

    def _record_device_failure(self, deviceName, detail):
        """记录构建失败的设备，供实验窗口在运行前弹窗提醒（不再静默吞掉
        后让后续引用该设备时在别处崩溃）。

        Args:
            deviceName (str): device_db 设备名。
            detail: 失败原因（会被 str() 化保存）。
        """
        try:
            if not hasattr(self, '_device_failures'):
                self._device_failures = []
            self._device_failures.append((deviceName, str(detail)))
        except Exception:
            pass

    def _profile_dev(self, t_start, t_imported=None, t_classed=None, dtype=None,
                     count=True):
        """把一次 ``setattr_device`` 的分段耗时记入 ``self._profile``。

        必须在“成功”与“失败”路径都调用：设备加载失败时往往正是最贵的那次
        （例如模块导入一个拖入 pyvisa 的驱动要 50~80 ms，而它最终因类名不对
        而失败）——只在成功路径记，诊断行就会把这些时间藏起来（实测
        ``tickle_ion: build() 0.084 s`` 而分项全是 0.000，就是这个缺口）。

        Args:
            t_start (float): 进入 setattr_device 的时间戳。
            t_imported (float|None): 模块解析/导入完成的时间戳（None = 失败在导入前）。
            t_classed (float|None): 类解析完成的时间戳（None = 失败在解析前）。
            dtype (str|None): device_db 里的 type（用于按类型分拆）。
            count (bool): 是否计入“已处理设备数”（失败设备不计入）。
        """
        prof = getattr(self, '_profile', None)
        if prof is None:
            return
        now = time.perf_counter()
        t_imported = now if t_imported is None else t_imported
        t_classed = t_imported if t_classed is None else t_classed
        prof.t_import += t_imported - t_start
        prof.t_class += t_classed - t_imported
        ctor = now - t_classed
        prof.t_ctor += ctor
        if count:
            prof.n_dev += 1
        _typ = dtype if dtype is not None else '?'
        prof.by_type[_typ] = prof.by_type.get(_typ, 0.0) + ctor

    def setattr_device(self, deviceName):
        """在实验中登记一个设备/属性（按 device_db 条目创建并挂到类/实例上）。

        根据条目 type 决定挂载方式：attr/generic_attr 挂成类属性（赋值即输出），
        driver/local 挂到实例 ``self.__dict__``。加载失败会记录到
        ``_device_failures``，模拟模式下注册占位属性。

        Args:
            deviceName (str): device_db 中的设备名。
        """

        # find class in module
        _prof = getattr(self, '_profile', None)
        _t_start = time.perf_counter()
        _entry = self._device_db.get(deviceName) or {}
        _dtype = _entry.get('type')
        dev = None
        module = None
        # ---- 惰性类型（driver/local）：连模块导入一起推迟到首次使用 ----
        # 驱动模块常在导入期拖入 pyvisa/usbtmc 等重库（实测单个 60~110 ms）；
        # 而窗口打开时未必用得到（例如 tickle_ion 引用的 rigol 只在运行时用）。
        # 只解析模块名（两次 stat，带缓存），不 import、不解析类。
        #
        # **模拟模式也惰性**：构造期不再 import 驱动模块；首次真正使用时由
        # _LazyDevice._resolve() 按当时的 simulating 标志构造模拟子类/真实驱动
        # （见 _wants_simulation）。Dashboard/Defaults 面板在模拟项目里曾因此
        # eager import 一个驱动就拉进 matplotlib/pint 十几秒（实测 24s 冷启动）。
        #
        # 例外：模拟命令栈（_simulate_cmd_stack，Simulate 窗口/ExperimentParser）
        # 保持 eager——它依赖“build 期导入失败 → 注册记录型占位属性”的既有
        # 语义（时序图通道不能悄悄消失），这块行为本次不改。
        #
        # 解析失败同样回退——由常规路径按原有格式报错并记入 _device_failures。
        if (_LAZY_DEVICES and _dtype in ('driver', 'local')
                and not getattr(self, '_simulate_cmd_stack', False)):
            try:
                _mod = _resolve_device_module_name(self, deviceName)
                _cls_name = self._device_db[deviceName]['class']
            except Exception:  # noqa: BLE001 - 交给常规路径报错
                pass
            else:
                dev = _LazyDevice(
                    None, self._device_db[deviceName].get('arguments'),
                    deviceName, self,
                    prepend=(self if _dtype == 'local' else None),
                    module_name=_mod, class_name=_cls_name,
                    follow_simulation=True)
                self.__dict__[deviceName] = dev
                if _prof is not None:
                    self._profile_dev(_t_start, _t_start, _t_start, _dtype)
                return dev
        try:
            # 模块名解析 + 缓存（导入由下面的 importlib 负责）
            device_module = _resolve_device_module_name(self, deviceName)
            module = sys.modules.get(device_module)
            if module is None:
                module = importlib.import_module(device_module)
        except KeyError as e:
            # 名字不在 device_db 里（实验引用了未定义的设备）。这跟“模块导入
            # 失败”是两件事：以前统一报成 “Error while importing module:
            # 'dds_0'”，让人以为是驱动/导入问题——实际是 device_db 缺条目。
            # 实测 00_Interaction 引用的 dds_0/dds_1/quadrupole_fet_off 就属于
            # 这一类（前两个只在 generic_attr 的命令模板字符串里出现过）。
            send_error(tr('[Experiment Device] device %r is not defined in '
                       'device_db: %s') % (deviceName, e),
                       extra={'device': deviceName,
                              'reason': 'missing-in-device_db'})
            self._record_device_failure(deviceName, 'not defined in device_db')
            if getattr(self, '_simulate_cmd_stack', False):
                if _prof is not None:
                    self._profile_dev(_t_start, dtype=_dtype, count=False)
                return self._register_fallback_attr(deviceName)
            if _prof is not None:
                self._profile_dev(_t_start, dtype=_dtype, count=False)
            return
        except Exception as e:
            # if the device module can't be imported, log it and skip the device
            # instead of crashing the whole experiment window with an
            # UnboundLocalError on 'module' further down
            send_error(f'[Experiment Device] Error while importing module: {e}',
                       extra={'device': deviceName})
            self._record_device_failure(deviceName, e)
            # Simulate 模式：注册一个记录型占位属性，让 run() 里对该设备的
            # 赋值照样进入模拟命令栈——否则设备静默消失、时序图缺通道
            # （真实模式下不占位：属性写无效必须保持可见）。
            if getattr(self, '_simulate_cmd_stack', False):
                if _prof is not None:
                    self._profile_dev(_t_start, dtype=_dtype, count=False)
                return self._register_fallback_attr(deviceName)
            if _prof is not None:
                self._profile_dev(_t_start, dtype=_dtype, count=False)
            return
        _t_imported = time.perf_counter()
        # loop through the module and try to find the class
        # 解析目标类：device_db 的 'class' 就是模块内的属性名，因此直接
        # getattr 等价于旧实现“遍历 inspect.getmembers(module, isclass) 后
        # 按名查找”，却省掉对整个模块命名空间逐个 getattr（驱动模块会导入
        # numpy 等大库，几十个设备累积可观）。取不到（或不是类）时才回退到
        # 旧逻辑，并把结果写入缓存。
        _cls_name = self._device_db[deviceName]['class']
        _cached_cls = _DEVICE_CLASS_CACHE.get((device_module, _cls_name))
        if _cached_cls is not None:
            _members = ((_cls_name, _cached_cls),)
        else:
            _direct = getattr(module, _cls_name, None)
            if _direct is not None and inspect.isclass(_direct):
                _DEVICE_CLASS_CACHE[(device_module, _cls_name)] = _direct
                _members = ((_cls_name, _direct),)
            else:
                _members = tuple(inspect.getmembers(module, inspect.isclass))
                for _n, _o in _members:
                    if _n == _cls_name:
                        _DEVICE_CLASS_CACHE[(device_module, _cls_name)] = _o
                        break
        _t_classed = time.perf_counter()
        for name, obj in _members:
            if name == _cls_name:
                # send a reference to the experiment as first argument in constructor
                # except when type equals driver
                if 'driver' == self._device_db[deviceName]['type']:
                    # `_simulate_cmd_stack` marks the Simulate window /
                    # ExperimentParser: in that mode drivers are ALWAYS
                    # simulated, no matter what the global
                    # /Devices/simulating property says (it is False on
                    # machines that run real experiments). A simulation must
                    # never instantiate real drivers - that would flip real
                    # TTLs / DACs while "simulating".
                    if not self._simulating and not self._simulate_cmd_stack:
                        if _LAZY_DEVICES:
                            # 惰性：窗口打开时只登记，不构造驱动（USB/VISA 枚举
                            # 可占数百毫秒；多窗口恢复叠加数秒）。首次真正使用
                            # 时才构造，失败在首次使用时可见。
                            dev = _LazyDevice(
                                obj,
                                self._device_db[deviceName].get('arguments'),
                                deviceName, self)
                        else:
                            try:
                                # print('loading driver:',deviceName)
                                dev = obj(**self._device_db[deviceName]['arguments'])
                            except Exception as e:
                                # 之前是裸 except 只报“loading driver failed”，
                                # 把 pyvisa/设备的真实错误吞掉（如串号不对、
                                # 设备未枚举），用户无从排查。现在连参数一起暴露。
                                send_error(tr('[Experiment Device] Experiment loading '
                                           'driver failed: %s') % deviceName,
                                           extra={'device': deviceName})
                                send_error(tr('[Experiment Device]   arguments: %r')
                                           % (self._device_db[deviceName].get(
                                               'arguments'),),
                                           extra={'device': deviceName})
                                send_error(tr('[Experiment Device]   error: %s: %s')
                                           % (type(e).__name__, e),
                                           extra={'device': deviceName})
                                import traceback as _tb
                                _tb.print_exc()
                                self._record_device_failure(deviceName, e)
                    else:
                        dev = _simulated_device(
                            obj, self._device_db[deviceName].get('arguments'))
                elif ('local' == self._device_db[deviceName]['type']
                      and (self._simulating or self._simulate_cmd_stack)):
                    # local 设备（第一个参数是 experiment，如 BaLi 的
                    # 'adwin' → AdWinClient、新式 'controller' 条目）在模拟
                    # 模式下同样不得构造真实实例：AdWinClient 的构造函数会
                    # 建 AdWinDriver，后者按 /ADwin/simulating 决定是否真的
                    # 连接板卡/Boot 进程——那个开关与实验的模拟标志无关，
                    # 所以“模拟”时仍可能碰硬件。这里与 driver 分支同口径地
                    # 构造模拟占位对象（见 _simulated_device 的选择理由）。
                    dev = _simulated_device(
                        obj, self._device_db[deviceName].get('arguments'),
                        experiment=self)
                elif (_LAZY_DEVICES
                      and 'local' == self._device_db[deviceName]['type']
                      and not self._simulating
                      and not self._simulate_cmd_stack):
                    # local 设备（第一个参数是 experiment，如 'adwin' 控制器）
                    # 同样惰性化：AdWinDriver 构造会连 ADwin 板、再开一个
                    # Properties 客户端并起保活线程，实测是 Browser 恢复多个
                    # 实验窗口时 Experiment() 构造函数的最大单项开销。
                    dev = _LazyDevice(
                        obj,
                        self._device_db[deviceName].get('arguments'),
                        deviceName, self, prepend=self)
                else:
                    dev = obj(self, **self._device_db[deviceName]['arguments'])
                break
        else:
            send_error(f"[Experiment Device] class {self._device_db[deviceName]['class']} not found",
                       extra={'device': deviceName,
                              'class': self._device_db[deviceName]['class']})
            self._record_device_failure(deviceName, 'class %r not found in module'
                                        % self._device_db[deviceName]['class'])
            if _prof is not None:
                self._profile_dev(_t_start, _t_imported, _t_classed, _dtype,
                                  count=False)
            if getattr(self, '_simulate_cmd_stack', False):
                return self._register_fallback_attr(deviceName)
            return

        if dev is None:
            send_error(f'[Experiment Device] device not found: {deviceName}',
                       extra={'device': deviceName})
            self._record_device_failure(deviceName, 'device not created')
            if getattr(self, '_simulate_cmd_stack', False):
                return self._register_fallback_attr(deviceName)

        elif self._device_db[deviceName]['type'] == 'attr':
            # record the attribute name so DAC/TTLOut.__set__ can label
            # simulated/realtime commands with the device_db name (e.g.
            # 'xODT_power') instead of the raw 'DAC 05' / 'TTL 03'
            dev.__name__ = deviceName
            setattr(type(self), deviceName, dev)
            self._attributes[deviceName] = 0
        elif self._device_db[deviceName]['type'] == 'generic_attr':
            dev.set_class(self._device_db[deviceName]['driver'])
            dev.set_function(self._device_db[deviceName]['function'])
            # record the attribute name so genericAttr.__set__ can label
            # simulated/realtime commands (__set_name__ does NOT fire when the
            # descriptor is setattr'ed onto an already-existing class)
            dev.__name__ = deviceName
            setattr(type(self), deviceName, dev)
            self._attributes[deviceName] = 0
        else:
            self.__dict__[deviceName] = dev

        if _prof is not None:
            self._profile_dev(_t_start, _t_imported, _t_classed, _dtype)

        return dev

    def build_profile_line(self):
        """返回本次 ``build()`` 的分项摘要（供 ``[startup]`` 输出）。

        不调用 ``build()`` 也能调用（返回 ctor+build 期间的累计值），供实验
        窗口在 build 结束后立即输出，便于判断“开窗慢”是导入、类解析还是
        构造/参数登记造成的。

        Returns:
            str|None: 摘要文本；未开启统计时为 None。
        """
        prof = getattr(self, '_profile', None)
        if prof is None:
            return None
        return prof.line(self.name,
                         len(getattr(self, '_device_failures', []) or []),
                         len(getattr(self, '_missing_gaugefiles', []) or []))

    def _register_fallback_attr(self, deviceName):
        """为“加载失败的设备”注册一个记录型占位属性。

        仅在模拟模式（``_simulate_cmd_stack``）下使用：避免因 device_db 条目
        缺失或模块导入失败而在时序图上悄悄丢掉通道（过去会让 Simulate 只
        显示部分设备）。占位属性把赋值记入模拟命令栈（以 device_db 名标注），
        使每个通道仍出现在图上；配置问题仍由上方的 send_error 暴露。

        Args:
            deviceName (str): device_db 设备名。

        Returns:
            _UnregisteredAttr: 占位属性实例。
        """
        dev = _UnregisteredAttr(deviceName)
        setattr(type(self), deviceName, dev)
        self._attributes[deviceName] = 0
        return dev

    def setattr_argument(self, name, argtype=None):
        """创建一个用户可调的实验参数（GUI 里会出现对应控件）。

        Args:
            name (str): 参数名——在实验内以 ``self.<name>`` 访问。
            argtype: 参数类型对象（如 ``NumberValue``、``BoolValue``…）。

        Example::

            self.setattr_argument("Nr_Ablation_Pulses",
                                  NumberValue(ndecimals=0, step=1, value=20))
        """
        prof = getattr(self, '_profile', None)
        if prof is None:
            self.__dict__[name] = argtype.value
        else:
            _t0 = time.perf_counter()
            self.__dict__[name] = argtype.value
            prof.n_args += 1
            prof.t_args += time.perf_counter() - _t0
        argtype.name = name
        self._arguments.append(argtype)
        # 登记“实验声明了哪些参数”（队列/扫描 schema 校验的唯一可靠来源；
        # 参数控件的名字一旦写错这里就没有，提交/运行前会被拒绝而不是静默忽略）
        self._declared_arguments[name] = argtype

    def _start_build(self):
        """触发一次构建（遗留入口：等价于直接调用 :meth:`build`）。"""
        ## unused ???
        self.build()

    def build(self):
        """子类覆写：在加载实验时声明设备与参数。

        在这里用 :meth:`setattr_device` / :meth:`setattr_argument` 声明；
        基类只发一条“未定义 build”的警告。
        """
        send_warning(tr('Experiment: No Build function defined'))

    def _in_simulation(self):
        """当前实例是否处于模拟运行（Simulate 窗口或全局 /Devices/simulating）。

        模拟运行没有任何真实测量数据，数据通道上的 ``.start``/``.end`` 只会让
        DataSummary 生成一条零填充的“幽灵 run”（数据表/H5 里多出一条从没跑过
        的测量）。两个来源都要算：Simulate 窗口用 ``_simulate_cmd_stack``，
        整机模拟用 ``/Devices/simulating``；真机（两者皆假）行为不变。

        Returns:
            bool: 模拟运行时为 True。
        """
        return bool(getattr(self, '_simulate_cmd_stack', False)
                    or self._simulating)

    def _publish_startvalues(self, task):
        """把本次运行开始的元信息发到数据通道（``.start`` 事件）。

        会带上起始时间、任务/run/重复编号、实验名与全部参数当前值，供
        数据分析/摘要侧定位本次测量。模拟运行不发（见 :meth:`_in_simulation`）。

        Args:
            task (int|None): 任务号。
        """
        if self._in_simulation():
            return
        if not hasattr(self, 'expName'):
            self.expName = self._name
        info = {'_starttime': self._starttime, '_run': self._run, '_repetition': self._repetition, '_task': task,
                '_name': self._name, '_expName': self.expName}
        arguments = dict((arg.name, self.__dict__[arg.name]) for arg in self._arguments)
        info.update(arguments)
        self._dataq.send(info, channel='.start')

    def _publish_endvalues(self):
        """在数据通道发 ``.end`` 事件（含结束时间戳），标记一次运行结束。

        模拟运行不发（见 :meth:`_in_simulation`）：模拟没有对应的 ``.start``，
        发 ``.end`` 同样会污染 DataSummary 的 run 记录。
        """
        if self._in_simulation():
            return
        info = {'_endtime': self._endtime}
        self._dataq.send(info, channel='.end')

    def set_ack_context(self, token):
        """设置/清除本实验进程数据消息的保存回执上下文（Manager 调用）。

        数据消息会带上 ``_ack={'token':..., 'seq':...}``（见
        :func:`quatm.servers.clients.set_ack_context`），Data Manager 据此统计
        “本任务的消息收到了哪些”；旧 Data Manager 会忽略该键。

        Args:
            token (str|None): 任务 token；None 表示停止标记。

        Returns:
            int: 起始序号。
        """
        try:
            from quatm.servers.clients import set_ack_context
            return set_ack_context(token)
        except Exception:  # noqa: BLE001 - 老环境/导入失败时静默降级
            return 0

    def set_save_ack_hook(self, hook):
        """注册“真实 run 的 ``.end`` 发出后”的回调（Manager 等待保存回执用）。

        回调签名 ``hook(expected_seq)``：expected_seq 是本 run 发出的带 ack 标记
        消息的最大序号；回调在实验线程里同步执行，必须自己限制等待时长（Manager
        用 ``save_ack_timeout_s``）。回调异常不影响实验收尾。

        Args:
            hook (callable|None): 回调；None 表示清除。
        """
        self._save_ack_hook = hook

    def _notify_save_ack(self, failed=False):
        """在 ``.end`` 发出后通知保存回执回调（per-run 落盘确认）。

        失败 run 不等待（任务本来就会标 Failed）；模拟 run 不发标记也不等。
        序号取本进程 data clients 的全局 ack 计数（无数据 run 时两者都是 0，
        快速通过）。

        Args:
            failed (bool): 本次 run 是否已失败。
        """
        if failed or self._in_simulation():
            return
        hook = getattr(self, '_save_ack_hook', None)
        if hook is None:
            return
        try:
            from quatm.servers.clients import ack_context_seq
            expected = ack_context_seq()
        except Exception:  # noqa: BLE001 - 取不到序号按 0 处理（无数据 run）
            expected = 0
        try:
            hook(expected)
        except Exception:  # noqa: BLE001 - 回调问题不影响实验收尾
            pass

    def _start_run(self, task=None):
        """启动一次真实运行：放行硬件写、发 ``.start``、执行 :meth:`run`。

        框架在队列 worker 线程里逐 run 调用。失败时先做框架级清理
        （:meth:`_cleanup_after_failed_run`：复位解析状态/清命令栈/停控制器）
        再上抛；成功与否都会在 finally 里记录结束时间并发 ``.end``。

        模拟运行（Simulate 窗口 / 全局 /Devices/simulating）也走这个入口，
        但**不发** ``.start``/``.end``：模拟没有真实测量数据，发了只会在
        DataSummary 里留下一条零填充的幽灵 run（见 :meth:`_in_simulation`）。

        Args:
            task (int|None): 任务号；为 None 时从属性
                ``/Experiments/_task`` 读取。
        """
        # change to __enter__ ???
        global globaltime
        global starttime
        global _experiment
        _experiment = self
        # 进入真实运行：允许属性写直达硬件（保险丝，通常已被
        # start_measurement 解除）
        self._building = False
        self._name = self.name
        self._starttime = time.time()
        run = self._props.get('_totalrun', 0)
        self._props.set('_totalrun', run + 1)
        if task is None:
            task = self._props.get('/Experiments/_task')
        globaltime = 0
        starttime = time.time()
        self._publish_startvalues(task)
        run_failed = False
        try:
            self.run()
        except BaseException:
            # 运行失败：先做框架级清理（复位 parsing / 清命令栈 / 停控制器），
            # 再原样上抛让队列标记 Failed——否则失败会污染整个进程的实时
            # 行为，后续实验卡住/连续失败（见 _cleanup_after_failed_run）。
            run_failed = True
            try:
                self._cleanup_after_failed_run()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            self._endtime = time.time()
            self._publish_endvalues()
            # .end 已发出：通知 Manager 做 per-run 保存回执等待（数据健康）。
            # 任何问题都只影响“确认”本身，绝不影响实验收尾；用 getattr 兼容
            # 测试/旧版里以 duck-typing 方式复用 _start_run 的对象。
            notify = getattr(self, '_notify_save_ack', None)
            if notify is not None:
                try:
                    notify(failed=run_failed)
                except Exception:  # noqa: BLE001 - 回执通知失败不影响实验
                    pass

    def start_measurement(self):
        """子类可覆写：每次测量(任务)开始时调用。

        适合做耗时初始化/硬件设置；框架在真实运行开始时解除构建期写保护。
        """
        # 首次进入真实测量生命周期：允许属性写直达硬件
        self._building = False
        pass

    def start_sequence(self):
        """子类可覆写：每个重复/序列(sequence)开始前调用（扫描前钩子）。"""
        pass

    def run(self):
        """用户子类覆写的单次运行主逻辑。

        基类为空实现（pass）。框架按扫描/重复在每次运行时调用本方法；
        在里头用属性赋值（如 ``self.rf = ...``）、:func:`delay`、
        ``with realtime():`` 描述一次测量。

        Returns:
            None: 无返回值；失败请抛异常让任务标为 Failed。
        """
        pass

    def end_sequence(self):
        """子类可覆写：每个重复/序列(sequence)结束后调用（扫描后钩子）。"""
        pass

    def end_measurement(self):
        """子类可覆写：最后一个重复（sequence）之后调用，作为任务收尾钩子。"""
        pass

    def close(self):
        """释放实验持有的设备与 IPC 连接（窗口关闭时调用，防止句柄/fd 泄漏）。

        每个 Properties 实例持有 2+ 个 ZMQ socket 与循环线程且永不自行
        关闭；反复开合实验窗口会把 GUI 进程拖到 "Too many open files"。

        **设备也要在这里关**：以前只关 Properties/DataClient，设备一个都不关
        ——VISA/串口句柄、驱动后台线程（ADwin 保活线程、AWG 板卡等）只能等
        GC，窗口反复开关后设备被占用 / fd 耗尽。现在遍历 ``self.__dict__``
        对**已解析**设备调用其 ``close()``（没有 close 的跳过；单个失败只发
        warning 并继续，一个坏驱动不能让其余句柄泄漏）。未解析的惰性驱动
        （:class:`_LazyDevice`）调用其 ``close()``：只释放可能已持有的按名
        租约句柄，绝不会为了关它而触发一次 USB/VISA 构造。

        顺序：设备先关（驱动 close 会停序列、释放板卡/USB/串口），再用
        ``properties.close_all`` 并行关各 Properties，最后关 DataClient。

        性能：close() 常在 GUI 线程（窗口 closeEvent）调用。一个实验持有的
        多个 Properties 若逐个 close()，GUI 要串行等待 N×RCVTIMEO
        （“关窗卡顿”）。这里先全部收集、去重，再用 properties.close_all
        并行关闭（先统一唤醒线程→统一 join→统一关 socket，总耗时 ≈
        单个 RCVTIMEO ≈ 0.2 s），且保持 crash 安全顺序（线程退出后才关
        socket，避免 libzmq signaler 的 Bad file descriptor / 断言 abort）。

        **类属性描述符也要关**：DAC/TTL/`CommandAttr` 等属性是 ``setattr_device``
        用 ``setattr(type(self), ...)`` 挂到**类**上的描述符，不在
        ``vars(self)`` 里；其中 `CommandAttr` 每条持有一个 `CommandClient`
        （2 个 ZMQ socket）。这里在设备之后、Properties 之前，按
        ``type(self).__mro__`` 遍历各类的 ``__dict__``，对**已经存在**且实现了
        ``close()`` 的属性对象逐个关闭（``id()`` 去重、失败只 warning）。绝不
        为了关闭而触发惰性构造：只处理类字典里现成的对象。

        幂等：窗口关闭路径可能被 closeEvent 与 Browser 清理各调一次；
        重复调用直接返回，不会重复关闭设备/连接。
        """
        if getattr(self, '_closed', False):
            return
        self._closed = True
        # ---- 设备回收（先于 Properties：驱动 close 需要自己的连接/句柄）----
        closed_devices = set()
        for name, value in list(vars(self).items()):
            # 实验自身的属性库/数据通道在下面统一关闭，不当作设备。
            if name in ('_props', '_dataq'):
                continue
            if isinstance(value, _LazyDevice):
                # 走 _LazyDevice.close()：先关真实设备（释放 transport 地址
                # 租约），再释放按名租约；幂等，且不会为未构造的设备触发构造。
                if id(value) in closed_devices:
                    continue
                closed_devices.add(id(value))
                try:
                    value.close()
                except Exception as e:  # noqa: BLE001 - 单个设备失败不阻断
                    send_warning(tr('[Experiment Device] closing device %r '
                                    'failed: %s: %s')
                                 % (name, type(e).__name__, e),
                                 extra={'device': name})
                continue
            if value is None or id(value) in closed_devices:
                continue
            try:
                closer = getattr(value, 'close', None)
            except Exception:  # noqa: BLE001 - 取属性失败就当没有 close
                continue
            if not callable(closer):
                continue
            closed_devices.add(id(value))
            try:
                closer()
            except Exception as e:  # noqa: BLE001 - 单个设备失败不阻断其余回收
                send_warning(tr('[Experiment Device] closing device %r '
                                'failed: %s: %s')
                             % (name, type(e).__name__, e),
                             extra={'device': name})
        # ---- 类属性描述符回收（CommandAttr 持有 CommandClient）----
        # 只处理类字典里已有的对象：绝不能为了 close 去 getattr 惰性设备。
        try:
            from quatm.experiment.attributes import AttributeBaseClass as _AttrBase
        except Exception:  # noqa: BLE001 - 导入失败就跳过这一段回收
            _AttrBase = None
        if _AttrBase is not None:
            closed_attrs = set()
            for klass in type(self).__mro__:
                for attr_name, attr_value in list(vars(klass).items()):
                    if not isinstance(attr_value, _AttrBase):
                        continue
                    if id(attr_value) in closed_attrs:
                        continue
                    try:
                        attr_closer = getattr(attr_value, 'close', None)
                    except Exception:  # noqa: BLE001
                        continue
                    if not callable(attr_closer):
                        continue
                    closed_attrs.add(id(attr_value))
                    try:
                        attr_closer()
                    except Exception as e:  # noqa: BLE001 - 单个属性失败不阻断
                        send_warning(
                            tr('[Experiment Device] closing device %r '
                               'failed: %s: %s')
                            % (attr_name, type(e).__name__, e),
                            extra={'device': attr_name})
        try:
            from quatm.servers.properties import close_all as _close_all
        except Exception:  # noqa: BLE001
            _close_all = None
        props = []
        seen = set()

        def _add(p):
            if (p is not None and hasattr(p, 'close')
                    and id(p) not in seen):
                seen.add(id(p))
                props.append(p)

        for value in vars(self).values():
            # 未使用过的惰性驱动：从未构造，也就没有任何连接需要释放——
            # 若在此 getattr('_props') 会触发构造（又白白付一次 USB/VISA
            # 初始化，甚至报错），因此先跳过。
            if isinstance(value, _LazyDevice) and not value.is_resolved():
                continue
            # 设备/驱动内部的 Properties 属性命名不统一：多数用 ``_props``，
            # 也有用 ``props``（如 adwin 驱动）。两种都释放，避免窗口关闭
            # 后驱动仍带着 Properties 的 4 线程 + 4 socket 不回收。
            for _attr in ('_props', 'props'):
                p = getattr(value, _attr, None)
                if p is not None and hasattr(p, 'close'):
                    _add(p)
                    break
        _add(getattr(self, '_props', None))
        if _close_all is not None:
            try:
                _close_all(*props)
            except Exception:  # noqa: BLE001
                pass
        else:
            for p in props:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
        # DataClient 也持有 2 个 ZMQ socket；被窗口/对象长持有时不 GC 就
        # 永远占 fd，必须在这里一并关闭（否则每个泄漏窗口额外 +2 fd）。
        dq = getattr(self, '_dataq', None)
        if dq is not None and hasattr(dq, 'close'):
            try:
                dq.close()
            except Exception:  # noqa: BLE001
                pass

    def _stop_run(self):
        """停止/复位实时控制器（等价 ``controller.stop()``）。"""
        ctrl = getattr(self, 'controller', None)
        if ctrl is not None:
            ctrl.stop()

    def _abort_realtime(self):
        """中止“尚未提交”的实时批（realtime 块中途失败时由 realtime()
        上下文管理器调用）。

        复位解析状态并丢弃控制器里已 push 但未 start_realtime 的命令——
        否则中止批次的残留命令会被下一次 realtime 合并执行，把旧值在
        错误的时刻下发到硬件（可能损坏设备或让后续实验卡住）。
        """
        try:
            self.parsing = False
        except Exception:  # noqa: BLE001
            pass
        try:
            self._cmd_stack = []
        except Exception:  # noqa: BLE001
            pass
        ctrl = getattr(self, 'controller', None)
        if ctrl is None:
            return
        discard = getattr(ctrl, 'discard', None)
        if discard is not None:
            try:
                discard()
            except Exception:  # noqa: BLE001
                pass

    def _cleanup_after_failed_run(self):
        """运行失败后的框架级清理（_start_run 的 except 分支调用）。

        任何异常退出 run() 都必须：
          * 复位模块级实时解析状态（parsing 卡 True → delay() 不 sleep、
            实时命令永不提交 → 实验“假跑”，表现为后续实验卡住/连续失败）；
          * 清空未执行的命令栈与控制器残留缓冲；
          * 复位控制器（stop），避免板卡停留在 realtime/序列运行中。
        清理本身绝不掩盖原始异常，也不得再次抛错。
        """
        try:
            self.parsing = False
        except Exception:  # noqa: BLE001
            pass
        try:
            self._cmd_stack = []
        except Exception:  # noqa: BLE001
            pass
        ctrl = getattr(self, 'controller', None)
        if ctrl is None:
            return
        for name in ('discard', 'stop'):
            fn = getattr(ctrl, name, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def _load_device_db(self):
        """从用户项目加载 ``device_db.py`` 并存入 ``self._device_db``。

        同时把项目根加入 sys.path，使条目可引用项目本地模块（如
        ``drivers.xxx`` / ``analysis.xxx``）。

        解析结果按 (绝对路径, mtime, 大小) 缓存：Browser 启动会为每个恢复的
        实验窗口各加载一次 device_db，重复 exec 纯属浪费（多窗口时明显）。
        缓存条目在文件被编辑后自动失效。

        容错（新机器上很关键）：

        * **文件不存在**不是错误：全新项目（还没登记设备）里没有任何设备，
          实验照常能打开/运行；以前这里直接抛 ``FileNotFoundError``，于是
          “空项目里打开示例实验”必崩，而且报错完全看不出该做什么；
        * **文件有错**（语法错/没定义 ``device_db``）只报一条清晰错误并使用空表，
          不要把开窗整个弄失败。
        """
        from quatm.servers.configreader import CONFIG_DIR, PROJECT_ROOT
        # make the user project importable, so device_db entries can point at
        # project-local modules (e.g. 'driver.mydevice', 'analysis.myanalysis')
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        path = str(CONFIG_DIR / 'device_db.py')  # lives in the user project
        if not os.path.isfile(path):
            global _DEVICE_DB_MISSING_REPORTED
            if not _DEVICE_DB_MISSING_REPORTED:
                _DEVICE_DB_MISSING_REPORTED = True
                send_warning(tr('[Experiment] no configuration/device_db.py in '
                                'this project yet: no hardware device is '
                                'defined. Run the first-run setup wizard (or '
                                'create the file) to add your devices.'))
            self._device_db = {}
            return
        try:
            st = os.stat(path)
            key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None:
            cached = _DEVICE_DB_CACHE.get(key)
            if cached is not None:
                self._device_db = cached
                return
        try:
            spec = importlib.util.spec_from_file_location('_quatm_device_db',
                                                          path)
            foo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(foo)
            self._device_db = dict(getattr(foo, 'device_db', None) or {})
        except Exception as exc:  # noqa: BLE001 - 坏 device_db 不该弄死开窗
            send_error(tr('[Experiment] configuration/device_db.py could not be '
                          'loaded: %s') % exc)
            self._device_db = {}
            return
        if key is not None:
            _DEVICE_DB_CACHE[key] = self._device_db

    # Realtime related stuff

    def _start_realtime(self, repetitions=1):
        """结束实时解析期：把已缓冲命令提交给控制器并执行。

        由 ``with realtime():`` 的正常出口调用。这里先调
        ``controller.start_realtime()`` —— **硬件实时序列的入口**：控制器
        （Adwin / NI / FPGA 后端）把整批命令一次性下发，由板卡按时序执行
        （µs 级、多通道严格并发；参考 ``quatm/drivers/adwin/client.py`` 的
        ``AdWinClient``）。随后执行 :meth:`_run_realtime_stack`，那只是
        Python 侧的**非硬实时回放**（兼容经命令栈走的 genericAttr 等），
        边界与测量见 ``docs/REALTIME_BOUNDARY.md``。

        Args:
            repetitions (int): 实时序列重复次数。
        """
        ctrl = getattr(self, 'controller', None)
        if ctrl is not None:
            ctrl.start_realtime(repetitions=repetitions)
        self._run_realtime_stack()

    def _run_realtime_stack(self):
        """执行实时命令栈 ``_cmd_stack`` 中的全部命令（Python 侧**非硬实时**回放）。

        按时间戳排序逐条执行：真实运行（非模拟）时先 ``time.sleep`` 到目标时刻
        再 ``setattr``；无论成功/失败都清空命令栈，失败不留半截命令给下一次运行。

        **硬实时边界（重要）**：这不是硬实时调度。``time.sleep`` 的唤醒精度
        取决于 OS 调度与系统负载（典型 ms 级抖动，负载高时更差），本方法只适合
        慢速通道与兼容旧代码。需要 µs 级、多通道严格并发时序时必须走控制器的
        **硬件序列**：``with realtime(): ...`` 结束时由 :meth:`_start_realtime`
        调 ``controller.start_realtime()`` 把整批命令下发给板卡按时序执行
        （参考 ``quatm/drivers/adwin/client.py`` 的 ``AdWinClient``）；细节见
        ``docs/REALTIME_BOUNDARY.md``。

        观测（不改时序语义）：逐条用 ``time.monotonic()`` 量“实际执行时刻 -
        目标时刻”的滞后并取最大值，存到实例属性 ``_realtime_max_lag``（秒）与
        ``_realtime_command_count``（命令条数）；最大滞后超过
        ``REALTIME_JITTER_WARN_S``（默认 2 ms）时，**每次 run 只发一条**
        ``send_warning``。模拟模式（``_simulate_cmd_stack``）只记录命令、不 sleep，
        因此跳过度量与告警。sleep 目标 ``t + starttime - time.time()`` 与命令
        顺序保持原样不变（时序语义冻结）。
        """
        global starttime
        # 排序语义与旧实现完全一致（按 [t, attr, v] 排序）；先取一份列表，
        # 好在 finally 里统计命令条数（异常时也能记录命令总数）。
        # 排序也放进 try：若两条命令时间/属性相同而值不可比较（numpy 数组
        # 等），sorted 会抛 TypeError——放外面会跳过 finally、留下半截命令栈。
        commands = ()
        max_lag = 0.0
        try:
            commands = sorted(self._cmd_stack)
            for t, attr, v in commands:
                # during a simulated run (_simulate_cmd_stack) never sleep in real
                # time - just record the commands via the attribute __set__
                if not self._simulate_cmd_stack:
                    # 目标时刻基准保持 time.time()（starttime 的既有语义不变）；
                    # monotonic 只用于测量“实际执行 - 目标”的滞后。
                    _start_mono = time.monotonic()
                    _sleep_s = t + starttime - time.time()
                    time.sleep(_sleep_s)
                    _lag = time.monotonic() - (_start_mono + _sleep_s)
                    if _lag > max_lag:
                        max_lag = _lag
                setattr(self, attr, v)
        finally:
            # 无论成功/失败都清空，失败时不留半截命令栈给下一次运行
            self._cmd_stack = []
            if self._simulate_cmd_stack:
                # 模拟模式只记录命令、不真实回放：度量无意义，清零且不告警
                self._realtime_max_lag = 0.0
                self._realtime_command_count = 0
            else:
                self._realtime_max_lag = max_lag
                self._realtime_command_count = len(commands)
                threshold = getattr(self, 'REALTIME_JITTER_WARN_S', 0.002)
                if max_lag > threshold:
                    # 观测本身绝不能影响时序/异常传播：warning 失败就算了。
                    # 文案拆成两条短 tr()（长 key 治理规则：单 key < 200 字符）。
                    try:
                        send_warning(
                            tr('[Experiment RealTime] Python replay jitter: '
                               'max lag %.1f ms over %d commands; this is '
                               'NOT hard realtime.')
                            % (max_lag * 1000.0, len(commands))
                            + ' ' + tr('For microsecond timing use the Adwin '
                                       'hardware sequence.'))
                    except Exception:  # noqa: BLE001 - 观测不能影响实验
                        pass

    def _add_realtime_cmd(self, parname, value):
        """把一条实时命令压入命令栈（实时期 genericAttr 用）。

        Args:
            parname (str): 属性/通道名。
            value: 命令值。
        """
        self._cmd_stack.append([globaltime, parname, value])

    def _add_simulated_cmd(self, parname, value):
        """把一条模拟命令记入模拟栈（Simulate 画时序用）。

        Args:
            parname (str): 设备名/通道名。
            value: 命令值。
        """
        self._simulated_cmd_stack.append([globaltime, parname, value])


class NumberValue:
    """实验窗口的浮点参数（GUI 用 QDoubleSpinBox 编辑）。

    传给 :meth:`Experiment.setattr_argument` 使用。

    Args:
        name (str): 参数名。
        ndecimals (int): 小数位数（如 2 表示 0.01 精度）。
        step (float): 步进。
        value (float): 默认值。
        minval (float): 最小值。
        maxval (float): 最大值。
        tooltip (str): 可选，界面提示。
    """
    __argtype__ = 'NumberValue'

    def __init__(self, name='No name', ndecimals=0, step=1, value=42, minval=0, maxval=1E6, **kwargs):
        """构造浮点参数对象。

        会把额外 kwargs（如 tooltip/unit）原样存入实例，供 GUI 使用。

        Args:
            name (str): 参数名（setattr_argument 时会覆盖）。
            ndecimals (int): 显示小数位。
            step (float): 步进。
            value (float): 默认值。
            minval (float): 最小值。
            maxval (float): 最大值。
        """
        self.__dict__.update(kwargs)
        self.step = step
        self.ndecimals = ndecimals
        self.value = value
        self.minval = minval
        self.maxval = maxval


class StringCombo:
    """字符串下拉参数（实验窗口用 QComboBox）。

    传给 :meth:`Experiment.setattr_argument` 使用；默认值取 ``stringlist[0]``。

    Args:
        name (str): 参数名。
        stringlist (list[str]): 候选字符串列表。

    Attributes:
        value (str): 当前值（默认首个候选）。
    """
    __argtype__ = 'StringCombo'

    def __init__(self, name='Noname', stringlist=['']):
        """构造字符串下拉参数。

        Args:
            name (str): 参数名。
            stringlist (list[str]): 候选字符串列表。
        """
        self.name = name
        self.stringlist = stringlist
        self.value = stringlist[0]

class StringValue:
    """字符串参数（实验窗口用文本输入）。

    Args:
        name (str): 参数名。
        value (str): 默认字符串。
    """

    __argtype__ = 'StringValue'

    def __init__(self, name='Noname', value=""):
        """构造字符串参数。

        Args:
            name (str): 参数名。
            value (str): 默认值。
        """
        self.name = name
        self.value = value

class BoolValue:
    """布尔参数（实验窗口用勾选框）。

    Args:
        name (str): 参数名。
        value (bool): 默认值。
    """
    __argtype__ = 'BoolValue'

    def __init__(self, name='Noname', value=False):
        """构造布尔参数。

        Args:
            name (str): 参数名。
            value (bool): 默认值。
        """
        self.name = name
        self.value = value


def delay(delta):
    """在实验里制造延时。

    普通期：推进内部时钟并把剩余时间用 ``time.sleep`` 等掉（扣除已花 CPU
    时间），保证按相对时钟准时。实时（解析）期与模拟期：只推进内部时钟，
    不真睡（真实时序由控制器按时间戳执行；模拟期避免卡 GUI）。

    Args:
        delta (float): 期望延长的秒数。

    Example:
        ::

            delay(0.01)   # 等 10 ms（实时块内只推时钟）
    """
    global globaltime
    global starttime
    global _experiment
    # print(starttime)
    globaltime += delta
    # never sleep in real time during a simulated run (Simulate window /
    # CodeEditorParser) - otherwise a scan loop of delay() calls freezes the
    # GUI for the whole (simulated) duration of the experiment.
    if not _experiment.parsing and not getattr(_experiment, '_simulate_cmd_stack', False):
        wait = globaltime + starttime - time.time()
        if wait >= 0:
            time.sleep(wait)
        else:
            send_warning('negative delay occurred: {}'.format(wait))


def experiment_starttime():
    """返回当前实验运行的起始墙钟时间（time.time() 秒）。"""
    global starttime
    return starttime


def now():
    """返回实验内部时钟读数（相对起点的累计秒数，供实验代码取时间）。"""
    global globaltime
    return globaltime


if __name__ == '__main__':
    Experiment()
