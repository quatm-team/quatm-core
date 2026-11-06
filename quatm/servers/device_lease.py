"""跨进程设备租约：记录“哪台仪器正被谁打开”，用文件原子互斥。

为什么不用 Properties
--------------------
Properties 是属性树：只能读写“值”，没有“谁先到谁独占”的原子 CAS，也不知道
写者是否还活着。两个进程可以同时读-改-写同一键而都以为自己拿到了设备。
这里改用**文件**：``os.open(path, O_CREAT | O_EXCL)`` 由内核保证同一路径只有
一个创建者成功（跨进程、跨线程都成立），随后把租约内容原子写入该文件；心跳
字段（``pid``/``host``/``ts``）让其它进程能判断持有者是否还活着、是否需要
清理陈旧租约。这样设备健康视图（GUI）与 selftest 探针也能读到同一个事实。

租约文件
--------
``<项目根>/configuration/devices/leases/<sha1(归一化地址)[:16]>.json``
（目录 0700、文件 0600），内容::

    {address, name, owner, pid, host, experiment, task, acquired_at, ts,
     kind, nonce, heartbeat, ttl_s}

``kind`` 区分两类租约：``'address'``（经 ``open_transport`` 打开的设备地址）
与 ``'name'``（DLL/本地设备按 device_db 设备名，地址形如 ``name://adwin``）。
旧文件没有 ``kind`` 时按 ``address`` 处理（向后兼容）。

地址先经 :func:`normalize_address` 归一化，因此 ``tcp://HOST:5025``、
``socket://host:05025`` 指的是同一台设备、同一个租约；``COM03`` 与
``serial://COM3`` 同理。整个 ``configuration/devices/`` 目录是运行时状态
（租约/历史/报告），已加入 ``.gitignore``。

总线锁（``bus_<hash>.json``）是另一类短时文件锁：只覆盖“打开设备”那一小段
时间（GPIB/USB/串口/TCP 适配器并发打开会卡死），TTL 默认 15s；它不属于设备
租约，``list_leases``/``clean_stale`` 不处理它（由 :func:`acquire_bus` 自己
回收陈旧锁）。

语义（详见各函数）
------------------
* 同地址/同名已被**存活**进程占用 → :func:`acquire`/:func:`acquire_name`
  返回 ``None``（``wait>0`` 时每 100 ms 轮询到超时）；持锁进程已退出或租约
  超龄 → 清理后重新获取；非本机 host 的租约按“占用”处理（不误抢）。
* 租约写失败（只读盘/权限不足）只打印警告并返回 ``None``，不抛异常——
  记录不了不能把实验打挂。
* ``QUATM_DEVICE_LEASE=0`` 或 configfile 的 ``Devices/device_lease: false``
  可全局关闭（见 :func:`lease_enabled`）；``QUATM_DEVICE_BUS_LOCK=0`` 单独
  关闭总线锁（见 :func:`bus_lock_enabled`）。
"""

import contextlib
import hashlib
import os
import re
import socket
import sys
import time
import uuid

from quatm.servers import heartbeat, jsonutil

#: 按名租约（DLL/本地设备）的地址前缀：``name://<device_db 名>``。
NAME_PREFIX = 'name://'

#: 总线锁默认 TTL [s]：只保护“打开设备”这一小段时间，过期即可被回收。
BUS_TTL_S = 15.0

#: 关闭总线锁的环境变量（``0/false/no/off``）。
ENV_BUS_LOCK = 'QUATM_DEVICE_BUS_LOCK'

#: 租约默认超龄时间 [s]：超过它即使 pid 看起来还活着也允许清理（跨主机时
#: 无法检查 pid，需要这个兜底；24 h 远长于任何正常实验）。
DEFAULT_TTL_S = 24 * 60 * 60.0

#: 心跳租约的默认 TTL/续期参数。
#:
#: 常年自动运行的实验可能连续跑几周；若只用 24h TTL 而进程崩溃，陈旧租约
#: 最长要 24h 才被回收（期间设备被"幽灵锁"挡住）；而 TTL 太短、没有续期机制
#: 又会误抢正常运行的长实验。因此 transport 打开的租约默认带**心跳续期**：
#: TTL 默认 120s，每 TTL/3 刷新一次 ``ts``；进程崩溃后最多 TTL 秒即可回收。
HEARTBEAT_TTL_S = 120.0
#: 心跳最小续期间隔（秒），避免 TTL 配得极小时线程空转。
HEARTBEAT_MIN_INTERVAL_S = 0.25
#: 覆盖心跳 TTL 的环境变量（秒）。
ENV_HEARTBEAT_TTL = 'QUATM_DEVICE_LEASE_TTL_S'

#: 内容损坏/半截的租约文件多久算陈旧 [s]。刚创建、正在写内容的文件必须
#: 当作“可能有人持有”，否则并发会误抢。
_BROKEN_LEASE_STALE_S = 60.0

#: ``acquire(wait=...)`` 的轮询间隔 [s]。
_POLL_INTERVAL_S = 0.1

#: 清理陈旧文件后重试的次数（并发时被别人抢先一步就放弃本次 acquire）。
_ACQUIRE_ATTEMPTS = 3

#: 租约目录相对项目根的位置。
_LEASE_SUBDIR = ('configuration', 'devices', 'leases')

_SCHEME_RE = re.compile(
    r'^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<rest>.*)$')
_HOSTPORT_RE = re.compile(r'^(?P<host>[A-Za-z0-9_.\-]+):(?P<port>[0-9]+)$')
_COM_PORT_RE = re.compile(r'^COM\s*([0-9]+)$', re.IGNORECASE)


def _tr(text):
    """延迟导入 i18n 的 ``tr``（本模块在 servers 包内，避免 import 期循环）。

    Args:
        text (str): 英文原文（同时是 zh.json 的 key）。

    Returns:
        str: 当前语言下的译文；缺 key 时回退英文原文。
    """
    from quatm.utils.i18n import tr
    return tr(text)


def _project_root():
    """返回当前项目根（延迟读 ``configreader``，便于测试/多项目进程切换）。

    Returns:
        str: 项目根目录；拿不到时退回当前工作目录。
    """
    try:
        from quatm.servers import configreader
        root = getattr(configreader, 'PROJECT_ROOT', None)
    except Exception:  # noqa: BLE001 - 租约不能因为项目配置问题把调用方打挂
        root = None
    if root:
        return str(root)
    return os.getcwd()


def _leases_dir():
    """返回租约目录的绝对路径（不创建）。

    Returns:
        str: ``<项目根>/configuration/devices/leases``。
    """
    return os.path.join(_project_root(), *_LEASE_SUBDIR)


def _local_host():
    """返回本机主机名（与心跳文件同一判定口径）。

    Returns:
        str: ``socket.gethostname()`` 的结果。
    """
    return socket.gethostname()


def lease_enabled():
    """设备租约是否启用：环境变量优先，其次 configfile 的 ``Devices`` 段。

    ``QUATM_DEVICE_LEASE=0/false/no/off`` 关闭；其它非空值开启；未设置时
    读 ``configfile.json`` 的 ``Devices/device_lease``（缺省开启）。配置文件
    读不了也按开启处理（安全默认：宁可拒绝并发打开，也不要静默抢设备）。

    Returns:
        bool: True=``open_transport`` 打开设备前必须拿到租约。
    """
    raw = os.environ.get('QUATM_DEVICE_LEASE')
    if raw is not None and str(raw).strip() != '':
        return str(raw).strip().lower() not in (
            '0', 'false', 'no', 'off', 'disabled')
    try:
        from quatm.servers import configreader
        config = jsonutil.read_json(configreader.configfilename, default={})
    except Exception:  # noqa: BLE001 - 配置问题不应改变默认行为
        return True
    if isinstance(config, dict):
        devices = config.get('Devices')
        if isinstance(devices, dict) and 'device_lease' in devices:
            return bool(devices.get('device_lease'))
    return True


def bus_lock_enabled():
    """总线级短时互斥是否启用（``QUATM_DEVICE_BUS_LOCK=0`` 可关闭）。

    Returns:
        bool: True=打开设备前必须先拿到总线锁（默认）。
    """
    raw = os.environ.get(ENV_BUS_LOCK)
    if raw is None or str(raw).strip() == '':
        return True
    return str(raw).strip().lower() not in (
        '0', 'false', 'no', 'off', 'disabled')


def normalize_name(name):
    """把 device_db 设备名归一化成按名租约 key：``name://<名字>``。

    只去掉首尾空白（设备名是代码里的标识符，大小写/内部字符保持原样）；
    空名/None 返回 ``None``（不租）。已经带 ``name://`` 前缀的输入会被
    再次归一化（幂等）。

    Args:
        name (str|None): device_db 设备名。

    Returns:
        str|None: ``name://<name>``，或 ``None``。
    """
    if name is None:
        return None
    if not isinstance(name, str):
        name = str(name)
    text = name.strip()
    if not text:
        return None
    if text.startswith(NAME_PREFIX):
        text = text[len(NAME_PREFIX):].strip()
        if not text:
            return None
    return NAME_PREFIX + text


def normalize_address(address):
    """把地址归一化成租约 key（同一台设备的多种写法得到同一字符串）。

    规则：

    * ``tcp://``/``socket://``/``scpi://``/``raw://`` → ``tcp://<小写 host>:<端口>``
      （无端口时用 SCPI 默认 5025；``host:5025`` 这种裸写法也归一到 tcp）；
    * ``serial://``/``asrl://`` → ``serial://<设备路径>``（丢弃 ``?baudrate=``
      查询串；``COM03`` 统一成 ``COM3``；POSIX 设备路径保持大小写）；
    * ``visa://``/``instr://`` 与纯 VISA 资源名 → 去掉首尾空白后的资源名；
    * ``name://<设备名>`` → 按 :func:`normalize_name` 归一（DLL/本地设备按名租约
      与地址租约共用同一套 renew/is_held/release/list_leases 实现）；
    * 空地址/非字符串 → ``None``（表示“不租”）。

    Args:
        address (str|None): 仪器地址。

    Returns:
        str|None: 归一化地址；空地址返回 ``None``。
    """
    if address is None:
        return None
    if not isinstance(address, str):
        address = str(address)
    text = address.strip()
    if not text:
        return None
    match = _SCHEME_RE.match(text)
    if match:
        scheme = match.group('scheme').lower()
        rest = match.group('rest')
        if scheme in ('tcp', 'socket', 'scpi', 'raw'):
            return _normalize_tcp(rest)
        if scheme in ('serial', 'asrl'):
            return _normalize_serial(rest)
        if scheme in ('visa', 'instr'):
            rest = rest.strip()
            return rest or None
        if scheme == 'name':
            return normalize_name(rest)
        return text
    if '::' in text:
        return text
    port_match = _HOSTPORT_RE.match(text)
    if port_match:
        return 'tcp://%s:%d' % (port_match.group('host').lower(),
                                int(port_match.group('port')))
    if _COM_PORT_RE.match(text) or text.startswith('/dev/') \
            or text.startswith('\\\\.\\'):
        return _normalize_serial(text)
    return text


def _normalize_tcp(rest):
    """归一化 ``tcp://`` 之后的 ``host[:port]`` 部分。

    Args:
        rest (str): ``tcp://`` 后面的内容（可能带前导 ``/``）。

    Returns:
        str|None: ``tcp://host:port``；host 为空时返回 ``None``。
    """
    rest = rest.lstrip('/').strip()
    host, sep, port = rest.partition(':')
    host = host.strip().lower()
    if not host:
        return None
    port = port.strip()
    port_number = int(port) if (sep and port.isdigit()) else 5025
    return 'tcp://%s:%d' % (host, port_number)


def _normalize_serial(rest):
    """归一化 ``serial://`` 之后的设备路径（去掉查询串、统一 COM 大小写）。

    Args:
        rest (str): ``serial://`` 后面的内容（可能带前导 ``/`` 与 ``?查询串``）。

    Returns:
        str|None: ``serial://<设备>``；设备为空时返回 ``None``。
    """
    body = rest.partition('?')[0].strip()
    if not body:
        return None
    if body.startswith('/'):
        device = '/' + body.lstrip('/')
    else:
        device = body.lstrip('/')
    com_match = _COM_PORT_RE.match(device)
    if com_match:
        device = 'COM%d' % int(com_match.group(1))
    return 'serial://' + device


def _path_for(normalized):
    """由归一化地址算出租约文件路径（内部用，不再重复归一化）。

    Args:
        normalized (str): 已归一化地址。

    Returns:
        str: ``<项目根>/configuration/devices/leases/<hash>.json``。
    """
    digest = hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:16]
    return os.path.join(_leases_dir(), digest + '.json')


def lease_path(address):
    """返回某个地址对应的租约文件路径。

    Args:
        address (str|None): 仪器地址。

    Returns:
        str: 租约文件路径；空地址/无法归一化时返回空串。
    """
    normalized = normalize_address(address)
    if not normalized:
        return ''
    return _path_for(normalized)


def _ensure_leases_dir():
    """创建租约目录（0700）并返回路径；失败时打印警告并返回 ``None``。

    Returns:
        str|None: 目录路径，或 ``None``（权限/IO 问题）。
    """
    folder = _leases_dir()
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        # umask 会让中间目录（configuration/devices）用默认权限：租约/健康
        # 历史里含 pid 与主机名，能收紧就收紧（失败不致命）。
        for directory in (folder, os.path.dirname(folder)):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        return folder
    except OSError as exc:
        print(_tr('[Device Lease] cannot create the lease directory %s: %s')
              % (folder, exc), file=sys.stderr)
        return None


def _read_lease_file(path):
    """读取租约文件内容；不存在/损坏/不可读都返回 ``None``。

    Args:
        path (str): 租约文件路径。

    Returns:
        dict|None: 租约内容副本，或 ``None``。
    """
    try:
        payload = jsonutil.read_json(path, default=None)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _file_mtime(path):
    """返回文件修改时间；取不到时返回 ``None``。

    Args:
        path (str): 文件路径。

    Returns:
        float|None: mtime 时间戳。
    """
    try:
        return float(os.stat(path).st_mtime)
    except OSError:
        return None


def _is_stale(record, path, ttl_s):
    """判断一条租约是否已经陈旧（可以清理）。

    判定顺序：

    1. 文件存在但 JSON 损坏/为空 → 只有写入超过
       :data:`_BROKEN_LEASE_STALE_S` 秒才算陈旧（正在写的文件不能抢）；
    2. 年龄（``ts`` 到现在）超过 ``ttl_s`` → 陈旧（跨主机也能兜底）；
    3. 同一主机的持有者 pid 已退出 → 陈旧；**非本机 host 一律按占用处理**
       （无法检查对方 pid，不能误抢）。

    Args:
        record (dict|None): :func:`_read_lease_file` 的结果。
        path (str): 租约文件路径。
        ttl_s (float): 超龄阈值（秒）。

    Returns:
        bool: True=可清理后重新获取。
    """
    now = time.time()
    if record is None:
        mtime = _file_mtime(path)
        if mtime is None:
            return True      # 文件已经没了：下一次 O_EXCL 直接重试
        return (now - mtime) > _BROKEN_LEASE_STALE_S
    try:
        age = now - float(record.get('ts', 0))
    except (TypeError, ValueError):
        age = 0.0
    if age > float(ttl_s):
        return True
    host = record.get('host')
    if not host or host == _local_host():
        return not heartbeat.pid_alive(record.get('pid'))
    return False


def _remove_lease_file(path):
    """删除租约文件（尽力而为），失败只打印警告。

    Args:
        path (str): 租约文件路径。

    Returns:
        bool: 文件已不存在（删掉了或本来就没有）时为 True。
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(_tr('[Device Lease] cannot remove the lease file %s: %s')
              % (path, exc), file=sys.stderr)
        return False


def _remove_if_stale(path, ttl_s):
    """再次确认文件仍陈旧后删除它（缩小与并发 acquire 的竞态窗口）。

    Args:
        path (str): 租约文件路径。
        ttl_s (float): 超龄阈值（秒）。

    Returns:
        bool: True=陈旧的租约文件已被清掉，可以重试获取。
    """
    record = _read_lease_file(path)
    if not _is_stale(record, path, ttl_s):
        return False
    try:
        before = os.stat(path)
    except OSError:
        return True
    record_again = _read_lease_file(path)
    try:
        after = os.stat(path)
    except OSError:
        return True
    if record_again != record or (before.st_ino, before.st_mtime_ns) != (
            after.st_ino, after.st_mtime_ns):
        # 期间别人更新了租约：当作被占用，交给调用方放弃本次获取。
        return False
    return _remove_lease_file(path)


def _lease_payload(normalized, name, owner, experiment, task,
                   heartbeat=False, ttl_s=None):
    """构造租约文件内容（心跳字段 + 业务字段）。

    Args:
        normalized (str): 归一化地址（地址租约 ``tcp://…`` / 按名租约
            ``name://…``）。
        name (str): 设备名/驱动名（诊断用）。
        owner (str): 占用者标识（默认 ``owner or pid``）。
        experiment (str): 实验名。
        task: 任务序号（可 None）。
        heartbeat (bool): 是否心跳租约。
        ttl_s (float|None): 心跳 TTL（写入文件供续期线程使用）。

    Returns:
        dict: 可直接 JSON 序列化的租约内容。
    """
    now = time.time()
    kind = 'name' if str(normalized).startswith(NAME_PREFIX) else 'address'
    return {
        'address': normalized,
        'name': '' if name is None else str(name),
        'owner': '' if owner is None else str(owner),
        'pid': os.getpid(),
        'host': _local_host(),
        'experiment': '' if experiment is None else str(experiment),
        'task': jsonutil.jsonable(task),
        'acquired_at': now,
        'ts': now,
        'kind': kind,
        'nonce': uuid.uuid4().hex,
        'heartbeat': bool(heartbeat),
        'ttl_s': None if ttl_s is None else float(ttl_s),
    }


def _write_lease(path, payload):
    """原子写租约内容（0600），失败打印警告。

    Args:
        path (str): 租约文件路径。
        payload (dict): :func:`_lease_payload` 的结果。

    Returns:
        bool: 写成功为 True。
    """
    try:
        jsonutil.atomic_write_json(path, payload)
    except (OSError, TypeError, ValueError) as exc:
        print(_tr('[Device Lease] cannot write the lease file %s: %s')
              % (path, exc), file=sys.stderr)
        return False
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def _try_claim(path, payload, ttl_s):
    """原子抢一个文件（``O_CREAT|O_EXCL``）并写入内容；被占用/陈旧按规则处理。

    设备租约与总线锁共用这一套：调用方负责构造 ``payload`` 与目标路径。

    Args:
        path (str): 目标文件路径。
        payload (dict): 写进文件的内容。
        ttl_s (float): 判定已有文件是否陈旧的阈值（秒）。

    Returns:
        dict|None: 抢到时的内容；被占用/写失败时为 ``None``。
    """
    if _ensure_leases_dir() is None:
        return None
    for _attempt in range(_ACQUIRE_ATTEMPTS):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 已有文件：存活/未超龄 → 被占用；陈旧 → 清掉后重试一次。
            if not _remove_if_stale(path, ttl_s):
                return None
            continue
        except OSError as exc:
            print(_tr('[Device Lease] cannot create the lease file %s: %s')
                  % (path, exc), file=sys.stderr)
            return None
        try:
            os.close(fd)
        except OSError:
            pass
        if not _write_lease(path, payload):
            # 自己刚创建的占位文件写失败：删掉，别留一个空文件挡住别人。
            _remove_lease_file(path)
            return None
        return payload
    return None


def _try_acquire(normalized, path, name, owner, experiment, task, ttl_s,
                 heartbeat=False):
    """单次尝试获取设备租约（不等待）。

    Args:
        normalized (str): 归一化地址。
        path (str): 租约文件路径。
        name (str): 设备名。
        owner (str): 占用者标识。
        experiment (str): 实验名。
        task: 任务序号。
        ttl_s (float): 超龄阈值（秒）。
        heartbeat (bool): 是否心跳租约。

    Returns:
        dict|None: 成功时的租约内容；被占用/失败时为 ``None``。
    """
    payload = _lease_payload(normalized, name, owner, experiment, task,
                             heartbeat=heartbeat, ttl_s=ttl_s)
    return _try_claim(path, payload, ttl_s)


def heartbeat_ttl():
    """返回心跳租约的 TTL（秒）：环境变量 > configfile > 默认 120s。

    Returns:
        float: 逾期未续期即视为陈旧的时间。
    """
    raw = os.environ.get(ENV_HEARTBEAT_TTL, '').strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        from quatm.servers.configreader import ConfigReader
        conf = ConfigReader.getConfiguration() or {}
        devices = conf.get('Devices') or {}
        value = float(devices.get('device_lease_ttl_s'))
        if value > 0:
            return value
    except Exception:  # noqa: BLE001 - 配置不可用就用默认
        pass
    return HEARTBEAT_TTL_S


def renew_interval(ttl_s):
    """心跳续期间隔：TTL 的 1/3（下限 0.25s）。

    Args:
        ttl_s (float): 心跳 TTL。

    Returns:
        float: 建议续期间隔（秒）。
    """
    try:
        ttl_s = max(0.3, float(ttl_s))
    except (TypeError, ValueError):
        ttl_s = HEARTBEAT_TTL_S
    return max(HEARTBEAT_MIN_INTERVAL_S, ttl_s / 3.0)


def renew(lease):
    """刷新一条心跳租约的 ``ts``（只续自己持有的租约）。

    只在租约文件仍属于「本进程 + 本机 + 同一 nonce」时更新；失败（文件被清、
    被别的进程重写、权限问题）返回 False，调用方应停止续期（继续重试没有
    意义，而且可能覆盖别人的新租约）。

    Args:
        lease (dict): :func:`acquire` 返回的租约（必须带 address/nonce）。

    Returns:
        bool: 成功刷新为 True。
    """
    if not isinstance(lease, dict):
        return False
    normalized = normalize_address(lease.get('address'))
    if not normalized:
        return False
    path = _path_for(normalized)
    record = _read_lease_file(path)
    if record is None:
        return False
    try:
        if int(record.get('pid', -1)) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    if record.get('host') != _local_host():
        return False
    nonce = lease.get('nonce')
    if nonce and record.get('nonce') and record.get('nonce') != nonce:
        return False
    written_at = time.time()
    record['ts'] = written_at
    if 'heartbeat' in lease:
        record['heartbeat'] = bool(lease.get('heartbeat'))
    if 'ttl_s' in lease:
        record['ttl_s'] = lease.get('ttl_s')
    ok = _write_lease(path, record)
    if not ok:
        return False
    # 慢盘/Windows 杀软/fsync 可能让“写入”本身耗时超过 TTL（尤其测试里
    # ttl_s 很小）；ts 是写入前取的，写完后若已追近 TTL，再补一次新鲜 ts，
    # 否则刚续期的租约会被 is_held() 立即判成 stale（Windows CI 实测）。
    try:
        ttl = float(lease.get('ttl_s') or heartbeat_ttl())
    except (TypeError, ValueError):
        ttl = HEARTBEAT_TTL_S
    try:
        age = time.time() - written_at
    except (TypeError, ValueError):
        age = 0.0
    if age > max(0.05, ttl / 2.0):
        record['ts'] = time.time()
        return _write_lease(path, record)
    return True


def acquire(address, *, name='', owner='', experiment='', task=None,
            wait=0.0, ttl_s=None, heartbeat=False):
    """获取一个设备租约（跨进程互斥）。

    Args:
        address (str): 仪器地址（内部先 :func:`normalize_address`）；也接受
            ``name://<device_db 名>`` 的按名租约 key（见 :func:`acquire_name`）。
        name (str): 设备名/驱动名，写进租约供健康视图显示。
        owner (str): 占用者标识（实验窗口/任务/进程名）；空则由调用方决定
            （``open_transport`` 会用进程号）。
        experiment (str): 实验名，写进租约。
        task: 任务序号（写入租约，可为 None）。
        wait (float): 被占用时最多等待多少秒（0=不等待，立刻返回）。
        ttl_s (float|None): 超龄阈值覆盖（秒）；心跳租约默认用
            :func:`heartbeat_ttl`（120s），普通租约默认 :data:`DEFAULT_TTL_S`
            （24h）。
        heartbeat (bool): 为真时租约带 ``nonce/heartbeat/ttl_s``，由持有方
            定期 :func:`renew`；``open_transport`` 默认用它。

    Returns:
        dict|None: 成功时返回租约内容（含 address/owner/pid/host/ts）；
        空地址、被占用或写入失败时返回 ``None``。
    """
    normalized = normalize_address(address)
    if not normalized:
        return None
    if not owner:
        # 直接调用方（selftest/脚本）不传 owner 时也要能看出是谁占的。
        owner = str(os.getpid())
    if ttl_s is None:
        ttl = heartbeat_ttl() if heartbeat else DEFAULT_TTL_S
    else:
        ttl = float(ttl_s)
    path = _path_for(normalized)
    try:
        wait_s = max(0.0, float(wait or 0.0))
    except (TypeError, ValueError):
        wait_s = 0.0
    deadline = time.time() + wait_s
    while True:
        record = _try_acquire(normalized, path, name, owner, experiment, task,
                              ttl, heartbeat=heartbeat)
        if record is not None:
            return record
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(_POLL_INTERVAL_S, remaining))


def _release_owned(normalized, expected_nonce=None):
    """删除一条「本进程 + 本机（+ 可选 nonce）」持有的租约文件。

    Args:
        normalized (str): 已归一化地址。
        expected_nonce (str|None): 传入租约的 nonce；文件里的 nonce 不同说明
            同名租约已被重新获取（例如自己释放后别人/自己又拿到），不能删。

    Returns:
        bool: 确实删掉时为 True。
    """
    path = _path_for(normalized)
    record = _read_lease_file(path)
    if record is None:
        return False
    try:
        if int(record.get('pid', -1)) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    if record.get('host') != _local_host():
        return False
    if expected_nonce and record.get('nonce') \
            and record.get('nonce') != expected_nonce:
        return False
    return _remove_lease_file(path)


def release(lease_or_address):
    """释放租约（只删自己这个进程持有的那条，幂等）。

    Args:
        lease_or_address (dict|str): :func:`acquire` 返回的租约字典，或设备
            地址字符串（此时只释放“本进程 + 本机”持有的租约）。

    Returns:
        bool: 确实删除了一条本进程持有的租约为 True；文件不存在或属于别人
        时为 False（不会删除别人的租约）。
    """
    # **不信任调用方传入的 dict**：它可能来自 is_held()/list_leases() 里别的
    # 进程的租约；若按 dict 里的 pid/host 匹配，调用 release(foreign_lease)
    # 会把正在运行实验的锁删掉。只允许释放「本进程 + 本机」持有的租约
    # （acquire() 返回的 dict 本来就带着本进程 pid/本机 host，正常路径不受影响）。
    # 另外用 nonce 区分「同进程重新获取过的另一条租约」：旧 dict 不能删新锁。
    # fork 出的子进程要释放父进程租约必须显式 `QUATM_DEVICE_LEASE=0`/改 design，
    # 不属于默认契约。
    if isinstance(lease_or_address, dict):
        address = lease_or_address.get('address')
        expected_nonce = lease_or_address.get('nonce')
    else:
        address = lease_or_address
        expected_nonce = None
    normalized = normalize_address(address)
    if not normalized:
        return False
    return _release_owned(normalized, expected_nonce)


def is_held(address, ttl_s=None):
    """查询某地址/按名租约当前的租约（含占用者信息），供健康视图/诊断使用。

    Args:
        address (str): 仪器地址，或 ``name://<设备名>``。
        ttl_s (float|None): 判定 ``stale`` 用的超龄阈值；None 用
            :data:`DEFAULT_TTL_S`。调用方若用自定义 ``acquire(ttl_s=...)``
            应传同一个值，否则诊断出的 stale 与 acquire 的实际判定不一致。

    Returns:
        dict|None: 现存租约副本，额外带 ``kind``（address/name）、``age_s``
        （秒）与 ``stale`` 标记；没有租约文件时返回 ``None``。
    """
    normalized = normalize_address(address)
    if not normalized:
        return None
    path = _path_for(normalized)
    record = _read_lease_file(path)
    if record is None:
        return None
    info = dict(record)
    info.setdefault('address', normalized)
    info.setdefault(
        'kind', 'name' if normalized.startswith(NAME_PREFIX) else 'address')
    mtime = _file_mtime(path)
    try:
        age = time.time() - float(record.get('ts', mtime or 0.0))
    except (TypeError, ValueError):
        age = 0.0
    info['age_s'] = round(max(0.0, age), 3)
    info['stale'] = bool(_is_stale(
        record, path, DEFAULT_TTL_S if ttl_s is None else float(ttl_s)))
    return info


def list_leases():
    """列出当前所有可读的**设备租约**（供设备健康视图）。

    总线锁文件（``bus_*.json``）是短时内部锁，不属于设备租约，这里跳过。

    Returns:
        list[dict]: 每条含 ``address``/``kind``/``name``/``owner``/``pid``/
        ``host``/``ts``/``age_s``/``stale``；按地址排序。损坏的租约文件跳过
        （由 :func:`clean_stale` 负责清理）。
    """
    folder = _leases_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    now = time.time()
    result = []
    for name in names:
        if not name.endswith('.json') or name.startswith('bus_'):
            continue
        path = os.path.join(folder, name)
        record = _read_lease_file(path)
        if record is None:
            continue
        info = dict(record)
        info.setdefault(
            'kind',
            'name' if str(info.get('address', '')).startswith(NAME_PREFIX)
            else 'address')
        mtime = _file_mtime(path)
        try:
            age = now - float(record.get('ts', mtime or now))
        except (TypeError, ValueError):
            age = 0.0
        info['age_s'] = round(max(0.0, age), 3)
        info['stale'] = bool(_is_stale(record, path, DEFAULT_TTL_S))
        result.append(info)
    result.sort(key=lambda item: str(item.get('address', '')))
    return result


def clean_stale(ttl_s=None):
    """清理陈旧**设备租约**（持有者 pid 已退出，或超过 ``ttl_s`` 未更新）。

    总线锁（``bus_*.json``）不在这里处理：它们 TTL 很短，由
    :func:`acquire_bus` 在下次争用时自动回收。

    Args:
        ttl_s (float|None): 超龄阈值覆盖（秒）；``None`` 用
            :data:`DEFAULT_TTL_S`。

    Returns:
        int: 成功删除的租约文件数。
    """
    ttl = DEFAULT_TTL_S if ttl_s is None else float(ttl_s)
    folder = _leases_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.endswith('.json') or name.startswith('bus_'):
            continue
        path = os.path.join(folder, name)
        record = _read_lease_file(path)
        if not _is_stale(record, path, ttl):
            continue
        if _remove_if_stale(path, ttl):
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# 按名租约（DLL / 本地设备：device_db 里没有可租地址的 type: driver/local）
# ---------------------------------------------------------------------------
def acquire_name(name, *, owner='', experiment='', task=None, wait=0.0,
                 ttl_s=None, heartbeat=True):
    """按 device_db 设备名获取租约（厂商 DLL / 本地设备，跨进程互斥）。

    与地址租约共用同一套文件互斥/心跳/陈旧判定实现，租约 key 是
    ``name://<设备名>``、payload 的 ``kind`` 为 ``'name'``；因此
    :func:`renew`/:func:`is_held`/:func:`release`/:func:`list_leases`/
    :func:`clean_stale` 对两类租约都工作。

    Args:
        name (str): device_db 设备名（内部经 :func:`normalize_name`）。
        owner (str): 占用者标识（实验窗口/任务名）；空时用进程号。
        experiment (str): 实验名，写进租约供健康视图显示。
        task: 任务序号（可为 None）。
        wait (float): 被占用时最多等待多少秒（0=立刻返回 None）。
        ttl_s (float|None): 超龄阈值覆盖；``None`` 且 ``heartbeat=True`` 时用
            :func:`heartbeat_ttl`（120s），否则 :data:`DEFAULT_TTL_S`。
        heartbeat (bool): 默认 True：租约带 ``nonce/heartbeat/ttl_s``，由持有方
            （如 ``_LazyDevice`` 的续期线程）定期 :func:`renew`；进程崩溃后
            最多一个 TTL 就能被回收。

    Returns:
        dict|None: 成功时返回租约内容（address 为 ``name://…``）；空名/被占用/
        写失败时返回 ``None``。
    """
    if not lease_enabled():
        return None
    normalized = normalize_name(name)
    if not normalized:
        return None
    display = str(name).strip()
    if display.startswith(NAME_PREFIX):
        display = display[len(NAME_PREFIX):].strip()
    return acquire(normalized, name=display, owner=owner,
                   experiment=experiment, task=task, wait=wait,
                   ttl_s=ttl_s, heartbeat=heartbeat)


def is_name_held(name, ttl_s=None):
    """查询某个 device_db 设备名的按名租约（含占用者信息）。

    Args:
        name (str): device_db 设备名。
        ttl_s (float|None): 判定 ``stale`` 用的超龄阈值；``None`` 用
            :data:`DEFAULT_TTL_S`。

    Returns:
        dict|None: 现存租约副本（``kind='name'``）；没有则为 ``None``。
    """
    normalized = normalize_name(name)
    if not normalized:
        return None
    return is_held(normalized, ttl_s=ttl_s)


def release_name(name_or_lease):
    """释放按名租约（只删本进程 + 本机 + nonce 匹配的那条，幂等）。

    Args:
        name_or_lease (dict|str): :func:`acquire_name` 返回的租约，或设备名
            （可带/不带 ``name://`` 前缀）。

    Returns:
        bool: 确实删掉时为 True；不存在/属于别人时为 False。
    """
    if isinstance(name_or_lease, dict):
        return release(name_or_lease)
    normalized = normalize_name(name_or_lease)
    if not normalized:
        return False
    return release(normalized)


# ---------------------------------------------------------------------------
# 总线级短时互斥：同一适配器上的“打开/探测”不并发（正常实验仍可共享总线）
# ---------------------------------------------------------------------------
_VISA_BUS_RE = re.compile(
    r'^(?P<proto>GPIB|USB|ASRL|TCPIP)\s*(?P<index>[0-9]+)', re.IGNORECASE)


def _visa_bus_id(resource):
    """从 VISA 资源名解析总线标识（``GPIB0::5::INSTR`` → ``gpib0``）。

    Args:
        resource (str): VISA 资源字符串。

    Returns:
        str|None: 总线标识（小写）或 ``None``。
    """
    match = _VISA_BUS_RE.match(str(resource).strip())
    if not match:
        return None
    return match.group('proto').lower() + match.group('index')


def bus_id(address):
    """把设备地址映射到“总线/适配器”标识；不需要总线锁时返回 ``None``。

    规则（返回小写稳定串）：

    * VISA：``GPIB0::5::INSTR``→``gpib0``、``USB0::…``→``usb0``、
      ``ASRL3::…``→``asrl3``、``TCPIP0::…``→``tcpip0``；``visa://``/``instr://``
      前缀先剥掉；
    * ``tcp://host:port``/``socket://``（以及裸 ``host:port``）→ ``tcp:<host>``
      （同一主机的不同端口共用一个适配器/网络栈入口）；
    * ``serial://COM3``/``COM3``/``/dev/tty…`` → ``serial:<归一化设备>``（小写）；
    * ``name://…``（按名租约）、纯地址别名、未知写法 → ``None``（不锁）。

    Args:
        address (str|None): 设备地址。

    Returns:
        str|None: 总线标识，或 ``None``。
    """
    if address is None or not isinstance(address, str):
        return None
    text = address.strip()
    if not text:
        return None
    if text.lower().startswith(NAME_PREFIX):
        return None
    match = _SCHEME_RE.match(text)
    if match:
        scheme = match.group('scheme').lower()
        rest = match.group('rest')
        if scheme in ('visa', 'instr'):
            return _visa_bus_id(rest)
        if scheme in ('tcp', 'socket', 'scpi', 'raw'):
            normalized = _normalize_tcp(rest)
            if not normalized:
                return None
            host = normalized[len('tcp://'):].rpartition(':')[0]
            return 'tcp:' + host.lower()
        if scheme in ('serial', 'asrl'):
            normalized = _normalize_serial(rest)
            if not normalized:
                return None
            return 'serial:' + normalized[len('serial://'):].lower()
        return None
    if '::' in text:
        return _visa_bus_id(text)
    port_match = _HOSTPORT_RE.match(text)
    if port_match:
        return 'tcp:' + port_match.group('host').lower()
    if _COM_PORT_RE.match(text) or text.startswith('/dev/') \
            or text.startswith('\\\\.\\'):
        normalized = _normalize_serial(text)
        if not normalized:
            return None
        return 'serial:' + normalized[len('serial://'):].lower()
    return None


def bus_path(address):
    """返回某地址对应总线锁文件的路径。

    Args:
        address (str): 设备地址。

    Returns:
        str: ``<项目根>/configuration/devices/leases/bus_<hash>.json``；
        该地址不需要总线锁时返回空串。
    """
    key = bus_id(address)
    if not key:
        return ''
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
    return os.path.join(_leases_dir(), 'bus_' + digest + '.json')


def _bus_payload(bus_key, address, owner, ttl_s):
    """构造总线锁文件内容。

    Args:
        bus_key (str): :func:`bus_id` 的结果。
        address (str): 原始设备地址（写进文件便于诊断）。
        owner (str): 占用者标识。
        ttl_s (float): TTL（秒）。

    Returns:
        dict: 总线锁内容。
    """
    return {
        'kind': 'bus',
        'bus': bus_key,
        'address': normalize_address(address) or str(address).strip(),
        'owner': '' if owner is None else str(owner),
        'pid': os.getpid(),
        'host': _local_host(),
        'ts': time.time(),
        'nonce': uuid.uuid4().hex,
        'ttl_s': float(ttl_s),
    }


def acquire_bus(address, *, owner='', timeout=5.0, ttl_s=None):
    """获取总线级短时互斥（只保护“打开设备”这一小段时间）。

    同一总线上的多个设备可以**先后**打开；并发打开时后到者轮询等待。
    与设备租约不同，这里不长期占用总线：``open_transport`` 在打开完成后
    立即释放。

    Args:
        address (str): 设备地址（内部经 :func:`bus_id` 归类）。
        owner (str): 占用者标识（默认进程号）。
        timeout (float): 最长等待时间（秒）；0=只试一次。
        ttl_s (float|None): 锁超龄阈值；``None`` 用 :data:`BUS_TTL_S`（15s）。

    Returns:
        dict|None: 成功时返回锁内容；该地址不需要总线锁（:func:`bus_id` 为
        ``None``）或等待超时时返回 ``None``。
    """
    if not bus_lock_enabled():
        return None
    bus_key = bus_id(address)
    if not bus_key:
        return None
    ttl = BUS_TTL_S if ttl_s is None else float(ttl_s)
    if not owner:
        owner = str(os.getpid())
    path = bus_path(address)
    payload = _bus_payload(bus_key, address, owner, ttl)
    try:
        wait_s = max(0.0, float(timeout or 0.0))
    except (TypeError, ValueError):
        wait_s = 0.0
    deadline = time.time() + wait_s
    while True:
        record = _try_claim(path, payload, ttl)
        if record is not None:
            return record
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(_POLL_INTERVAL_S, remaining))


def is_bus_held(address, ttl_s=None):
    """查询某地址所在总线当前的总线锁（诊断/健康视图用）。

    Args:
        address (str): 设备地址。
        ttl_s (float|None): 判定 ``stale`` 用的超龄阈值；``None`` 用
            :data:`BUS_TTL_S`。

    Returns:
        dict|None: 锁内容副本（附 ``age_s``/``stale``）；没有锁或该地址不需要
        总线锁时返回 ``None``。
    """
    key = bus_id(address)
    if not key:
        return None
    path = bus_path(address)
    record = _read_lease_file(path)
    if record is None:
        return None
    info = dict(record)
    info.setdefault('bus', key)
    ttl = BUS_TTL_S if ttl_s is None else float(ttl_s)
    try:
        age = time.time() - float(record.get('ts', 0))
    except (TypeError, ValueError):
        age = 0.0
    info['age_s'] = round(max(0.0, age), 3)
    info['stale'] = bool(_is_stale(record, path, ttl))
    return info


def release_bus(lease_or_address):
    """释放总线锁（只删本进程 + 本机 + nonce 匹配的那条，幂等）。

    Args:
        lease_or_address (dict|str): :func:`acquire_bus` 返回的锁内容，或设备
            地址字符串。

    Returns:
        bool: 确实删掉时为 True；不存在/属于别人时为 False。
    """
    if isinstance(lease_or_address, dict):
        address = lease_or_address.get('address')
        expected_nonce = lease_or_address.get('nonce')
    else:
        address = lease_or_address
        expected_nonce = None
    key = bus_id(address)
    if not key:
        return False
    path = bus_path(address)
    record = _read_lease_file(path)
    if record is None:
        return False
    try:
        if int(record.get('pid', -1)) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    if record.get('host') != _local_host():
        return False
    if expected_nonce and record.get('nonce') \
            and record.get('nonce') != expected_nonce:
        return False
    return _remove_lease_file(path)


@contextlib.contextmanager
def bus_lock(address, owner='', timeout=5.0):
    """``with bus_lock(address):`` 形式的短时总线互斥（自动释放）。

    Args:
        address (str): 设备地址。
        owner (str): 占用者标识（默认进程号）。
        timeout (float): 最长等待时间（秒）。

    Yields:
        dict|None: 拿到的总线锁内容；该地址不需要总线锁时 ``None``。

    Raises:
        TimeoutError: 超时仍拿不到锁（说明另一进程/窗口正在同一总线上打开
            设备）。
    """
    bus_key = bus_id(address)
    if not bus_key or not bus_lock_enabled():
        yield None
        return
    lease = acquire_bus(address, owner=owner, timeout=timeout)
    if lease is None:
        raise TimeoutError(
            _tr('[Device Bus] cannot acquire the bus lock for %s within '
                '%.1f s') % (bus_key, float(timeout or 0.0)))
    try:
        yield lease
    finally:
        release_bus(lease)
