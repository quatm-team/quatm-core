"""Persistent service that records Message Hub traffic.

The service subscribes to the message stream, batches records and writes them
to the message store. It is independent of any GUI: as long as the service is
running, messages are archived even when no monitoring window is open.

The module also arbitrates a single-writer heartbeat lock so the service and an
optional UI fallback recorder cannot write the same message twice.
"""

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


import signal
import sqlite3
import time

from quatm.servers import heartbeat
from quatm.servers.messageclient import MessageClient
from quatm.servers.messagestore import (MessageRow, MessageStore,
                                        load_level_policy)

# 记录权心跳文件（与消息库同目录）：同一时刻只允许一个写者持有
LOGGER_LOCK = 'message_logger.lock'      # 常驻服务持有
RECEIVER_LOCK = 'message_receiver.lock'  # 界面兜底记录时持有
# 心跳刷新间隔 [s]
HEARTBEAT_INTERVAL = 2.0
# 心跳超过这么久没更新 → 认为持有者已经不在了 [s]
STALE_AFTER = 6.0
# 缓冲里最久滞留多久就必须写库 [s]：安静期消息很少时，不能等攒够一批才写
# （否则进程被停/崩溃会丢掉这几十条里的前几条）
MAX_BUFFER_DELAY = 2.0
# 记录权持有者类型 → 心跳文件名
_LOCK_FILES = {'logger': LOGGER_LOCK, 'gui': RECEIVER_LOCK}
# 开库遇到 database is locked 时的重试次数与间隔 [s]（给另一个写者让时间）。
# 注意：一次构造内部已经会等锁 + 重试，所以这里只留几次，避免库真的坏掉时
# 启动卡好几分钟都不报错。
OPEN_RETRIES = 3
OPEN_RETRY_DELAY_S = 2.0
# 消息库淘汰（prune）的默认触发条件：写满这么多条，或距上次清理这么久。
# 两者都设在记录器自己的线程里检查（GUI 线程不参与），避免清理大库时卡界面。
# 0 表示关闭对应触发条件；实际阈值由 MessageStore.prune 自己解析配置。
PRUNE_EVERY_MESSAGES = 5000
PRUNE_INTERVAL_S = 3600.0


def lock_path(kind='logger', db_path=None):
    """返回记录权心跳文件的路径（与消息库同目录）。

    Args:
        kind (str): 持有者类型，``'logger'``（常驻服务）或 ``'gui'``（界面）。
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        str: 心跳文件绝对路径。
    """
    from quatm.servers.messagestore import _default_db_path
    db_path = db_path or _default_db_path()
    return os.path.join(os.path.dirname(db_path),
                        _LOCK_FILES.get(kind, LOGGER_LOCK))


def read_heartbeat(kind='logger', db_path=None, max_age=STALE_AFTER):
    """读一个记录权心跳文件，判断它是否还新鲜。

    读取/年龄校验/pid 存活判断统一在 :mod:`quatm.servers.heartbeat`（与推送
    网关共用同一套实现）；本函数只负责把“记录权类型”映射到文件路径，并把
    ``kind`` 放进返回值。

    Args:
        kind (str): 持有者类型（``'logger'``/``'gui'``）。
        db_path (str|None): 消息库路径；None 用默认位置。
        max_age (float): 心跳最大可接受年龄 [s]。

    Returns:
        dict|None: 新鲜时返回心跳内容（加了 ``kind`` 与 ``age``）；否则 None。
    """
    info = heartbeat.read_heartbeat(lock_path(kind, db_path),
                                    max_age=float(max_age))
    if info is None:
        return None
    info['kind'] = kind
    return info


def recorder_status(db_path=None, max_age=STALE_AFTER):
    """返回当前持有“记录权”的一方（决定谁能写消息库）。

    常驻服务优先：两边心跳都新鲜时以常驻服务为准（界面会立刻停写）。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。
        max_age (float): 心跳最大可接受年龄 [s]。

    Returns:
        dict|None: ``{'kind': 'logger'|'gui', 'pid', 'host', 'db', 'age', ...}``；
        没人在记录时返回 None。
    """
    return (read_heartbeat('logger', db_path, max_age)
            or read_heartbeat('gui', db_path, max_age))


def logger_is_alive(db_path=None, max_age=STALE_AFTER):
    """常驻消息记录服务是否正在记录（界面用它决定要不要自己兜底）。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。
        max_age (float): 心跳最大可接受年龄 [s]。

    Returns:
        bool: 常驻服务在位为 True。
    """
    status = recorder_status(db_path, max_age)
    return bool(status) and status.get('kind') == 'logger'


def receiver_is_alive(db_path=None, max_age=STALE_AFTER):
    """是否已经有界面在兜底记录（常驻服务启动时用它决定要不要让位）。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。
        max_age (float): 心跳最大可接受年龄 [s]。

    Returns:
        bool: 有界面在记录为 True。
    """
    status = read_heartbeat('gui', db_path, max_age)
    return bool(status)


def write_heartbeat(kind='logger', db_path=None, name='', extra=None):
    """写下/刷新自己的记录权心跳（原子替换，内容含 pid/主机/时间戳）。

    原子写统一在 :mod:`quatm.servers.heartbeat`；这里只把“记录权类型/数据库
    路径”写进业务字段（``kind``/``db``），保持旧文件的键不变。

    Args:
        kind (str): 持有者类型（``'logger'``/``'gui'``）。
        db_path (str|None): 消息库路径；None 用默认位置。
        name (str): 显示名（写入内容，便于手工查看是谁在记录）。
        extra (dict|None): 额外字段。

    Returns:
        bool: 写成功为 True（失败不抛异常，只是别的进程可能以为你不在）。
    """
    payload = {'kind': kind, 'db': db_path}
    if isinstance(extra, dict):
        payload.update(extra)
    return heartbeat.write_heartbeat(lock_path(kind, db_path), name=name,
                                     extra=payload)


def clear_heartbeat(kind='logger', db_path=None):
    """删除自己的记录权心跳（干净退出时用；被强杀时靠心跳过期兑底）。

    Args:
        kind (str): 持有者类型（``'logger'``/``'gui'``）。
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        bool: 确实删掉了为 True。
    """
    return heartbeat.clear_heartbeat(lock_path(kind, db_path))

# 冲刷请求文件（与消息库同目录）：ProcessManager 关闭时要保证“停机前的最后几条
# 消息”真的进了库，而 Windows 上 terminate() 是 TerminateProcess —— 信号处理
# 根本不跑，缓冲里还没写库的那几条就没了（POSIX 上 SIGTERM 会触发 flush）。
# 所以用一个**跨平台**的请求文件：谁要冲刷就建这个文件，记录器看到就冲刷并删掉。
FLUSH_REQUEST = 'message_logger.flush'


def flush_request_path(db_path=None):
    """返回冲刷请求文件的路径（与消息库同目录）。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        str: 请求文件绝对路径。
    """
    from quatm.servers.messagestore import _default_db_path
    db_path = db_path or _default_db_path()
    return os.path.join(os.path.dirname(db_path), FLUSH_REQUEST)


def take_flush_request(db_path=None):
    """检查并**消费**冲刷请求（记录器主循环调用；没有请求时是零成本判断）。

    只在请求存在时才做删除与日志，所以即便每轮（默认 50 ms）都调用也不会
    产生可观的系统调用。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        bool: 确实取到（并已删除）一个冲刷请求为 True。
    """
    path = flush_request_path(db_path)
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


def request_flush(timeout_ms=1500, db_path=None, poll_ms=25):
    """请常驻记录器把缓冲立刻写库，并等它接单（跨平台，Windows 也能用）。

    做法：建一个 :data:`FLUSH_REQUEST` 文件，记录器看见就冲刷并删掉它；这里等到
    文件被删（或超时）为止。

    返回值的 ``ok`` 语义是“**没有未落盘的缓冲**”：记录器冲刷完成了是 True；
    压根没有记录器在记录（例如界面正在兜底写库，或者根本没起服务）也算 True——
    这种情况本来就没有缓冲可丢，调用方不必报警。没人接单（心跳在、但不响应）
    才返回 False，并会把残留的请求文件清掉，免得下一个记录器一启动就白冲刷一次。

    Args:
        timeout_ms (int): 最多等多久 [ms]。
        db_path (str|None): 消息库路径；None 用默认位置。
        poll_ms (int): 轮询间隔 [ms]。

    Returns:
        tuple[bool, str]: ``(是否已无未落盘缓冲, 说明文字)``。
    """
    status = recorder_status(db_path)
    if not status or status.get('kind') != 'logger':
        return True, 'no message logger is recording; nothing to flush'
    path = flush_request_path(db_path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
    except OSError as exc:
        return False, 'cannot write flush request: %s' % exc
    deadline = time.time() + max(0.0, timeout_ms / 1000.0)
    while time.time() < deadline:
        if not os.path.exists(path):
            return True, 'message logger flushed its buffer'
        time.sleep(max(0.005, poll_ms / 1000.0))
    try:
        os.remove(path)          # 没人接单：把残留请求清掉
    except OSError:
        pass
    return False, 'message logger did not answer in %d ms' % timeout_ms

class MessageLogger:
    """无界面消息记录器：订阅消息流、攒批写库、维护心跳文件。

    Args:
        name (str): 订阅时用的流名（ProcessManager 传 ``'Servers/Message Logger'``）。
        store (MessageStore|None): 目标存储；None 时按默认路径新建。
        batch_size (int): 攒够多少条写一次库。
        interval (float): 没有新消息时的轮询间隔 [s]。
        max_per_tick (int): 单次最多处理多少条（防消息风暴把内存/事件循环打爆）。
        flush_interval (float): 缓冲里最久滞留多久就写库 [s]（安静期不抩消息）。
        heartbeat (float): 心跳刷新间隔 [s]，0 表示不写心跳文件（测试用）。
        prune_every (int): 每写入这么多条消息触发一次消息库淘汰（0=不按条数）。
        prune_interval (float): 每隔这么多秒触发一次消息库淘汰（0=不按时间）。
        db_path (str|None): 消息库路径（心跳文件与策略文件都以它定位）。
        client: 已建好的消息客户端（测试注入用）；None 时自己建一个。

    Attributes:
        received (int): 已从消息流收到的条数。
        written (int): 已写入数据库的条数（不含被存储策略跳过/让位期间丢弃的）。
        owns_recorder (bool): 是否持有记录权（没取到时先让位等待，不写库）。
    """

    def __init__(self, name='Servers/Message Logger', store=None,
                 batch_size=5, interval=0.05, max_per_tick=500,
                 flush_interval=MAX_BUFFER_DELAY, heartbeat=HEARTBEAT_INTERVAL,
                 prune_every=PRUNE_EVERY_MESSAGES,
                 prune_interval=PRUNE_INTERVAL_S,
                 db_path=None, client=None):
        """构造记录器（此时还不会连接消息流，调用 :meth:`open`/:meth:`run` 开始）。"""
        self.name = name
        self.store = store if store is not None else MessageStore(path=db_path)
        self.db_path = db_path or getattr(self.store, 'path', None)
        self.batch_size = max(1, int(batch_size))
        self.interval = max(0.005, float(interval))
        self.max_per_tick = max(1, int(max_per_tick))
        self.flush_interval = max(0.0, float(flush_interval))
        self.heartbeat = max(0.0, float(heartbeat))
        self.prune_every = max(0, int(prune_every or 0))
        self.prune_interval = max(0.0, float(prune_interval or 0.0))
        self._client = client
        self._buffer = []
        self._buffer_since = None
        self.received = 0
        self.written = 0
        self.running = False
        self.owns_recorder = False
        self.started_at = None
        self._last_heartbeat = 0.0
        self._last_policy_check = 0.0
        self._last_wait_note = 0.0
        self._last_prune = time.time()
        self._since_prune = 0
        self.last_pruned = 0
        self._policy = None
        self.last_error = ''

    # -------------------------------------------------------------- lifecycle
    def acquire(self):
        """尝试取得记录权：没人在记录（或持有人就是自己）时写下自己的心跳。

        界面正在兜底记录时返回 False：让位，不要两个写者（消息不会丢，
        界面正在写）。

        Returns:
            bool: 取得记录权为 True。
        """
        holder = recorder_status(self.db_path,
                                 max_age=self.heartbeat or STALE_AFTER)
        # 别人（界面，或另一个 logger 进程）在记录 → 让位；只有“心跳就是我自己的
        # logger 心跳”（同一个进程重复 open）才算没人在记录
        if holder and not (holder.get('kind') == 'logger'
                           and holder.get('pid') == os.getpid()):
            return False
        self.owns_recorder = True
        self._touch_lock()
        return True

    def open(self, force=False):
        """取得记录权并连接消息流（可重复调用）。

        Args:
            force (bool): True 时忽略“界面正在记录”直接接管（测试/人工介入用）。

        Returns:
            bool: 取到记录权且消息流可用为 True；别人正在记录、或消息流不可用
            时返回 False（后者会把原因记在 :attr:`last_error`）。
        """
        if not (force or self.acquire()):
            return False
        self._refresh_policy(force=True)
        self.started_at = self.started_at or time.time()
        try:
            if self._client is None:
                self._client = self._make_client()
        except Exception as exc:  # noqa: BLE001 - 没 hub 时不该把服务打挂
            self._client = None
            self.last_error = 'message stream unavailable: %s' % exc
        self.running = True
        return self._client is not None

    def _make_client(self):
        """建立并订阅消息流客户端（单独一个方法便于测试替换/子类覆盖）。

        Returns:
            MessageClient: 已 ``subscribe('')`` 的客户端实例。
        """
        client = MessageClient(self.name)
        client.subscribe('')
        return client

    def run(self):
        """主循环：等记录权 → 收消息 → 攒批入库，顺带刷心跳与重读存储策略（常驻）。

        界面正在兜底记录时会先让位（消息一条不少：界面在写它），等界面关掉
        或心跳过期才接管。收到 ``SIGTERM``/``SIGINT``（ProcessManager 停服务、
        Ctrl-C）时会先冲刷缓冲再退出，最后几条消息不会丢。
        """
        self._install_signal_handlers()
        try:
            while True:
                if not self.running:
                    if not self.open():
                        if time.time() - self._last_wait_note > 30:
                            self._last_wait_note = time.time()
                            # print('[message logger] 等界面停止记录后接管 (%s)'
                            #       % self.db_path, flush=True)
                        time.sleep(1.0)
                        continue
                    # print('[message logger] 记录中：%s' % self.status_line(),
                    #       flush=True)
                self.poll_once()
                if self.heartbeat and time.time() - self._last_heartbeat >= self.heartbeat:
                    self._touch_lock()
                if not self.heartbeat or time.time() - self._last_policy_check >= self.heartbeat:
                    self._refresh_policy()
                self._serve_flush_request()
                self._maybe_prune()
                if self.interval:
                    time.sleep(self.interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self):
        """冲刷缓冲、关闭消息流并交出记录权（幂等；被杀时靠心跳过期兜底）。"""
        self.running = False
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - 退出路径绝不抛
            pass
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if self.owns_recorder:
            self.owns_recorder = False
            clear_heartbeat('logger', self.db_path)

    # ------------------------------------------------------------------- poll
    def poll_once(self):
        """轮询一次消息流：解析新消息 → 攒批 → 必要时写库。

        除了“攒够一批”，缓冲里最久滞留超过 :attr:`flush_interval` 也会先冲刷：
        实验间隙消息很少时，不能等攒够 5 条才写（进程被停就白收了）。

        Returns:
            int: 本次调用实际写入数据库的条数（0 表示没有新消息或还没该写）。
        """
        if (self.flush_interval and self._buffer and self._buffer_since
                and time.time() - self._buffer_since >= self.flush_interval):
            self.flush()
        client = self._client
        if client is None:
            return 0
        parsed = []
        remaining = self.max_per_tick
        try:
            while remaining > 0 and client.has_new_data():
                raw = client.recv()
                row = MessageRow.parse(raw)
                remaining -= 1
                if row is None:
                    continue
                parsed.append(row)
        except Exception as exc:  # noqa: BLE001 - 单次轮询失败不致命
            self.last_error = 'receive failed: %s' % exc
            return 0
        if not parsed:
            return 0
        if not self._buffer:
            self._buffer_since = time.time()
        self.received += len(parsed)
        self._buffer.extend(parsed)
        if len(self._buffer) >= self.batch_size:
            return self.flush()
        return 0

    def flush(self):
        """把缓冲里的消息立即写库。

        Returns:
            int: 实际写入条数（失败返回 0 并记录 :attr:`last_error`）。
        """
        if not self._buffer or self.store is None:
            self._buffer = []
            self._buffer_since = None
            return 0
        batch, self._buffer = self._buffer, []
        self._buffer_since = None
        try:
            written = self.store.append(batch)
        except Exception as exc:  # noqa: BLE001 - 写库失败只记状态
            self.last_error = 'store failed: %s' % exc
            return 0
        self.written += written
        self._since_prune += max(0, int(written or 0))
        return written

    def _maybe_prune(self):
        """周期性淘汰消息库里的旧消息（每 N 条或每 T 秒，由记录器线程触发）。

        只在**写入足够多**或**距上次清理足够久**时才真的调用
        :meth:`~quatm.servers.messagestore.MessageStore.prune`，因此正常轮询
        里只是几次比较、不会影响记录延迟。清理失败只记 :attr:`last_error`，
        绝不让常驻服务停摆；具体保留天数/容量上限由消息库自身解析配置。

        Returns:
            int: 本次清理删除的条数（没触发或失败时为 0）。
        """
        if not self.prune_every and not self.prune_interval:
            return 0
        now = time.time()
        due_by_count = bool(self.prune_every
                            and self._since_prune >= self.prune_every)
        due_by_time = bool(self.prune_interval
                           and now - self._last_prune >= self.prune_interval)
        if not (due_by_count or due_by_time):
            return 0
        self._last_prune = now
        self._since_prune = 0
        if self.store is None or not hasattr(self.store, 'prune'):
            return 0
        try:
            deleted = int(self.store.prune() or 0)
        except Exception as exc:  # noqa: BLE001 - 清理失败不能停记录
            self.last_error = 'prune failed: %s' % exc
            return 0
        self.last_pruned = deleted
        return deleted

    # ---------------------------------------------------------------- helpers
    def _serve_flush_request(self):
        """响应“请立刻冲刷缓冲”的请求文件（跨平台，见 :func:`request_flush`）。

        为什么需要它：ProcessManager 关闭时会先发几条停机消息再停服务，Windows
        上 ``terminate()`` 是硬杀（信号处理不跑），缓冲里那几条就永远进不了库。
        请求方建文件、这里冲刷并删文件，等于给记录器一个**跨平台的 flush 命令**。

        Returns:
            int: 本次冲刷写入的条数（没有请求时返回 0）。
        """
        if not take_flush_request(self.db_path):
            return 0
        written = 0
        try:
            written = self.flush()
        except Exception as exc:  # noqa: BLE001 - 冲刷失败不能把服务打挂
            self.last_error = 'flush request failed: %s' % exc
        return written

    def _install_signal_handlers(self):
        """把 SIGTERM/SIGINT 转成 KeyboardInterrupt，好让 :meth:`run` 冲完缓冲再退。

        ProcessManager 停服务发的是 SIGTERM，默认动作是直接杀进程——缓冲里
        还没写库的几条消息就没了。只能在主线程安装，装不上就算了（不影响记录）。
        """
        def _handler(signum, frame):
            raise KeyboardInterrupt('signal %d' % signum)

        for sig_name in ('SIGTERM', 'SIGINT'):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError, RuntimeError):
                pass  # 非主线程/不支持：退化成默认行为

    def _touch_lock(self):
        """刷新自己的记录权心跳文件（写入失败只记录状态，不影响继续收消息与写库）。"""
        if not self.heartbeat:
            return
        if write_heartbeat('logger', self.db_path, name=self.name,
                           extra={'started': self.started_at}):
            self._last_heartbeat = time.time()
        else:
            self.last_error = 'heartbeat write failed'

    def _refresh_policy(self, force=False):
        """从共享策略文件重读“只保存哪些级别”并应用到存储。

        Args:
            force (bool): True 时忽略每次调用的代价，立刻重读一次
                （策略文件很小，正常轮询里也每 :attr:`heartbeat` 秒读一次，
                这样界面改了设置常驻服务会跟着变）。
        """
        self._last_policy_check = time.time()
        try:
            levels = load_level_policy(self.db_path)
        except Exception as exc:  # noqa: BLE001 - 策略读不到就按“全存”处理
            self.last_error = 'policy failed: %s' % exc
            return
        if levels == self._policy:
            return
        self._policy = levels
        try:
            self.store.set_level_filter(levels)
        except Exception as exc:  # noqa: BLE001
            self.last_error = 'policy failed: %s' % exc

    # -------------------------------------------------------------- accessors
    def counters(self):
        """返回 ``(收到条数, 写入条数, 因存储策略跳过条数)``。

        Returns:
            tuple[int, int, int]: 计数三元组。
        """
        skipped = 0
        if self.store is not None and hasattr(self.store, 'skipped_count'):
            skipped = self.store.skipped_count()
        return self.received, self.written, skipped

    def status_line(self):
        """返回一行可显示/可记日志的状态文本。

        Returns:
            str: 形如 ``'received 12 · stored 12 · pid 345'``。
        """
        received, written, skipped = self.counters()
        parts = ['received %d' % received, 'stored %d' % written]
        if skipped:
            parts.append('skipped %d' % skipped)
        if not self.owns_recorder:
            parts.append('waiting for the in-window receiver')
        parts.append('pid %d' % os.getpid())
        if self.last_error:
            parts.append(self.last_error)
        return ' · '.join(parts)


def run_message_logger(name='Servers/Message Logger', **kwargs):
    """以独立进程方式运行消息记录服务（由 ProcessManager 拉起）。

    开库失败（例如另一个进程正持写锁、且重试用尽后仍 ``database is
    locked``）时给出**可读提示**并返回 False，不抛 traceback：用户机器上
    曾因为这里直接抛异常，Message Logger 一启动就整个挂掉。

    Args:
        name (str): 订阅时用的流名（也是心跳文件里记录的显示名）。
        **kwargs: 透传给 :class:`MessageLogger`（batch_size/interval 等）。

    Returns:
        bool: 跑过主循环（正常退出）为 True；开库失败为 False。
    """
    logger = open_logger_with_retry(name, **kwargs)
    if logger is None:
        print('[message logger] 消息库被占用（database is locked）：'
              '请先关掉其它正在记录消息的程序（Stream Monitor 窗口 / 另一个 '
              'Message Logger 进程），再重启本服务。', file=sys.stderr)
        return False
    logger.run()
    return True


def open_logger_with_retry(name='Servers/Message Logger', attempts=None,
                           delay=None, **kwargs):
    """构造 :class:`MessageLogger`，遇“库被锁”按间隔重试几次。

    Args:
        name (str): 订阅时用的流名。
        attempts (int|None): 最多尝试次数；None 用 :data:`OPEN_RETRIES`
            （读模块常量，便于调用方/测试临时调整预算）。
        delay (float|None): 两次尝试之间的等待秒数；None 用
            :data:`OPEN_RETRY_DELAY_S`。
        **kwargs: 透传给 :class:`MessageLogger`。

    Returns:
        MessageLogger|None: 构造成功返回实例；一直锁着返回 None。
    """
    attempts = OPEN_RETRIES if attempts is None else attempts
    delay = OPEN_RETRY_DELAY_S if delay is None else delay
    last = None
    for attempt in range(max(1, attempts)):
        try:
            return MessageLogger(name, **kwargs)
        except sqlite3.OperationalError as exc:  # noqa: BLE001 - 库被锁最常见
            last = exc
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            print('[message logger] 消息库被占用，%.1f s 后重试（%d/%d）：%s'
                  % (delay, attempt + 1, attempts, exc), file=sys.stderr)
            time.sleep(delay)
    print('[message logger] 放弃：%s' % last, file=sys.stderr)
    return None


def main(argv=None):
    """命令行入口：``python quatm/servers/messagelogger.py [流名]``。

    Args:
        argv (list|None): 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        int: 进程退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    name = args[0] if args and not args[0].startswith('-') else 'Servers/Message Logger'
    return 0 if run_message_logger(name) else 1


if __name__ == '__main__':
    sys.exit(main())
