"""Distributed property tree and property attributes."""

import sys
import os
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


from quatm.servers import zmqcontext
from quatm.servers import configreader as cr
from quatm.servers import zmq_security
from quatm.servers.xsub_xpub import event_monitor
import copy
import zmq
import threading
import atexit
import weakref
import uuid
import time
import logging, sys
# logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
from quatm.servers import send_info, send_error, send_warning
import json
from quatm.utils.i18n import tr


# set once the Propertylogger server is known to be unreachable in this process,
# so later Properties instances skip the (blocking) handshake and load from file
_PROPERTYLOGGER_DOWN = False

# sentinel returned by Properties._raw_get for missing keys (distinguishes a
# missing entry from one whose value is None)
_MISSING = object()

# ---------------------------------------------------------------------------
# 进程内实例注册 + 解释器退出时的安全统一关闭。
#
# 每个 Properties 都带 daemon 线程阻塞在 sub_socket.recv（RCVTIMEO=200 ms）。
# 若进程在仍有线程阻塞时直接退出，解释器关闭阶段与 libzmq context 的拆除
# 会发生竞态——Windows 上典型表现是 libzmq signaler 的中断断言：
#   Assertion failed: Interrupted system call [10004]
#   (.../bundled_libzmq-src/src/signaler.cpp:345)
# 随后进程 SIGABRT。可见于“浏览器里 Looper 循环结束后重启全部服务/浏览器”。
# 这里把活着的实例登记成弱引用，注册 atexit handler 在解释器退出早期统一
# 执行“先唤醒线程 → 统一 join → 再关 socket”的安全关闭（close_all），
# 线程都退出后才拆 socket，避免上述竞态。close() 幂等，重复调用无害。
_LIVE_LOCK = threading.Lock()
_LIVE_INSTANCES = []  # list of weakref.ref -> Properties


def _register(instance):
    """把活着的 Properties 实例登记到进程级注册表（弱引用，不阻碍 GC）。

    Args:
        instance (Properties): 刚构造完成的 Properties 实例。
    """
    with _LIVE_LOCK:
        _LIVE_INSTANCES.append(weakref.ref(instance))


# 共享属性快照：新客户端可直接从进程内已有实例复制整棵属性树，跳过与
# Property Logger 的 ``INIT?`` 往返。动机（Browser 启动实测）：握手是
# “客户端 REQ → 服务端 sleep 10 ms → 序列化整棵树（实测 ~330 KB）→ 回包 →
# 客户端 json 反序列化”，而 Browser 恢复十几个实验窗口时每个窗口都会新建
# 属性客户端（实验一个、AdWinDriver 一个），这些往返在服务端串行执行，
# 叠加成秒级。复用来源是一个仍在运行的实例：它的订阅线程持续接收更新，
# 因此副本的新鲜度与“新做一次 INIT?”等价，只是换成了进程内 deepcopy。
# 需要彻底关闭时设 QUATM_PROPS_SHARED_SNAPSHOT=0。
_SHARE_SNAPSHOT = str(os.environ.get('QUATM_PROPS_SHARED_SNAPSHOT', '1')) \
    .strip().lower() not in ('0', 'false', 'no', 'off')


def shared_snapshot_source():
    """返回进程内可用于复制属性快照的存活实例（没有则 None）。

    Returns:
        Properties|None: 尚未关闭、且已持有非空属性树的实例（取最近创建的
        一个，它最可能还“热”）；关闭了快照复用或进程内还没有任何客户端时
        返回 None（调用方走常规 ``INIT?`` 路径）。
    """
    if not _SHARE_SNAPSHOT:
        return None
    with _LIVE_LOCK:
        refs = list(_LIVE_INSTANCES)
    for ref in reversed(refs):
        instance = ref()
        if instance is None:
            continue
        try:
            closed = getattr(instance, '_closed', None)
            if closed is not None and closed.is_set():
                continue
            if getattr(instance, '_from_file', False):
                # 属性树来自盘文件（properties.json）的实例：它的内容可能
                # 早于 hub 当前值（如 Property Logger 当时不可达），不能当
                # “新鲜快照”的来源，否则会把陈旧值复制给新客户端。
                continue
            properties = instance.properties
        except Exception:  # noqa: BLE001 - 半初始化/已回收的实例直接跳过
            continue
        if isinstance(properties, dict) and properties:
            return instance
    return None


def _snapshot_copy(source):
    """深拷贝来源实例的属性树（受其锁保护）；不可用时返回 None。

    Args:
        source (Properties): :func:`shared_snapshot_source` 返回的实例。

    Returns:
        tuple: ``(属性树副本|None, 来源是否为“从文件初始化”)``；不可用时
            ``(None, False)``。第二个元素用于把“Propertylogger 不可用”这个
            事实传播给复用快照的新实例（否则它不会再往 pending 文件里记
            改动，服务不在时改的属性依然会丢）。
    """
    try:
        lock = getattr(source, 'properties_lock', None)
        if lock is None:
            return None, False
        with lock:
            properties = getattr(source, 'properties', None)
            if not isinstance(properties, dict) or not properties:
                return None, False
            return copy.deepcopy(properties), bool(
                getattr(source, '_from_file', False))
    except Exception:  # noqa: BLE001 - 复用失败就退回网络握手
        return None, False


def _shutdown_all_properties():
    """解释器退出时统一安全关闭仍存活的 Properties 实例（幂等、异常隔离）。"""
    global _LIVE_INSTANCES
    with _LIVE_LOCK:
        refs = _LIVE_INSTANCES
        _LIVE_INSTANCES = []
    alive = [r() for r in refs]
    alive = [p for p in alive if p is not None]
    if not alive:
        return
    try:
        close_all(*alive)
    except Exception:  # noqa: BLE001 - 退出路径绝不因清理失败抛错
        pass


atexit.register(_shutdown_all_properties)


# ---------------------------------------------------------------------------
# 本地“待落盘”日志：Propertylogger 没在跑时的兑底
#
# 属性写入是 PUB（fire-and-forget，无 ack），落盘完全靠常驻 Propertylogger。
# 服务没在跑时，客户端的 set() 只改了本进程内存 —— 重启就丢，而且没有任何
# 提示（用户报告过“改完属性重启又回去了”）。
#
# 所以：客户端发现自己是从文件初始化的（即没接到 logger 的应答）时，把改动
# 追加记到 ``properties_pending.json``；Propertylogger 启动时读回并写进属性
# 树，**成功落盘之后才删**该文件，所以中途崩了也不会丢。
#
# 只在“服务不可用”时记，不进入正常路径（否则实验期间的高频属性写入会白白
# 多一份磁盘 I/O）。
_PENDING_FILENAME = os.path.join(os.path.dirname(cr.propertyfilename),
                                 'properties_pending.json')
_PENDING_DELAY_S = 0.5          # 改动后多久写 pending 文件（防抖）
# 存活探测周期 [s]（0 关闭）与连续失败多少次才判定不可用。
# 不要调得太密：每个 Properties 实例都探一次，GUI 进程里实例数按窗口/驱动
# 叠加（数十个）→ 5 s 就是每秒多次建连；15 s 对“服务掉线/回归”的判定足够快。
PROBE_INTERVAL_S = float(os.environ.get('QUATM_PROPS_PING_S', '15') or 15)
PROBE_FAILS_BEFORE_DOWN = 2
# 探测应答超时 [ms]：必须明显小于 close_all()/_wait_threads 的 2 s join
# 预算，否则“探测正好在超时”会让关窗白等满 2 s（propertylogger 不在时）。
PROBE_TIMEOUT_MS = int(os.environ.get('QUATM_PROPS_PING_TIMEOUT_MS', '800') or 800)
_PENDING_LOCK = threading.Lock()
_pending_data = None            # type: dict | None
_pending_timer = None
_pending_warned = False         # 写 pending 失败只提示一次


def pending_file():
    """返回本地“待落盘”日志文件的路径。

    Returns:
        str: ``configuration/properties/properties_pending.json``。
    """
    return _PENDING_FILENAME


def _pending_load_locked():
    """（需持锁）读 pending 文件到内存缓存；文件缺失/损坏时视为空。"""
    global _pending_data
    if _pending_data is None:
        try:
            with open(_PENDING_FILENAME, encoding='utf-8') as f:
                data = json.load(f)
            _pending_data = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - 缺失/损坏就当没有待落盘改动
            _pending_data = {}
    return _pending_data


def _pending_write_locked():
    """（需持锁）把内存里的待落盘改动原子写进 pending 文件。

    写失败不抛异常（记不下来也不能影响 set()），但**只提示一次**——
    踩过：这里早期忘了 ``import json``，NameError 被静默吞掉，表现为
    “pending 文件永远不生成”，非常难查。
    """
    global _pending_warned
    data = _pending_data or {}
    if not data:
        return
    try:
        os.makedirs(os.path.dirname(_PENDING_FILENAME), exist_ok=True)
        tmp = '%s.%d.tmp' % (_PENDING_FILENAME, os.getpid())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _PENDING_FILENAME)
    except Exception as e:  # noqa: BLE001 - 记不下来也不能影响 set()
        if not _pending_warned:
            _pending_warned = True
            send_warning(tr('[Properties] cannot write the local pending file '
                         '%s: %s') % (_PENDING_FILENAME, e))


def flush_pending():
    """立即把内存里的待落盘改动写进 pending 文件（进程退出前调用）。"""
    with _PENDING_LOCK:
        _pending_write_locked()


atexit.register(flush_pending)


def _pending_record(keys, value):
    """记下一条“服务不可用期间”的属性改动（防抖写盘）。

    Args:
        keys (list[str]): 已解析的属性路径（来自 :meth:`Properties._parsekey`）。
        value: 新的属性值（可 JSON 序列化）。
    """
    global _pending_timer
    try:
        key = '/'.join(str(k) for k in keys)
        if not key:
            return
        with _PENDING_LOCK:
            data = _pending_load_locked()
            data[key] = value
        if _pending_timer is None:
            _pending_timer = threading.Timer(_PENDING_DELAY_S, flush_pending)
            _pending_timer.daemon = True
        if not _pending_timer.is_alive():
            _pending_timer = threading.Timer(_PENDING_DELAY_S, flush_pending)
            _pending_timer.daemon = True
            _pending_timer.start()
    except Exception:  # noqa: BLE001 - 绝不能因为记账失败打断 set()
        pass


def consume_pending_file(clear=False):
    """读取本地待落盘改动（供 Propertylogger 启动时合并）。

    Args:
        clear (bool): True 时读完后删除该文件（调用方已保证改动进了树并且
            已经落盘）；False 时只读不删。

    Returns:
        dict: ``{'a/b/c': value, ...}``（无内容时为空字典）。
    """
    global _pending_data
    with _PENDING_LOCK:
        data = _pending_load_locked() if not clear else None
        if data is None:
            try:
                with open(_PENDING_FILENAME, encoding='utf-8') as f:
                    loaded = json.load(f)
                data = loaded if isinstance(loaded, dict) else {}
            except Exception:  # noqa: BLE001
                data = {}
        out = dict(data)
        if clear:
            _pending_data = {}
            try:
                os.remove(_PENDING_FILENAME)
            except OSError:
                pass
        return out


_PROBE_ENDPOINT = None          # 缓存的 Property Logger REP 端点
_PROBE_ENDPOINT_KEY = None      # 缓存对应的 configfile (mtime_ns, size)
_PROBE_SOCKET = None            # 各实例共用的探测 socket
_PROBE_LOCK = threading.Lock()


def _probe_endpoint():
    """返回 Property Logger 的 REP 端点（缓存，configfile 变化时自动重读）。

    旧实现每次探测都调 ``cr.Config()``：那要整份读一遍 configfile.json
    （Windows 上还要把所有端点重写成 tcp），而探测是每个实例每轮都在做的。

    Returns:
        str|None: REP 端点；未配置时为 None（此时不应判定“服务不可用”）。
    """
    global _PROBE_ENDPOINT, _PROBE_ENDPOINT_KEY
    try:
        _st = os.stat(cr.configfilename)
        _key = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _key = None
    if _PROBE_ENDPOINT is not None and _key is not None \
            and _key == _PROBE_ENDPOINT_KEY:
        return _PROBE_ENDPOINT
    try:
        conf = cr.Config()
        _PROBE_ENDPOINT = cr.server_endpoints(conf, 'Property Logger')['rep']
        _PROBE_ENDPOINT_KEY = _key
    except Exception:  # noqa: BLE001 - 没配置时不要误判为“服务挂了”
        return None
    return _PROBE_ENDPOINT


def _probe_close_socket():
    """关闭并丢弃共享的探测 socket（进程退出时用，幂等）。

    探测 socket 是模块级共享的（见 :func:`_propertylogger_alive`），因此必须
    显式关掉：pyzmq 的 socket 只在 GC 时才释放底层 fd，而这里持有它的是模块级
    全局——进程退出阶段由 atexit 关掉可以避免 ZMQ 上下文销毁时报错。

    Returns:
        None: 无返回值。
    """
    global _PROBE_SOCKET
    with _PROBE_LOCK:
        sock, _PROBE_SOCKET = _PROBE_SOCKET, None
    if sock is not None:
        try:
            sock.close(0)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_probe_close_socket)


def _propertylogger_alive(timeout_ms=None):
    """轻量探测常驻 Propertylogger 是否活着（发一条 ``PING?``）。

    为什么不复用 ``INIT?``：那会把整棵属性树（可能近 1 MB）序列化回来，做
    心跳太重。探测 socket 在本进程内**共享复用**（各实例串行使用），只在超时
    后重建——REQ 的状态机一旦超时就脏了，必须重建才能再发。

    Args:
        timeout_ms (int|None): 等待应答的超时 [ms]；None 时用
            :data:`PROBE_TIMEOUT_MS`。

    Returns:
        bool: 收到 ``PONG`` 为 True（服务不在/超时/被占用为 False）。
    """
    global _PROBE_SOCKET
    if not PROBE_INTERVAL_S:
        return True
    endpoint = _probe_endpoint()
    if endpoint is None:
        return True
    timeout = int(PROBE_TIMEOUT_MS if timeout_ms is None else timeout_ms)
    with _PROBE_LOCK:
        for _attempt in (0, 1):
            sock = _PROBE_SOCKET
            if sock is None:
                try:
                    sock = zmqcontext.socket(zmq.REQ)
                    sock.setsockopt(zmq.LINGER, 0)
                    # 本机 ZMQ 通道默认 CURVE 加密：connect 端是 client role
                    zmq_security.client_configure(sock)
                    sock.connect(endpoint)
                except Exception:  # noqa: BLE001 - 建连不上即视为不可用
                    return False
                _PROBE_SOCKET = sock
            try:
                sock.RCVTIMEO = timeout
                sock.SNDTIMEO = timeout
                sock.send_string('PING?')
                return sock.recv_string() == 'PONG'
            except Exception:  # noqa: BLE001 - 服务没跑/超时/弯形应答
                try:
                    sock.close(0)
                except Exception:  # noqa: BLE001
                    pass
                _PROBE_SOCKET = None
        return False


def flush_propertylogger(timeout_ms=3000):
    """让 Propertylogger 立刻把属性树写盘（先合并本地 pending 改动）。

    用途：Property Editor 的 Save now 按钮、Property Editor 关窗、
    ProcessManager 停服务之前——Windows 上 ``TerminateProcess`` 无法被服务端
    捕获（进程直接消失），只能靠这种显式时机把最后一段改动落盘。

    Args:
        timeout_ms (int): 等待应答的超时 [ms]。

    Returns:
        tuple: ``(ok, message)``；ok 为 True 表示服务端确认已落盘。
    """
    try:
        conf = cr.Config()
        endpoint = cr.server_endpoints(conf, 'Property Logger')['rep']
    except Exception as e:  # noqa: BLE001 - 没有配置/端点就用不了
        return False, ('no Property Logger endpoint: %s' % e)
    flush_pending()          # 先把本地待落盘改动写好，服务端 flush 时会一并合并
    sock = None
    try:
        sock = zmqcontext.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.RCVTIMEO = int(timeout_ms)
        sock.SNDTIMEO = int(timeout_ms)
        # 本机 ZMQ 通道默认 CURVE 加密：connect 端是 client role
        zmq_security.client_configure(sock)
        sock.connect(endpoint)
        sock.send_string('FLUSH?')
        reply = sock.recv_string()
        return (reply == 'OK'), reply
    except Exception as e:  # noqa: BLE001 - 服务没跑/超时
        return False, ('Property Logger not reachable: %s' % e)
    finally:
        if sock is not None:
            try:
                sock.close(0)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 误用防护：进程内同名重复实例去重 + 存活实例数量上限。
#
# Properties 是 threading.Thread 子类：daemon run() 线程阻塞在 recv 上且被
# threading._active 强引用，因此“在某循环/run() 里每次 new Properties(...)
# 又不 close”的写法会使对象永不回收——socket/线程/fd 持续累积，最终拖垮
# 进程（Windows 上常表现为 libzmq signaler 断言/SIGABRT，或 “Too many open
# files”）。
#
# 这里在构造完成后自动：
#   1) 同 name 去重：同一进程出现同 name 且仍存活的新旧实例时，安全关闭旧实例
#      （先置 _closed→线程退出→再关 socket），使“每轮重建同名 props”的写法
#      稳态只保留一个实例；
#   2) 数量上限：不同名也无限增长时，超过 _LIVE_MAX 自动关闭创建最早的实例。
# 两者都通过既有的安全 close()（先 join 线程再关 socket），不影响正常用法。
_LIVE_MAX = 200  # 同一进程允许同时存活的 Properties 实例上限


def _prune_live_locked():
    """过滤注册表里已回收/已关闭的条目（须在 _LIVE_LOCK 内调用）。

    Returns:
        list: 仍存活且未关闭的实例列表（按创建顺序）。
    """
    keep = []
    alive = []
    for r in _LIVE_INSTANCES:
        p = r()
        if p is None:
            continue
        closed = getattr(p, '_closed', None)
        if closed is not None and closed.is_set():
            continue
        keep.append(r)
        alive.append(p)
    _LIVE_INSTANCES[:] = keep
    return alive


def _dedupe_same_name(name):
    """构造新实例早期调用：关闭与 name 同名且仍存活的旧实例（幂等）。

    必须在创建任何 socket/线程之前执行：若旧同名实例仍开着 monitor，新实例
    的 monitor 可能与之发生 inproc 端点冲突（"Address already in use"），
    且两实例短暂共存会重复握手。

    关闭分两段：先同步 ``_request_close()``（置位 _closed，旧实例的线程立即
    停止接新消息），再把“等待线程退出 + 关 socket”交给后台线程完成。
    理由：整套 close 需要等旧实例的 run() 线程从 ``sub_socket.recv_string``
    醒来（RCVTIMEO），实测 0.2~0.5 s；而同一份客户端会被多个实验窗口反复
    重建（如项目驱动自建的 ``Properties('trap_shim')``，每个窗口 build() 一
    个），同步关闭就等于每个窗口白等一次。monitor 端点已改成每实例唯一
    （inproc UUID），短暂共存不会再撞名。

    Args:
        name (str): 新实例的属性/进程名；与它同名的存活旧实例会被关闭。
    """
    if not name:
        return
    with _LIVE_LOCK:
        alive = _prune_live_locked()
    for p in alive:
        if getattr(p, 'name', None) != name:
            continue
        try:
            if p._request_close():
                _close_async(p)
        except Exception:  # noqa: BLE001 - 关闭旧实例失败不应阻断新建
            pass


def _close_async(instance):
    """把已 ``_request_close()`` 的实例余下的收尾放到后台线程完成。

    调用方（通常是新实例的构造函数）不等它——旧实例的线程已经收到退出
    信号，不会再处理新消息；等待 join 与关 socket 只是为了安全释放资源。

    Args:
        instance (Properties): 已置位 _closed 的实例。
    """
    def _finish():
        try:
            instance._wait_threads(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            instance._close_sockets()
        except Exception:  # noqa: BLE001 - 资源释放失败只影响本实例
            pass

    threading.Thread(target=_finish, daemon=True,
                     name='properties-close').start()


def _enforce_live_cap():
    """构造完成后调用：若存活实例数超上限，关闭创建最早的实例直至回到上限内。"""
    with _LIVE_LOCK:
        alive = _prune_live_locked()
    if len(alive) <= _LIVE_MAX:
        return
    excess = len(alive) - _LIVE_MAX
    for p in alive[:excess]:
        try:
            closed = getattr(p, '_closed', None)
            if closed is None or not closed.is_set():
                p.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            send_warning(
                'Properties({0!r}): too many live Properties instances '
                '({1} > {2}); closed the oldest ones automatically. Please '
                'call close() when an instance is no longer needed.'.format(
                    getattr(p, 'name', '?'), len(alive), _LIVE_MAX))
        except Exception:  # noqa: BLE001 - 告警失败绝不影响关闭
            pass



"""关于本模块涉及的四类配置的说明（信息性文字）：

   Configuration:  系统深层的、基础属性（运行哪些硬件、有哪些驱动）。
   Properties:     通常指对象（如 ROI 位置）。
   Settings/Options:（当前未使用）。
   Preferences:    次要选择，不影响数据！（鼠标指针形状、窗口位置等）。
"""


class PropertyAttribute:
    """把属性树中的一个属性当作普通 Python attribute 来读写。

    宿主类必须有一个 ``_props`` 属性（Properties 实例）。通过 ``value``
    property 可完成基本操作：读、写、自增等。

    Attributes:
        _propname (str): 所访问的属性的名字。
        _default: 属性不存在时的默认值。
        parent: 宿主对象（须有 ``_props``）。
    """

    def __init__(self, propname, defaultval, parent=None):
        """构造属性访问器。

        Args:
            propname (str): 要访问的属性名。
            defaultval: 属性不存在时的默认值；须可 JSON 序列化（可哈希）。
            parent: 通常传 self，即持有 ``_props`` 的对象；供 value 使用。
        """
        self._default = defaultval
        self._propname = propname
        self.parent = parent
        self._value = defaultval

    @property
    def value(self):
        """返回本地缓存值（尚未与属性树同步，读取前请先经 getter 刷新）。"""
        return self._value

    @value.getter
    def value(self):
        """从属性树读取当前值（缺失时返回默认值）并缓存到本地。

        Returns:
            属性树中的当前值或默认值。
        """
        self._value = self.parent._props.get(self._propname, self._default)
        return self._value

    @value.setter
    def value(self, value):
        """写回属性树：本地缓存并调用 ``parent._props.set``。

        Args:
            value: 要写入的值（须可 JSON 序列化）。
        """
        self._value = value
        self.parent._props.set(self._propname, value)

    # these work but I've disabled them because they could be confusing
    # def append(self, value):
    #     self._value.append(value)
    #     self.parent._props.set(self._propname, self._value)
    #
    # def extend(self, values):
    #     self._value.extend(values)
    #     self.parent._props.set(self._propname, self._value)

    def __get__(self, obj, t):
        """描述符协议：从宿主对象的属性树读取属性值。

        Args:
            obj: 宿主对象（须有 ``_props``）。

        Returns:
            属性树中的值或默认值。
        """
        return obj._props.get(self._propname, self._default)

    def __set__(self, obj, v):
        """描述符协议：把值写入宿主对象的属性树。

        Args:
            obj: 宿主对象（须有 ``_props``）。
            v: 要写入的值。
        """
        obj._props.set(self._propname, v)


class Properties(threading.Thread):
    """管理 quatm 控制中心的配置与属性：维护属性字典的同步副本。

    通过 Property Hub 与 Propertylogger 双向同步：本地 ``set/get`` 会发布到
    中央，中央的变更也会推送回本地副本。属性寻址类似文件系统（如
    ``'/Drivers/xxx'`` 或相对名），详见 :meth:`set` / :meth:`get`。

    Attributes:
        name (str): 本进程的名字（set/get 时的默认命名空间）。
        properties (dict): 本地维护的属性字典（受 properties_lock 保护）。
        properties_lock (threading.Lock): 保护 properties 字典的锁。
        recent_changes (set): 自上次 :meth:`changes` 以来变更过的键集合。
    """

    def __init__(self, name, initfromfile=False, enable_monitors=True,
                 snapshot_source=None):
        """构造 Properties 实例并连接到 Property Hub 同步属性。

        Args:
            name (string): 进程名，作为 set/get 等的默认命名空间。
            initfromfile (bool): 一般保持默认 False。早期版本使用；
                目前只有 propertylogger 启动时从文件初始化，其余实例应
                向 propertylogger 请求最新副本。
            enable_monitors (bool): 是否开启 socket monitor（诊断用，每实例
                额外 2 线程 2 socket）。轻量客户端建议关掉。
            snapshot_source (Properties|None): 已有的存活实例；给定时直接
                深拷贝它的属性树，跳过 ``INIT?`` 网络往返（见
                :func:`shared_snapshot_source`）。仅当拷贝失败（源已关闭/
                为空）时才回退到网络握手。默认 None 表示“自动复用进程内
                任一新鲜实例”（可用 ``QUATM_PROPS_SHARED_SNAPSHOT=0`` 彻底
                关闭）；本进程尚无客户端时才走常规网络握手。
        """
        threading.Thread.__init__(self, daemon=True)

        # 误用防护：先安全关闭同名的旧实例，再创建本实例的任何 socket/线程，
        # 避免新旧实例短暂共存时 monitor 端点冲突或重复握手。
        _dedupe_same_name(name)

        conf = cr.Config()
        # 属性树是否来自盘文件（properties.json）而非 hub：这种实例的值可能
        # 陈旧，不能作为其他实例的“快照来源”（见 shared_snapshot_source）
        self._from_file = False
        # iniitialize dictionaries
        self.properties_lock = threading.Lock()
        # ZMQ sockets are not thread-safe: guard the PUB socket so concurrent
        # set() calls from the GUI thread and worker threads cannot corrupt it
        self._send_lock = threading.Lock()
        # 生命周期：close() 后停止线程并关闭全部 socket（实验窗口反复开合
        # 会泄漏 fd，最终把 GUI 进程拖到 "Too many open files"）
        self._closed = threading.Event()
        self._monitor_sockets = []
        self._monitor_threads = []   # event_monitor 线程（close 时先 join）
        self._monitor_stops = []     # 与 _monitor_threads 一一对应的停止标志
        # initialize sockets
        self.pub_socket = zmqcontext.socket(zmq.PUB)
        # 本机 ZMQ 通道默认 CURVE 加密：connect 端是 client role
        zmq_security.client_configure(self.pub_socket)
        # 向后兼容旧 configfile 的无空格键名（Propertyhub / Propertylogger）
        prop_hub = cr.server_endpoints(conf, 'Property Hub')
        self.pub_socket.connect(prop_hub['sub'])
        self.sub_socket = zmqcontext.socket(zmq.SUB)
        zmq_security.client_configure(self.sub_socket)
        self.sub_socket.connect(prop_hub['pub'])
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "Prop")
        # 接收超时：让 run() 线程在 close() 后尽快醒来检查 _closed 并退出
        # （pyzmq 不保证 close 会唤醒另一线程的阻塞 recv）。之前是 1000 ms，
        # GUI 线程在“关实验窗口”时逐实例 join 要等最多 1 s → 明显卡顿。
        # 200 ms 对正常接收无影响（有消息立刻返回），只是空闲时多醒几次。
        self.sub_socket.RCVTIMEO = 200

        # Socket monitors spawn 2 extra threads + 2 sockets per instance purely
        # for diagnostics (they only log connect/disconnect events). They are
        # unnecessary for the many lightweight client instances (every open
        # experiment window creates one) and just multiply threads/sockets.
        if enable_monitors:
            # monitor 线程/停止事件登记：close() 时先 stop 并 join 这些线程，
            # 再关 monitor socket——绝不能跨线程关闭仍被另一线程 poll 的
            # monitor socket（libzmq signaler 会断言/abort）。
            #
            # 关键：monitor 的 inproc 端点必须每实例唯一。pyzmq 默认按
            # socket 的底层 FD 命名（inproc://monitor.s-{FD}），而 OS 会复用
            # 已关闭 socket 的 FD——同名实例快速 create+close+recreate 时
            # 新实例会撞上旧实例尚未完全释放的 monitor 端点，报
            # "Address already in use"（甚至可能诱发 libzmq 内部问题）。
            _mon_uuid = uuid.uuid4().hex
            pub_mon = self.pub_socket.get_monitor_socket(
                addr='inproc://qmon-pub-' + _mon_uuid)
            self._monitor_sockets.append(pub_mon)
            pub_stop = threading.Event()
            self._monitor_stops.append(pub_stop)
            pub_mon_thread = threading.Thread(
                target=event_monitor,
                args=(pub_mon, 'Props: ' + name, 'PUB', pub_stop),
                daemon=True)
            pub_mon_thread.start()
            self._monitor_threads.append(pub_mon_thread)
            sub_mon = self.sub_socket.get_monitor_socket(
                addr='inproc://qmon-sub-' + _mon_uuid)
            self._monitor_sockets.append(sub_mon)
            sub_stop = threading.Event()
            self._monitor_stops.append(sub_stop)
            sub_mon_thread = threading.Thread(
                target=event_monitor,
                args=(sub_mon, 'Props: ' + name, 'SUB', sub_stop),
                daemon=True)
            sub_mon_thread.start()
            self._monitor_threads.append(sub_mon_thread)

        time.sleep(0.01)
        if not initfromfile:
            global _PROPERTYLOGGER_DOWN
            # 进程内快照复用：只要来源实例还活着，就省掉这次 INIT? 往返。
            # 仍保留上面的 sleep(0.01)：它保证 SUB 订阅已就绪，之后发布的
            # 属性更新不会因为慢连接（slow joiner）而漏掉。
            #
            # 未显式给 snapshot_source 时**默认**也复用：驱动/面板自建的客户
            # 端（如项目驱动的 Properties('trap_shim')，每个实验窗口 build()
            # 时都会新建一份）同样能免掉这次往返；服务端每个请求固定延迟 +
            # 序列化整棵树，在 logger 繁忙时客户端可阻塞数百毫秒。
            _snapshot = None
            _snapshot_from_file = False
            if snapshot_source is not None:
                _snapshot, _snapshot_from_file = _snapshot_copy(snapshot_source)
            elif _SHARE_SNAPSHOT:
                _snapshot, _snapshot_from_file = _snapshot_copy(
                    shared_snapshot_source())
            if _snapshot is not None:
                self.properties = _snapshot
                # 来源本身就是“从文件初始化”（Propertylogger 不可用）时，
                # 新实例也必须知道自己同样处于不可靠状态，才能把改动记进
                # pending 文件
                self._from_file = _snapshot_from_file
            elif _PROPERTYLOGGER_DOWN:
                # already known unreachable in this process: load from file
                # immediately instead of blocking the GUI thread again
                self._from_file = True
                self.properties = cr.Properties()
            else:
                self.init_socket = zmqcontext.socket(zmq.REQ)
                # 本机 ZMQ 通道默认 CURVE 加密：connect 端是 client role
                zmq_security.client_configure(self.init_socket)
                # Never block the GUI for a long time if the Propertylogger
                # server is down: short timeout + one-time-per-process flag, so
                # opening a GUI that creates several Properties instances (e.g.
                # image_group, h5storage) doesn't spin the cursor for 5 s each.
                self.init_socket.RCVTIMEO = 500  # milliseconds
                self.init_socket.connect(
                    cr.server_endpoints(conf, 'Property Logger')['rep'])
                self.init_socket.send_string('INIT?')
                try:
                    message = self.init_socket.recv_string()
                    self.properties = self.init_socket.recv_json()
                except zmq.ZMQError:
                    _PROPERTYLOGGER_DOWN = True
                    print('Properties({}): no reply from Propertylogger, loading properties from file.'.format(name))
                    self._from_file = True
                    self.properties = cr.Properties()
                finally:
                    self.init_socket.close()
        else:
            self._from_file = True
            self.properties = cr.Properties()
        self.recent_changes = set()  # keeps recent changes

        checkThread = threading.Thread(target=self.check, daemon=True)
        self._check_thread = checkThread
        checkThread.start()

        # start socket listening thread
        self.start()
        self.name = name
        if not name in self.properties:
            n = self.get('/' + name, {})
        logging.debug(self.properties)
        # 进程内统一注册：解释器退出时安全关闭（先停线程再关 socket）
        _register(self)
        # 误用防护：超上限自动裁剪最旧实例
        _enforce_live_cap()

    def _parsekey(self, key):
        '''把键转换成字符串列表：每项对应多层字典中的一层。

        Args:
            key (str): quatm 多层字典的键（命名类似 unix 文件系统）。

        Returns:
            list[str]: 键路径各层组成的列表。
        '''
        if type(key) != list:
            if key[0] != '/':
                if key[-1] == '/':
                    key = key[:-1]
                key = '/' + self.name + '/' + key
            keys = key.split('/')
        else:
            keys = key
        # if the key started with '/'  an empty entry has to be removed
        if keys[0] == '':
            keys = keys[1:]
        return keys

    def set(self, key, value):
        """设置属性值并发布变更到 Property Hub。

        地址规则与文件系统类似：以 ``/`` 开头从字典根开始寻址，否则以
        self.name 为起点。若新值与当前值相同则跳过（不重复发布）。

        Args:
            key (str|list[str]): 要设置的属性；若以 ``/`` 开头则从根寻址，
                否则相对于本进程名。
            value: 任意想存的值，须可 JSON 序列化（不要传自定义类对象）。
        """
        keys = self._parsekey(key)
        # Compare against the stored value WITHOUT deep-copying it first.
        # The old code called self.get(key) here, which deep-copies the whole
        # current value just to compare, and then _set deep-copies it again.
        if self._raw_get(keys) != value:
            self._set(keys, value)
            self._send({'keys': keys, 'value': value})
            # 服务没在跑（本实例是从盘文件初始化的）→ 没有任何人会落盘，
            # 把改动记进本地 pending 文件，Propertylogger 起来时合并。
            if self._from_file:
                _pending_record(keys, value)
        # print('properties.py Sending: {} with value {}'.format(key, value))

    def _raw_get(self, keys):
        """不深拷贝地读取一个已解析键的值（仅供 set() 内比较使用）。

        调用方不得修改返回对象。与 get() 不同：不创建缺失键的默认值；
        缺失时返回唯一哨兵对象，从而区分“存的是 None”与“键不存在”。

        Args:
            keys (list[str]): 已由 _parsekey 解析的键列表。

        Returns:
            对应值，或缺失时的哨兵 _MISSING。
        """
        try:
            with self.properties_lock:
                prop = self.properties
                for key in keys[:-1]:
                    prop = prop[key]
                return prop[keys[-1]]
        except KeyError:
            return _MISSING

    def _set(self, keys, value):
        """把解析后的键值写入本地属性字典（受锁保护，深拷贝）。

        处理带 ``options`` 字典的特殊语义：若目标已是带 options 的条目且
        新值不在选项中则拒绝修改；纯 dict 且带 options 的 value 则整体写入。

        Args:
            keys (list[str]): 已解析的键路径。
            value: 要写入的值（须可 JSON 序列化）。
        """
        with self.properties_lock:
            prop = self.properties
            # if the property does not exist we iteratively create it
            for key in keys[:-1]:
                if not key in prop:
                    prop[key] = {}
                prop = prop[key]
            # else:
            if isinstance(value, dict) and 'options' in value.keys():
                if not keys[-1] in prop: prop[keys[-1]] = {}
                # print('_set received a dict')
                # print('now setting:', value)
                prop[keys[-1]] = copy.deepcopy(value)
            elif keys[-1] in prop and isinstance(prop[keys[-1]], dict) and 'options' in prop[keys[-1]].keys():
                # print('_set found a dict')
                # print('which was:', prop[keys[-1]])
                # print('now setting:', value)
                if value not in prop[keys[-1]]['options']:
                    print('value {} not in options {}. no change made to {}'.format(value, prop[keys[-1]]['options'],
                                                                                    keys[-1]))
                else:
                    prop[keys[-1]]['value'] = copy.deepcopy(value)
            else:
                prop[keys[-1]] = copy.deepcopy(value)
            # for i in range(len(keys)):
            #    self.recent_changes.add('/'+'/'.join(keys[:i+1]))
            self.recent_changes.add('/' + '/'.join(keys))

    def _send(self, data, flags=0):
        """把一条变更消息发布到 Property Hub。

        已 close 或 socket 出错时静默忽略（不抛异常）。

        Args:
            data (dict): 变更载荷（{"keys":..., "value":...} 或 {"delete":...}）。
            flags (int): ZMQ 发送标志。
        """
        if self._closed.is_set():
            return
        with self._send_lock:
            try:
                # self.pub_socket.send(bytes('Propertychange_'+self.name,'utf8'),flags|zmq.SNDMORE)
                self.pub_socket.send_string('Propertychange_' + self.name, flags | zmq.SNDMORE)
                # print('properties.py sending _send')
                return self.pub_socket.send_json(data, flags)
            except zmq.ZMQError:
                # socket 已关闭（close() 之后）：静默忽略
                return

    def delete(self, key):
        """删除一个条目（含其全部子条目）并广播删除消息。

        Args:
            key (str|list[str]): 要删除的键，寻址规则同 set()。
        """
        keys = self._parsekey(key)
        if self._del(keys):
            self._send({'delete': keys})

    def _del(self, keys):
        """从本地属性字典删除解析后的键（受锁保护）。

        路径中间节点缺失或不是 dict 时不创建任何空分支，直接返回 False；
        只有确实删除了条目才返回 True（供 delete() 决定是否广播）。

        Args:
            keys (list[str]): 已解析的键路径。

        Returns:
            bool: 是否实际删除成功。
        """
        removed = False
        with self.properties_lock:
            prop = self.properties
            for key in keys[:-1]:
                if key not in prop or not isinstance(prop[key], dict):
                    return removed
                prop = prop[key]
            if keys[-1] in prop:
                del prop[keys[-1]]
                removed = True
                self.recent_changes.add('/' + '/'.join(keys[:-1]))
        return removed

    def _recv(self, flags=0):
        """接收并应用一条属性变更消息（阻塞，超时/关闭时返回）。

        收到的消息可能是删除或设置指令，直接应用到本地字典。超时或
        socket 已关闭时返回 None，供 run() 检查退出标志。

        Args:
            flags (int): ZMQ 接收标志。
        """
        try:
            message = self.sub_socket.recv_string(flags=flags)
            md = self.sub_socket.recv_json(flags=flags)
        except zmq.ZMQError:
            # 超时（RCVTIMEO）或 socket 已关闭：让 run() 循环检查 _closed
            return
        # logging.debug(self.name,' received: ',message,md)
        if 'delete' in md:
            # print(self.name,' properties._recv deleting',md['delete'])
            self._del(md['delete'])
        elif 'keys' in md and 'value' in md:
            self._set(md['keys'], md['value'])
            # logging.debug('setting: ',md['keys'],md['value'])

    def get(self, key, defaultvalue=None):
        """从属性树读取值（总是深拷贝副本）。

        若键不存在，会用 defaultvalue 创建并广播该默认值，使属性树能自行
        生长和维护。

        Args:
            key (str): 属性键，规则同 set()；传 ``'/'`` 返回整棵树。
            defaultvalue: 键不存在时用于创建条目的默认值。

        Returns:
            属性树中的条目（深拷贝），或刚设置的默认值。
        """
        if key == '/':
            with self.properties_lock:
                return copy.deepcopy(self.properties)
        elif key[-1] == '/':
            key = key[:-1]
        keys = self._parsekey(key)
        return self._get(keys, defaultvalue)

    def _get(self, keys, defaultvalue):
        """按解析后的键读取（深拷贝），缺失时设置并广播默认值。

        Args:
            keys (list[str]): 已解析的键路径。
            defaultvalue: 缺失时创建的默认值。

        Returns:
            读取到的值（深拷贝）或默认值。
        """
        # print('_get:')
        try:
            with self.properties_lock:
                prop = self.properties
                for key in keys[:-1]:
                    prop = prop[key]
                value = copy.deepcopy(prop[keys[-1]])
                if isinstance(value, dict) and 'options' in value.keys():
                    # print('_get found a dict')
                    # print('which is:',value)
                    value = value['value']

                    # prop[keys[-1]]=copy.deepcopy(value['value'])

        except KeyError:
            logging.debug('properties.py key does not exist')
            self._set(keys, defaultvalue)
            self._send({'keys': keys, 'value': defaultvalue})
            value = defaultvalue
            logging.debug('tried to _get an unset property {}'.format(keys))
            if isinstance(value, dict) and 'options' in value.keys():
                value = value['value']
            logging.debug('setting default value: {}'.format(value))
        return value

    def changes(self, includeparent=True):
        """返回自上次调用以来发生变更的所有键。

        Args:
            includeparent (bool): 若为 True，同时包含所有父级路径。

        Returns:
            set[str]: 自上次调用以来变更的键集合。
        """
        with self.properties_lock:
            changes = self.recent_changes
            self.recent_changes = set()

        if includeparent:
            chang = set()
            for key in changes:
                keys = key[1:].split('/')
                for i in range(len(keys)):
                    chang.add('/' + '/'.join(keys[:i + 1]))
            return chang
        else:
            return changes

    def check(self):
        """后台检查线程：心跳与 Propertylogger 存活探测。

        每 0.5 s 唤醒一次；每隔 interval 秒读取 ``/Checker/check_time`` 并与
        本地时钟比较（历史遗留，当前只作为心跳记录）。

        另外每隔 :data:`PROBE_INTERVAL_S` 用 ``PING?`` 探测一次常驻
        Propertylogger：**探测失败就把本实例标记为“从文件初始化”**，于是
        后续 ``set()`` 会把改动记进本地 pending 文件（下次服务启动时合并），
        避免“服务中途挂掉 → 之后的属性改动静默丢失”。连续失败
        :data:`PROBE_FAILS_BEFORE_DOWN` 次才判定不可用（写盘阻塞导致的偶发
        超时不会误报）。

        用 ``_closed.wait`` 代替 ``time.sleep``：``close()`` 会 join 本线程，
        若这里睡死 0.5 s，则每次关闭都要白等最多 0.5 s——实验窗口反复开合、
        或构造同名客户端时自动关闭旧实例（见 :func:`_dedupe_same_name`）时
        会直接表现为卡顿。
        """
        # ToDo: here we need to add a switch for the check, to avoid the unnecessary error output. as a dirty solution, I disabled the send_error.
        lastTime = 0
        timeout = 50
        interval = 30
        probe_time = 0.0
        probe_fails = 0
        while not self._closed.is_set():
            if self._closed.wait(0.5):
                return
            t = time.time()
            if t - lastTime > interval:
                lastTime = t
                check_time = self.get('/Checker/check_time', 0)
                if abs(check_time - t) > timeout:
                    pass
                    # send_error('Property check timeout {}; time.time {}, check_time {}'.format(self.name, t, check_time))
            if PROBE_INTERVAL_S > 0 and t - probe_time > PROBE_INTERVAL_S:
                probe_time = t
                if _propertylogger_alive():
                    probe_fails = 0
                    self._from_file = False
                else:
                    probe_fails += 1
                    if probe_fails >= PROBE_FAILS_BEFORE_DOWN:
                        if not self._from_file:
                            send_warning(
                                ('Properties({0!r}): the Propertylogger '
                                 'service is not reachable; property '
                                 'changes are kept in the local pending '
                                 'file and merged when it starts.'
                                 ).format(self.name))
                        self._from_file = True

    def run(self):
        """线程主体：循环接收属性变更并应用到本地副本，直到 _closed 置位。"""
        while not self._closed.is_set():
            self._recv()

    def _request_close(self):
        """置位 _closed（幂等）。只唤醒后台线程，不等待、不关 socket——
        供多个实例“先全部唤醒、再统一等待、最后统一关 socket”的并行关闭
        使用（见 :func:`close_all`）。返回 True 表示本次真正发起了关闭请求。"""
        if getattr(self, '_closed', None) is None:
            self._closed = threading.Event()
        if self._closed.is_set():
            return False
        self._closed.set()
        return True

    def _wait_threads(self, timeout=3.0):
        """等待本实例的 run()/check() 线程退出（收到 _closed 后自行醒来）。
        应在所有相关实例都 _request_close() 之后再逐个调用——此时
        各线程并发醒来，总等待 ≈ RCVTIMEO，而不是 N×RCVTIMEO 串行累加。

        Args:
            timeout (float): 单个线程 join 的超时秒数。
        """
        try:
            if self is not threading.current_thread():
                self.join(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        ct = getattr(self, '_check_thread', None)
        if ct is not None and ct is not threading.current_thread():
            try:
                ct.join(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    def _close_sockets(self):
        """关闭主 socket 与 monitor socket（先停线程、再关 socket；幂等）。

        顺序是崩溃安全的关键：
          1) 置位各 stop 标志并 join event_monitor 线程——它们 poll 带短超时
             （~50 ms），醒来检查 stop 后自行退出并关闭各自 monitor socket；
          2) 确认无线程阻塞后再关闭主 pub/sub socket；
          3) 兜底幂等关闭 monitor socket。

        绝不能先跨线程关闭仍被另一线程 poll 的 monitor socket——libzmq
        signaler 会报 “Interrupted system call [10004]” / “Bad file
        descriptor” 并 abort（Windows 上表现为“即使正确调用了 close() 也
        崩溃”，例如在实验 run() 里 create+close Properties）。
        """
        # 1) 让 event_monitor 线程退出
        for stop in getattr(self, '_monitor_stops', []):
            try:
                stop.set()
            except Exception:  # noqa: BLE001
                pass
        for t in getattr(self, '_monitor_threads', []):
            try:
                t.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        # 2) 发送屏障：置 _closed 后仍可能有在途 _send 持锁执行（由其它
        #    线程调用），先同步 _send_lock，确保没有任何线程正在使用
        #    pub_socket，再关 socket，避免跨线程关闭/撕裂 SNDMORE 多帧。
        try:
            with self._send_lock:
                pass
        except Exception:  # noqa: BLE001
            pass
        # 3) 关主 socket（此时已无线程阻塞/发送在 socket 上；linger=0 立即
        #    释放，避免关闭线程被积压发送队列拖住）
        for attr in ('pub_socket', 'sub_socket'):
            s = getattr(self, attr, None)
            try:
                if s is not None and not s.closed:
                    s.close(0)
            except Exception:  # noqa: BLE001
                pass
        # 4) 兜底幂等关闭 monitor socket（线程已退出）
        for s in getattr(self, '_monitor_sockets', []):
            try:
                if s is not None and not s.closed:
                    s.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        """关闭 socket 并停止后台线程（实验窗口关闭时调用，防止 fd 泄漏）。

        每个 Properties 实例持有 PUB+SUB（以及可选 monitor）socket 和循环
        线程；不关闭的话反复开合实验窗口会把 GUI 进程的 fd 耗尽
        （"Too many open files"）。幂等：可重复调用。

        顺序至关重要：**先让线程自行退出，再关 socket**，绝不能先跨线程
        关闭正被另一线程 recv 的 socket。否则 libzmq 的信号器（signaler）
        会报：

          * ``Bad file descriptor (src/signaler.cpp:365)``
          * ``Assertion failed: nbytes == sizeof (dummy) (src/signaler.cpp:368)``

        并直接 abort 进程——表现为“关/删实验窗口时 Browser 崩溃
        （SIGABRT）”。run() 线程阻塞在 ``sub_socket.recv_string``
        （RCVTIMEO=200 ms），置位 _closed 后 ≤0.2 s 自行醒来退出；
        check() 每 0.5 s 醒来一次检查 _closed。join 成功后再关主 socket；
        关闭被监控的主 socket 会让 event_monitor 收到 EVENT_MONITOR_STOPPED
        并自行收尾 monitor socket。

        单实例逐个关闭（GUI 线程同时持有很多 Properties 时）会串行等待；
        需要一次性关闭多个实例时请用 :func:`close_all`（并行，总耗时 ≈
        单个 RCVTIMEO）。
        """
        if not self._request_close():
            return
        self._wait_threads()
        self._close_sockets()

    def __enter__(self):
        """支持 ``with Properties(...) as p:`` —— 退出块时自动 close。

        临时读写属性的代码若新建 Properties 后忘记 close，会泄漏
        4 线程 + 4 socket（“Too many open files”）。用 with 块即可安全释放。
        """
        return self

    def __exit__(self, *exc_info):
        """退出 with 块时自动调用 close() 释放资源。"""
        self.close()
        return False


def close_all(*instances):
    """并发关闭多个 Properties 实例（保持与 close() 相同的崩溃安全顺序）。

    GUI 线程“关闭实验窗口”时，一个实验会持有多个 Properties（实验自身的
    ``_props`` + 各设备驱动的 ``_props``）。若逐个 ``close()``，GUI 线程要
    串行等待 N × RCVTIMEO（例如 N=3、200 ms/个 ≈ 0.6 s 冻结）——这就是关
    窗“卡顿/不跟手”的主要来源。

    这里先给所有实例置位 ``_closed``（各 run 线程**并行**醒来，总等待 ≈
    单个 RCVTIMEO），再统一 join，最后统一关 socket。线程都退出后才关
    socket，避免 libzmq signaler 的 ``Bad file descriptor`` / 断言 abort。
    """
    items = []
    for p in instances:
        if p is None or not hasattr(p, '_request_close'):
            continue
        try:
            if p._request_close():
                items.append(p)
        except Exception:  # noqa: BLE001
            pass
    for p in items:
        p._wait_threads(timeout=2.0)
    for p in items:
        try:
            p._close_sockets()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    pass
