"""心跳文件的共用实现：原子写 / 读校验 / 清除 + 跨平台 pid 存活判断。

仓库里“谁在运行/谁是唯一写者”这类心跳（``message_logger.lock``、
``message_receiver.lock``、``push_gateway.lock``、Property/看门狗）原来各自
实现了一遍 JSON 落盘、原子替换与 pid 检查。本模块是唯一实现：

* :func:`write_heartbeat`：写 ``{pid, host, name, ts, ...}``，同目录临时文件 +
  ``os.replace``，读取方永远看不到半截 JSON；
* :func:`read_heartbeat`：解析 + 年龄校验 + 同主机时校验 pid 是否存活；
* :func:`clear_heartbeat`：正常退出时删除心跳（被强杀时靠年龄过期兜底）；
* :func:`pid_alive` / :func:`parent_alive`：跨平台进程存活判断
  （Windows 用 ``OpenProcess``/``GetExitCodeProcess``，POSIX 用
  ``os.kill(pid, 0)``，不依赖 psutil）。

调用方自己决定心跳文件放在哪、文件里放什么业务字段（通过 ``extra`` 传入），
因此旧的 JSON 格式（多出的 ``kind``/``db`` 等键）仍然可按原样读写。
"""

import ctypes
import json
import os
import socket
import sys
import time


def pid_alive(pid, unknown=True):
    """跨平台判断一个进程是否仍在运行（不依赖 psutil）。

    Args:
        pid (int): 目标进程号。
        unknown (bool): 无法确认时的返回值。心跳检查应传 True（宁可多信一次
            心跳，也不要误判“写者已死”而重复写库）。

    Returns:
        bool: True=存活；False=确定已退出（或 pid 非法）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == still_active
        except Exception:  # noqa: BLE001 - 无法确认：交给调用方的 unknown 策略
            return bool(unknown)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parent_alive(pid):
    """看门狗用的“父进程还在吗”（与 :func:`pid_alive` 同一实现）。

    保留这个名字是因为 ``quatm.servers.__init__`` 的父进程看门狗在包
    import 早期就要调用它；把实现放在本模块可以避免看门狗与心跳逻辑各写
    一份。

    Args:
        pid (int): 父进程号。

    Returns:
        bool: 仍存活为 True；无法确认时按存活处理。
    """
    return pid_alive(pid, unknown=True)


def write_heartbeat(path, name='', extra=None):
    """原子地写一份心跳 JSON（同目录临时文件 + ``os.replace``）。

    Args:
        path (str): 心跳文件路径；目录不存在时自动创建。
        name (str): 写入者名字（诊断用）。
        extra (dict|None): 额外字段（如 ``kind``/``db``/业务计数）；名为
            ``ts`` 的键会覆盖默认时间戳（测试/特殊场景用）。

    Returns:
        bool: 写成功为 True；失败不抛异常（调用方可能因此以为你不在，
        但绝不能因为心跳失败把服务打挂）。
    """
    payload = {'pid': os.getpid(), 'host': socket.gethostname(),
               'name': str(name), 'ts': time.time()}
    if isinstance(extra, dict):
        payload.update(extra)
    target = str(path)
    try:
        folder = os.path.dirname(os.path.abspath(target))
        os.makedirs(folder, exist_ok=True)
        tmp = target + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001 - 心跳失败绝不能让服务/推送挂掉
        return False
    return True


def read_heartbeat(path, max_age=30.0, check_pid=True):
    """读取心跳并校验“是否仍然新鲜/可能还活着”。

    判定为无效的情况：文件不存在/不是 JSON/不是 dict、``ts`` 非法、
    年龄超过 ``max_age``、或（同主机且 ``check_pid`` 时）写者 pid 已退出。

    Args:
        path (str): 心跳文件路径。
        max_age (float): 心跳最大可接受年龄 [s]。
        check_pid (bool): 是否在“同主机”时校验 pid 存活。

    Returns:
        dict|None: 有效时返回心跳内容副本（额外带 ``age`` 秒数，缺省
        ``pid`` 补 0）；无效时返回 None。
    """
    try:
        with open(str(path), 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        age = time.time() - float(payload.get('ts', 0))
    except (TypeError, ValueError):
        return None
    if age < 0:
        # 轻微时钟回拨/写入方时间戳略超前：当作“刚写过”，不要误判为过期。
        age = 0.0
    if age > float(max_age):
        return None
    if check_pid:
        host = payload.get('host')
        if host and host == socket.gethostname() \
                and not pid_alive(payload.get('pid')):
            return None
    info = dict(payload)
    info['age'] = age
    info.setdefault('pid', 0)
    return info


def clear_heartbeat(path):
    """删除心跳文件（正常退出时调用；重复调用/文件不存在都安全）。

    Args:
        path (str): 心跳文件路径。

    Returns:
        bool: 确实删掉了为 True。
    """
    try:
        os.remove(str(path))
    except OSError:
        return False
    return True
