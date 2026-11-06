# -*- coding: utf-8 -*-
"""长期运行资源采样（resource_monitor）的回归测试（P0-1 / P1-6）。

覆盖：

* 路径/尺寸：临时项目里各 DB/JSON/历史文件尺寸正确；``include_data_dir``
  时才统计 ``Data/`` 递归大小与 ``*.h5`` 数量；
* 磁盘/阈值：``disk_usage`` 结果映射、失败降级为 None；默认阈值与
  Properties 覆盖；磁盘/DB/props/fd/线程告警等级；阈值 0 = 关闭；
* 进程指标：POSIX 上 rss/fd/threads/uptime 有值（Windows 跳过 POSIX-only）；
* 跃迁去重：连续两次同类告警只通知一次，等级变化/恢复时产生跃迁（info）；
* 历史：append/read、坏行跳过、按字节/行数轮转（原子、无 tmp 残留）、
  不存在的文件返回空列表；
* 只读：``sample_once`` 不写任何 Properties；通知失败不影响采样；
* 轻量：默认 ``collect_resources`` 不调用递归 walk（GUI 每秒快照路径），
  ``Data/`` 权限失败/超预算不抛、返回 None；
* 接入：``system_status.collect_health`` 增加新字段且仍轻量、失败降级。

全部 stdlib unittest + tempfile / fake props，不接硬件、不连网络。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from unittest import mock

from quatm.servers import resource_monitor as rm
from quatm.servers import system_status

_USAGE = namedtuple('usage', 'total used free')
_MB = 1024 * 1024


class FakeProps:
    """最小 Properties 替身：get 返回值、set 只记录（用于断言只读）。"""

    def __init__(self, values=None, raising=False):
        self.values = dict(values or {})
        self.raising = raising
        self.sets = []

    def get(self, path, default=None):
        if self.raising:
            raise RuntimeError('property hub down')
        return self.values.get(path, default)

    def set(self, path, value):
        self.sets.append((path, value))


def make_project():
    """建一个临时项目目录：messages/push 库、properties、设备历史、Data/。

    Returns:
        tuple[str, str]: ``(root, cleanup_dir)``。
    """
    tmp = tempfile.TemporaryDirectory()
    root = tmp.name
    config = os.path.join(root, 'configuration')
    os.makedirs(os.path.join(config, 'messagelist'))
    os.makedirs(os.path.join(config, 'push'))
    os.makedirs(os.path.join(config, 'properties'))
    os.makedirs(os.path.join(config, 'devices'))
    os.makedirs(os.path.join(root, 'Data', 'sub'))
    sizes = {
        os.path.join(config, 'messagelist', 'messages.db'): 1000,
        os.path.join(config, 'push', 'push.db'): 2000,
        os.path.join(config, 'properties', 'properties.json'): 3000,
        os.path.join(config, 'devices', 'health_history.jsonl'): 4000,
        os.path.join(root, 'Data', 'a.h5'): 5000,
        os.path.join(root, 'Data', 'sub', 'b.h5'): 6000,
        os.path.join(root, 'Data', 'notes.txt'): 111,
    }
    for path, size in sizes.items():
        with open(path, 'wb') as handle:
            handle.write(b'x' * size)
    return root, tmp.cleanup


def snapshot_with_alerts(alerts):
    """构造 collect_resources 形状的最小快照（供 sample_once 测试注入）。"""
    return {'ts': time.time(), 'project': '/tmp/project',
            'disk': {}, 'sizes': {}, 'process': {},
            'thresholds': {}, 'alerts': list(alerts)}


class TestSizes(unittest.TestCase):
    """路径与尺寸计算。"""

    def setUp(self):
        self.root, cleanup = make_project()
        self.addCleanup(cleanup)

    def test_resource_path_layout(self):
        self.assertTrue(rm.resource_path(self.root).endswith(
            os.path.join('configuration', 'health',
                         'resource_history.jsonl')))

    def test_light_sizes_from_project_files(self):
        record = rm.collect_resources(props=FakeProps(), root=self.root)
        sizes = record['sizes']
        self.assertEqual(sizes['message_db_bytes'], 1000)
        self.assertEqual(sizes['push_db_bytes'], 2000)
        self.assertEqual(sizes['properties_json_bytes'], 3000)
        self.assertEqual(sizes['device_history_bytes'], 4000)
        self.assertIsNone(sizes['resource_history_bytes'])
        # 轻量路径不递归 Data/
        self.assertIsNone(sizes['data_dir_bytes'])
        self.assertIsNone(sizes['h5_file_count'])

    def test_include_data_dir_walks_once(self):
        record = rm.collect_resources(props=FakeProps(), root=self.root,
                                      include_data_dir=True)
        sizes = record['sizes']
        self.assertEqual(sizes['data_dir_bytes'], 5000 + 6000 + 111)
        self.assertEqual(sizes['h5_file_count'], 2)

    def test_missing_files_are_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sizes = rm.collect_resources(props=FakeProps(),
                                     root=tmp.name)['sizes']
        for key in ('message_db_bytes', 'push_db_bytes',
                    'properties_json_bytes', 'device_history_bytes'):
            with self.subTest(key=key):
                self.assertIsNone(sizes[key])


class TestDiskAndAlerts(unittest.TestCase):
    """磁盘字段、阈值与告警等级。"""

    def setUp(self):
        rm.reset_alert_state()
        self.addCleanup(rm.reset_alert_state)

    def test_disk_fields(self):
        usage = _USAGE(total=100 * 1024 ** 3, used=40 * 1024 ** 3,
                       free=60 * 1024 ** 3)
        with mock.patch.object(rm.shutil, 'disk_usage', return_value=usage):
            disk = rm.collect_resources(props=FakeProps(), root='/tmp')['disk']
        self.assertEqual(disk['free_gb'], 60.0)
        self.assertEqual(disk['total_gb'], 100.0)
        self.assertAlmostEqual(disk['used_pct'], 40.0, places=1)

    def test_disk_failure_is_none(self):
        with mock.patch.object(rm.shutil, 'disk_usage',
                               side_effect=OSError('denied')):
            record = rm.collect_resources(props=FakeProps(), root='/tmp')
        self.assertIsNone(record['disk']['free_gb'])
        self.assertIsNone(record['disk']['total_gb'])
        self.assertIsNone(record['disk']['used_pct'])

    def test_threshold_defaults_and_overrides(self):
        defaults = rm.read_thresholds(FakeProps())
        self.assertEqual(defaults['min_free_gb'], 1.0)
        self.assertEqual(defaults['warn_free_gb'], 2.0)
        self.assertEqual(defaults['max_db_mb'], 1024.0)
        self.assertEqual(defaults['max_fds'], 512.0)
        props = FakeProps({'/System/health/min_free_gb': 5.5,
                           '/System/health/max_threads': '42'})
        thresholds = rm.read_thresholds(props)
        self.assertEqual(thresholds['min_free_gb'], 5.5)
        self.assertEqual(thresholds['max_threads'], 42.0)

    def _alerts_for(self, free_gb, props_values=None):
        usage = _USAGE(total=100 * 1024 ** 3,
                       used=100 * 1024 ** 3 - free_gb * 1024 ** 3,
                       free=free_gb * 1024 ** 3)
        with mock.patch.object(rm.shutil, 'disk_usage', return_value=usage):
            return rm.collect_resources(
                props=FakeProps(props_values), root='/tmp')['alerts']

    def test_disk_alert_levels(self):
        alerts = self._alerts_for(0.5)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['kind'], 'disk_free')
        self.assertEqual(alerts[0]['level'], 'error')

        alerts = self._alerts_for(1.5)
        self.assertEqual(alerts[0]['level'], 'warning')

        self.assertEqual(self._alerts_for(50.0), [])

    def test_threshold_zero_disables_disk_check(self):
        alerts = self._alerts_for(0.0, {'/System/health/min_free_gb': 0,
                                        '/System/health/warn_free_gb': 0})
        self.assertEqual(alerts, [])

    def test_database_alert(self):
        root, cleanup = make_project()
        self.addCleanup(cleanup)
        # messages.db 1000B + push.db 2000B；阈值设成 0.001 MB 必然超
        props = FakeProps({'/System/health/max_db_mb': 0.001})
        alerts = rm.collect_resources(props=props, root=root)['alerts']
        kinds = {(a['kind'], a['level']) for a in alerts}
        self.assertIn(('database', 'error'), kinds)

    def test_properties_json_alert_is_warning(self):
        root, cleanup = make_project()
        self.addCleanup(cleanup)
        props = FakeProps({'/System/health/max_props_mb': 0.001})
        alerts = rm.collect_resources(props=props, root=root)['alerts']
        prod = [a for a in alerts if a['kind'] == 'properties_json']
        self.assertEqual(len(prod), 1)
        self.assertEqual(prod[0]['level'], 'warning')

    def test_fd_and_thread_alerts(self):
        process = {'pid': 1, 'rss_mb': 10.0, 'threads': 100,
                   'open_fds': 1000, 'uptime_s': 1.0}
        with mock.patch.object(rm, '_collect_process', return_value=process):
            alerts = rm.collect_resources(
                props=FakeProps({'/System/health/max_fds': 10,
                                 '/System/health/max_threads': 5}),
                root='/tmp')['alerts']
        kinds = {(a['kind'], a['level']) for a in alerts}
        self.assertIn(('open_fds', 'error'), kinds)
        self.assertIn(('threads', 'error'), kinds)

    @unittest.skipIf(sys.platform.startswith('win'), 'POSIX-only 断言')
    def test_process_metrics_on_posix(self):
        process = rm._collect_process()
        self.assertEqual(process['pid'], os.getpid())
        self.assertIsInstance(process['open_fds'], int)
        self.assertGreater(process['open_fds'], 0)
        self.assertIsInstance(process['rss_mb'], float)
        self.assertGreater(process['rss_mb'], 0.0)
        self.assertGreaterEqual(process['threads'], 1)
        self.assertGreaterEqual(process['uptime_s'], 0.0)


class TestTransitionsAndHistory(unittest.TestCase):
    """跃迁去重 + JSONL 历史（append/read/坏行/轮转）。"""

    def setUp(self):
        rm.reset_alert_state()
        self.addCleanup(rm.reset_alert_state)

    def test_transition_dedup_and_recovery(self):
        alert = {'kind': 'disk_free', 'level': 'error',
                 'message': 'disk low', 'value': 0.5}
        records = [snapshot_with_alerts([alert]),
                   snapshot_with_alerts([alert]),
                   snapshot_with_alerts([])]
        notes = []
        with mock.patch.object(rm, 'collect_resources',
                               side_effect=records), \
                mock.patch.object(rm, 'append_history', return_value=True):
            first = rm.sample_once(notify=lambda m, level: notes.append(
                (level, m)))
            second = rm.sample_once(notify=lambda m, level: notes.append(
                (level, m)))
            third = rm.sample_once(notify=lambda m, level: notes.append(
                (level, m)))
        self.assertEqual(len(first['alert_transitions']), 1)
        self.assertEqual(first['alert_transitions'][0]['level'], 'error')
        self.assertEqual(second['alert_transitions'], [])   # 持续不重复
        self.assertEqual(len(third['alert_transitions']), 1)
        self.assertEqual(third['alert_transitions'][0]['level'], 'info')
        self.assertEqual([level for level, _message in notes],
                         ['error', 'info'])

    def test_level_change_produces_transition(self):
        warning = {'kind': 'disk_free', 'level': 'warning', 'message': 'low',
                   'value': 1.5}
        error = {'kind': 'disk_free', 'level': 'error', 'message': 'lower',
                 'value': 0.5}
        with mock.patch.object(rm, 'collect_resources',
                               side_effect=[snapshot_with_alerts([warning]),
                                            snapshot_with_alerts([error])]), \
                mock.patch.object(rm, 'append_history', return_value=True):
            rm.sample_once(notify=lambda *args: None)
            second = rm.sample_once(notify=lambda *args: None)
        self.assertEqual(len(second['alert_transitions']), 1)
        self.assertEqual(second['alert_transitions'][0]['from'], 'warning')
        self.assertEqual(second['alert_transitions'][0]['to'], 'error')

    def test_history_append_read_and_bad_lines(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        for index in range(3):
            self.assertTrue(rm.append_history({'index': index, 'ts': index},
                                              root=root))
        with open(rm.resource_path(root), 'a', encoding='utf-8') as handle:
            handle.write('{not json\n')
        records = rm.read_history(root=root)
        self.assertEqual([r['index'] for r in records], [0, 1, 2])

    def test_history_rotation_keeps_last_lines(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        for index in range(6):
            rm.append_history({'index': index}, root=root,
                              max_bytes=10 * _MB, max_lines=3)
        records = rm.read_history(root=root)
        self.assertEqual([r['index'] for r in records], [3, 4, 5])
        folder = os.path.dirname(rm.resource_path(root))
        self.assertEqual([name for name in os.listdir(folder)
                          if '.tmp.' in name], [])

    def test_history_rotation_by_bytes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        rm.append_history({'payload': 'x' * 5000}, root=root,
                          max_bytes=6000, max_lines=1)
        rm.append_history({'payload': 'y' * 5000}, root=root,
                          max_bytes=6000, max_lines=1)
        records = rm.read_history(root=root)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['payload'].startswith('y'))

    def test_read_missing_history_is_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(rm.read_history(root=tmp.name), [])

    def test_sample_once_is_read_only_and_writes_history(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        props = FakeProps()
        record = rm.sample_once(props, include_data_dir=False,
                                notify=lambda *args: None, root=tmp.name)
        self.assertIn('alert_transitions', record)
        self.assertFalse(props.sets)                 # 只读，不写属性
        self.assertEqual(len(rm.read_history(root=tmp.name)), 1)

    def test_notify_failure_does_not_break_sampling(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        alert = {'kind': 'threads', 'level': 'error',
                 'message': 'too many threads', 'value': 999}

        def broken_notify(_message, _level):
            raise RuntimeError('push failed')

        with mock.patch.object(rm, 'collect_resources',
                               return_value=snapshot_with_alerts([alert])):
            record = rm.sample_once(FakeProps(), notify=broken_notify,
                                    root=tmp.name)
        self.assertEqual(len(record['alert_transitions']), 1)
        self.assertEqual(len(rm.read_history(root=tmp.name)), 1)


class TestLightweight(unittest.TestCase):
    """轻量路径不 walk Data/；walk 失败/超预算不抛。"""

    def test_default_collect_does_not_walk(self):
        with mock.patch.object(rm, '_walk_data_dir') as walk:
            record = rm.collect_resources(props=FakeProps(), root='/tmp')
        self.assertFalse(walk.called)
        self.assertIsNone(record['sizes']['data_dir_bytes'])

    def test_include_data_dir_calls_walk(self):
        with mock.patch.object(rm, '_walk_data_dir',
                               return_value=(123, 4)) as walk:
            record = rm.collect_resources(props=FakeProps(), root='/tmp',
                                          include_data_dir=True)
        self.assertTrue(walk.called)
        self.assertEqual(record['sizes']['data_dir_bytes'], 123)
        self.assertEqual(record['sizes']['h5_file_count'], 4)

    def test_walk_permission_error_returns_none(self):
        with mock.patch.object(rm.os, 'walk',
                               side_effect=PermissionError('denied')):
            total, count = rm._walk_data_dir('/tmp')
        self.assertIsNone(total)
        self.assertIsNone(count)

    def test_walk_budget_exhausted_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'a.bin'), 'wb') as handle:
                handle.write(b'x')
            total, count = rm._walk_data_dir(root, budget_s=-1.0)
        self.assertIsNone(total)
        self.assertIsNone(count)


class TestSystemStatusIntegration(unittest.TestCase):
    """system_status.collect_health 接入新字段（轻量、只读、失败降级）。"""

    def _resource(self):
        return {'ts': 1.0, 'project': '/tmp', 'disk': {
                    'path': '/tmp/Data', 'free_gb': 12.5,
                    'total_gb': 100.0, 'used_pct': 87.5},
                'sizes': {'message_db_bytes': 111,
                          'push_db_bytes': 222,
                          'properties_json_bytes': 333,
                          'device_history_bytes': 444,
                          'resource_history_bytes': 555,
                          'data_dir_bytes': None, 'h5_file_count': None},
                'process': {'pid': 1, 'rss_mb': 42.0, 'threads': 7,
                            'open_fds': 9, 'uptime_s': 1.0},
                'thresholds': dict(rm.DEFAULT_THRESHOLDS),
                'alerts': [{'kind': 'database', 'level': 'error',
                            'message': 'db big', 'value': 1.0}]}

    def test_health_contains_resource_fields(self):
        with mock.patch.object(system_status.resource_monitor,
                               'collect_resources',
                               return_value=self._resource()):
            health = system_status.collect_health(FakeProps(), now=1000.0)
        self.assertEqual(health['free_gb'], 12.5)
        self.assertEqual(health['total_gb'], 100.0)
        self.assertEqual(health['used_pct'], 87.5)
        self.assertEqual(health['message_db_bytes'], 111)
        self.assertEqual(health['push_db_bytes'], 222)
        self.assertEqual(health['properties_json_bytes'], 333)
        self.assertEqual(health['device_history_bytes'], 444)
        self.assertEqual(health['open_fds'], 9)
        self.assertEqual(health['threads'], 7)
        self.assertEqual(health['rss_mb'], 42.0)
        self.assertEqual(health['resource_alerts'][0]['kind'], 'database')
        # 旧字段保持不变
        self.assertIsInstance(health['disk_free_gb'], float)

    def test_health_does_not_walk_data_dir(self):
        with mock.patch.object(rm, '_walk_data_dir') as walk:
            system_status.collect_health(FakeProps(), now=1000.0)
        self.assertFalse(walk.called, '每秒健康快照不能递归 walk Data/')

    def test_resource_failure_degrades_without_raising(self):
        with mock.patch.object(system_status.resource_monitor,
                               'collect_resources',
                               side_effect=RuntimeError('boom')):
            health = system_status.collect_health(FakeProps(), now=1000.0)
        for field in ('free_gb', 'total_gb', 'used_pct', 'open_fds',
                      'threads', 'rss_mb', 'resource_alerts'):
            with self.subTest(field=field):
                self.assertIn(field, health)
        self.assertIsNone(health['free_gb'])
        self.assertEqual(health['resource_alerts'], [])


if __name__ == '__main__':
    unittest.main()
