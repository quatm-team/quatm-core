# -*- coding: utf-8 -*-
"""隔离子进程实验执行器（AGPL 核心；无 Qt、无 web 依赖）。

用途：把一个实验任务放到**独立子进程**里跑，父进程（GUI 队列或 Web）可以
``pause``/``resume``/``terminate`` 它而不会冻结界面；实验代码崩溃/卡死也只
影响这个子进程。本模块只实现 runner 侧协议（manager 集成见 the isolated runner protocol）。

执行语义与 ``quatm/GUI/experiment/experiment_manager.py`` 的队列 worker 一致：

* 实例化实验 → :meth:`Experiment.build`（声明设备/参数）；
* 参数按 ``set_dict`` 语义写入（数据描述符走 ``setattr``，普通参数写
  ``__dict__``，与 manager 相同；本模块自带等价实现，**不 import GUI**）；
* ``start_measurement()`` → 每个 repetition：``start_sequence()`` → 每个 run：
  写扫描值、设 ``experiment._run``、``experiment._start_run(task)``（真实
  ``_start_run`` 内部照旧发 ``.start``/``.end``）→ ``end_sequence()`` →
  ``end_measurement()`` → 最后 ``experiment.close()``；
* 每个 repetition 设置 ``experiment._repetition``；
* ``nReps`` 与 GUI 一致表示**额外重复次数**：实际 sequence 数 = ``nReps + 1``
  （GUI ``nRepsQSB`` 缺省 0 = 跑一次；见 ExperimentManager 的
  ``while self.rep <= self.nReps``）。

协议（行分隔 JSON；**stdout 只输出事件行**）
==========================================

启动::

    python -m quatm.experiment.runner run --project <project-root> \\
        --file <experiments/ 下相对路径或绝对路径> [--class NAME] \\
        [--task '<task-json>'] [--no-control]

* 任务 JSON 可以从 ``--task`` 传入；不传时从 **stdin 首行**读取。
* stdin 首行任务之后（若用 ``--task`` 则从第一行开始）逐行接收控制命令::

      {"cmd": "pause"}
      {"cmd": "resume"}
      {"cmd": "terminate"}

  命令在 **run 之间**检查（与 GUI 的 pause 语义一致：不打断正在进行的硬件
  调用/当前 run）。后台线程读 stdin，主执行路径不阻塞在 ``input()`` 上。
* 运行期间实验代码的 ``print()`` 被重定向到 **stderr**，stdout 只留给协议行。

事件（每行一个 JSON 对象；带 ``task`` 字段便于 manager 映射）::

    {"event": "started", "task": 7, "experiment": "...", "class": "...",
     "protocol": 1, "n_runs": 3, "n_reps": 0, "scanpars": ["p"],
     "scan_name": "..."}
    {"event": "run_started", "task": 7, "run": 0, "rep": 0}
    {"event": "run_done",    "task": 7, "run": 0, "rep": 0}
    {"event": "paused",      "task": 7}            # pause 在 run 边界生效
    {"event": "resumed",     "task": 7}
    {"event": "terminated",  "task": 7, "rep": 0}  # 收到 terminate（优雅收尾）
    {"event": "finished",    "task": 7, "ok": true}          # 正常完成
    {"event": "finished",    "task": 7, "ok": true, "terminated": true}
    {"event": "error",       "task": 7, "message": "...", "traceback": "..."}
    {"event": "log",         "task": 7, "level": "warning", "message": "..."}

退出码
======

* ``0``：任务正常完成；或收到 ``terminate`` 后完成收尾（``end_sequence`` /
  ``end_measurement`` / ``close``）——与 GUI “优雅终止”一致；
* ``1``：实验加载/``build``/``run``/schema 校验/保存回执(strict)/``close``
  失败（发生 ``error`` 事件）；
* ``2``：命令行或 stdin 协议错误（坏任务 JSON、缺少必需参数等）。

数据保存回执（the save-ack protocol 共用 API）
================================

runner 在**每个 run 的 ``.end`` 之后**（``_start_run`` 返回时 ``.end`` 已由实验
对象发出）调用 the save-ack protocol 的共用 API ``quatm.servers.datamgr.save_ack(token,
expected_seq, timeout)``，语义与 GUI ``ExperimentManager`` 一致：

* 配置优先级：任务里的 ``save_ack`` 段 > 实验属性库的
  ``/Browser/ExperimentQueue/save_ack_{mode,strict,timeout_s}`` > 默认
  ``mode=off``（隔离 runner 不在没有 manager 配置时凭猜测等回执）::

      "save_ack": {"mode": "off|run|task|auto", "token": "...",
                   "strict": true, "timeout_s": 5.0}

  ``run``/``auto`` = 每 run 等一次；``task`` = 整个任务结束等一次；``off`` = 不等。
* token：任务上下文里的 ``save_ack.token`` / ``token``，否则 runner 生成
  ``runner-<pid>-<ms>``；它会通过 ``Experiment.set_ack_context()`` 挂到数据消息的
  ``_ack`` 标记上（the save-ack protocol），Data Manager 据此统计本任务收到了哪些消息。
* ``expected_seq`` 取自 the save-ack protocol 的 clients ack 计数（本 run 结束时已发出的最大
  序号）。runner **不**自己轮询 h5/文件系统（不使用第二套回执实现）。
* ``available=False``（没有 Data Manager、旧版无控制端点、控制端点不可达）→
  **降级**：只发 ``log`` warning，退回任务结束后的 ``save_errors`` 事后检查，
  **不**判任务失败（与 the save-ack protocol 约定一致）；
* 明确回执失败（``ok=False``）且 ``strict=True`` → ``error`` 事件 + 退出码 1；
  ``strict=False`` → ``log`` error 后继续。
"""

import argparse
import importlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time
import traceback

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

from quatm.utils.i18n import tr

#: 协议版本（父进程可据此判断兼容性；事件里只在 ``started`` 带一次）。
PROTOCOL_VERSION = 1

#: 退出码（见模块文档）。
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_PROTOCOL_ERROR = 2

#: 主线程检查控制命令/暂停状态的轮询间隔（秒）。
_CONTROL_POLL_S = 0.02

#: 合法的保存回执模式。
_SAVE_ACK_MODES = ('off', 'warn', 'strict')

#: the save-ack protocol 的保存回执 API 查找表：``(模块名, 函数名)``。
#: API 落地后**只改这里**（不要在 runner 里写第二套轮询 h5/文件系统的实现）。
_SAVE_ACK_PROVIDERS = (
    ('quatm.servers.datamgr', 'save_ack'),
)

#: 父进程死亡钩子给 runner 的收尾时间（秒）。servers 看门狗总预算 10s 兜底；
#: 这里留 5s 给“请求终止 + 等主线程安全点 + 尽力 close”。
PARENT_DEATH_HOOK_TIMEOUT_S = 5.0

#: 钩子里等主线程自行走到正常收尾（finally 的 close）的宽限（秒）。超时后
#: 由钩子线程直接 close（Experiment.close 幂等），随后看门狗会硬退。
PARENT_DEATH_MAIN_WAIT_S = 2.0


# ---------------------------------------------------------------------------
# 事件输出
# ---------------------------------------------------------------------------
class EventSink:
    """把事件写成 JSON 行到**协议流**（真正的 stdout），并自动带上 ``task``。

    Args:
        task (int|None): 任务号；后续可通过 :attr:`task` 更新。
        stream: 协议输出流；None 表示 ``sys.__stdout__``（实验代码的 print 会被
            重定向到 stderr，但 ``sys.__stdout__`` 不受影响）。
    """

    def __init__(self, task=None, stream=None):
        self.task = task
        self._stream = stream if stream is not None else sys.__stdout__

    def __call__(self, event, **fields):
        """输出一条事件。

        Args:
            event (str): 事件名（``started``/``run_started``/...）。
            **fields: 事件附加字段（会被合并进 JSON 对象）。
        """
        payload = {'event': event}
        if self.task is not None:
            payload['task'] = self.task
        payload.update(fields)
        try:
            self._stream.write(json.dumps(payload, ensure_ascii=False,
                                          default=str) + '\n')
            self._stream.flush()
        except Exception:  # noqa: BLE001 - 协议流坏了也不能让实验崩
            pass


# ---------------------------------------------------------------------------
# 控制通道
# ---------------------------------------------------------------------------
class ControlChannel:
    """stdin 控制通道：后台线程读 JSON 行，主线程在 run 边界消费。

    后台线程只更新状态（暂停/终止）；真正的等待发生在 :meth:`wait_until_ready`，
    所以不会打断正在执行的硬件调用（与 GUI 的 pause 语义一致）。

    Args:
        stream: 控制命令输入流；None 表示 ``sys.stdin``。
        emit (callable): 事件输出函数（用于报告坏命令）。
    """

    def __init__(self, stream=None, emit=None):
        self._stream = stream if stream is not None else sys.stdin
        self._emit = emit or (lambda *args, **kwargs: None)
        self._lock = threading.Lock()
        self._paused = False
        self._terminated = False
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        """启动后台读线程（daemon；stdin EOF 时自然结束）。

        Returns:
            ControlChannel: self（便于链式调用）。
        """
        self._thread = threading.Thread(target=self._read_loop,
                                        name='quatm-runner-control',
                                        daemon=True)
        self._thread.start()
        return self

    def request_terminate(self):
        """从外部（信号处理器）请求优雅终止。"""
        with self._lock:
            self._terminated = True
        self._wake.set()

    def _read_loop(self):
        """后台线程主体：逐行解析控制命令，更新状态。"""
        while True:
            try:
                line = self._stream.readline()
            except Exception:  # noqa: BLE001 - stdin 关闭/编码问题都当 EOF
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception as exc:  # noqa: BLE001 - 坏命令只告警，不中断任务
                self._emit('log', level='warning',
                           message=tr('runner: invalid control command: %s') % exc)
                continue
            if not isinstance(message, dict):
                self._emit('log', level='warning',
                           message=tr('runner: invalid control command: %s') % message)
                continue
            command = str(message.get('cmd', '')).strip().lower()
            if command == 'pause':
                with self._lock:
                    self._paused = True
                self._wake.set()
            elif command == 'resume':
                with self._lock:
                    self._paused = False
                self._wake.set()
            elif command == 'terminate':
                self.request_terminate()
            else:
                self._emit('log', level='warning',
                           message=tr('runner: unknown control command: %s')
                           % command)

    def wait_until_ready(self):
        """在 run 边界等待：暂停时阻塞，直到 resume 或 terminate。

        Returns:
            bool: True = 收到 terminate（调用方应停止后续 run）；False = 可以继续。
        """
        with self._lock:
            if self._terminated:
                return True
            if not self._paused:
                return False
            self._emit('paused')
        while True:
            self._wake.wait(_CONTROL_POLL_S)
            self._wake.clear()
            with self._lock:
                if self._terminated:
                    return True
                if not self._paused:
                    self._emit('resumed')
                    return False


def _install_signal_handlers(control):
    """把 SIGTERM/SIGINT 转成“优雅终止”请求（POSIX；Windows 上尽力而为）。

    ``proc.terminate()``（SIGTERM）默认会直接杀进程，设备来不及释放；装了
    处理器后 runner 会跑完当前 run、走 ``end_sequence``/``end_measurement``/
    ``close`` 再退出。拿不到信号/非主线程时静默跳过。

    Args:
        control (ControlChannel): 控制通道。
    """
    def _handler(_signum, _frame):
        """信号处理器：只置位，不做实际收尾（安全）。"""
        control.request_terminate()

    for name in ('SIGTERM', 'SIGINT'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            # 非主线程（测试里可能在后台线程调用）或平台不支持：跳过。
            pass


# ---------------------------------------------------------------------------
# 实验加载 / 参数 schema 校验 / set_dict
# ---------------------------------------------------------------------------
def load_experiment(project, filepath, class_name=None):
    """加载实验文件并返回实验类。

    filepath 相对 ``<project>/experiments/`` 或为绝对路径。项目根会被设为
    ``QUATM_PROJECT_ROOT`` 并插到 ``sys.path`` 最前（与 web/runner.py 一致）。

    Args:
        project (str|os.PathLike): 用户项目根目录。
        filepath (str): 实验文件路径（相对 experiments/ 或绝对）。
        class_name (str|None): 类名；None 时取模块里第一个 Experiment 子类。

    Returns:
        type: 实验类（必须是 ``Experiment`` 子类）。

    Raises:
        FileNotFoundError: 实验文件不存在。
        ValueError: 模块里找不到 Experiment 子类/指定类不是子类。
    """
    project = os.fspath(project)
    os.environ['QUATM_PROJECT_ROOT'] = project
    if project not in sys.path:
        sys.path.insert(0, project)
    path = filepath if os.path.isabs(filepath) else os.path.join(
        project, 'experiments', filepath)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location('_quatm_isolated_experiment',
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from quatm.experiment.experiment import Experiment
    if class_name:
        candidate = getattr(module, class_name, None)
        if candidate is None:
            raise ValueError(tr('runner: experiment class %r not found in %s')
                             % (class_name, filepath))
        if not (isinstance(candidate, type)
                and issubclass(candidate, Experiment)):
            raise ValueError(tr('runner: experiment class %r is not an '
                                'Experiment subclass') % class_name)
        return candidate
    for name in dir(module):
        candidate = getattr(module, name)
        if (isinstance(candidate, type) and issubclass(candidate, Experiment)
                and candidate is not Experiment):
            return candidate
    raise ValueError(tr('runner: no Experiment subclass found in %s')
                     % filepath)


def declared_parameter_names(experiment):
    """返回实验通过 ``setattr_argument`` 声明过的参数名集合。

    与 GUI ``ExperimentManager`` 的同名 helper 语义一致（AGPL 核心不 import
    GUI，所以这里自带等价实现）：优先 ``_declared_arguments``，回退
    ``_arguments`` 列表（旧对象/替身）。

    Args:
        experiment: 实验实例（可为 None）。

    Returns:
        set[str]: 参数名集合。
    """
    names = set()
    if experiment is None:
        return names
    try:
        declared = getattr(experiment, '_declared_arguments', None) or {}
        names.update(str(key) for key in declared)
    except Exception:  # noqa: BLE001 - 取登记表失败时回退
        pass
    try:
        for argument in getattr(experiment, '_arguments', None) or []:
            name = getattr(argument, 'name', None)
            if name:
                names.add(str(name))
    except Exception:  # noqa: BLE001
        pass
    return names


def experiment_device_names(experiment):
    """返回 ``setattr_device`` 可能登记过的设备/属性名集合（``_device_db``/``_attributes``）。

    Args:
        experiment: 实验实例（可为 None）。

    Returns:
        set[str]: 设备名集合。
    """
    names = set()
    if experiment is None:
        return names
    for attribute in ('_device_db', '_attributes'):
        try:
            value = getattr(experiment, attribute, None)
            if isinstance(value, dict):
                names.update(str(key) for key in value)
        except Exception:  # noqa: BLE001
            pass
    return names


def experiment_has_parameter_schema(experiment):
    """实验对象是否带有可用的参数登记表（决定能否做严格 schema 校验）。

    没有登记表的对象（duck-typed 替身/旧序列化对象）**放行**而不是全判未知：
    这是有意的 fail-open，与 GUI manager 一致。

    Args:
        experiment: 实验实例（可为 None）。

    Returns:
        bool: 能拿到登记表时为 True。
    """
    if experiment is None:
        return False
    try:
        return (hasattr(experiment, '_declared_arguments')
                or hasattr(experiment, '_arguments'))
    except Exception:  # noqa: BLE001
        return False


def task_schema_unknown(experiment, task):
    """校验任务的 ``args`` / ``scanpars`` 是否都是实验声明过的名字。

    与 GUI ``ExperimentManager.task_schema_unknown`` 语义一致（规则见其
    docstring）：``scanpars`` 必须是声明参数（``--NONE--`` 跳过）；``args``
    的键可以是声明参数**或**设备名。名字保持首次出现顺序。

    Args:
        experiment: 实验实例（None 时视为“什么参数都没声明”）。
        task (dict): 任务字典；读 ``args`` / ``scanpars``。

    Returns:
        tuple[list[str], list[str]]: ``(未知扫描参数, 未知 args 键)``。
    """
    if not experiment_has_parameter_schema(experiment):
        return [], []
    declared = declared_parameter_names(experiment)
    allowed_args = declared | experiment_device_names(experiment)
    task = task or {}
    scanpars = task.get('scanpars') or []
    if not isinstance(scanpars, (list, tuple)):
        scanpars = [scanpars]
    unknown_scan = []
    reported = set()
    for name in scanpars:
        if name is None or name == '--NONE--':
            continue
        text = str(name)
        if text in declared or text in reported:
            continue
        reported.add(text)
        unknown_scan.append(text)
    args = task.get('args') or {}
    if not isinstance(args, dict):
        args = {}
    unknown_args = []
    for key in args:
        text = str(key)
        if text in allowed_args or text in reported:
            continue
        reported.add(text)
        unknown_args.append(text)
    return unknown_scan, unknown_args


def set_dict(experiment, name, value):
    """把单个参数/设备属性写到实验对象（与 manager ``set_dict`` 语义一致）。

    普通参数写 ``__dict__``；数据描述符（DAC/TTLOut/genericAttr 等带
    ``__set__``）必须 ``setattr``，否则会静默遮蔽描述符、硬件收不到值。

    Args:
        experiment: 实验实例。
        name (str): 参数/属性名。
        value: 目标值。
    """
    descriptor = None
    try:
        # 必须查 MRO 的类字典；类访问会触发描述符 __get__ 返回默认值。
        for klass in type(experiment).__mro__:
            raw = vars(klass).get(name)
            if raw is not None:
                descriptor = raw
                break
    except Exception:  # noqa: BLE001 - 取类属性失败按普通参数处理
        descriptor = None
    if descriptor is not None and hasattr(descriptor, '__set__'):
        setattr(experiment, name, value)
    else:
        experiment.__dict__[name] = value


# ---------------------------------------------------------------------------
# 数据保存回执（the save-ack protocol 共用 API：quatm.servers.datamgr.save_ack）
# ---------------------------------------------------------------------------
#: 与 GUI ``ExperimentManager`` 相同的属性树配置键（the isolated runner protocol 会把它们带进任务，
#: runner 在能连属性库时也可直接读；隔离 runner 的缺省是 ``off``，见
#: :func:`save_ack_config`）。
SAVE_ACK_MODE_PROP = '/Browser/ExperimentQueue/save_ack_mode'
SAVE_ACK_STRICT_PROP = '/Browser/ExperimentQueue/save_ack_strict'
SAVE_ACK_TIMEOUT_PROP = '/Browser/ExperimentQueue/save_ack_timeout_s'

#: 保存回执模式（与 GUI 一致）：off / run（每 run）/ task（任务结束一次）/
#: auto（能连 Data Manager 就按 run，连不上降级；runner 里等同 run+降级）。
_SAVE_ACK_MODES = ('off', 'run', 'task', 'auto')


def find_save_ack():
    """查找 the save-ack protocol 的共用保存回执 API ``save_ack(token, expected_seq, timeout)``。

    落地位置在 ``quatm/servers/datamgr.py``（见 :data:`_SAVE_ACK_PROVIDERS`）。
    找不到时返回 None：调用方按"回执不可用"降级，**不要**在 runner 里另写一套
    轮询 h5/文件系统的实现（那是第二套语义，正是本任务要避免的）。

    Returns:
        callable|None: 找到的 API；未就绪返回 None。
    """
    for module_name, attribute in _SAVE_ACK_PROVIDERS:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 模块还没落地/导入失败都算未就绪
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate
    return None


def _experiment_property(experiment, path, default):
    """读实验属性库里的配置（没有属性客户端时返回 default）。"""
    props = getattr(experiment, '_props', None)
    if props is None:
        return default
    try:
        return props.get(path, default)
    except Exception:  # noqa: BLE001 - 属性库不可用时用默认值
        return default


def _new_save_ack_token():
    """生成一个本 runner 进程内唯一的数据保存回执 token。"""
    return 'runner-%d-%d' % (os.getpid(), int(time.time() * 1000))


def save_ack_config(experiment, task):
    """解析保存回执配置（优先级：任务 ``save_ack`` 段 > 属性树 > 默认 off）。

    GUI ``ExperimentManager`` 从 ``/Browser/ExperimentQueue/save_ack_*`` 三个属性
    读配置；隔离 runner 允许任务里显式携带 ``save_ack``（the isolated runner protocol 会把 manager
    的配置放进任务），两者都没有时缺省 ``off``（不在隔离进程里凭猜测等回执）。

    Args:
        experiment: 实验实例（可为 None）。
        task (dict): 任务字典。

    Returns:
        dict: ``{'mode', 'strict', 'timeout', 'token'}``（mode ∈ off/run/task/auto）。
    """
    task = task if isinstance(task, dict) else {}
    config = task.get('save_ack')
    if not isinstance(config, dict):
        config = {}
    mode = config.get('mode')
    if mode is None:
        mode = _experiment_property(experiment, SAVE_ACK_MODE_PROP, 'off')
    mode = str(mode).strip().lower()
    if mode not in _SAVE_ACK_MODES:
        mode = 'off'
    strict = config.get('strict')
    if strict is None:
        strict = bool(_experiment_property(experiment, SAVE_ACK_STRICT_PROP, True))
    timeout = config.get('timeout_s')
    if timeout is None:
        timeout = _experiment_property(experiment, SAVE_ACK_TIMEOUT_PROP, 5.0)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 5.0
    token = config.get('token') or task.get('token')
    if not token:
        token = _new_save_ack_token()
    return {'mode': mode, 'strict': bool(strict),
            'timeout': max(0.0, timeout), 'token': str(token)}


def _ack_context_seq():
    """取本进程已发出的带回执标记消息的最大序号（the save-ack protocol 的 clients 计数）。

    Returns:
        int: 序号；取不到时 0（无数据 run 应快速通过）。
    """
    try:
        from quatm.servers.clients import ack_context_seq
        return int(ack_context_seq())
    except Exception:  # noqa: BLE001 - 旧环境/导入失败按 0（无数据）
        return 0


def wait_save_ack(config, expected_seq, emit, state=None):
    """等待一次保存回执（调用 the save-ack protocol 的 ``save_ack`` 共用 API）。

    语义（与 GUI manager 的 ``_wait_save_ack`` 对齐）：

    * ``mode == 'off'``：直接返回成功（不调用 API）；
    * API 未就绪 / 返回 ``available=False``（旧 Data Manager、控制端点不可达）：
      **降级**——只发一条 ``log`` warning，不判任务失败（与 the save-ack protocol 的约定一致：
      回执不可用不是数据丢失，调用方应退回事后 ``save_errors`` 检查）；
    * 回执明确失败（``ok=False``）且 ``strict=True``：把失败写进 ``state``，
      由调用方转成 ``error`` 事件 + 退出码 1；``strict=False`` 只发 log 后继续。

    Args:
        config (dict): :func:`save_ack_config` 的返回值。
        expected_seq (int): 本 run/task 结束时应确认的最大序号。
        emit (callable): 事件输出函数。
        state (dict|None): 可选状态字典（``failure``/``unavailable`` 会被更新）。

    Returns:
        dict: ``save_ack`` 的原始结果（或 ``{'available': ..., 'ok': ...}``）。
    """
    if config.get('mode') == 'off':
        return {'available': True, 'ok': True, 'reason': 'disabled'}
    # 同一任务里回执不可用只需要提示一次：否则每个 run 一条 warning，扫描
    # 任务（几十~上百 run）会把消息监视器刷屏，反而淹没真正的错误。
    already_warned = bool(state is not None and state.get('unavailable'))
    api = find_save_ack()
    if api is None:
        if not already_warned:
            emit('log', level='warning',
                 message=tr('runner: save-ack API is not available; falling '
                            'back to the post-run save_errors check'))
        if state is not None:
            state['unavailable'] = True
        return {'available': False, 'ok': False, 'reason': 'unavailable'}
    try:
        result = api(config['token'], int(expected_seq),
                     timeout=config['timeout'])
    except TypeError:
        # 兼容旧签名 save_ack(token, expected_seq, timeout)（位置参数）。
        try:
            result = api(config['token'], int(expected_seq), config['timeout'])
        except Exception as exc:  # noqa: BLE001
            return _save_ack_error(config, exc, emit, state)
    except Exception as exc:  # noqa: BLE001 - API 自身异常：按回执失败处理
        return _save_ack_error(config, exc, emit, state)
    if not isinstance(result, dict):
        # 旧实现没有返回结构：按“已确认”处理（不能把旧 API 当失败）。
        return {'available': True, 'ok': True, 'reason': 'legacy'}
    if not result.get('available', True):
        if not already_warned:
            emit('log', level='warning',
                 message=tr('runner: save-ack API is not available; falling '
                            'back to the post-run save_errors check'))
        if state is not None:
            state['unavailable'] = True
        return result
    if not result.get('ok'):
        reason = result.get('reason') or result.get('error') or 'nack'
        emit('log', level='error',
             message=tr('runner: save-ack failed: %s') % reason)
        if state is not None and config.get('strict'):
            state['failure'] = tr('runner: save-ack failed in strict mode; '
                                  'aborting task')
    return result


def _save_ack_error(config, exc, emit, state):
    """把 API 异常转成回执失败结果（strict 时写 state）。"""
    emit('log', level='error',
         message=tr('runner: save-ack failed: %s') % exc)
    result = {'available': True, 'ok': False, 'reason': 'error',
              'error': str(exc)}
    if state is not None and config.get('strict'):
        state['failure'] = tr('runner: save-ack failed in strict mode; '
                              'aborting task')
    return result


def _install_save_ack(experiment, config, emit, state):
    """把任务 token 挂到实验数据流上（``Experiment.set_ack_context``）。

    the save-ack protocol 让数据消息带上 ``_ack={'token','seq'}``，Data Manager 据此统计本任务
    收到了哪些消息。runner 自己**在每 run 结束（``_start_run`` 返回、``.end`` 已
    发出）后**调用共用 ``save_ack`` 等待回执（见 :func:`wait_save_ack`）——不复用
    GUI manager 的 ``set_save_ack_hook`` 回调：隔离 runner 需要自己的 strict/warn
    处理与事件上报，而且不是所有实验都覆盖 ``_start_run`` 的发事件路径。

    Args:
        experiment: 实验实例。
        config (dict): :func:`save_ack_config` 的返回值。
        emit (callable): 事件输出函数（本函数不用，保留签名便于未来扩展）。
        state (dict): 可变状态（本函数不用，保留签名便于未来扩展）。

    Returns:
        bool: 是否成功挂上 ack 上下文（False 时 token 没生效，回执降级）。
    """
    if config.get('mode') == 'off':
        return False
    setter = getattr(experiment, 'set_ack_context', None)
    if not callable(setter):
        return False
    try:
        setter(config['token'])
    except Exception:  # noqa: BLE001 - 旧实验对象没有该能力：降级
        return False
    return True


def _clear_save_ack(experiment):
    """任务结束清理回执上下文/回调（幂等；老实验对象静默跳过）。"""
    for method_name, value in (('set_save_ack_hook', None),
                               ('set_ack_context', None)):
        method = getattr(experiment, method_name, None)
        if not callable(method):
            continue
        try:
            method(value)
        except Exception:  # noqa: BLE001 - 清理尽力而为
            pass


# ---------------------------------------------------------------------------
# 任务字段解析 / 扫描值写入
# ---------------------------------------------------------------------------
def _task_int(task, *names, **kwargs):
    """从任务字典按候选键名取整数（兼容 GUI ``nRuns`` 与 Web ``n_runs``）。

    Args:
        task (dict): 任务字典。
        *names: 候选键名（按顺序）。
        **kwargs: ``default`` 缺省返回值。

    Returns:
        int|None: 取到的整数；都没有时返回 default。
    """
    default = kwargs.get('default')
    for name in names:
        if isinstance(task, dict) and task.get(name) is not None:
            try:
                return int(task[name])
            except (TypeError, ValueError):
                continue
    return default


def _task_list(task, name):
    """从任务字典取 list 字段（缺失/类型不对时返回 ``[]``）。"""
    if not isinstance(task, dict):
        return []
    value = task.get(name)
    return list(value) if isinstance(value, (list, tuple)) else []


def _apply_scan_values(experiment, scanpars, scanvals, scansequence, run_index):
    """按 GUI 语义把本次 run 的扫描值写入实验（scansequence 决定取值顺序）。

    Args:
        experiment: 实验实例。
        scanpars (list): 参数名列表（``--NONE--`` 槽位跳过）。
        scanvals (list): 二维扫描值（每行一次 run 的参数值）。
        scansequence (list): 运行顺序（空时按原顺序）。
        run_index (int): 本次 run 序号。

    Raises:
        ValueError: 扫描定义不完整（run 数/参数个数对不上）——转成 error 事件。
    """
    if not scanpars:
        return
    sequence = scansequence or list(range(len(scanvals)))
    if run_index >= len(sequence):
        raise ValueError(tr('runner: scan definition is inconsistent: '
                            'run %d is outside scansequence (%d entries)')
                         % (run_index, len(sequence)))
    value_index = sequence[run_index]
    if value_index >= len(scanvals):
        raise ValueError(tr('runner: scan definition is inconsistent: '
                            'run %d points at missing scan value %d')
                         % (run_index, value_index))
    row = scanvals[value_index]
    for position, parameter in enumerate(scanpars):
        if parameter is None or parameter == '--NONE--':
            continue
        if position >= len(row):
            raise ValueError(tr('runner: scan definition is inconsistent: '
                                'run %d has no value for parameter %s')
                             % (run_index, parameter))
        set_dict(experiment, str(parameter), row[position])


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------
def _schema_error_message(experiment, task, task_nr, unknown_scan, unknown_args):
    """拼出“任务参数未声明”的错误文本（含实验名与未知名字）。

    复用 GUI manager 已有的 i18n key（措辞一致、无需新增译文），拆短句拼接。

    Args:
        experiment: 实验实例。
        task (dict): 任务字典。
        task_nr (int|None): 任务号。
        unknown_scan (list[str]): 未知扫描参数。
        unknown_args (list[str]): 未知 args 键。

    Returns:
        str: 错误消息。
    """
    parts = []
    if unknown_scan:
        parts.append(tr('scan parameter(s): %s') % ', '.join(unknown_scan))
    if unknown_args:
        parts.append(tr('argument(s): %s') % ', '.join(unknown_args))
    exp_name = str(getattr(experiment, 'name', '') or
                   task.get('expName', '') or '?')
    message = (tr('[Experiment Queue] task #%d uses parameters that '
                  'experiment %s does not declare (%s).')
               % (task_nr if task_nr is not None else -1, exp_name,
                  '; '.join(parts)))
    message += tr(' They would be silently ignored, so the task was refused.')
    return message


class _HardwareSafeCleanup:
    """父进程死亡时的收尾守卫：请求终止 + 与正常收尾互斥地 ``close()`` 一次。

    父进程（Browser/ProcessManager）被 ``kill -9`` 时，子进程看门狗会先执行
    本钩子再 ``os._exit(0)``。约束与取舍：

    * 先 :meth:`ControlChannel.request_terminate`：让主线程在下一个 run 边界
      停止（不打断正在进行的硬件调用）；
    * 再等主线程最多 ``PARENT_DEATH_MAIN_WAIT_S`` 秒：正常情况下它会走到
      ``end_sequence``/``end_measurement``/``close``（与正常退出同一条路径）；
    * 主线程仍卡在阻塞的 ``run()`` 里时，钩子线程**直接** ``close()``
      （Experiment.close 幂等且会停控制器/关设备）；不调 ``end_measurement``
      ——实验对象不是线程安全的，不能在别的线程里跑测量收尾；
    * 用锁 + 标志保证 ``close()`` 只执行一次（正常路径与父死路径互斥），
      避免二次 close 异常。

    Args:
        experiment: 已 build 的实验实例。
        control (ControlChannel|None): 控制通道（请求终止用）。
        emit (callable|None): 事件输出（尽力发 log；父进程可能已死）。
    """

    def __init__(self, experiment, control=None, emit=None):
        """保存实验/控制通道引用与互斥状态。"""
        self._experiment = experiment
        self._control = control
        self._emit = emit
        self._lock = threading.Lock()
        self._closed = False
        self._close_failed = False
        self._main_done = threading.Event()

    def request_terminate(self):
        """请求主线程在 run 边界优雅终止（异常吞掉）。"""
        try:
            if self._control is not None:
                self._control.request_terminate()
        except Exception:  # noqa: BLE001 - 紧急路径：请求失败也要继续收尾
            pass

    def close_once(self):
        """幂等地调用 ``experiment.close()``（正常路径与父死路径只关一次）。

        Returns:
            bool: True=已关闭过/本次关闭成功；False=本次 close() 抛异常。
        """
        with self._lock:
            if self._closed:
                return True
            self._closed = True
        try:
            self._experiment.close()
            return True
        except Exception as exc:  # noqa: BLE001 - close 失败也要继续退出
            self._close_failed = True
            try:
                if self._emit is not None:
                    self._emit('log', level='error',
                               message=tr('runner: close() failed: %s') % exc)
            except Exception:  # noqa: BLE001 - 父进程已死时写事件也可能失败
                pass
            return False

    def mark_main_done(self):
        """主线程收尾完成：先置“正常路径已收尾”，再由它执行（唯一一次）close。

        Returns:
            bool: 同 :meth:`close_once`。
        """
        self._main_done.set()
        return self.close_once()

    def __call__(self):
        """父进程死亡钩子（在 servers 看门狗的独立线程里执行）。"""
        self.request_terminate()
        try:
            if self._emit is not None:
                self._emit('log', level='warning',
                           message=tr('runner: parent process died; '
                                      'requesting graceful shutdown'))
        except Exception:  # noqa: BLE001 - 父进程已死时 stdout 可能已断开
            pass
        if self._main_done.wait(timeout=PARENT_DEATH_MAIN_WAIT_S):
            return
        # 主线程卡在阻塞 run() 里：尽力 close（幂等；不跑 end_measurement）。
        self.close_once()


def execute_task(project, filepath, class_name, task, control=None, emit=None):
    """执行一个任务（加载→build→schema 校验→测量/序列/run→close）。

    Args:
        project (str): 用户项目根目录。
        filepath (str): 实验文件路径（相对 experiments/ 或绝对）。
        class_name (str|None): 实验类名。
        task (dict): 任务字典（字段见模块文档）。
        control (ControlChannel|None): 控制通道（None 表示无控制，用于测试）。
        emit (callable|None): 事件输出函数；None 用默认 :class:`EventSink`。

    Returns:
        int: 退出码（见模块文档）。
    """
    if emit is None:
        emit = EventSink(task=(task.get('task') if isinstance(task, dict)
                               else None))
    task_nr = task.get('task', task.get('taskNr')) if isinstance(task, dict) else None
    scanpars = _task_list(task, 'scanpars')
    scanvals = _task_list(task, 'scanvals')
    scansequence = _task_list(task, 'scansequence')
    n_runs = _task_int(task, 'nRuns', 'n_runs', default=None)
    if n_runs is None:
        n_runs = len(scanvals) if scanvals else 1
    n_runs = max(1, int(n_runs))
    n_reps = _task_int(task, 'nReps', 'n_reps', default=0) or 0
    n_reps = max(0, int(n_reps))
    start_rep = max(0, _task_int(task, 'repetition', default=0) or 0)
    args = task.get('args') if isinstance(task, dict) else None
    if not isinstance(args, dict):
        args = {}

    experiment = None
    cleanup = None
    exit_code = EXIT_TASK_FAILED
    try:
        experiment_class = load_experiment(project, filepath, class_name)
        experiment = experiment_class()
        experiment.build()

        # ---- 父进程死亡钩子（the hardware safe-state watchdog 硬件安全态）----
        # 加载/built 之后、进入运行循环之前注册：父进程被 kill -9 时先请求
        # 终止并尽力 close()，而不是直接 os._exit 让设备保持最后输出。
        cleanup = _HardwareSafeCleanup(experiment, control=control, emit=emit)
        try:
            from quatm.servers import register_parent_death_hook
            register_parent_death_hook(cleanup,
                                       timeout=PARENT_DEATH_HOOK_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 旧环境没有该 API：不影响正常执行
            pass

        # ---- 参数/扫描名 schema 校验（隔离任务可能没有 GUI 的 experiment 对象）----
        unknown_scan, unknown_args = task_schema_unknown(experiment, task)
        if unknown_scan or unknown_args:
            emit('error',
                 message=_schema_error_message(experiment, task, task_nr,
                                              unknown_scan, unknown_args),
                 traceback='')
            exit_code = EXIT_TASK_FAILED
            return exit_code

        emit('started', protocol=PROTOCOL_VERSION,
             experiment=str(getattr(experiment, 'name', '') or ''),
             **{'class': experiment_class.__name__},
             n_runs=int(n_runs), n_reps=int(n_reps),
             scanpars=list(scanpars),
             scan_name=task.get('scan_name') if isinstance(task, dict) else None)

        # 参数下发（与 manager.start_measurement 一致：先写 args 再 start）
        for name, value in args.items():
            set_dict(experiment, str(name), value)

        ack_config = save_ack_config(experiment, task)
        ack_state = {'failure': None, 'unavailable': False}
        ack_context_ready = _install_save_ack(experiment, ack_config, emit,
                                              ack_state)
        experiment.start_measurement()

        def _check_save_ack_failure():
            """strict 模式下回执失败：中止任务（转成 error 事件 + 退出码 1）。"""
            if ack_state['failure']:
                raise RuntimeError(ack_state['failure'])

        terminated = False
        last_rep = start_rep
        while last_rep <= n_reps:
            experiment._repetition = last_rep
            experiment.start_sequence()
            run_index = 0
            for run_index in range(n_runs):
                if control is not None and control.wait_until_ready():
                    terminated = True
                    break
                _apply_scan_values(experiment, scanpars, scanvals,
                                   scansequence, run_index)
                experiment._run = run_index
                emit('run_started', run=run_index, rep=last_rep)
                experiment._start_run(task_nr)
                emit('run_done', run=run_index, rep=last_rep)
                if ack_config['mode'] in ('run', 'auto'):
                    # 每 run 结束（``.end`` 已由 ``_start_run`` 发出）后等一次
                    # 保存回执；expected_seq 取自 the save-ack protocol 的 clients 计数。
                    if ack_context_ready:
                        expected_seq = _ack_context_seq()
                    else:
                        expected_seq = 0
                    wait_save_ack(ack_config, expected_seq, emit, ack_state)
                    _check_save_ack_failure()
            experiment.end_sequence()
            if terminated:
                emit('terminated', rep=last_rep, run=run_index)
                break
            last_rep += 1

        # terminate 与正常完成都做测量收尾（任务要求：end_sequence/
        # end_measurement/close；GUI 的 terminate 路径只到 end_sequence，
        # 隔离 runner 统一补齐，保证设备/数据收尾一致）。
        experiment.end_measurement()
        if ack_config['mode'] == 'task' and not terminated:
            # task 模式：只在整个任务结束后等一次最终回执。
            wait_save_ack(ack_config, _ack_context_seq(), emit, ack_state)
            _check_save_ack_failure()
        if terminated:
            emit('finished', ok=True, terminated=True)
        else:
            emit('finished', ok=True)
        exit_code = EXIT_OK
    except Exception as exc:  # noqa: BLE001 - 任务执行失败统一转 error 事件
        emit('error', message=str(exc), traceback=traceback.format_exc())
        exit_code = EXIT_TASK_FAILED
        if experiment is not None:
            # 注意变量名不能叫 cleanup：那是父死钩子守卫，覆盖它会导致
            # finally 里跳过 close()（runner 生命周期回归测试会抓出来）。
            failed_cleanup = getattr(
                experiment, '_cleanup_after_failed_run', None)
            if callable(failed_cleanup):
                try:
                    failed_cleanup()
                except Exception as cleanup_exc:  # noqa: BLE001
                    emit('log', level='warning',
                         message=tr('runner: failed-run cleanup failed: %s')
                         % cleanup_exc)
    finally:
        if experiment is not None:
            _clear_save_ack(experiment)
            # 正常收尾：置“主线程已完成”，并由守卫执行唯一一次 close()。
            # 若父进程死亡钩子已经先 close 过，这里是幂等空操作（不会二次关）。
            if cleanup is not None:
                try:
                    if not cleanup.mark_main_done() and exit_code == EXIT_OK:
                        exit_code = EXIT_TASK_FAILED
                except Exception:  # noqa: BLE001 - 收尾失败也要返回退出码
                    if exit_code == EXIT_OK:
                        exit_code = EXIT_TASK_FAILED
            else:
                try:
                    experiment.close()
                except Exception as exc:  # noqa: BLE001 - close 失败也要报出来
                    emit('log', level='error',
                         message=tr('runner: close() failed: %s') % exc)
                    if exit_code == EXIT_OK:
                        exit_code = EXIT_TASK_FAILED
    return exit_code


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _read_task(args):
    """读取任务 JSON（``--task`` 优先，否则 stdin 首行）。

    Args:
        args: argparse 解析结果（``task``/``no_control``）。

    Returns:
        dict: 任务字典。

    Raises:
        ValueError: 没有任务 JSON / 不是合法 JSON 对象。
    """
    if args.task is not None:
        raw = args.task
    else:
        raw = sys.stdin.readline()
        if not raw:
            raise ValueError(tr('runner: no task JSON on stdin'))
    raw = raw.strip()
    if not raw:
        raise ValueError(tr('runner: no task JSON on stdin'))
    try:
        task = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 统一成协议错误
        raise ValueError(tr('runner: invalid task JSON: %s') % exc)
    if not isinstance(task, dict):
        raise ValueError(tr('runner: task JSON must be an object'))
    return task


def run_command(args):
    """``run`` 子命令：读任务、建控制通道、执行并返回退出码。

    Args:
        args: argparse 解析结果。

    Returns:
        int: 退出码（见模块文档）。
    """
    sink = EventSink()
    try:
        task = _read_task(args)
    except ValueError as exc:
        sink('error', message=str(exc), traceback='')
        return EXIT_PROTOCOL_ERROR
    if isinstance(task, dict):
        sink.task = task.get('task', task.get('taskNr'))
    # 从这里开始 stdout 只留给协议行；实验代码的 print 走 stderr。
    sys.stdout = sys.stderr
    control = None
    if not args.no_control:
        control = ControlChannel(emit=sink).start()
        _install_signal_handlers(control)
    return execute_task(args.project, args.file, args.class_name, task,
                        control=control, emit=sink)


def main(argv=None):
    """runner 命令行入口：解析子命令并分派。

    Args:
        argv (list[str]|None): 命令行参数；None 用 ``sys.argv[1:]``。

    Returns:
        int: 退出码。
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(
        prog='quatm.experiment.runner',
        description='隔离子进程实验执行器（JSON 行协议；见模块 docstring）')
    subparsers = parser.add_subparsers(dest='command', required=True)
    run_parser = subparsers.add_parser(
        'run', help='加载实验，按 GUI ExperimentManager 语义执行一个任务')
    run_parser.add_argument('--project', required=True,
                            help='用户项目根目录（含 experiments/）')
    run_parser.add_argument('--file', required=True,
                            help='实验文件（相对 experiments/ 或绝对路径）')
    run_parser.add_argument('--class', dest='class_name', default=None,
                            help='实验类名（缺省取模块里第一个 Experiment 子类）')
    run_parser.add_argument('--task', default=None,
                            help='任务 JSON 字符串（缺省从 stdin 首行读取）')
    run_parser.add_argument('--no-control', action='store_true',
                            help='不读 stdin 控制命令（测试/手动运行用）')
    run_parser.set_defaults(func=run_command)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
