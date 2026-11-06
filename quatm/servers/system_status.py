# -*- coding: utf-8 -*-
"""系统状态快照聚合（任务 / 端口 / 健康）——供 GUI 监视器与 Web 共用。

本模块是**纯数据层**：只读 Properties / configfile / 文件系统并做本机探测，
**不写任何属性、不触发任何控制动作、不 import Qt / psutil**，可在任何进程
（GUI、Web、CLI、测试）里安全调用。GUI 窗口（the status view）只负责渲染这些字段。

字段契约
========

``collect_snapshot(props=None, *, probe_timeout=0.2, now=None) -> dict``

返回::

    {
      'ts': float,                                  # 快照时间戳（unix）
      'tasks': {
        'items': [ {                                # 按 taskNr 升序
            'taskNr': int|None,
            'expName': str, 'label': str,
            'status': str,                          # Queued/Running/Scanning/...
            'run': int|None, 'nRuns': int|None, 'repetition': int|None,
            'isolated': bool|None,                  # True=隔离子进程
            'pid': int|None,                        # 隔离子进程 pid（结束为 None）
            'started_at': float|None,               # 本次开始时间（unix）
            'elapsed_s': float|None,                # now-started_at（有 started_at 时）
            'last_event': str|None,                 # 最近事件摘要（≤200 字符）
            'save_ack': dict|None,                  # {'mode','strict','timeout_s','token'}
            'filepath': str, 'dueDateTime': str, 'priority': int|None,
            'terminated': bool|None, ...
        }, ...],
        'heartbeat_age_s': float|None,              # /Browser/ExperimentQueue/_heartbeat
        'browser_online': bool,                     # age < BROWSER_ONLINE_MAX_AGE_S
      },
      'ports': [ {                                  # configfile Servers 各 hub 端点
          'name': str,                              # imagepub/imagesub/.../propertyrep
          'service': str,                           # Image Hub / Property Logger / ...
          'endpoint': str,                          # 配置里的端点（缺失时用默认端口）
          'transport': 'tcp'|'ipc'|'other',
          'port': int|None,                         # tcp 端口
          'configured': bool,                       # configfile 是否显式配置了该端点
          'reachable': bool|None,                   # None=无法探测（other/预算耗尽）
          'error': str|None,
      }, ...],
      'health': {
          'save_errors': int|None, 'last_save_error': str|None,
          'data_manager_alive': bool|None, 'worker_blocked': bool|None,
          'disk_free_gb': float|None, 'data_dir': str|None,
          'min_free_gb': float|None, 'warn_free_gb': float|None,
          'stuck_alert_s': float|None, 'stuck_cleanup_s': float|None,
          'stuck_worker_grace_s': float|None,
          'save_ack_mode': str|None, 'save_ack_strict': bool|None,
          'save_ack_timeout_s': float|None, 'isolated_tasks': bool|None,
          # 长期运行资源采样（resource_monitor，只读、轻量）：
          'free_gb': float|None, 'total_gb': float|None,
          'used_pct': float|None,
          'message_db_bytes': int|None, 'push_db_bytes': int|None,
          'properties_json_bytes': int|None,
          'device_history_bytes': int|None,
          'open_fds': int|None, 'threads': int|None, 'rss_mb': float|None,
          'resource_alerts': list,             # [{'kind','level',...}]
      },
    }

只读与容错
==========

* Properties 读取优先直接查属性树（``props.properties`` + ``properties_lock``，
  **不创建缺失键**）；没有属性树（测试替身）时退回 ``props.get(path, default)``。
  任何读取异常都按“缺失”处理，不抛给调用方。
* ``props=None`` 时自行创建只读的 ``Properties('SystemStatus')`` 并**用后关闭**；
  创建失败 → 返回空快照（``items=[]`` / ``ports`` 仍按 configfile 探测）。
* 快照永远返回上述三段结构（缺数据就是空/None），GUI/Web 不必做存在性判断。

端口探测
========

* 覆盖 configfile ``Servers`` 里的 Image/Command/Data/Property/Message Hub 的
  ``pub``/``sub`` 与 Property Logger 的 ``rep``；端点缺失时回退
  :data:`quatm.servers.configreader.DEFAULT_TCP_PORTS`（``configured=False``）。
* ``tcp://host:port`` **只探本机 127.0.0.1:port**（不解析配置里的 LAN/0.0.0.0
  地址——那是绑定地址，不是本机连接地址），``socket.create_connection`` 带
  ``probe_timeout`` 超时；``ipc:///path`` 探“套接字文件是否存在”；
  ``inproc://``/抽象 ipc 等不可探测 → ``reachable=None`` 并写 ``error``。
* 总探测时间有预算（:data:`PROBE_BUDGET_S`），预算耗尽后剩余端点标
  ``reachable=None, error=<本地化文案>``（tr('probe budget exhausted')），单次 collect 不会拖住 GUI。
"""

import os
import shutil
import socket
import time

from quatm.servers.configreader import (
    DEFAULT_TCP_PORTS, PROJECT_ROOT, ConfigReader, server_endpoints)
from quatm.servers import resource_monitor
from quatm.utils.i18n import tr

#: ``/Browser/ExperimentQueue/_heartbeat`` 距今小于该秒数视为 Browser 在线。
BROWSER_ONLINE_MAX_AGE_S = 10.0

#: 单个端点探测的默认超时（秒）。
DEFAULT_PROBE_TIMEOUT = 0.2

#: 一次 collect 的探测总预算（秒）：防止多个不可达端点叠加拖慢 GUI。
PROBE_BUDGET_S = 1.8

#: 端点探测计划：``(configfile Servers 服务名, 端点键, 默认端口名)``。
ENDPOINT_SPECS = (
    ('Image Hub', 'pub', 'imagepub'),
    ('Image Hub', 'sub', 'imagesub'),
    ('Command Hub', 'pub', 'controlpub'),
    ('Command Hub', 'sub', 'controlsub'),
    ('Data Hub', 'pub', 'datapub'),
    ('Data Hub', 'sub', 'datasub'),
    ('Property Hub', 'pub', 'propertypub'),
    ('Property Hub', 'sub', 'propertysub'),
    ('Message Hub', 'pub', 'messagepub'),
    ('Message Hub', 'sub', 'messagesub'),
    ('Property Logger', 'rep', 'propertyrep'),
)

#: 健康字段 → Properties 路径（只读；缺失返回 None）。
HEALTH_PATHS = (
    ('save_errors', '/DataManager/save_errors'),
    ('last_save_error', '/DataManager/last_save_error'),
    ('data_manager_alive', '/DataManager/alive'),
    ('worker_blocked', '/Browser/ExperimentQueue/worker_blocked'),
    ('min_free_gb', '/Browser/ExperimentQueue/min_free_gb'),
    ('warn_free_gb', '/Browser/ExperimentQueue/warn_free_gb'),
    ('stuck_alert_s', '/Browser/ExperimentQueue/stuck_alert_s'),
    ('stuck_cleanup_s', '/Browser/ExperimentQueue/stuck_cleanup_s'),
    ('stuck_worker_grace_s', '/Browser/ExperimentQueue/stuck_worker_grace_s'),
    ('save_ack_mode', '/Browser/ExperimentQueue/save_ack_mode'),
    ('save_ack_strict', '/Browser/ExperimentQueue/save_ack_strict'),
    ('save_ack_timeout_s', '/Browser/ExperimentQueue/save_ack_timeout_s'),
    ('isolated_tasks', '/Browser/ExperimentQueue/isolated_tasks'),
)

#: 任务快照里保留的“字符串/布尔/数值”字段（其余字段按需筛选，见 _task_item）。
_TASK_STR_FIELDS = ('expName', 'label', 'status', 'filepath', 'dueDateTime',
                    'last_event', 'runner')
_TASK_INT_FIELDS = ('run', 'nRuns', 'repetition', 'pid', 'priority')
_TASK_FLOAT_FIELDS = ('started_at',)
_TASK_BOOL_FIELDS = ('isolated', 'terminated')


def _tree_lookup(tree, path, default):
    """在属性树 dict 里按 ``/a/b/c`` 取值（缺失返回 default，不创建条目）。"""
    node = tree
    for part in str(path).strip('/').split('/'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if isinstance(node, dict) and 'options' in node:
        node = node.get('value', default)
    return default if node is None else node


def _read_prop(props, path, default=None):
    """只读一个属性：优先查属性树（不创建缺失键），异常一律按缺失处理。

    Args:
        props: Properties 实例或测试替身（可为 None）。
        path (str): 绝对属性路径。
        default: 缺失/不可读时的返回值。

    Returns:
        属性值或 default。
    """
    if props is None:
        return default
    try:
        tree = getattr(props, 'properties', None)
    except Exception:  # noqa: BLE001 - 属性树不可读按缺失处理
        tree = None
    if isinstance(tree, dict):
        lock = getattr(props, 'properties_lock', None)
        try:
            if lock is not None:
                with lock:
                    return _tree_lookup(tree, path, default)
            return _tree_lookup(tree, path, default)
        except Exception:  # noqa: BLE001 - 读树失败按缺失处理
            return default
    try:
        return props.get(path, default)
    except Exception:  # noqa: BLE001 - 属性客户端不可用按缺失处理
        return default


def _safe_float(value, default=None):
    """把值转 float（None/非法值返回 default）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=None):
    """把值转 int（None/非法值返回 default）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_safe(value):
    """把任意值收敛成可 JSON 序列化的结构（未知对象转 str）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _default_props():
    """创建只读的 ``Properties('SystemStatus')``（独立函数便于测试注入失败）。"""
    from quatm.servers import Properties
    return Properties('SystemStatus', enable_monitors=False)


def _task_item(key, value, now):
    """把一个快照条目整理成监视器字段（缺失容错，保证 JSON 可序列化）。

    Args:
        key: 快照字典的键（通常是 taskNr 字符串）。
        value (dict): manager 发布的单任务快照。
        now (float): 快照时间戳（用于 elapsed_s）。

    Returns:
        dict: 规范化后的任务项。
    """
    item = {'taskNr': _safe_int(key)}
    for field in _TASK_STR_FIELDS:
        if field in value:
            item[field] = _json_safe(value.get(field))
    for field in _TASK_INT_FIELDS:
        item[field] = _safe_int(value.get(field))
    for field in _TASK_FLOAT_FIELDS:
        item[field] = _safe_float(value.get(field))
    for field in _TASK_BOOL_FIELDS:
        item[field] = bool(value[field]) if field in value else None
    if 'save_ack' in value:
        item['save_ack'] = _json_safe(value.get('save_ack'))
    started = item.get('started_at')
    item['elapsed_s'] = (max(0.0, now - started)
                         if started is not None else None)
    return item


def collect_tasks(props, now=None):
    """收集任务快照段（``items`` 排序 + 心跳/在线判定）。

    Args:
        props: Properties 实例/替身（可为 None）。
        now (float|None): 快照时间戳；None 用 ``time.time()``。

    Returns:
        dict: ``{'items': [...], 'heartbeat_age_s': float|None,
        'browser_online': bool}``。
    """
    now = time.time() if now is None else float(now)
    raw = _read_prop(props, '/Browser/ExperimentQueue/tasks', {}) or {}
    entries = []
    try:
        if isinstance(raw, dict):
            entries = list(raw.items())
        elif isinstance(raw, (list, tuple)):
            entries = [(None, item) for item in raw]
    except Exception:  # noqa: BLE001 - 畸形快照按空处理
        entries = []
    items = []
    for key, value in entries:
        if not isinstance(value, dict):
            continue
        try:
            item = _task_item(key, value, now)
            if item['taskNr'] is None:
                item['taskNr'] = _safe_int(value.get(
                    'task', value.get('taskNr')))
            items.append(item)
        except Exception:  # noqa: BLE001 - 单条坏数据不影响其它任务
            continue
    items.sort(key=lambda item: (item.get('taskNr') is None,
                                 item.get('taskNr') or 0))
    heartbeat = _safe_float(
        _read_prop(props, '/Browser/ExperimentQueue/_heartbeat', None))
    age = max(0.0, now - heartbeat) if heartbeat is not None else None
    return {'items': items, 'heartbeat_age_s': age,
            'browser_online': bool(age is not None
                                   and age < BROWSER_ONLINE_MAX_AGE_S)}


def parse_endpoint(endpoint):
    """解析端点字符串为 ``(transport, port)``。

    Args:
        endpoint (str|None): ``tcp://host:port`` / ``ipc:///path`` /
            ``inproc://name`` 或其它。

    Returns:
        tuple[str, int|None]: ``(transport, port)``；transport 为
        ``'tcp'|'ipc'|'other'``。
    """
    text = str(endpoint or '').strip()
    if not text:
        return 'other', None
    low = text.lower()
    if low.startswith('tcp://'):
        rest = text[len('tcp://'):]
        host_port = rest.split('/', 1)[0]
        port_part = host_port.rsplit(':', 1)[-1] if ':' in host_port else ''
        # 只取端口；主机名不解析（探测固定走 127.0.0.1，见 _probe_endpoint）
        return 'tcp', _safe_int(port_part)
    if low.startswith('ipc://'):
        return 'ipc', None
    return 'other', None


def _ipc_path(endpoint):
    """返回 ipc 端点的文件系统路径；抽象端点（``ipc://@name``）返回 None。"""
    text = str(endpoint or '')
    rest = text[len('ipc://'):] if text.lower().startswith('ipc://') else text
    if rest.startswith('@'):
        return None
    return rest or None


def probe_endpoint(endpoint, probe_timeout=DEFAULT_PROBE_TIMEOUT):
    """探测单个端点是否可达（只读；异常不抛）。

    Args:
        endpoint (str|None): 端点字符串。
        probe_timeout (float): tcp 连接超时（秒）。

    Returns:
        tuple[str, int|None, bool|None, str|None]:
            ``(transport, port, reachable, error)``；``reachable=None`` 表示
            该类型无法探测（``inproc``/抽象 ipc/空端点）。
    """
    transport, port = parse_endpoint(endpoint)
    try:
        if transport == 'tcp':
            if port is None:
                return transport, None, False, tr('malformed tcp endpoint')
            try:
                with socket.create_connection(
                        ('127.0.0.1', int(port)),
                        timeout=float(probe_timeout)):
                    pass
                return transport, port, True, None
            except OSError as exc:
                return transport, port, False, '%s: %s' % (
                    type(exc).__name__, exc)
        if transport == 'ipc':
            path = _ipc_path(endpoint)
            if path is None:
                return transport, None, None, tr(
                    'abstract ipc endpoint (not probeable)')
            try:
                exists = os.path.exists(path)
            except OSError as exc:
                return transport, None, False, str(exc)
            return transport, None, bool(exists), (
                None if exists else tr('ipc socket file not found'))
        return transport, None, None, tr(
            'in-process endpoint (not probeable)')
    except Exception as exc:  # noqa: BLE001 - 探测绝不能让快照失败
        return transport, port, False, '%s: %s' % (type(exc).__name__, exc)


def collect_ports(probe_timeout=DEFAULT_PROBE_TIMEOUT):
    """收集端口段：configfile 各 hub 端点 + 本机可达性探测。

    Args:
        probe_timeout (float): 单端点探测超时（秒）。

    Returns:
        list[dict]: 端口条目（字段见模块 docstring）。
    """
    try:
        conf = ConfigReader.getConfiguration() or {}
    except Exception:  # noqa: BLE001 - 无 configfile：全部按默认端口探测
        conf = {}
        conf_error = tr('configfile unavailable')
    else:
        conf_error = None
    timeout = max(0.01, float(probe_timeout))
    deadline = time.monotonic() + PROBE_BUDGET_S
    ports = []
    for service, key, default_name in ENDPOINT_SPECS:
        entry = {}
        try:
            entry = server_endpoints(conf, service)
        except Exception:  # noqa: BLE001 - 服务未配置：用默认端口
            entry = {}
        endpoint = entry.get(key) if isinstance(entry, dict) else None
        configured = bool(endpoint)
        if not endpoint:
            default_port = DEFAULT_TCP_PORTS.get(default_name)
            endpoint = (('tcp://127.0.0.1:%d' % default_port)
                        if default_port else '')
        if time.monotonic() > deadline:
            transport, port = parse_endpoint(endpoint)
            ports.append({'name': default_name, 'service': service,
                          'endpoint': str(endpoint), 'transport': transport,
                          'port': port, 'configured': configured,
                          'reachable': None,
                          'error': tr('probe budget exhausted')})
            continue
        transport, port, reachable, error = probe_endpoint(
            endpoint, probe_timeout=timeout)
        if conf_error is not None and not configured:
            # 没有 configfile 时端点是猜的默认端口：把“配置缺失”写进 error，
            # 避免把默认端口上的普通连接失败误报成服务故障。
            error = conf_error if not error else '%s; %s' % (conf_error, error)
        ports.append({'name': default_name, 'service': service,
                      'endpoint': str(endpoint), 'transport': transport,
                      'port': port, 'configured': configured,
                      'reachable': reachable, 'error': error})
    return ports


def collect_health(props, now=None):
    """收集健康段（DataManager / 队列看门狗 / 保存回执 / 磁盘）。

    Args:
        props: Properties 实例/替身（可为 None）。
        now (float|None): 保留参数（当前用于未来时间戳字段）。

    Returns:
        dict: 健康字段（缺失为 None；只读，不写任何属性）。
    """
    health = {}
    for field, path in HEALTH_PATHS:
        health[field] = _json_safe(_read_prop(props, path, None))
    data_dir = None
    disk_free = None
    try:
        root = str(PROJECT_ROOT)
        candidate = os.path.join(root, 'Data')
        data_dir = candidate if os.path.isdir(candidate) else root
        disk_free = float(shutil.disk_usage(data_dir).free) / (1024.0 ** 3)
    except Exception:  # noqa: BLE001 - 磁盘信息拿不到只是 None
        disk_free = None
    health['data_dir'] = data_dir
    health['disk_free_gb'] = (round(disk_free, 2)
                              if disk_free is not None else None)
    # 长期运行资源字段（轻量：不 walk Data/，重活留给 resource_monitor.sample_once；
    # 任何失败都只让对应字段为 None，绝不影响快照总耗时/可用性）。
    try:
        resource = resource_monitor.collect_resources(props)
    except Exception:  # noqa: BLE001 - 资源采样失败只影响新增字段
        resource = {}
    disk = resource.get('disk') or {}
    sizes = resource.get('sizes') or {}
    process = resource.get('process') or {}
    health['free_gb'] = _safe_float(disk.get('free_gb'))
    health['total_gb'] = _safe_float(disk.get('total_gb'))
    health['used_pct'] = _safe_float(disk.get('used_pct'))
    for field in ('message_db_bytes', 'push_db_bytes',
                  'properties_json_bytes', 'device_history_bytes'):
        health[field] = _safe_int(sizes.get(field))
    health['open_fds'] = _safe_int(process.get('open_fds'))
    health['threads'] = _safe_int(process.get('threads'))
    health['rss_mb'] = _safe_float(process.get('rss_mb'))
    alerts = resource.get('alerts') or []
    health['resource_alerts'] = [_json_safe(alert) for alert in alerts
                                 if isinstance(alert, dict)]
    return health


def _empty_tasks():
    """空任务段（props 不可用时的降级结构）。"""
    return {'items': [], 'heartbeat_age_s': None, 'browser_online': False}


def collect_snapshot(props=None, *, probe_timeout=DEFAULT_PROBE_TIMEOUT,
                     now=None):
    """收集一份系统状态快照（任务 / 端口 / 健康）；只读、不抛、不触发控制。

    Args:
        props: Properties 实例或测试替身；None 时临时创建只读客户端（用后关闭）。
        probe_timeout (float): 单个端口探测超时（秒），默认 0.2。
        now (float|None): 快照时间戳（测试注入）；None 用 ``time.time()``。

    Returns:
        dict: ``{'ts', 'tasks', 'ports', 'health'}``（字段见模块 docstring）。
    """
    ts = time.time() if now is None else float(now)
    own_props = False
    try:
        if props is None:
            props = _default_props()
            own_props = True
    except Exception:  # noqa: BLE001 - 属性服务不可用：空快照降级
        props = None
    try:
        try:
            tasks = collect_tasks(props, now=ts)
        except Exception:  # noqa: BLE001 - 任务段失败不影响端口/健康
            tasks = _empty_tasks()
        try:
            ports = collect_ports(probe_timeout=probe_timeout)
        except Exception:  # noqa: BLE001
            ports = []
        try:
            health = collect_health(props, now=ts)
        except Exception:  # noqa: BLE001
            health = {field: None for field, _path in HEALTH_PATHS}
            health['data_dir'] = None
            health['disk_free_gb'] = None
        return {'ts': ts, 'tasks': tasks, 'ports': ports, 'health': health}
    finally:
        if own_props:
            try:
                props.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响快照
                pass
