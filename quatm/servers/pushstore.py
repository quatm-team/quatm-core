"""推送网关的持久化 outbox：消息事件 + 每个 sink 的投递状态。

设计要点（对应“实验线程绝不等待网络”的要求）：

* **事件先落库再投递**：``add_event()`` 只做一次 SQLite 写（毫秒级），
  调用方（实验脚本、GUI、其它服务）立刻返回；真正的 HTTP 请求由常驻
  :class:`~quatm.servers.pushgateway.PushGateway` 的投递线程完成。
* **每 sink 独立状态**：同一条事件可能同时推给 Gotify / 及时通知 /
  Mattermost，某个 sink 失败不影响其它 sink 的 ``sent`` 状态。
* **退避重试**：失败写 ``attempts`` 与 ``next_try``（指数退避），到上限
  转 ``failed``（可人工重投），不会无限打日志。
* **限流用 defer 而不是失败**：令牌桶没 token 时只推迟 ``next_try``，
  不计入 ``attempts``，因此不会被误判为“坏 sink”。
* **去重/聚合**：同一 ``dedupe_key`` 在窗口期内重复出现时只累加 ``count``
  （投递时正文补 ``(×N)``），避免消息风暴把手机打爆。

数据库默认位于 ``<configuration>/push/push.db``（WAL，与消息库分开，
因此推送的保留策略不会和消息历史互相影响）。所有写操作遇
``database is locked`` 都会重试——两台进程（GUI 里的“测试推送”和常驻
网关）同时写是正常情况。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from quatm.servers import heartbeat, sqlite_utils

#: 支持的级别（与 :mod:`quatm.servers.messageclient` 的线格式一致，小写）。
LEVELS = ('debug', 'info', 'warning', 'error')

#: 级别排序（用于 ``min_level`` 门槛比较）。
LEVEL_RANK = {'debug': 10, 'info': 20, 'warning': 30, 'error': 40}

#: 投递状态：等待中 / 已认领（租约内投递中） / 已送达 / 超过重试上限 / 主动跳过。
STATUS_PENDING = 'pending'
STATUS_INFLIGHT = 'inflight'
STATUS_SENT = 'sent'
STATUS_FAILED = 'failed'
STATUS_SKIPPED = 'skipped'

#: 认领租约时长 [s]：``claim()`` 把行置为 ``inflight`` 并给出租约，网关崩溃/
#: 被杀后租约过期，行会被下一次 claim/prune 自动放回 ``pending``（不会永久
#: 锁死，也不会被第二个网关重复投递）。要覆盖一次 HTTP 投递的最坏耗时
#: （每个 sink 的 timeout 默认 5 s，最多 8 次重试是跨轮次的，不在单次租约内），
#: 60 s 足够宽裕。
CLAIM_LEASE_S = 60.0
#: ``failed`` 行的保留天数下限：失败记录是人工排查/重投的依据，保留期通常
#: 要比成功投递长一点（默认至少 7 天），但也不能永久留存。
FAILED_RETENTION_MIN_DAYS = 7.0

#: 单次写操作的锁重试预算（与消息库保持同一套思路）。
SQLITE_TIMEOUT_S = 30
WRITE_RETRIES = 4
WRITE_RETRY_BACKOFF_S = 0.2

#: 网关心跳文件名（与 ``push.db`` 同目录），供界面判断“常驻网关是否在跑”。
HEARTBEAT_FILENAME = 'push_gateway.lock'
#: 心跳超过这个秒数没更新就当网关已退出（进程被强杀也能看出来）。
HEARTBEAT_STALE_S = 8.0


def level_rank(level):
    """把级别名转换成可比较的序号（未知级别按 info 处理）。

    Args:
        level (str): 级别名，如 ``'warning'``。

    Returns:
        int: 排序权重；``debug`` 10、``info`` 20、``warning`` 30、``error`` 40。
    """
    return LEVEL_RANK.get(str(level or '').strip().lower(), LEVEL_RANK['info'])


def default_db_path(config_dir):
    """返回某个项目配置目录下的推送库路径（不含目录创建）。

    Args:
        config_dir (str|Path): 项目 ``configuration`` 目录。

    Returns:
        str: ``<config_dir>/push/push.db`` 的绝对路径。
    """
    return os.path.join(str(config_dir), 'push', 'push.db')


class PushStore:
    """推送 outbox 的 SQLite 实现（线程安全，WAL，写操作带锁重试）。

    Args:
        path (str|None): 数据库路径；None 时要求调用方显式给出（见
            :func:`default_db_path`），避免本模块反向依赖 configreader。

    Attributes:
        path (str): 数据库文件绝对路径。
    """

    def __init__(self, path):
        """打开（必要时创建）推送库并建表。

        Args:
            path (str): ``push.db`` 的路径；目录会自动创建。
        """
        self.path = os.path.abspath(str(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=SQLITE_TIMEOUT_S,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # 启动时回收“上一个网关崩溃/被强杀时留下的”过期认领租约：否则这些行
        # 会一直是 inflight，永远不会再被投递（直到下一次 claim 时才顺带回收）。
        self.recover_expired_leases()

    # ------------------------------------------------------------------ 基础设施
    def _run(self, func):
        """在进程内锁里执行一个写操作，遇锁按指数退避重试。

        算法统一在 :func:`quatm.servers.sqlite_utils.run_with_retry`（与消息库
        共用同一份 locked/busy 判定、回滚与退避逻辑）；本方法只固定本模块的
        重试预算（:data:`WRITE_RETRIES` / :data:`WRITE_RETRY_BACKOFF_S`）并
        负责持有进程内锁。

        Args:
            func (callable): 无参可调用对象，内部负责 execute + commit。

        Returns:
            object: ``func()`` 的返回值。

        Raises:
            sqlite3.OperationalError: 重试耗尽后原样抛出最后一个错误。
        """
        with self._lock:
            return sqlite_utils.run_with_retry(
                self._conn, self._lock, func,
                retries=WRITE_RETRIES, backoff=WRITE_RETRY_BACKOFF_S)

    def _init_schema(self):
        """建表、建索引并把日志模式切成 WAL；幂等，重复调用不会清空已有数据。"""

        def _create():
            c = self._conn
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=NORMAL')
            c.execute("""CREATE TABLE IF NOT EXISTS events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            level TEXT NOT NULL,
                            source TEXT NOT NULL,
                            title TEXT NOT NULL,
                            body TEXT NOT NULL,
                            extras TEXT,                            options TEXT,                            dedupe_key TEXT,
                            count INTEGER NOT NULL DEFAULT 1)""")
            c.execute('CREATE INDEX IF NOT EXISTS idx_events_created '
                      'ON events(created_at)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_events_dedupe '
                      'ON events(dedupe_key, created_at)')
            c.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                            event_id INTEGER NOT NULL,
                            sink TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            next_try REAL NOT NULL DEFAULT 0,
                            updated_at REAL NOT NULL DEFAULT 0,
                            lease_until REAL NOT NULL DEFAULT 0,
                            last_error TEXT,
                            PRIMARY KEY (event_id, sink))""")
            c.execute('CREATE INDEX IF NOT EXISTS idx_deliveries_due '
                      'ON deliveries(status, next_try)')
            self._migrate_deliveries(c)
            c.commit()

        self._run(_create)

    @staticmethod
    def _migrate_deliveries(conn):
        """给旧库的 ``deliveries`` 表补上新增列（幂等；旧库没有租约列也能用）。

        老版本的 push.db 没有 ``lease_until``（也就没有 inflight 状态），直接
        按新 schema 写会报 ``no such column``。这里只 ``ALTER TABLE ADD COLUMN``，
        不动已有投递数据。

        Args:
            conn (sqlite3.Connection): 数据库连接。
        """
        existing = {row[1] for row in conn.execute(
            'PRAGMA table_info(deliveries)')}
        for name, declaration in (
                ('lease_until', 'REAL NOT NULL DEFAULT 0'),
                ('updated_at', 'REAL NOT NULL DEFAULT 0'),
                ('last_error', 'TEXT')):
            if name not in existing:
                conn.execute('ALTER TABLE deliveries ADD COLUMN %s %s'
                             % (name, declaration))
                existing.add(name)

    def close(self):
        """关闭数据库连接并释放文件句柄；可重复调用，已在关闭状态时不抛异常。"""
        try:
            with self._lock:
                self._conn.close()
        except Exception:  # noqa: BLE001 - 关闭路径不允许抛异常
            pass

    # ------------------------------------------------------------------ 租约
    def _release_expired_locked(self, now):
        """把租约已过期的 ``inflight`` 行放回 ``pending``（调用方持锁）。

        Args:
            now (float): 参考时间。

        Returns:
            int: 被回收的投递行数。
        """
        cur = self._conn.execute(
            "UPDATE deliveries SET status = ?, lease_until = 0, updated_at = ? "
            "WHERE status = ? AND lease_until <= ?",
            (STATUS_PENDING, float(now), STATUS_INFLIGHT, float(now)))
        return max(0, int(cur.rowcount or 0))

    def recover_expired_leases(self, now=None):
        """回收过期的认领租约（网关崩溃/被强杀后，在途行不能永久锁死）。

        打开推送库（``__init__``）时调用一次；``claim()`` 每次也会先回收一遍，
        因此即使运行中的网关崩溃，下一轮认领也能接着投递。被回收的行会立刻
        变回 ``pending``（``next_try`` 不变，通常已经到期）。

        Args:
            now (float|None): 参考时间；None 用当前时间。

        Returns:
            int: 被回收的投递行数。
        """
        now = time.time() if now is None else float(now)

        def _write():
            recovered = self._release_expired_locked(now)
            self._conn.commit()
            return recovered

        return self._run(_write)

    def release(self, event_id, sink):
        """把一条仍在租约内的 ``inflight`` 投递放回 ``pending``。

        网关正常停止、或投递循环中途退出时调用：不必等租约过期，重启后能
        立刻继续投递。只影响 ``inflight`` 行（已送达/失败的不会被改回）。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
        """
        now = time.time()

        def _write():
            self._conn.execute(
                "UPDATE deliveries SET status = ?, lease_until = 0, updated_at = ? "
                "WHERE event_id = ? AND sink = ? AND status = ?",
                (STATUS_PENDING, now, int(event_id), str(sink),
                 STATUS_INFLIGHT))
            self._conn.commit()

        self._run(_write)

    # ------------------------------------------------------------------ 写入
    def add_event(self, level, source, title, body, extras=None,
                  dedupe_key=None, dedupe_window=0.0, sinks=None,
                  options=None):
        """写入一条推送事件（可选去重聚合），并为给定 sink 建立投递行。

        去重语义：若 ``dedupe_key`` 非空、窗口内已有**同一 key 且尚未全部
        投递完**的事件，则只把那条事件的 ``count`` 加一，不新增事件行。
        已投递完的事件不会被复用——否则“上周的错误”会被今天的重复消息
        复活成一条旧时间的通知。

        Args:
            level (str): 级别（debug/info/warning/error）。
            source (str): 消息来源（一般是发出消息的脚本文件名）。
            title (str): 推送标题。
            body (str): 推送正文。
            extras (dict|None): 附加上下文（写进事件的 JSON 列）。
            dedupe_key (str|None): 去重键（网关用 ``level|source|body``）。
            dedupe_window (float): 去重窗口秒数；``<=0`` 表示不去重。
            sinks (Iterable[str]|None): 目标 sink 名列表。
            options (dict|None): 调用级参数（附件/频道/配置覆盖），投递时原样
                交给 sink；与 ``extras`` 分开，后者会随消息发给手机端。

        Returns:
            tuple[int, int]: ``(事件 id, 该事件的累计条数)``。
        """
        now = time.time()
        stamp = datetime.now().isoformat(sep=' ', timespec='milliseconds')
        extra_json = json.dumps(extras, ensure_ascii=False) if extras else None
        options_json = json.dumps(options, ensure_ascii=False) if options else None
        key = str(dedupe_key or '')

        def _write():
            c = self._conn
            if key and dedupe_window > 0:
                row = c.execute(
                    'SELECT id, count FROM events WHERE dedupe_key = ? '
                    'AND created_at >= ? ORDER BY id DESC LIMIT 1',
                    (key, now - float(dedupe_window))).fetchone()
                if row is not None:
                    # pending 与 inflight 都算“还没投递完”：认领后的行仍在
                    # 投递中，窗口内重复消息应该继续聚合到它上面。
                    pending = c.execute(
                        'SELECT COUNT(*) FROM deliveries WHERE event_id = ? '
                        'AND status IN (?, ?)',
                        (row['id'], STATUS_PENDING,
                         STATUS_INFLIGHT)).fetchone()[0]
                    if pending:
                        count = int(row['count']) + 1
                        c.execute('UPDATE events SET count = ?, ts = ? '
                                  'WHERE id = ?', (count, stamp, row['id']))
                        c.commit()
                        return int(row['id']), count
            cur = c.execute(
                'INSERT INTO events (ts, created_at, level, source, title, '
                'body, extras, options, dedupe_key, count) '
                'VALUES (?,?,?,?,?,?,?,?,?,1)',
                (stamp, now, str(level), str(source), str(title or ''),
                 str(body or ''), extra_json, options_json, key))
            event_id = int(cur.lastrowid)
            self._enqueue_locked(event_id, sinks)
            c.commit()
            return event_id, 1

        return self._run(_write)

    def _enqueue_locked(self, event_id, sinks):
        """为一条事件插入投递行（调用方已持有事务/锁）。

        Args:
            event_id (int): 事件 id。
            sinks (Iterable[str]|None): sink 名列表，重复项会被忽略。
        """
        now = time.time()
        for name in dict.fromkeys(sinks or ()):
            if not name:
                continue
            self._conn.execute(
                'INSERT OR IGNORE INTO deliveries '
                '(event_id, sink, status, attempts, next_try, updated_at) '
                'VALUES (?, ?, ?, 0, 0, ?)',
                (int(event_id), str(name), STATUS_PENDING, now))

    def enqueue(self, event_id, sinks):
        """给已存在的事件补建投递行（例如后来新增了一个 sink）。

        Args:
            event_id (int): 事件 id。
            sinks (Iterable[str]): sink 名列表。
        """

        def _write():
            self._enqueue_locked(event_id, sinks)
            self._conn.commit()

        self._run(_write)

    # ------------------------------------------------------------------ 投递
    def claim(self, limit=20, now=None, lease_seconds=None):
        """认领到期的待投递行（“认领 + 租约”），供投递线程处理。

        两步走（不依赖 ``RETURNING``，兼容旧 sqlite）：

        1. 先 ``UPDATE`` 把候选行置为 ``inflight`` 并写 ``lease_until``；
        2. 再按**本次真正认领到的 (event_id, sink)** 重新 ``SELECT`` 完整行。

        这样双网关/双进程并发认领同一批行时，只有真正 UPDATE 成功（rowcount=1）
        的那一方会拿到行；另一方拿不到，不会重复投递。网关崩溃时行停在
        ``inflight``，租约过期后由下一次 :meth:`recover_expired_leases`/
        :meth:`claim` 自动放回 ``pending``，不会永久锁死。

        Args:
            limit (int): 最多认领多少行。
            now (float|None): 参考时间；None 用当前时间。
            lease_seconds (float|None): 租约时长；None 用 :data:`CLAIM_LEASE_S`。

        Returns:
            list[dict]: 每项含事件字段与 ``event_id`` / ``sink`` / ``attempts``。
        """
        now = time.time() if now is None else float(now)
        lease = CLAIM_LEASE_S if lease_seconds is None \
            else max(1.0, float(lease_seconds))
        limit = max(1, int(limit))
        lease_until = now + lease

        def _claim():
            c = self._conn
            # 先把过期租约回收（本进程/别的网关崩溃留下的 inflight 行）
            self._release_expired_locked(now)
            candidates = c.execute(
                """SELECT event_id, sink FROM deliveries
                   WHERE status = ? AND next_try <= ?
                   ORDER BY next_try, event_id LIMIT ?""",
                (STATUS_PENDING, now, limit)).fetchall()
            claimed = []
            for row in candidates:
                event_id = int(row['event_id'])
                sink = str(row['sink'])
                cur = c.execute(
                    "UPDATE deliveries SET status = ?, lease_until = ?, "
                    "updated_at = ? WHERE event_id = ? AND sink = ? "
                    "AND status = ?",
                    (STATUS_INFLIGHT, lease_until, now, event_id, sink,
                     STATUS_PENDING))
                if cur.rowcount:
                    claimed.append((event_id, sink))
            c.commit()
            if not claimed:
                return []
            # 只 SELECT 本次真正认领到的行（用主键集合，不依赖 RETURNING）
            where = ' OR '.join('(d.event_id = ? AND d.sink = ?)'
                                for _ in claimed)
            params = [value for pair in claimed for value in pair]
            rows = c.execute(
                """SELECT d.event_id, d.sink, d.attempts, e.ts, e.level,
                          e.source, e.title, e.body, e.extras, e.options, e.count
                   FROM deliveries d JOIN events e ON e.id = d.event_id
                   WHERE %s""" % where, params).fetchall()
            order = {pair: index for index, pair in enumerate(claimed)}
            rows = sorted(rows, key=lambda r: order.get(
                (int(r['event_id']), str(r['sink'])), len(claimed)))
            return rows

        rows = self._run(_claim)
        result = []
        for row in rows:
            item = dict(row)
            for key in ('extras', 'options'):
                try:
                    item[key] = json.loads(item[key]) if item[key] else None
                except (TypeError, ValueError):
                    item[key] = None
            result.append(item)
        return result

    def mark_sent(self, event_id, sink):
        """标记一条投递已成功送达。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
        """
        self._update_delivery(event_id, sink, STATUS_SENT, sent=True)

    def mark_failed(self, event_id, sink, error, max_attempts=8):
        """记录一次投递失败并安排退避重试（超过上限转 ``failed``）。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
            error (str): 失败原因（写进 ``last_error``，界面上能看到）。
            max_attempts (int): 最多尝试次数；达到后不再重试。

        Returns:
            bool: 仍会重试为 True，已放弃为 False。
        """
        now = time.time()

        def _write():
            row = self._conn.execute(
                'SELECT attempts FROM deliveries WHERE event_id = ? AND sink = ?',
                (int(event_id), str(sink))).fetchone()
            attempts = int(row['attempts']) + 1 if row else 1
            stopped = attempts >= max(1, int(max_attempts))
            if stopped:
                self._conn.execute(
                    'UPDATE deliveries SET status = ?, attempts = ?, '
                    'updated_at = ?, lease_until = 0, last_error = ? '
                    'WHERE event_id = ? AND sink = ?',
                    (STATUS_FAILED, attempts, now, str(error)[:500],
                     int(event_id), str(sink)))
            else:
                delay = min(300.0, 2.0 ** attempts)
                self._conn.execute(
                    'UPDATE deliveries SET status = ?, attempts = ?, '
                    'next_try = ?, updated_at = ?, lease_until = 0, '
                    'last_error = ? WHERE event_id = ? AND sink = ?',
                    (STATUS_PENDING, attempts, now + delay, now,
                     str(error)[:500], int(event_id), str(sink)))
            self._conn.commit()
            return not stopped

        return self._run(_write)

    def mark_skipped(self, event_id, sink, reason=''):
        """标记一条投递被主动跳过（sink 被删/停用等，不算失败）。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
            reason (str): 跳过原因。
        """
        self._update_delivery(event_id, sink, STATUS_SKIPPED, error=reason)

    def defer(self, event_id, sink, delay):
        """把一条待投递行推迟 ``delay`` 秒（限流用，不计入尝试次数）。

        认领后的行是 ``inflight``，推迟时必须同时放回 ``pending`` 并清掉租约，
        否则限流会把行留在 inflight 直到租约过期。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
            delay (float): 推迟秒数。
        """
        now = time.time()

        def _write():
            self._conn.execute(
                "UPDATE deliveries SET status = ?, lease_until = 0, "
                "next_try = ?, updated_at = ? "
                "WHERE event_id = ? AND sink = ? AND status IN (?, ?)",
                (STATUS_PENDING, now + max(0.0, float(delay)), now,
                 int(event_id), str(sink), STATUS_PENDING, STATUS_INFLIGHT))
            self._conn.commit()

        self._run(_write)

    def _update_delivery(self, event_id, sink, status, sent=False, error=None):
        """更新单条投递行的状态（内部辅助，统一走锁重试）。

        Args:
            event_id (int): 事件 id。
            sink (str): sink 名。
            status (str): 新状态。
            sent (bool): 是否为“成功送达”（顺带清空 last_error）。
            error (str|None): 失败/跳过原因。
        """
        now = time.time()

        def _write():
            if sent:
                self._conn.execute(
                    'UPDATE deliveries SET status = ?, updated_at = ?, '
                    'lease_until = 0, last_error = NULL '
                    'WHERE event_id = ? AND sink = ?',
                    (status, now, int(event_id), str(sink)))
            else:
                self._conn.execute(
                    'UPDATE deliveries SET status = ?, updated_at = ?, '
                    'lease_until = 0, last_error = ? '
                    'WHERE event_id = ? AND sink = ?',
                    (status, now, str(error or '')[:500],
                     int(event_id), str(sink)))
            self._conn.commit()

        self._run(_write)

    # ------------------------------------------------------------------ 查询
    def stats(self):
        """汇总 outbox 状态（供 GUI/Web 显示“待发/失败”数量）。

        Returns:
            dict: 键为 ``pending/inflight/sent/failed/skipped/events``。
        """
        with self._lock:
            counts = {row['status']: int(row['n']) for row in
                      self._conn.execute('SELECT status, COUNT(*) AS n '
                                         'FROM deliveries GROUP BY status')}
            events = self._conn.execute(
                'SELECT COUNT(*) FROM events').fetchone()[0]
        return {'pending': counts.get(STATUS_PENDING, 0),
                'inflight': counts.get(STATUS_INFLIGHT, 0),
                'sent': counts.get(STATUS_SENT, 0),
                'failed': counts.get(STATUS_FAILED, 0),
                'skipped': counts.get(STATUS_SKIPPED, 0),
                'events': int(events)}

    def recent(self, limit=50):
        """返回最近的事件及每个 sink 的投递状态（界面“推送历史”用）。

        Args:
            limit (int): 最多返回多少条事件。

        Returns:
            list[dict]: 每项含 ``id/ts/level/source/title/body/count/deliveries``。
        """
        with self._lock:
            events = self._conn.execute(
                'SELECT id, ts, level, source, title, body, count FROM events '
                'ORDER BY id DESC LIMIT ?', (max(1, int(limit)),)).fetchall()
            result = []
            for event in events:
                deliveries = self._conn.execute(
                    'SELECT sink, status, attempts, last_error FROM deliveries '
                    'WHERE event_id = ? ORDER BY sink', (event['id'],)).fetchall()
                item = dict(event)
                item['deliveries'] = [dict(d) for d in deliveries]
                result.append(item)
        return result

    def pending_counts(self):
        """按 sink 统计待投递条数（网关据此丢弃积压过多的新消息）。

        Returns:
            dict[str, int]: ``{sink: 待投递数}``；没有积压时为空字典。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sink, COUNT(*) AS n FROM deliveries "
                "WHERE status = 'pending' GROUP BY sink").fetchall()
        return {row['sink']: int(row['n']) for row in rows}

    def retry_failed(self):
        """把所有 ``failed`` 投递重新排队（人工“重投”按钮）。

        Returns:
            int: 被重置的投递行数。
        """
        now = time.time()

        def _write():
            cur = self._conn.execute(
                "UPDATE deliveries SET status = 'pending', attempts = 0, "
                'next_try = 0, lease_until = 0, updated_at = ? WHERE status = ?',
                (now, STATUS_FAILED))
            self._conn.commit()
            return int(cur.rowcount or 0)

        return self._run(_write)

    def prune(self, max_events=20000, retention_days=30,
              failed_retention_days=None, now=None):
        """按保留期与容量上限清理旧数据（定期调用，避免库无限增长）。

        清理规则：

        * ``pending`` / ``inflight`` 行**永不清理**（还没投递完，清掉就是丢消息）；
        * ``sent`` / ``skipped`` 行及其事件按 ``retention_days`` 清理；
        * ``failed`` 行单独按“至少 :data:`FAILED_RETENTION_MIN_DAYS` 天”的保留期
          清理（人工排查/重投的依据比成功投递多留几天，但不会永久留存）；
        * 事件数超过 ``max_events`` 时，从最旧的“没有待投递/在途行”的事件删起。

        Args:
            max_events (int): 事件总数上限。
            retention_days (float): 已终结（sent/skipped）数据的保留天数。
            failed_retention_days (float|None): failed 行的保留天数；None 表示
                取 ``max(retention_days, FAILED_RETENTION_MIN_DAYS)``。
            now (float|None): 参考时间（测试用）；None 用当前时间。
        """
        now = time.time() if now is None else float(now)
        retention = max(1.0, float(retention_days))
        cutoff = now - retention * 86400.0
        if failed_retention_days is None:
            failed_days = max(retention, FAILED_RETENTION_MIN_DAYS)
        else:
            failed_days = max(1.0, float(failed_retention_days))
        failed_cutoff = now - failed_days * 86400.0

        def _write():
            c = self._conn
            c.execute(
                "DELETE FROM deliveries WHERE status IN (?, ?) "
                'AND updated_at < ?',
                (STATUS_SENT, STATUS_SKIPPED, cutoff))
            c.execute(
                'DELETE FROM deliveries WHERE status = ? AND updated_at < ?',
                (STATUS_FAILED, failed_cutoff))
            c.execute(
                'DELETE FROM events WHERE created_at < ? AND id NOT IN '
                '(SELECT DISTINCT event_id FROM deliveries)', (cutoff,))
            extra = c.execute('SELECT COUNT(*) FROM events').fetchone()[0] - \
                max(1, int(max_events))
            if extra > 0:
                # 保护 pending 与 inflight：它们还等着投递，删了就是丢消息。
                doomed = c.execute(
                    'SELECT id FROM events WHERE id NOT IN (SELECT DISTINCT '
                    'event_id FROM deliveries WHERE status IN (?, ?)) '
                    'ORDER BY id LIMIT ?',
                    (STATUS_PENDING, STATUS_INFLIGHT, int(extra))).fetchall()
                for row in doomed:
                    c.execute('DELETE FROM deliveries WHERE event_id = ?',
                              (row['id'],))
                    c.execute('DELETE FROM events WHERE id = ?', (row['id'],))
            c.commit()

        self._run(_write)


# ---------------------------------------------------------------------------
# 心跳（常驻网关是否在跑）
# ---------------------------------------------------------------------------

def _heartbeat_path(directory):
    """返回网关心跳文件路径（``<directory>/push_gateway.lock``）。

    Args:
        directory (str): 心跳文件所在目录（一般是 ``push.db`` 的目录）。

    Returns:
        str: 心跳文件路径。
    """
    return os.path.join(str(directory), HEARTBEAT_FILENAME)


def write_heartbeat(directory, name='pushgateway', extra=None):
    """写/刷新网关心跳文件（pid + 主机 + 时间戳），供界面判断运行状态。

    文件格式/位置与旧实现完全一致（``push_gateway.lock`` 的 JSON），因此新版
    网关与旧版读取方互相兼容；原子写/pid 处理统一在
    :mod:`quatm.servers.heartbeat`。

    Args:
        directory (str): 心跳文件所在目录（一般是 ``push.db`` 的目录）。
        name (str): 写入者名字（诊断用）。
        extra (dict|None): 附加字段（例如已投递条数）。
    """
    heartbeat.write_heartbeat(_heartbeat_path(directory), name=name,
                              extra=extra)


def read_heartbeat(directory):
    """读取网关心跳；文件不存在、过期或 pid 已消失时返回 None。

    Args:
        directory (str): 心跳文件所在目录。

    Returns:
        dict|None: ``{'pid','host','name','ts','age'}``，网关不在时为 None。
    """
    return heartbeat.read_heartbeat(_heartbeat_path(directory),
                                    max_age=HEARTBEAT_STALE_S)


def clear_heartbeat(directory):
    """删除网关心跳文件（正常退出时调用；重复调用安全）。

    Args:
        directory (str): 心跳文件所在目录。
    """
    heartbeat.clear_heartbeat(_heartbeat_path(directory))
