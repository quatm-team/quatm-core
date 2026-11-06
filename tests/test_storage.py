"""存储/配置层的回归测试（P1-2b）：消息库淘汰、配置原子写、推送认领租约。

覆盖的验收点：

* :meth:`quatm.servers.messagestore.MessageStore.prune` 按 ``max_rows`` /
  ``retention_days`` 淘汰旧消息；阈值可来自 ``Properties('MessageStore')``
  并被环境变量覆盖；旧 schema 的库打开时能自动补列、不炸。
* :func:`quatm.servers.notifications.save_config` 原子写：失败时旧文件不被
  破坏、不留半截 JSON/临时文件、日志里不出现 token。
* :meth:`quatm.servers.pushstore.PushStore.claim` 认领 + 租约：两次认领不会
  返回同一行、过期租约能被回收、网关停止时在途行放回 ``pending``。
* :meth:`quatm.servers.pushstore.PushStore.prune` 会清理 ``failed`` 行，但
  永不清理 ``pending``/``inflight`` 行。

测试约定见 ``tests/__init__.py``：只用 stdlib ``unittest`` + ``tempfile``，
不连硬件、不连 ZMQ 网络（推送投递函数被 mock）。
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from quatm.servers import messagestore, messagelogger, notifications, pushstore
from quatm.servers.messagestore import MessageRow, MessageStore
from quatm.servers.pushstore import PushStore

_ENV_KEYS = (messagestore.RETENTION_DAYS_ENV, messagestore.MAX_ROWS_ENV)


class _TempDirMixin:
    """给每个测试一个临时目录，结束时自动清理。"""

    def make_tempdir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


def _row(index, days_ago=0):
    """造一条带可识别正文的 :class:`MessageRow`。

    Args:
        index (int): 正文编号（``message <index>``）。
        days_ago (float): 时间戳往前推多少天。

    Returns:
        MessageRow: 测试记录。
    """
    stamp = datetime.now() - timedelta(days=days_ago)
    ts = stamp.isoformat(sep=' ', timespec='milliseconds')
    return MessageRow(ts, 'test.py', 'info', 'message %d' % index, None)


class TestMessageStorePrune(_TempDirMixin, unittest.TestCase):
    """MessageStore.prune：按容量/保留期淘汰，旧库 schema 可迁移。"""

    def setUp(self):
        self.tmp = self.make_tempdir()
        # 不连真实属性系统（否则没起 Property Logger 时要等握手超时），
        # 默认返回空配置 → 用模块默认值；需要测试配置优先级的用例自己 patch。
        patcher = mock.patch.object(messagestore, '_property_prune_settings',
                                    return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 环境变量也要清理，避免外部残留影响默认值断言
        saved = {key: os.environ.pop(key, None) for key in _ENV_KEYS}

        def _restore_env():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(_restore_env)

    def _store(self, name='messages.db'):
        store = MessageStore(path=os.path.join(self.tmp, name),
                             jsonl_mirror=False, migrate_jsonl=False)
        self.addCleanup(store.close)
        return store

    def test_prune_by_max_rows_deletes_oldest(self):
        store = self._store()
        store.append([_row(i) for i in range(10)])

        deleted = store.prune(retention_days=3650, max_rows=4, vacuum=False)

        self.assertEqual(deleted, 6)
        rows = store.query(limit=100, newest_first=False)
        self.assertEqual([r.content for r in rows],
                         ['message %d' % i for i in range(6, 10)])
        self.assertEqual(store.count(), 4)
        # FTS 镜像行也要跟着删掉，否则“幽灵行”会让全文检索命中已删除的消息
        self.assertEqual(store.query(text='message 0', limit=10), [])
        self.assertEqual(len(store.query(text='message 9', limit=10)), 1)

    def test_prune_by_retention_deletes_old_rows(self):
        store = self._store()
        store.append([_row(i, days_ago=60) for i in range(3)]
                     + [_row(100 + i) for i in range(2)])

        deleted = store.prune(retention_days=30, max_rows=0, vacuum=False)

        self.assertEqual(deleted, 3)
        rows = store.query(limit=100, newest_first=False)
        self.assertEqual([r.content for r in rows],
                         ['message 100', 'message 101'])

    def test_prune_uses_property_settings_then_env_override(self):
        store = self._store()
        store.append([_row(i) for i in range(10)])

        with mock.patch.object(messagestore, '_property_prune_settings',
                               return_value={'retention_days': 3650,
                                             'max_rows': 8}):
            store.prune(vacuum=False)
        self.assertEqual(store.count(), 8)

        # 环境变量优先于 Properties
        with mock.patch.object(messagestore, '_property_prune_settings',
                               return_value={'retention_days': 3650,
                                             'max_rows': 8}), \
                mock.patch.dict(os.environ,
                                {messagestore.MAX_ROWS_ENV: '6'}):
            store.prune(vacuum=False)
        self.assertEqual(store.count(), 6)

    def test_vacuum_is_thresholded(self):
        store = self._store()
        now = time.time()

        # 删除量太小：不做 VACUUM（GB 级库上 VACUUM 太贵，不能每次 prune 都做）
        self.assertFalse(store._maybe_vacuum(
            messagestore.VACUUM_MIN_DELETED_ROWS - 1, now))
        # 刚 VACUUM 过：即使删除量够大也要等间隔
        store._last_vacuum = now
        self.assertFalse(store._maybe_vacuum(
            messagestore.VACUUM_MIN_DELETED_ROWS, now))
        # 删除量够大且间隔已过：执行一次（小临时库上很快）
        store._last_vacuum = 0.0
        self.assertTrue(store._maybe_vacuum(
            messagestore.VACUUM_MIN_DELETED_ROWS, now))
        self.assertEqual(store._last_vacuum, now)

    def test_old_database_without_extra_column_migrates(self):
        path = os.path.join(self.tmp, 'old.db')
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts TEXT NOT NULL,
                            date TEXT NOT NULL,
                            level TEXT NOT NULL,
                            source TEXT NOT NULL,
                            content TEXT NOT NULL)""")
        conn.execute("INSERT INTO messages (ts, date, level, source, content) "
                     "VALUES ('2020-01-01 00:00:00.000', '2020-01-01', "
                     "'info', 'old.py', 'legacy')")
        conn.commit()
        conn.close()

        store = MessageStore(path=path, jsonl_mirror=False, migrate_jsonl=False)
        self.addCleanup(store.close)
        # 补充的新列能写；旧数据仍在
        store.append([MessageRow('2026-01-01 00:00:00.000', 'new.py', 'info',
                                 'fresh', {'device': 'd1'})])
        rows = store.query(limit=10, newest_first=False)
        self.assertEqual([r.content for r in rows], ['legacy', 'fresh'])
        self.assertEqual(rows[-1].extra, {'device': 'd1'})
        columns = MessageStore._table_columns(store._conn, 'messages')
        self.assertIn('extra', columns)
        self.assertIn('date', columns)
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertEqual(int(version), MessageStore.SCHEMA_VERSION)


class TestNotificationsAtomicWrite(_TempDirMixin, unittest.TestCase):
    """notifications.save_config：原子写、失败保留旧文件、不泄漏 token。"""

    def setUp(self):
        self.tmp = self.make_tempdir()
        self.path = os.path.join(self.tmp, 'notifications.json')
        self._saved = (notifications._CONFIG_FILE,
                       notifications._mirror_to_properties,
                       notifications._cache,
                       notifications._props_mirror)
        notifications._CONFIG_FILE = self.path
        # 测试里不碰属性系统（没有 Property Logger）
        notifications._mirror_to_properties = False
        notifications._cache = None
        notifications._props_mirror = None

        def _restore():
            (notifications._CONFIG_FILE, notifications._mirror_to_properties,
             notifications._cache,
             notifications._props_mirror) = self._saved

        self.addCleanup(_restore)

    def _read(self):
        with open(self.path, 'r', encoding='utf-8') as handle:
            return json.load(handle)

    def test_save_writes_complete_json_and_leaves_no_temp_file(self):
        cfg = {'push_enabled': True,
               'default_service': 'Gotify',
               'services': [{'name': 'Gotify', 'type': 'gotify',
                             'config': {'token': 'SECRET-TOKEN'}}]}
        notifications.save_config(cfg)

        self.assertEqual(self._read(), cfg)
        self.assertEqual(os.listdir(self.tmp), ['notifications.json'])

    def test_failed_replace_keeps_old_file_and_no_partial_json(self):
        old = {'push_enabled': False, 'services': []}
        new = {'push_enabled': True,
               'services': [{'name': 'X', 'config': {'token': 'SECRET-TOKEN'}}]}
        notifications.save_config(old)

        with mock.patch.object(notifications.os, 'replace',
                               side_effect=OSError('disk full')):
            with self.assertLogs('quatm.notifications', level='WARNING') as logs:
                notifications.save_config(new)

        # 旧文件原样保留（不是半截 JSON），临时文件已清理
        self.assertEqual(self._read(), old)
        self.assertEqual(os.listdir(self.tmp), ['notifications.json'])
        # 失败信息里不能出现 token
        self.assertNotIn('SECRET-TOKEN', '\n'.join(logs.output))

    def test_concurrent_saves_never_expose_partial_json(self):
        notifications.save_config({'push_enabled': False, 'services': [],
                                   'index': -1})
        errors = []
        stop = threading.Event()

        def writer(index):
            try:
                for _ in range(20):
                    notifications.save_config({'push_enabled': bool(index % 2),
                                               'services': [],
                                               'index': index})
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append('writer %d: %r' % (index, exc))

        def reader():
            reads = 0
            while not stop.is_set() and reads < 300:
                try:
                    # 用生产读取助手（Windows 上 os.replace 期间 open 可能
                    # 短暂 PermissionError，read_json 会退避重试；损坏 JSON
                    # 仍会抛，测试依旧能抓到"半截文件"）。
                    from quatm.servers import jsonutil
                    data = jsonutil.read_json(self.path)
                    if data.get('index') not in (-1, 0, 1, 2, 3):
                        errors.append('unexpected payload: %r' % (data,))
                        return
                except FileNotFoundError:
                    errors.append('config file disappeared')
                    return
                except Exception as exc:  # noqa: BLE001 - 半截 JSON 会在这里炸
                    errors.append('partial read: %r' % (exc,))
                    return
                reads += 1

        writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        watcher = threading.Thread(target=reader)
        watcher.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(timeout=30)
        stop.set()
        watcher.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertIn(self._read().get('index'), (0, 1, 2, 3))

    def test_properties_client_is_reused_not_leaked(self):
        created = []

        class _FakeProps:
            def __init__(self, name, enable_monitors=True):
                created.append(name)

        with mock.patch('quatm.servers.properties.Properties', _FakeProps):
            first = notifications._properties_client()
            second = notifications._properties_client()

        self.assertIs(first, second)
        self.assertEqual(created, ['Notifications'])


class TestPushStoreClaimLease(_TempDirMixin, unittest.TestCase):
    """PushStore.claim：认领 + 租约（不重复投递、过期可回收、停止可释放）。"""

    def setUp(self):
        self.tmp = self.make_tempdir()

    def _store(self, name='push.db'):
        store = PushStore(os.path.join(self.tmp, name))
        self.addCleanup(store.close)
        return store

    @staticmethod
    def _keys(rows):
        return {(int(row['event_id']), str(row['sink'])) for row in rows}

    def _add_event(self, store, sinks):
        store.add_event(level='warning', source='test.py', title='t',
                        body='body', sinks=list(sinks))

    def test_second_claim_does_not_return_same_row(self):
        store = self._store()
        self._add_event(store, ['a', 'b'])

        first = store.claim(limit=10, now=1000.0, lease_seconds=30)
        second = store.claim(limit=10, now=1000.0, lease_seconds=30)

        self.assertEqual(self._keys(first), {(1, 'a'), (1, 'b')})
        self.assertEqual(second, [])
        self.assertEqual(store.stats()['inflight'], 2)
        self.assertEqual(store.stats()['pending'], 0)

    def test_disjoint_claims_for_multiple_events(self):
        store = self._store()
        for sink in ('a', 'b', 'c'):
            self._add_event(store, [sink])

        first = store.claim(limit=2, now=1000.0)
        second = store.claim(limit=2, now=1000.0)

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertTrue(self._keys(first).isdisjoint(self._keys(second)))

    def test_expired_lease_is_reclaimed(self):
        store = self._store()
        self._add_event(store, ['a', 'b'])

        first = store.claim(limit=10, now=1000.0, lease_seconds=30)
        self.assertEqual(len(first), 2)
        # 租约还没过期：第二个网关拿不到
        self.assertEqual(store.claim(limit=10, now=1020.0, lease_seconds=30), [])
        # 租约过期：行被回收，可以重新认领
        reclaimed = store.claim(limit=10, now=1031.0, lease_seconds=30)
        self.assertEqual(self._keys(reclaimed), self._keys(first))

    def test_reopen_recovers_expired_inflight(self):
        path = os.path.join(self.tmp, 'push.db')
        store = PushStore(path)
        self._add_event(store, ['a'])
        self.assertEqual(len(store.claim(limit=10, now=1000.0,
                                         lease_seconds=30)), 1)
        store.close()

        reopened = PushStore(path)
        self.addCleanup(reopened.close)
        rows = reopened.claim(limit=10)   # 真实时间早已超过租约
        self.assertEqual(self._keys(rows), {(1, 'a')})

    def test_release_puts_inflight_back_to_pending(self):
        store = self._store()
        self._add_event(store, ['a'])
        claimed = store.claim(limit=10, now=1000.0, lease_seconds=300)
        self.assertEqual(len(claimed), 1)

        store.release(claimed[0]['event_id'], claimed[0]['sink'])

        self.assertEqual(store.stats()['inflight'], 0)
        self.assertEqual(store.stats()['pending'], 1)
        self.assertEqual(self._keys(store.claim(limit=10, now=1000.0)),
                         self._keys(claimed))

    def test_failed_retry_is_pending_not_stuck_inflight(self):
        store = self._store()
        self._add_event(store, ['a'])
        claimed = store.claim(limit=10, now=1000.0, lease_seconds=300)
        event_id, sink = claimed[0]['event_id'], claimed[0]['sink']

        retrying = store.mark_failed(event_id, sink, 'boom', max_attempts=3)
        self.assertTrue(retrying)
        stats = store.stats()
        self.assertEqual(stats['pending'], 1)
        self.assertEqual(stats['inflight'], 0)
        # next_try 在未来：立刻认领拿不到，退避结束后可以
        self.assertEqual(store.claim(limit=10, now=1000.0), [])
        retried = store.claim(limit=10, now=time.time() + 10.0)
        self.assertEqual(len(retried), 1)
        self.assertEqual(int(retried[0]['attempts']), 1)

    def test_old_push_db_without_lease_column_migrates(self):
        path = os.path.join(self.tmp, 'old_push.db')
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE deliveries (
                            event_id INTEGER NOT NULL,
                            sink TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            next_try REAL NOT NULL DEFAULT 0,
                            PRIMARY KEY (event_id, sink))""")
        conn.commit()
        conn.close()

        store = PushStore(path)
        self.addCleanup(store.close)
        self._add_event(store, ['a'])
        columns = {row[1] for row in
                   store._conn.execute('PRAGMA table_info(deliveries)')}
        self.assertIn('lease_until', columns)
        rows = store.claim(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sink'], 'a')


class TestPushStorePrune(_TempDirMixin, unittest.TestCase):
    """PushStore.prune：failed 行按保留期清理，pending/inflight 永不清理。"""

    def setUp(self):
        self.tmp = self.make_tempdir()

    def _store(self):
        store = PushStore(os.path.join(self.tmp, 'push.db'))
        self.addCleanup(store.close)
        return store

    @staticmethod
    def _make_failed(store, sink='a'):
        event_id, _count = store.add_event(
            level='error', source='test.py', title='t', body='body',
            sinks=[sink])
        claimed = store.claim(limit=10)
        for item in claimed:
            store.mark_failed(item['event_id'], item['sink'], 'boom',
                              max_attempts=1)
        return event_id

    def test_failed_rows_are_pruned(self):
        store = self._store()
        self._make_failed(store)
        self.assertEqual(store.stats()['failed'], 1)

        now = time.time() + 10 * 86400.0
        store.prune(max_events=20000, retention_days=1, now=now)

        stats = store.stats()
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(stats['events'], 0)

    def test_pending_rows_are_never_pruned(self):
        store = self._store()
        store.add_event(level='warning', source='test.py', title='t',
                        body='body', sinks=['a'])
        store.add_event(level='warning', source='test.py', title='t',
                        body='body', sinks=['b'])

        now = time.time() + 100 * 86400.0
        store.prune(max_events=1, retention_days=1, now=now)

        stats = store.stats()
        self.assertEqual(stats['pending'], 2)
        self.assertEqual(stats['events'], 2)


class TestPushGatewayReleasesOnStop(_TempDirMixin, unittest.TestCase):
    """网关停止/异常时不得把在途行永久锁死（放回 pending，别等租约）。"""

    def setUp(self):
        self.tmp = self.make_tempdir()
        self.store = PushStore(os.path.join(self.tmp, 'push.db'))
        self.addCleanup(self.store.close)
        self.services = [{'name': 'sink', 'type': 'webhook', 'enabled': True,
                          'min_level': 'debug', 'config': {'url': 'nowhere'}}]

    def _gateway(self):
        from quatm.servers.pushgateway import PushGateway
        return PushGateway(gateway={'max_attempts': 8}, store=self.store,
                           source=[], service_getter=lambda: self.services)

    def test_stop_releases_claimed_rows(self):
        from quatm.servers.pushgateway import PushGateway
        self.store.add_event(level='warning', source='test.py', title='t',
                             body='body', sinks=['sink'])
        gateway = self._gateway()
        PushGateway.stop(gateway)       # 先置停止标志，再跑一轮

        with mock.patch.object(notifications, '_dispatch',
                               side_effect=AssertionError('must not dispatch')):
            delivered = gateway.deliver_once()

        self.assertEqual(delivered, 0)
        stats = self.store.stats()
        self.assertEqual(stats['pending'], 1)
        self.assertEqual(stats['inflight'], 0)
        # 重启后能立刻投递
        self.assertEqual(len(self.store.claim(limit=10)), 1)

    def test_successful_delivery_marks_sent(self):
        self.store.add_event(level='warning', source='test.py', title='t',
                             body='body', sinks=['sink'])
        gateway = self._gateway()

        with mock.patch.object(notifications, '_dispatch') as dispatch:
            delivered = gateway.deliver_once()

        self.assertEqual(delivered, 1)
        dispatch.assert_called_once()
        stats = self.store.stats()
        self.assertEqual(stats['sent'], 1)
        self.assertEqual(stats['inflight'], 0)


class _FakeLoggerStore:
    """MessageLogger 的假存储：只记录 append/prune 调用（不碰数据库）。"""

    path = ':memory:'

    def __init__(self):
        self.appended = 0
        self.prune_calls = 0

    def append(self, rows):
        self.appended += len(rows)
        return len(rows)

    def prune(self):
        self.prune_calls += 1
        return 7


class TestMessageLoggerPruneTrigger(unittest.TestCase):
    """messagelogger 周期性触发淘汰（按条数 / 按时间），且不阻塞写入。"""

    def _logger(self, **kwargs):
        store = _FakeLoggerStore()
        kwargs.setdefault('prune_every', 0)
        kwargs.setdefault('prune_interval', 0)
        logger = messagelogger.MessageLogger(store=store, heartbeat=0, **kwargs)
        return logger, store

    def test_triggers_after_n_messages(self):
        logger, store = self._logger(prune_every=3, prune_interval=0)

        self.assertEqual(logger._maybe_prune(), 0)   # 还没到阈值
        logger._since_prune = 3
        self.assertEqual(logger._maybe_prune(), 7)
        self.assertEqual(store.prune_calls, 1)
        self.assertEqual(logger.last_pruned, 7)
        self.assertEqual(logger._since_prune, 0)
        # 清理后计数归零，不重复触发
        self.assertEqual(logger._maybe_prune(), 0)
        self.assertEqual(store.prune_calls, 1)

    def test_triggers_after_interval(self):
        logger, store = self._logger(prune_every=0, prune_interval=60.0)
        logger._last_prune = time.time() - 61.0

        self.assertEqual(logger._maybe_prune(), 7)
        self.assertEqual(store.prune_calls, 1)

    def test_flush_counts_written_rows_for_prune(self):
        logger, store = self._logger(prune_every=3)
        logger._buffer = [_row(i) for i in range(2)]

        self.assertEqual(logger.flush(), 2)
        self.assertEqual(logger._since_prune, 2)
        self.assertEqual(store.appended, 2)

    def test_prune_failure_is_recorded_not_raised(self):
        logger, store = self._logger(prune_every=1)
        store.prune = mock.Mock(side_effect=sqlite3.OperationalError('locked'))
        logger._since_prune = 1

        self.assertEqual(logger._maybe_prune(), 0)
        self.assertIn('prune failed', logger.last_error)


if __name__ == '__main__':
    unittest.main()
