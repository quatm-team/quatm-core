"""本机 ZMQ 通道的 CURVE 加密（pyzmq 自带 libsodium，零新增依赖）。

背景：quatm 的进程间通信全部走本机 ZMQ（Hub 的 XSUB/XPUB、属性 REP/REQ、
数据 PUB/SUB）。ZMQ 默认**既无认证也无加密**：一旦端点被误绑到 0.0.0.0 / LAN，
同网段任何主机都能订阅数据、改属性、注入命令。本模块给所有本机通道加
**CURVE 加密**（默认开启），同时保留可配置的明文回退，避免因为密钥问题让
整套服务起不来。

密钥管理
--------
* 默认目录：``<项目根>/configuration/zmq/``（可用环境变量
  ``QUATM_ZMQ_KEY_DIR`` 覆盖，测试/多项目隔离用）；
* 首次运行时自动生成 server / client 两组 CURVE 密钥：
  ``server.key`` / ``server.key_secret`` / ``client.key`` / ``client.key_secret``；
* 目录权限 **0700**、文件权限 **0600**；已存在则原样复用（**不**每次重生成）；
* 密钥目录必须在 ``.gitignore`` 里（不得入库）。

开关
----
* 默认：**加密开启**；
* ``QUATM_ZMQ_CURVE=0``（或 false/no/off）→ 明文（环境变量优先级最高）；
* configfile ``Servers/zmq_curve: false`` → 明文；
* libzmq 不支持 CURVE 时 → 警告一次并回退明文。

用法（成对！bind 端用 server role，connect 端用 client role）::

    from quatm.servers import zmq_security
    zmq_security.server_configure(sock, endpoint=bind_endpoint)   # bind 前
    zmq_security.client_configure(sock)                           # connect 前

失败/回退
---------
密钥目录不可写、密钥文件损坏/只写了一半、libzmq 无 CURVE 支持时：打印明确
warning（含路径与建议）并**回退明文**；非回环绑定 + 明文时再额外打一条醒目
警告（与 :func:`quatm.servers.configreader.warn_if_exposed` 呼应）。
模块级缓存解析结果：**不要**每个 socket 都重读密钥文件；运行时轮换密钥必须
重启所有 quatm 进程（见 ``the ZMQ security notes``）。
"""

import logging
import os
import shutil
import threading
import time

import zmq

#: 打开/关闭 CURVE 的环境变量（'0'/'false'/'no'/'off' → 关闭）。
ENV_CURVE = 'QUATM_ZMQ_CURVE'

#: 覆盖密钥目录的环境变量（测试 / 多项目隔离）。
ENV_KEY_DIR = 'QUATM_ZMQ_KEY_DIR'

#: CURVE 密钥文件名（顺序固定，便于排查）。
KEY_FILES = ('server.key', 'server.key_secret',
             'client.key', 'client.key_secret')

#: Z85 编码的 CURVE 密钥长度（libzmq 规定）。
_KEY_LENGTH = 40

#: 密钥目录旁的生成锁（多进程首启/轮换时串行生成，避免半套密钥）。
_LOCK_SUFFIX = '.lock'
#: 等锁的上限（秒）；超时说明有进程卡死，回退明文而不是无限等。
_LOCK_TIMEOUT_S = 5.0
#: 锁文件超过该年龄视为陈旧（持锁进程崩溃），可抢占（秒）。
_LOCK_STALE_S = 30.0

_state = {
    'resolved': False,
    'enabled': False,
    'keys': None,       # {'server_public': bytes, ...}
    'key_dir': None,
    'reason': '',
}
_lock = threading.Lock()
_warned = set()
_log = logging.getLogger(__name__)


class ZmqKeyError(RuntimeError):
    """CURVE 密钥不可用（缺失/损坏/目录不可写），调用方应回退明文。"""


def _tr(text):
    """延迟导入 i18n 的 ``tr``（本模块会被底层模块 import，避免循环导入）。"""
    from quatm.utils.i18n import tr
    return tr(text)


def _warn(message):
    """打印醒目的安全警告（stdout，与 configreader 的暴露警告一致）。"""
    print(message)


def _warn_once(tag, message):
    """同一条警告在一个进程里只打印一次（避免每个客户端重连都刷屏）。

    Args:
        tag (str): 去重键。
        message (str): 警告正文（已过 tr()）。
    """
    if tag in _warned:
        return
    _warned.add(tag)
    _warn(message)


def _project_root():
    """返回当前项目根（延迟 import，避免与 configreader 的导入顺序耦合）。

    Returns:
        str: 项目根目录；取不到时退回当前工作目录。
    """
    try:
        from quatm.servers.configreader import PROJECT_ROOT
        return os.fspath(PROJECT_ROOT)
    except Exception as exc:  # noqa: BLE001 - 极端情况下退化为 cwd
        _log.debug('cannot resolve PROJECT_ROOT for ZMQ keys: %s', exc)
        return os.getcwd()


def key_dir():
    """返回 CURVE 密钥目录。

    优先 ``QUATM_ZMQ_KEY_DIR``；否则 ``<项目根>/configuration/zmq``。

    Returns:
        str: 密钥目录绝对路径。
    """
    override = os.environ.get(ENV_KEY_DIR, '').strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_project_root(), 'configuration', 'zmq')


def _env_enabled():
    """读取 ``QUATM_ZMQ_CURVE``：True/False/None（未设置）。

    Returns:
        bool|None: 环境变量显式开关；未设置/无法识别时 None。
    """
    raw = os.environ.get(ENV_CURVE, '').strip().lower()
    if not raw:
        return None
    if raw in ('0', 'false', 'no', 'off'):
        return False
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    return None


def _config_enabled():
    """读取 configfile ``Servers/zmq_curve``：True/False/None（未配置）。

    Returns:
        bool|None: 配置显式开关；没有 configfile/没写该键时 None。
    """
    try:
        from quatm.servers.configreader import ConfigReader
        conf = ConfigReader.getConfiguration() or {}
        value = (conf.get('Servers') or {}).get('zmq_curve')
    except Exception:  # noqa: BLE001 - 没配置/读不了就按默认（开启）走
        return None
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() not in ('', '0', 'false', 'no', 'off')
    return bool(value)


def _read_key(path):
    """读取并校验一个 CURVE 密钥文件。

    Args:
        path (str): 密钥文件路径。

    Returns:
        bytes: Z85 编码的密钥。

    Raises:
        ZmqKeyError: 文件不可读、不是 Z85 长度或包含非法字符。
    """
    try:
        with open(path, 'r', encoding='ascii') as handle:
            value = handle.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ZmqKeyError('cannot read %s: %s' % (path, exc)) from None
    if len(value) != _KEY_LENGTH:
        raise ZmqKeyError('%s is not a valid CURVE key (length %d, expected %d)'
                          % (path, len(value), _KEY_LENGTH))
    return value.encode('ascii')


def _write_key(path, value):
    """以 0600 权限原子写一个 CURVE 密钥文件。

    Args:
        path (str): 目标文件路径。
        value (str): Z85 密钥文本。

    Raises:
        ZmqKeyError: 写文件失败（目录不可写等）。
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write(value + '\n')
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ZmqKeyError('cannot write %s: %s' % (path, exc)) from None


def _read_complete_keys(directory):
    """读取目录里的完整密钥组；不完整/损坏时返回 ``None``。

    Returns:
        dict[str, bytes]|None: 四组密钥；不完整或读取失败返回 None。
    """
    paths = {name: os.path.join(directory, name) for name in KEY_FILES}
    if not all(os.path.isfile(paths[name]) for name in KEY_FILES):
        return None
    try:
        return {
            'server_public': _read_key(paths['server.key']),
            'server_secret': _read_key(paths['server.key_secret']),
            'client_public': _read_key(paths['client.key']),
            'client_secret': _read_key(paths['client.key_secret']),
        }
    except ZmqKeyError:
        return None


def _write_keypair_set(directory, values):
    """把一组密钥原子发布到目录：先写临时目录，再 rename 成目标目录。

    多进程（首启时 ProcessManager 会并发拉起多个服务）同时生成密钥时，
    原地写文件会出现"半套密钥"→ 有的进程读不到完整组而回退明文，导致
    同机部分服务加密、部分明文、互相连不上。这里：
    * 临时目录 ``<dir>.tmp-<pid>`` 里写完整四文件（0600），再整体 rename；
    * rename 是原子的：其它进程要么看到旧目录、要么看到完整新目录；
    * 目标目录已存在（历史上被写成半套）时先 rename 移开再发布。

    Args:
        directory (str): 目标密钥目录。
        values (dict[str, str]): 四个 key 的 Z85 文本。

    Raises:
        ZmqKeyError: 写入/rename 失败。
    """
    tmp = '%s.tmp-%d' % (directory, os.getpid())
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(tmp, mode=0o700)
        for name, key in (('server.key', values['server_public']),
                          ('server.key_secret', values['server_secret']),
                          ('client.key', values['client_public']),
                          ('client.key_secret', values['client_secret'])):
            _write_key(os.path.join(tmp, name), key)
        if os.path.isdir(directory):  # 半套/旧目录：先移开，避免 rename 失败
            stale = '%s.stale-%d-%d' % (directory, os.getpid(),
                                        time.time_ns())
            try:
                os.rename(directory, stale)
            except OSError:
                # 其它进程可能刚发布好完整目录：放弃本次发布，交给重读
                shutil.rmtree(tmp, ignore_errors=True)
                raise ZmqKeyError('key directory changed concurrently: %s'
                                  % directory) from None
            shutil.rmtree(stale, ignore_errors=True)
        os.rename(tmp, directory)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    except ZmqKeyError:
        raise
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ZmqKeyError('cannot publish CURVE keys to %s: %s'
                          % (directory, exc)) from None


def _acquire_key_lock(directory):
    """获取跨进程生成锁（原子 O_CREAT|O_EXCL）；超时返回 None。"""
    lock = directory + _LOCK_SUFFIX
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, str(os.getpid()).encode('ascii'))
            finally:
                os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > _LOCK_STALE_S:
                    os.unlink(lock)  # 持锁进程已崩溃
                    continue
            except OSError:
                pass
            time.sleep(0.02)
        except OSError as exc:
            raise ZmqKeyError('cannot create key lock %s: %s'
                              % (lock, exc)) from None
    return None


def _load_or_create_keys(directory):
    """加载或首次生成 CURVE 密钥（server/client 两组，多进程安全）。

    规则：
    * 目录不存在 → 以 0700 创建；
    * 四个密钥都存在 → 复用（不改文件）；
    * 四个都不存在 → 生成一组并原子发布（0600，临时目录 + rename）；
    * 只存在一部分 → 可能是另一个进程正在生成：先短暂等待其完成；仍不完整
      则持跨进程锁把半套目录移开、重新生成（**不**在无锁状态下重生成）。

    多进程并发（部署首启 ProcessManager 会同时拉起多个服务）必须收敛到
    **同一组**密钥，否则同机部分进程回退明文、互相连不上。

    Args:
        directory (str): 密钥目录。

    Returns:
        dict[str, bytes]: 四组密钥（``server_public`` 等）。

    Raises:
        ZmqKeyError: 目录不可用/密钥损坏/写入失败/等锁超时。
    """
    if os.path.exists(directory) and not os.path.isdir(directory):
        raise ZmqKeyError('%s exists but is not a directory' % directory)
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ZmqKeyError('cannot create key directory %s: %s'
                              % (directory, exc)) from None
    try:  # 已存在但权限过宽时收紧（失败不致命）
        os.chmod(directory, 0o700)
    except OSError:
        pass

    keys = _read_complete_keys(directory)
    if keys is not None:
        return keys
    had_key_files = os.path.isdir(directory) and any(
        os.path.isfile(os.path.join(directory, n)) for n in KEY_FILES)
    # 半套密钥：可能是并发生成中的临时状态，限时等生成者完成
    if had_key_files:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.02)
            keys = _read_complete_keys(directory)
            if keys is not None:
                return keys

    lock = _acquire_key_lock(directory)
    if lock is None:
        raise ZmqKeyError('timed out waiting for the CURVE key lock in %s'
                          % directory)
    try:
        keys = _read_complete_keys(directory)  # 等锁期间别人可能已生成
        if keys is not None:
            return keys
        if had_key_files:
            _warn_once(
                'keys-regenerated',
                _tr('[ZMQ] CURVE keys in %s were incomplete or corrupt; '
                    'generated a fresh key set. Restart ALL quatm processes so '
                    'every process uses the same keys.')
                % directory)
        try:
            server_public, server_secret = zmq.curve_keypair()
            client_public, client_secret = zmq.curve_keypair()
        except Exception as exc:  # noqa: BLE001 - libzmq 不支持/内部错误
            raise ZmqKeyError('cannot generate CURVE keypair: %s'
                              % exc) from None
        try:
            values = {
                'server_public': server_public.decode('ascii'),
                'server_secret': server_secret.decode('ascii'),
                'client_public': client_public.decode('ascii'),
                'client_secret': client_secret.decode('ascii'),
            }
        except UnicodeDecodeError as exc:
            raise ZmqKeyError('generated CURVE keys are not ASCII: %s'
                              % exc) from None
        _write_keypair_set(directory, values)
        keys = _read_complete_keys(directory)
        if keys is None:
            raise ZmqKeyError('CURVE keys were generated but cannot be '
                              're-read from %s' % directory)
        return keys
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _resolve_locked():
    """解析一次加密开关与密钥（调用方需持有 ``_lock``）。"""
    env_flag = _env_enabled()
    config_flag = _config_enabled()
    if env_flag is False or (env_flag is None and config_flag is False):
        _state.update(resolved=True, enabled=False, keys=None,
                      reason='disabled', key_dir=None)
        return

    if not zmq.has('curve') or not hasattr(zmq, 'curve_keypair'):
        _warn_once(
            'no-curve-support',
            _tr('[ZMQ] CURVE encryption is unavailable in this libzmq build; '
                'falling back to plaintext. Reinstall pyzmq with libsodium '
                'support to enable encryption.'))
        _state.update(resolved=True, enabled=False, keys=None,
                      reason='no-curve-support', key_dir=None)
        return

    directory = key_dir()
    # 多进程首启时，另一个进程可能正持有生成锁/刚发布/刚移开半套目录；
    # 单次失败就回退明文会造成"同机部分 CURVE、部分明文、互相连不上"。
    # 有界重试（每次先重读完整密钥，再尝试生成/等锁），仍失败才回退明文。
    keys = None
    last_error = None
    for attempt in range(3):
        try:
            keys = _load_or_create_keys(directory)
            break
        except ZmqKeyError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    if keys is None:
        _warn_once(
            'keys-unavailable',
            (_tr('[ZMQ] cannot use CURVE keys in %s (%s); falling back to '
                 'plaintext for this process.') % (directory, last_error))
            + _tr(' Check directory permissions or set QUATM_ZMQ_KEY_DIR; '
                  'delete the directory to regenerate keys, then restart all '
                  'quatm processes.'))
        _state.update(resolved=True, enabled=False, keys=None,
                      reason='keys-unavailable', key_dir=directory)
        return

    _state.update(resolved=True, enabled=True, keys=keys, reason='',
                  key_dir=directory)


def curve_enabled():
    """返回本进程是否启用 CURVE 加密（模块级缓存，首次调用时解析）。

    优先级：环境变量 ``QUATM_ZMQ_CURVE`` > configfile
    ``Servers/zmq_curve`` > 默认开启；libzmq 无 CURVE 支持或密钥不可用时
    回退 False（只警告一次）。

    Returns:
        bool: True=加密开启；False=明文回退/显式关闭。
    """
    with _lock:
        if not _state['resolved']:
            _resolve_locked()
        return bool(_state['enabled'])


def _plaintext_warning(endpoint, role):
    """明文 + 非回环端点 → 额外醒目警告（每个端点一次）。

    Args:
        endpoint (str|None): 实际 bind/connect 端点。
        role (str): ``'server'`` / ``'client'``。
    """
    if not endpoint:
        return
    try:
        from quatm.servers.configreader import is_loopback_endpoint
        if is_loopback_endpoint(endpoint):
            return
    except Exception as exc:  # noqa: BLE001 - 判断不了时不再加警告
        _log.debug('cannot classify endpoint %r for plaintext warning: %s',
                   endpoint, exc)
        return
    _warn_once(
        'plaintext-exposed:%s' % endpoint,
        _tr('[ZMQ] plaintext fallback on non-loopback endpoint %s (%s): the '
            'channel is neither encrypted nor authenticated. Bind 127.0.0.1, '
            'or set QUATM_ZMQ_CURVE=0 explicitly if this is intended.')
        % (endpoint, role))


def server_configure(sock, ctx=None, endpoint=None):
    """给 **bind** 端 socket 配置 CURVE server role（必须在 ``bind`` 之前调用）。

    Args:
        sock: pyzmq socket（XSUB/XPUB/REP/...）。
        ctx: 可选 ZMQ context（本实现不需要；为兼容/调用方签名保留）。
        endpoint (str|None): 实际 bind 端点（仅用于明文回退时的暴露警告）。

    Returns:
        bool: True=已启用 CURVE；False=明文回退。
    """
    return configure(sock, 'server', endpoint=endpoint, ctx=ctx)


def client_configure(sock, endpoint=None):
    """给 **connect** 端 socket 配置 CURVE client role（必须在 ``connect`` 之前）。

    Args:
        sock: pyzmq socket（SUB/REQ/PUB/...）。
        endpoint (str|None): 实际连接端点（保留给诊断/警告）。

    Returns:
        bool: True=已启用 CURVE；False=明文回退。
    """
    return configure(sock, 'client', endpoint=endpoint)


def configure(sock, role, endpoint=None, ctx=None):
    """统一入口：按 ``role`` 给 socket 配置 CURVE（bind=server / connect=client）。

    Args:
        sock: pyzmq socket。
        role (str): ``'server'``（bind 端）或 ``'client'``（connect 端）。
        endpoint (str|None): 实际端点；明文回退时用于非回环暴露警告。
        ctx: 可选 context（保留参数，不参与配置）。

    Returns:
        bool: True=已启用 CURVE；False=明文回退。

    Raises:
        ValueError: ``role`` 不是 'server'/'client'。
    """
    if role not in ('server', 'client'):
        raise ValueError('role must be "server" or "client", got %r' % (role,))
    if not curve_enabled():
        if role == 'server':
            _plaintext_warning(endpoint, role)
        return False
    keys = _state['keys']
    try:
        if role == 'server':
            sock.curve_server = True
            sock.curve_publickey = keys['server_public']
            sock.curve_secretkey = keys['server_secret']
        else:
            sock.curve_server = False
            sock.curve_publickey = keys['client_public']
            sock.curve_secretkey = keys['client_secret']
            sock.curve_serverkey = keys['server_public']
    except Exception as exc:  # noqa: BLE001 - 配置失败必须回退而不是崩
        _warn_once(
            'configure-failed',
            _tr('[ZMQ] failed to enable CURVE on a %s socket (%s); falling '
                'back to plaintext for this process. All quatm processes must '
                'use the same encryption mode.') % (role, exc))
        with _lock:
            _state.update(enabled=False, keys=None, reason='configure-failed')
        try:
            if role == 'server':
                sock.curve_server = False
        except Exception:  # noqa: BLE001 - 尽力清除半配置状态
            pass
        if role == 'server':
            _plaintext_warning(endpoint, role)
        return False
    return True


def reset_for_tests():
    """清空模块级缓存与"已警告"集合（仅供测试；生产不要调用）。"""
    with _lock:
        _state.update(resolved=False, enabled=False, keys=None,
                      reason='', key_dir=None)
        _warned.clear()
