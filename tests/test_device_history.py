# -*- coding: utf-8 -*-
"""设备历史 JSONL 存储测试（task-47）。

覆盖：

* append/read 顺序、limit 截断、按 address 过滤；
* 坏行（非法 JSON/非对象/空行）跳过；
* latest_by_address 每个地址最后一条；
* 行数轮转与字节轮转（保留最近记录、文件有界）；
* append 失败不抛（返回 False）、非 dict 记录拒绝；
* 默认项目根（模块级 ``PROJECT_ROOT``）路径。

只用 stdlib unittest + 临时目录；不接硬件/不依赖 Qt。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from quatm.servers import device_history


class _TempRootCase(unittest.TestCase):
    """每个用例一个临时项目根，并让模块默认根指向它。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='quatm-devhist-')
        self.addCleanup(self._cleanup)
        self._root_patch = mock.patch.object(
            device_history, 'PROJECT_ROOT', self.root)
        self._root_patch.start()
        self.addCleanup(self._root_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def path(self):
        """当前临时项目根里的历史文件路径。"""
        return os.path.join(self.root, 'configuration', 'devices',
                            'health_history.jsonl')

    def write_lines(self, lines):
        """直接写原始行（模拟坏行/半截写入）。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')


class TestAppendRead(_TempRootCase):
    """追加/读取/过滤/最新状态。"""

    def test_append_read_order_and_limit(self):
        for index in range(3):
            ok = device_history.append(
                {'address': 'tcp://127.0.0.1:50%02d' % index,
                 'name': 'dev%d' % index, 'status': 'ok', 'kind': 'probe',
                 'source': 'poll'}, root=self.root)
            self.assertTrue(ok)
        records = device_history.read(root=self.root)
        self.assertEqual([record['name'] for record in records],
                         ['dev0', 'dev1', 'dev2'])
        self.assertEqual([record['name']
                          for record in device_history.read(limit=2,
                                                           root=self.root)],
                         ['dev1', 'dev2'])
        # 自动补 ts
        self.assertIsInstance(records[0]['ts'], float)
        # 按地址过滤
        device_history.append({'address': 'serial:///dev/ttyUSB0',
                               'name': 'serial', 'status': 'ok',
                               'kind': 'probe', 'source': 'manual'},
                              root=self.root)
        only = device_history.read(address='serial:///dev/ttyUSB0',
                                   root=self.root)
        self.assertEqual([record['name'] for record in only], ['serial'])
        self.assertEqual(device_history.read(address='nope', root=self.root),
                         [])
        self.assertEqual(device_history.read(limit=0, root=self.root)[-1]
                         ['name'], 'serial')

    def test_read_skips_bad_lines(self):
        self.write_lines([
            json.dumps({'address': 'a', 'name': 'good1', 'status': 'ok'}),
            '{not json',
            '[1, 2, 3]',
            '',
            json.dumps({'address': 'a', 'name': 'good2',
                        'status': 'error'}),
        ])
        records = device_history.read(root=self.root)
        self.assertEqual([record['name'] for record in records],
                         ['good1', 'good2'])

    def test_latest_by_address(self):
        for name, status in (('old', 'ok'), ('new', 'error')):
            device_history.append({'address': 'tcp://h:1', 'name': name,
                                   'status': status, 'kind': 'probe',
                                   'source': 'poll'}, root=self.root)
        device_history.append({'address': 'tcp://h:2', 'name': 'other',
                               'status': 'ok', 'kind': 'probe',
                               'source': 'poll'}, root=self.root)
        latest = device_history.latest_by_address(root=self.root)
        self.assertEqual(latest['tcp://h:1']['name'], 'new')
        self.assertEqual(latest['tcp://h:2']['name'], 'other')
        self.assertEqual(len(latest), 2)

    def test_clear(self):
        device_history.append({'address': 'a', 'name': 'x', 'status': 'ok'},
                              root=self.root)
        self.assertTrue(os.path.isfile(self.path))
        self.assertTrue(device_history.clear(root=self.root))
        self.assertFalse(os.path.isfile(self.path))
        self.assertEqual(device_history.read(root=self.root), [])
        self.assertTrue(device_history.clear(root=self.root))

    def test_default_root_is_project_root(self):
        self.assertTrue(device_history.append(
            {'address': 'a', 'name': 'default-root', 'status': 'ok'}))
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(device_history.read()[-1]['name'], 'default-root')
        self.assertTrue(device_history.latest_by_address()
                        ['a']['name'] == 'default-root')


class TestRotation(_TempRootCase):
    """行数/字节轮转：文件有界且保留最近记录。"""

    def test_rotation_by_max_lines(self):
        for index in range(12):
            device_history.append(
                {'address': 'a', 'name': 'rec%d' % index, 'status': 'ok',
                 'kind': 'probe', 'source': 'poll'},
                root=self.root, max_lines=5, max_bytes=10 ** 9)
        records = device_history.read(limit=0, root=self.root)
        self.assertLessEqual(len(records), 5)
        self.assertEqual([record['name'] for record in records],
                         ['rec7', 'rec8', 'rec9', 'rec10', 'rec11'])
        with open(self.path, encoding='utf-8') as handle:
            line_count = sum(1 for _line in handle)
        self.assertLessEqual(line_count, 5)

    def test_rotation_by_max_bytes(self):
        for index in range(40):
            device_history.append(
                {'address': 'a', 'name': 'big%d' % index, 'status': 'error',
                 'kind': 'probe', 'source': 'poll',
                 'error': 'x' * 200},
                root=self.root, max_bytes=600, max_lines=3)
        with open(self.path, encoding='utf-8') as handle:
            line_count = sum(1 for _line in handle)
        self.assertLessEqual(line_count, 3)
        records = device_history.read(limit=0, root=self.root)
        self.assertEqual(records[-1]['name'], 'big39')

    def test_read_limit_still_last_records_after_rotation(self):
        for index in range(10):
            device_history.append(
                {'address': 'a', 'name': 'r%d' % index, 'status': 'ok'},
                root=self.root, max_lines=4, max_bytes=10 ** 9)
        records = device_history.read(limit=2, root=self.root)
        self.assertEqual([record['name'] for record in records],
                         ['r8', 'r9'])


class TestFailures(_TempRootCase):
    """写失败不抛、非法记录拒绝。"""

    def test_append_failure_returns_false_without_raising(self):
        with mock.patch.object(device_history.os, 'open',
                               side_effect=OSError('read-only')):
            self.assertFalse(device_history.append(
                {'address': 'a', 'name': 'x', 'status': 'ok'},
                root=self.root))
        # 没有留下半截文件内容
        self.assertFalse(os.path.isfile(self.path))

    def test_append_rejects_non_dict(self):
        self.assertFalse(device_history.append(['not', 'a', 'dict'],
                                               root=self.root))
        self.assertFalse(device_history.append(None, root=self.root))

    def test_clear_failure_returns_false(self):
        with mock.patch.object(device_history.os, 'remove',
                               side_effect=OSError('permission')):
            self.assertFalse(device_history.clear(root=self.root))

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(device_history.read(root=self.root), [])
        self.assertEqual(device_history.latest_by_address(root=self.root), {})


if __name__ == '__main__':
    unittest.main()
