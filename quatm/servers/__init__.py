"""Core servers for messaging, properties, data and process lifecycle."""

import os
import pkgutil
import threading
import time

# Allow the private platform to add ``quatm.servers`` modules (for example the
# Web Hub) when both source trees are on PYTHONPATH. The core itself remains
# complete without those modules.
__path__ = pkgutil.extend_path(__path__, __name__)

# The heartbeat module is the single cross-platform implementation of parent
# liveness. Import it before starting the parent watchdog; heartbeat only uses
# the standard library and cannot create an import cycle.
from .heartbeat import parent_alive

#: 父进程死亡钩子的总预算（秒）：到点无论钩子跑没跑完都强制 ``os._exit(0)``。
PARENT_DEATH_TOTAL_TIMEOUT_S = 10.0

#: 已注册的父进程死亡钩子：``[(callback, timeout_s), ...]``（注册顺序）。
#: 执行时**逆序**（后注册先执行，类似 atexit：依赖别人的模块先收尾）。
_PARENT_DEATH_HOOKS = []
_PARENT_DEATH_HOOKS_LOCK = threading.Lock()


def register_parent_death_hook(callback, timeout=5.0):
    """注册“父进程死亡”时要执行的优雅收尾钩子（硬件安全态）。

    背景：父进程（Browser/ProcessManager）被 ``kill -9`` 时，子进程原来的
    看门狗直接 ``os._exit(0)``：设备句柄没关、实时输出保持最后状态、h5 可能
    没 flush。现在子进程可以先跑收尾钩子，再退出。

    约定：

    * 回调在**独立线程**里执行，主看门狗 ``join(timeout)``，避免一个卡死的
      回调把整个退出流程拖死；
    * 多个钩子按注册**逆序**执行（后注册先跑）；
    * 单个回调最多 ``timeout`` 秒；总预算
      :data:`PARENT_DEATH_TOTAL_TIMEOUT_S` 秒，到点强制退出；
    * 任何异常都被吞掉（父进程已死，没有人能处理它）；
    * 回调里不要 import 重模块：父进程死亡是紧急路径，提前准备好对象/数据；
    * 没有注册任何钩子时行为与旧版一致（检测到父死立即退出）。

    Args:
        callback (callable): 无参可调用对象（收尾动作）。
        timeout (float): 该回调最多执行多久（秒）；非法值按 5.0 处理。
    """
    if not callable(callback):
        return
    try:
        timeout_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        timeout_s = 5.0
    with _PARENT_DEATH_HOOKS_LOCK:
        _PARENT_DEATH_HOOKS.append((callback, timeout_s))


def _safe_call_parent_death_hook(callback):
    """执行一个父进程死亡钩子，吞掉所有异常（含 SystemExit/KeyboardInterrupt）。"""
    try:
        callback()
    except BaseException:  # noqa: BLE001 - 紧急退出路径：绝不能因回调异常中断
        pass


def _run_parent_death_hooks(total_timeout=PARENT_DEATH_TOTAL_TIMEOUT_S):
    """父进程死亡时执行所有钩子（逆序、独立线程 + 超时、总预算）。

    Args:
        total_timeout (float): 所有钩子的总预算（秒）；到点直接返回
            （调用方随后 ``os._exit(0)``）。
    """
    try:
        with _PARENT_DEATH_HOOKS_LOCK:
            hooks = list(reversed(_PARENT_DEATH_HOOKS))
    except Exception:  # noqa: BLE001 - 取钩子列表失败按“没有钩子”处理
        return
    if not hooks:
        return
    try:
        deadline = time.monotonic() + max(0.0, float(total_timeout))
    except (TypeError, ValueError):
        deadline = time.monotonic() + PARENT_DEATH_TOTAL_TIMEOUT_S
    for callback, timeout_s in hooks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            thread = threading.Thread(
                target=_safe_call_parent_death_hook, args=(callback,),
                name='quatm-parent-death-hook', daemon=True)
            thread.start()
            thread.join(timeout=min(float(timeout_s), remaining))
        except Exception:  # noqa: BLE001 - 单个钩子启动/等待失败不影响其它钩子
            continue


def _start_parent_watchdog():
    """父进程看门狗：ProcessManager/Web 启动子进程时注入 QUATM_PARENT_PID。

    如果父进程（管理器）被强杀或崩溃，子进程会变成孤儿继续运行、继续占用
    端口。看门狗每 2s 检查父进程是否存活；父进程消失后：

    1. 若有注册的优雅钩子（:func:`register_parent_death_hook`），按逆序
       执行它们（各自限时、总预算 :data:`PARENT_DEATH_TOTAL_TIMEOUT_S`）；
    2. 不管钩子结果如何，最后 ``os._exit(0)`` 由 OS 释放全部套接字/端口。

    没有钩子时第 1 步是空操作，行为与旧版完全一致。
    """
    raw = os.environ.get('QUATM_PARENT_PID', '').strip()
    if not raw:
        return
    try:
        ppid = int(raw)
    except ValueError:
        return
    if ppid <= 0:
        return

    def watch():
        while True:
            time.sleep(2.0)
            if not parent_alive(ppid):
                _run_parent_death_hooks()
                os._exit(0)

    threading.Thread(target=watch, name='quatm-parent-watchdog', daemon=True).start()


_start_parent_watchdog()

# ---------------------------------------------------------------------------
# Licensing and activation are entry-point concerns in the optional platform.
# The public core must remain importable without the commercial package.
# ---------------------------------------------------------------------------

import zmq
zmqcontext = zmq.Context()
EVENT_MAP = {}
# print("Event names:")
for name in dir(zmq):
    if name.startswith('EVENT_'):
        value = getattr(zmq, name)
        # print("%21s : %4i" % (name, value))
        EVENT_MAP[value] = name
from .clients import DataClient
from .clients import ImageClient
from .clients import CommandClient
from .messageclient import send_error, send_warning, send_info, send_debug, send_msg
from .properties import Properties
from .properties import PropertyAttribute
from .configreader import (workpath, driverpath, iconpath, datapath,
                           experiment_path, configpath,
                           PROJECT_ROOT, CONFIG_DIR, EXPERIMENTS_DIR, DATA_DIR,
                           IMAGES_DIR, QUATM_DIR)
from quatm.utils.i18n import tr