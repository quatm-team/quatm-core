# -*- coding: utf-8 -*-
"""长期运行健康采样：磁盘余量 / 本地库尺寸 / 进程 fd/线程/RSS（reliability hardening、reliability hardening）。

本模块只做**只读**探测，不写 Properties、不触发硬件/网络，供 GUI（Task &
Process Monitor 的 Health 页）、Web 与启动前 preflight 共用。设计约束：

* 模块级只依赖标准库 + ``quatm.servers`` / ``quatm.utils``（不 import
  drivers/GUI/web/clients/licensing），因此任何进程都能安全 import；
* **轻量/重活分离**：:func:`collect_resources` 默认只做 O(1) 探测（文件
  ``getsize``、``shutil.disk_usage``、``/dev/fd`` 计数），**不递归 walk**
  大目录；只有 :func:`sample_once` 才统计 ``Data/`` 递归大小与 ``*.h5`` 数量。
  这样 GUI 每秒快照不会被几万个数据文件拖死；
* 所有探测失败（权限、平台不支持、文件不存在）都返回 ``None`` 或空列表，
  **绝不抛异常**——健康采样是诊断工具，不能反过来把界面弄崩。

API
===

``resource_path(root=None) -> str``
    ``<项目>/configuration/health/resource_history.jsonl``（运行时状态）。

``collect_resources(props=None, *, include_data_dir=False, root=None) -> dict``
    返回::

        {'ts': float,
         'project': str,
         'disk': {'path': str|None, 'free_gb': float|None,
                  'total_gb': float|None, 'used_pct': float|None},
         'sizes': {'message_db_bytes': int|None, 'push_db_bytes': int|None,
                   'properties_json_bytes': int|None,
                   'device_history_bytes': int|None,
                   'resource_history_bytes': int|None,
                   'data_dir_bytes': int|None,      # 仅 include_data_dir=True
                   'h5_file_count': int|None},      # 仅 include_data_dir=True
         'process': {'pid': int, 'rss_mb': float|None, 'threads': int,
                     'open_fds': int|None, 'uptime_s': float},
         'thresholds': {'min_free_gb','warn_free_gb','max_db_mb',
                        'max_props_mb','max_fds','max_threads'},
         'alerts': [{'kind': str, 'level': 'error'|'warning',
                     'message': str, 'value': number}, ...]}

``sample_once(props=None, *, include_data_dir=True, notify=None, root=None) -> dict``
    ``collect_resources`` + 追加 JSONL 历史 + 跃迁告警（同类告警持续时不重复
    通知，恢复时发一条 info）。``notify(text, level)`` 可注入（测试/静默）；
    默认惰性导入 ``quatm.servers.send_msg``。返回的记录多一个
    ``'alert_transitions'`` 字段（本次真正的状态跃迁）。

``read_history(limit=500, root=None) -> list[dict]``
    读取最近记录（新的在后），坏行/半截行跳过，文件不存在返回 ``[]``。

阈值（只读，缺失用默认；``0`` 或负数表示关闭该项检查）
=====================================================

==========================  =========================  =========
字段                        Properties 路径            默认值
==========================  =========================  =========
min_free_gb                 /System/health/min_free_gb  1.0
warn_free_gb                /System/health/warn_free_gb 2.0
max_db_mb                   /System/health/max_db_mb    1024
max_props_mb                /System/health/max_props_mb 64
max_fds                     /System/health/max_fds      512
max_threads                 /System/health/max_threads  80
==========================  =========================  =========

历史文件格式
============

JSONL，每行一个 ``collect_resources`` 记录（UTF-8，``ts`` 为 epoch 秒）。
默认超过 :data:`DEFAULT_MAX_BYTES`（2 MB）或 :data:`DEFAULT_MAX_LINES`
（5000 行）就重写文件只保留最近 5000 行（原子 ``os.replace``）；坏行跳过。
"""

import json
import os
import shutil
import sys
import threading
import time

from quatm.servers.configreader import CONFIG_DIR, PROJECT_ROOT
from quatm.utils.i18n import tr

#: 历史文件字节上限（超过即轮转）。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
#: 历史文件保留的最大行数。
DEFAULT_MAX_LINES = 5000
#: 历史文件在项目下的相对路径。
HISTORY_REL = os.path.join('configuration', 'health', 'resource_history.jsonl')

#: 阈值默认值（``<=0`` 关闭对应检查）。
DEFAULT_THRESHOLDS = {
    'min_free_gb': 1.0,
    'warn_free_gb': 2.0,
    'max_db_mb': 1024,
    'max_props_mb': 64,
    'max_fds': 512,
    'max_threads': 80,
}

#: 阈值字段 → Properties 路径（只读）。
THRESHOLD_PATHS = {
    'min_free_gb': '/System/health/min_free_gb',
    'warn_free_gb': '/System/health/warn_free_gb',
    'max_db_mb': '/System/health/max_db_mb',
    'max_props_mb': '/System/health/max_props_mb',
    'max_fds': '/System/health/max_fds',
    'max_threads': '/System/health/max_threads',
}

#: 进程启动时刻（``uptime_s`` 的参考，模块 import 时记一次）。
_PROCESS_START = time.monotonic()
#: 递归统计 Data/ 的预算（秒）与最多遍历的目录项数（防失控）。
DATA_WALK_BUDGET_S = 3.0
DATA_WALK_MAX_ENTRIES = 500000
#: 进程内历史写入锁（同进程多线程采样时串行化；跨进程由 O_APPEND 保证整行）。
_LOCK = threading.Lock()
#: 上次采样时的告警状态：``{kind: level}``（跃迁去重）。
_ALERT_STATE = {}
_ALERT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 路径与只读属性
# ---------------------------------------------------------------------------

def _project_root(root=None):
    """返回项目根（测试可注入 ``root`` 覆盖 :data:`PROJECT_ROOT`）。"""
    return str(root) if root is not None else str(PROJECT_ROOT)


def _config_dir(root=None):
    """返回项目 ``configuration`` 目录（测试可注入 root）。"""
    if root is not None:
        return os.path.join(str(root), 'configuration')
    return str(CONFIG_DIR)


def _data_dir(root=None):
    """返回数据目录：``<项目>/Data``（不存在则项目根）。"""
    base = _project_root(root)
    candidate = os.path.join(base, 'Data')
    return candidate if os.path.isdir(candidate) else base


def resource_path(root=None):
    """返回健康历史 JSONL 的绝对路径（不创建目录）。

    Args:
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`。

    Returns:
        str: ``<root>/configuration/health/resource_history.jsonl``。
    """
    return os.path.join(_project_root(root), HISTORY_REL)


def _message_db_path(root=None):
    """返回消息库路径（优先复用 messagestore 的默认路径）。

    Args:
        root (str|None): 项目根；给出时按项目布局推导（测试用）。

    Returns:
        str: ``messages.db`` 路径。
    """
    if root is None:
        try:
            from quatm.servers.messagestore import _default_db_path
            return _default_db_path()
        except Exception:  # noqa: BLE001 - 导入失败退回项目布局
            pass
    return os.path.join(_config_dir(root), 'messagelist', 'messages.db')


def _push_db_path(root=None):
    """返回推送 outbox 路径 ``<configuration>/push/push.db``。"""
    return os.path.join(_config_dir(root), 'push', 'push.db')


def _properties_json_path(root=None):
    """返回属性树文件 ``<configuration>/properties/properties.json``。"""
    return os.path.join(_config_dir(root), 'properties', 'properties.json')


def _device_history_path(root=None):
    """返回设备健康历史文件路径（复用 device_history 的布局）。"""
    if root is None:
        try:
            from quatm.servers.device_history import history_path
            return history_path()
        except Exception:  # noqa: BLE001 - 导入失败退回项目布局
            pass
    return os.path.join(_config_dir(root), 'devices',
                        'health_history.jsonl')


def _read_prop(props, path, default=None):
    """只读一个属性（优先查属性树，不创建缺失键；异常按缺失处理）。

    Args:
        props: Properties 实例或替身（可为 None）。
        path (str): 绝对属性路径。
        default: 缺失/不可读时的返回值。

    Returns:
        属性值或 default。
    """
    if props is None:
        return default
    tree = getattr(props, 'properties', None)
    if isinstance(tree, dict):
        lock = getattr(props, 'properties_lock', None)
        try:
            if lock is not None:
                with lock:
                    return _tree_lookup(tree, path, default)
            return _tree_lookup(tree, path, default)
        except Exception:  # noqa: BLE001 - 读树失败按缺失
            return default
    try:
        return props.get(path, default)
    except Exception:  # noqa: BLE001 - 属性客户端不可用按缺失
        return default


def _tree_lookup(tree, path, default):
    """在属性树里按 ``/a/b/c`` 取值（缺失返回 default，不创建条目）。"""
    node = tree
    for part in str(path).strip('/').split('/'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if isinstance(node, dict) and 'options' in node:
        node = node.get('value', default)
    return default if node is None else node


def _read_number(props, path, default):
    """读一个数值属性（缺失/非法返回 default）。"""
    value = _read_prop(props, path, None)
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def read_thresholds(props=None):
    """读取健康阈值（只读；缺失/非法用默认值）。

    Args:
        props: Properties 实例或替身（可为 None）。

    Returns:
        dict: 与 :data:`DEFAULT_THRESHOLDS` 同键的阈值字典。
    """
    return {key: _read_number(props, THRESHOLD_PATHS[key],
                              DEFAULT_THRESHOLDS[key])
            for key in DEFAULT_THRESHOLDS}


# ---------------------------------------------------------------------------
# 尺寸 / 进程指标探测（全部 best-effort，失败返回 None）
# ---------------------------------------------------------------------------

def _file_size(path):
    """返回文件字节数；不存在/不可读返回 None。"""
    try:
        return int(os.path.getsize(path))
    except OSError:
        return None


def _walk_data_dir(path, budget_s=DATA_WALK_BUDGET_S,
                   max_entries=DATA_WALK_MAX_ENTRIES):
    """递归统计目录总字节数与 ``*.h5`` 文件数（受时间/条目预算限制）。

    **只有** :func:`sample_once`（及显式 ``include_data_dir=True``）会调用，
    GUI 每秒快照绝不触发它。权限错误由 ``os.walk`` 的默认行为跳过；顶层
    不可读、超预算或其它异常返回 ``(None, None)`` 且不抛。

    Args:
        path (str): 目录路径。
        budget_s (float): 遍历时间预算（秒）。
        max_entries (int): 最多检查的目录项数。

    Returns:
        tuple[int|None, int|None]: ``(总字节数, h5 文件数)``。
    """
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = DATA_WALK_BUDGET_S
    if budget <= 0:
        # Windows 的 monotonic 分辨率较粗：deadline=now 时紧随其后的
        # `time.monotonic() > deadline` 可能仍为 False，反而遍历并返回结果。
        # “预算 <= 0”语义上就是不允许遍历，直接返回 None。
        return None, None
    deadline = time.monotonic() + budget
    total = 0
    h5_count = 0
    entries = 0
    try:
        for folder, _dirs, files in os.walk(path):
            if time.monotonic() > deadline or entries > max_entries:
                return None, None
            for name in files:
                entries += 1
                if entries > max_entries:
                    return None, None
                full = os.path.join(folder, name)
                try:
                    total += int(os.path.getsize(full))
                except OSError:
                    continue
                if name.lower().endswith('.h5'):
                    h5_count += 1
    except OSError:
        return None, None
    return total, h5_count


def _collect_sizes(props=None, root=None, include_data_dir=False):
    """收集各本地库/历史的文件尺寸（轻量；Data 目录可选）。"""
    sizes = {
        'message_db_bytes': _file_size(_message_db_path(root)),
        'push_db_bytes': _file_size(_push_db_path(root)),
        'properties_json_bytes': _file_size(_properties_json_path(root)),
        'device_history_bytes': _file_size(_device_history_path(root)),
        'resource_history_bytes': _file_size(resource_path(root)),
        'data_dir_bytes': None,
        'h5_file_count': None,
    }
    if include_data_dir:
        total, count = _walk_data_dir(_data_dir(root))
        sizes['data_dir_bytes'] = total
        sizes['h5_file_count'] = count
    return sizes


def _collect_disk(root=None):
    """收集磁盘余量（失败时字段为 None，不抛）。"""
    path = _data_dir(root)
    disk = {'path': path, 'free_gb': None, 'total_gb': None,
            'used_pct': None}
    try:
        usage = shutil.disk_usage(path)
    except (OSError, ValueError):
        return disk
    try:
        total = float(usage.total)
        free = float(usage.free)
        used = float(getattr(usage, 'used', max(0.0, total - free)))
        disk['free_gb'] = round(free / (1024.0 ** 3), 2)
        disk['total_gb'] = round(total / (1024.0 ** 3), 2)
        disk['used_pct'] = round(100.0 * used / total, 1) if total > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return disk


def _rss_mb():
    """返回当前进程常驻内存 [MB]（POSIX 用 getrusage，Windows 用 ctypes）。"""
    try:
        if sys.platform.startswith('win'):
            return _rss_mb_windows()
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: KB；macOS: bytes（历史/平台差异）
        divisor = 1024.0 * 1024.0 if sys.platform == 'darwin' else 1024.0
        return round(float(usage) / divisor, 1)
    except Exception:  # noqa: BLE001 - 平台不支持按 None
        return None


def _rss_mb_windows():
    """Windows 上读取当前进程 RSS（失败返回 None）。"""
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD),
            ('PageFaultCount', wintypes.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                      ctypes.byref(counters),
                                      counters.cb):
        return None
    return round(float(counters.WorkingSetSize) / (1024.0 * 1024.0), 1)


def _open_fds():
    """返回当前进程打开的 fd/句柄数（POSIX 目录计数；Windows API）。"""
    try:
        if sys.platform.startswith('win'):
            import ctypes
            from ctypes import wintypes
            count = wintypes.DWORD(0)
            if not ctypes.windll.kernel32.GetProcessHandleCount(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    ctypes.byref(count)):
                return None
            return int(count.value)
        for folder in ('/proc/self/fd', '/dev/fd'):
            if os.path.isdir(folder):
                return len(os.listdir(folder))
        return None
    except Exception:  # noqa: BLE001 - 平台不支持按 None
        return None


def _collect_process():
    """收集进程级指标（pid/uptime 一定有；其余平台失败为 None）。"""
    return {
        'pid': os.getpid(),
        'rss_mb': _rss_mb(),
        'threads': threading.active_count(),
        'open_fds': _open_fds(),
        'uptime_s': round(max(0.0, time.monotonic() - _PROCESS_START), 1),
    }


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------

def _alert(kind, level, message, value):
    """构造一条告警记录。"""
    return {'kind': kind, 'level': level, 'message': message, 'value': value}


def _compute_alerts(disk, sizes, process, thresholds):
    """按阈值计算当前告警列表（只读、不发送通知）。

    Args:
        disk (dict): :func:`_collect_disk` 的结果。
        sizes (dict): :func:`_collect_sizes` 的结果。
        process (dict): :func:`_collect_process` 的结果。
        thresholds (dict): :func:`read_thresholds` 的结果。

    Returns:
        list[dict]: 告警列表（可能为空）。
    """
    alerts = []
    free_gb = disk.get('free_gb')
    min_free = float(thresholds.get('min_free_gb') or 0)
    warn_free = float(thresholds.get('warn_free_gb') or 0)
    if free_gb is not None:
        if min_free > 0 and free_gb < min_free:
            alerts.append(_alert(
                'disk_free', 'error',
                tr('disk free {free_gb:.2f} GB is below the minimum '
                   '{min_free_gb:.2f} GB',
                   free_gb=free_gb, min_free_gb=min_free), free_gb))
        elif warn_free > 0 and free_gb < warn_free:
            alerts.append(_alert(
                'disk_free', 'warning',
                tr('disk free {free_gb:.2f} GB is below the warning '
                   'threshold {warn_free_gb:.2f} GB',
                   free_gb=free_gb, warn_free_gb=warn_free), free_gb))
    db_bytes = 0
    for key in ('message_db_bytes', 'push_db_bytes'):
        if sizes.get(key) is not None:
            db_bytes += int(sizes[key])
    max_db_mb = float(thresholds.get('max_db_mb') or 0)
    if db_bytes and max_db_mb > 0:
        db_mb = db_bytes / (1024.0 * 1024.0)
        if db_mb > max_db_mb:
            alerts.append(_alert(
                'database', 'error',
                tr('message/push databases use {value_mb:.1f} MB '
                   '(limit {max_db_mb:.0f} MB)',
                   value_mb=db_mb, max_db_mb=max_db_mb), round(db_mb, 1)))
    props_bytes = sizes.get('properties_json_bytes')
    max_props_mb = float(thresholds.get('max_props_mb') or 0)
    if props_bytes and max_props_mb > 0:
        props_mb = props_bytes / (1024.0 * 1024.0)
        if props_mb > max_props_mb:
            alerts.append(_alert(
                'properties_json', 'warning',
                tr('properties.json uses {value_mb:.1f} MB '
                   '(limit {max_props_mb:.0f} MB)',
                   value_mb=props_mb, max_props_mb=max_props_mb),
                round(props_mb, 1)))
    open_fds = process.get('open_fds')
    max_fds = float(thresholds.get('max_fds') or 0)
    if open_fds is not None and max_fds > 0 and open_fds > max_fds:
        alerts.append(_alert(
            'open_fds', 'error',
            tr('open file descriptors {open_fds} exceed the limit '
               '{max_fds:.0f}', open_fds=open_fds, max_fds=max_fds),
            open_fds))
    threads = process.get('threads')
    max_threads = float(thresholds.get('max_threads') or 0)
    if threads is not None and max_threads > 0 and threads > max_threads:
        alerts.append(_alert(
            'threads', 'error',
            tr('thread count {threads} exceeds the limit {max_threads:.0f}',
               threads=threads, max_threads=max_threads), threads))
    return alerts


def _transition_alerts(alerts):
    """计算并更新告警状态，返回本次真正的跃迁列表。

    “同类告警持续不重复”：同一 ``kind`` 的告警等级没变化就不再产生跃迁；
    告警消失时产生一条 ``level='info'`` 的恢复记录。进程内模块级状态。

    Args:
        alerts (list[dict]): 当前告警列表。

    Returns:
        list[dict]: 跃迁列表，每项 ``{'kind','from','to','level','message'}``。
    """
    current = {}
    for alert in alerts or ():
        if isinstance(alert, dict) and alert.get('kind'):
            current[str(alert['kind'])] = alert
    transitions = []
    with _ALERT_LOCK:
        previous = dict(_ALERT_STATE)
        for kind, alert in current.items():
            old = previous.get(kind)
            new_level = str(alert.get('level') or 'warning')
            if old is None or old.get('level') != new_level:
                transitions.append({'kind': kind,
                                    'from': old.get('level') if old else None,
                                    'to': new_level,
                                    'level': new_level,
                                    'message': str(alert.get('message') or '')})
            _ALERT_STATE[kind] = dict(alert)
        for kind, old in previous.items():
            if kind not in current:
                transitions.append({'kind': kind,
                                    'from': old.get('level'),
                                    'to': None, 'level': 'info',
                                    'message': tr('resource alert cleared: '
                                                  '{kind}', kind=kind)})
                _ALERT_STATE.pop(kind, None)
    return transitions


def reset_alert_state():
    """清空跃迁去重状态（测试/重新开始监控用）。"""
    with _ALERT_LOCK:
        _ALERT_STATE.clear()


def _default_notify(text, level='warning'):
    """默认告警通路：发到 Message Hub（惰性导入，失败不抛）。"""
    try:
        from quatm.servers import send_msg
        send_msg(str(text), str(level if level in ('info', 'warning', 'error')
                               else 'warning'))
    except Exception:  # noqa: BLE001 - 消息系统不可用只影响通知
        pass


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def collect_resources(props=None, include_data_dir=False, root=None):
    """采集一份资源快照（只读；默认轻量，不递归 walk 大目录）。

    Args:
        props: Properties 实例或替身（可为 None）；只读阈值。
        include_data_dir (bool): True 时额外统计 ``Data/`` 递归大小与 ``*.h5``
            数量（一次 walk，有预算；GUI 每秒快照应保持 False）。
        root (str|None): 项目根覆盖（测试用）；None 用 :data:`PROJECT_ROOT`。

    Returns:
        dict: 字段见模块 docstring；任何探测失败只让对应字段为 None/空列表。
    """
    thresholds = read_thresholds(props)
    disk = _collect_disk(root)
    sizes = _collect_sizes(props, root, include_data_dir=include_data_dir)
    process = _collect_process()
    try:
        alerts = _compute_alerts(disk, sizes, process, thresholds)
    except Exception:  # noqa: BLE001 - 告警计算失败不影响其它字段
        alerts = []
    return {'ts': time.time(),
            'project': _project_root(root),
            'disk': disk,
            'sizes': sizes,
            'process': process,
            'thresholds': thresholds,
            'alerts': alerts}


def _record_line(record):
    """把记录序列化为一行 JSON（无法序列化的字段转 str，绝不抛）。"""
    return json.dumps(record, ensure_ascii=False, default=str) + '\n'


def _line_count(path):
    """统计文件行数（读失败返回 0）。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return sum(1 for _line in handle)
    except OSError:
        return 0


def _rotate(path, max_lines):
    """把历史文件重写成“只保留最近 ``max_lines`` 行”（原子替换）。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()
    except OSError:
        return False
    keep = lines[-max_lines:] if max_lines > 0 else []
    temp = '%s.tmp.%d' % (path, os.getpid())
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ''.join(keep).encode('utf-8'))
        finally:
            os.close(fd)
        os.replace(temp, path)
        return True
    except OSError:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        return False


def _maybe_rotate(path, max_bytes, max_lines):
    """超过字节/行数上限时轮转（保留最近 max_lines 行）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size > int(max_bytes) or _line_count(path) > int(max_lines):
        _rotate(path, int(max_lines))


def append_history(record, root=None, max_bytes=DEFAULT_MAX_BYTES,
                   max_lines=DEFAULT_MAX_LINES):
    """追加一条历史记录（失败返回 False，不抛异常）。

    Args:
        record (dict): 记录；非 dict 直接返回 False。
        root (str|None): 项目根覆盖（测试用）。
        max_bytes (int): 字节上限（超过即轮转）。
        max_lines (int): 轮转后保留的最大行数。

    Returns:
        bool: 写入成功（或记录非法时 False）。
    """
    if not isinstance(record, dict):
        return False
    payload = dict(record)
    payload.setdefault('ts', time.time())
    path = resource_path(root)
    try:
        line = _record_line(payload).encode('utf-8')
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
    except (OSError, TypeError, ValueError):
        return False
    with _LOCK:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError:
            return False
        try:
            _maybe_rotate(path, max_bytes, max_lines)
        except OSError:
            pass
    return True


def read_history(limit=500, root=None):
    """读取最近的资源历史记录（新的在后），坏行跳过。

    Args:
        limit (int): 最多返回多少条（``<=0`` 不限）。
        root (str|None): 项目根覆盖（测试用）。

    Returns:
        list[dict]: 解析成功的记录；文件不存在/不可读返回空列表。
    """
    path = resource_path(root)
    records = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # 坏行/半截写入：跳过
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    if limit is not None and int(limit) > 0:
        records = records[-int(limit):]
    return records


def sample_once(props=None, include_data_dir=True, notify=None, root=None,
                max_bytes=DEFAULT_MAX_BYTES, max_lines=DEFAULT_MAX_LINES):
    """采集一次资源快照、写历史并处理告警跃迁（GUI “Sample now” 用）。

    Args:
        props: Properties 实例或替身（只读阈值）。
        include_data_dir (bool): 是否统计 ``Data/`` 递归大小（重活）。
        notify (callable|None): ``notify(text, level)``；None 用默认 Message Hub
            通路。测试可注入收集器。
        root (str|None): 项目根覆盖（测试用）。
        max_bytes (int): 历史字节上限。
        max_lines (int): 历史保留行数。

    Returns:
        dict: 资源快照记录（额外含 ``alert_transitions``）。
    """
    record = collect_resources(props, include_data_dir=include_data_dir,
                               root=root)
    try:
        transitions = _transition_alerts(record.get('alerts') or [])
    except Exception:  # noqa: BLE001 - 跃迁计算失败不影响采样
        transitions = []
    record['alert_transitions'] = transitions
    sender = notify or _default_notify
    for transition in transitions:
        try:
            sender(str(transition.get('message') or ''),
                   str(transition.get('level') or 'warning'))
        except Exception:  # noqa: BLE001 - 通知失败不影响采样
            pass
    try:
        append_history(record, root=root, max_bytes=max_bytes,
                       max_lines=max_lines)
    except Exception:  # noqa: BLE001 - 历史写失败不影响采样结果
        pass
    return record
