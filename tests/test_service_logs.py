"""服务子进程日志（``quatm.servers.service_logs``）回归测试。

覆盖：文件名消毒（``/``、``:``、空格、路径穿越）、目录创建、追加写入、
**按大小轮转**（含备份份数上限与最旧删除）、打开失败降级（返回 None 不抛）、
Windows 上 rename 被占用（``PermissionError``）时放弃轮转继续追加。

只用 stdlib ``unittest`` + ``tempfile``；不启动任何子进程（Popen 在
``tests/test_process_manager_logs.py`` 里用 mock 验证）。
"""

import os
import tempfile
import unittest
from unittest import mock

from quatm.servers import service_logs


class _TempProjectMixin:
    """每个测试一个临时项目根（``<tmp>/configuration/logs`` 由被测代码创建）。"""

    def make_project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def logs_dir(self, root):
        return os.path.join(root, 'configuration', 'logs')


class TestLogPath(_TempProjectMixin, unittest.TestCase):
    """文件名消毒与路径拼接。"""

    def test_safe_filename(self):
        root = self.make_project()
        path = service_logs.log_path('Servers', 'Web Hub', project_root=root)
        self.assertEqual(os.path.dirname(path),
                         os.path.join(root, 'configuration', 'logs'))
        self.assertEqual(os.path.basename(path), 'Servers_Web_Hub.log')

    def test_sanitizes_slashes_colons_spaces(self):
        root = self.make_project()
        path = service_logs.log_path('Servers/', 'a/b:c d', project_root=root)
        name = os.path.basename(path)
        self.assertEqual(name, 'Servers_a_b_c_d.log')
        for bad in ('/', ':', ' ', '\\'):
            self.assertNotIn(bad, name)

    def test_path_traversal_is_neutralized(self):
        root = self.make_project()
        path = service_logs.log_path('..', '../..', project_root=root)
        name = os.path.basename(path)
        self.assertNotIn('..', name)
        self.assertEqual(name, 'unknown_unknown.log')
        # 仍然落在 logs 目录里
        self.assertEqual(os.path.dirname(path),
                         os.path.join(root, 'configuration', 'logs'))

    def test_hidden_prefix_is_stripped(self):
        root = self.make_project()
        name = os.path.basename(
            service_logs.log_path('Servers', '.hidden', project_root=root))
        self.assertEqual(name, 'Servers_hidden.log')


class TestLogDir(_TempProjectMixin, unittest.TestCase):
    """目录创建与失败降级。"""

    def test_creates_directory(self):
        root = self.make_project()
        folder = service_logs.log_dir(root)
        self.assertEqual(folder, os.path.join(root, 'configuration', 'logs'))
        self.assertTrue(os.path.isdir(folder))

    def test_idempotent(self):
        root = self.make_project()
        self.assertEqual(service_logs.log_dir(root),
                         service_logs.log_dir(root))

    def test_explicit_project_root_wins(self):
        root = self.make_project()
        self.assertTrue(service_logs.log_dir(root).startswith(root))

    def test_uncreatable_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        blocker = os.path.join(tmp.name, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as handle:
            handle.write('x')
        # 项目根其实是个普通文件 → configuration/logs 无法创建
        self.assertIsNone(service_logs.log_dir(blocker))
        self.assertIsNone(service_logs.open_service_log(
            'Servers', 'X', project_root=blocker))


class TestOpenServiceLog(_TempProjectMixin, unittest.TestCase):
    """打开/追加/失败降级。"""

    def test_writes_and_appends(self):
        root = self.make_project()
        handle = service_logs.open_service_log(
            'Servers', 'Message Hub', project_root=root)
        self.assertIsNotNone(handle)
        handle.write(b'hello\n')
        handle.close()

        path = service_logs.log_path('Servers', 'Message Hub',
                                     project_root=root)
        with open(path, 'rb') as reader:
            self.assertEqual(reader.read(), b'hello\n')

        handle = service_logs.open_service_log(
            'Servers', 'Message Hub', project_root=root)
        handle.write(b'world\n')
        handle.close()
        with open(path, 'rb') as reader:
            self.assertEqual(reader.read(), b'hello\nworld\n')

    def test_returns_binary_append_handle(self):
        root = self.make_project()
        handle = service_logs.open_service_log('GUI', 'Panel', project_root=root)
        self.addCleanup(handle.close)
        self.assertTrue(handle.writable())
        self.assertTrue(handle.fileno() > 0)
        with self.assertRaises(TypeError):
            handle.write('text')      # 二进制句柄：Popen 需要 fileno，不写文本

    @unittest.skipIf(os.name == 'nt', 'POSIX 权限语义')
    def test_readonly_dir_returns_none_without_raising(self):
        root = self.make_project()
        folder = service_logs.log_dir(root)
        os.chmod(folder, 0o500)
        self.addCleanup(os.chmod, folder, 0o700)
        self.assertIsNone(service_logs.open_service_log(
            'Servers', 'X', project_root=root))


class TestRotation(_TempProjectMixin, unittest.TestCase):
    """按大小轮转：.1/.2 顺序、备份份数上限、最旧删除、失败继续追加。"""

    MAX_BYTES = 1024
    BACKUPS = 2

    def _round(self, root, payload, **kwargs):
        handle = service_logs.open_service_log(
            'Servers', 'Rot', project_root=root,
            max_bytes=kwargs.pop('max_bytes', self.MAX_BYTES),
            backups=kwargs.pop('backups', self.BACKUPS))
        self.assertIsNotNone(handle)
        handle.write(payload)
        handle.flush()
        handle.close()

    def _logs(self, root):
        return sorted(os.listdir(self.logs_dir(root)))

    def _read(self, root, suffix=''):
        path = service_logs.log_path('Servers', 'Rot', project_root=root) + suffix
        with open(path, 'rb') as reader:
            return reader.read()

    def test_rotates_and_keeps_backup_count(self):
        root = self.make_project()
        for index in range(3):
            self._round(root, b'%d' % index + b'x' * 2000)
        self.assertEqual(self._logs(root),
                         ['Servers_Rot.log', 'Servers_Rot.log.1',
                          'Servers_Rot.log.2'])
        self.assertTrue(self._read(root).startswith(b'2'))
        self.assertTrue(self._read(root, '.1').startswith(b'1'))
        self.assertTrue(self._read(root, '.2').startswith(b'0'))

        # 第 4 轮：最旧的 .2 被删除，历史整体后移
        self._round(root, b'3' + b'x' * 2000)
        self.assertEqual(self._logs(root),
                         ['Servers_Rot.log', 'Servers_Rot.log.1',
                          'Servers_Rot.log.2'])
        self.assertTrue(self._read(root).startswith(b'3'))
        self.assertTrue(self._read(root, '.1').startswith(b'2'))
        self.assertTrue(self._read(root, '.2').startswith(b'1'))

    def test_no_rotation_below_threshold(self):
        root = self.make_project()
        self._round(root, b'small')
        self._round(root, b'small')
        self.assertEqual(self._logs(root), ['Servers_Rot.log'])
        self.assertEqual(self._read(root), b'smallsmall')

    def test_zero_backups_truncates(self):
        root = self.make_project()
        self._round(root, b'old' * 500, backups=0)
        self._round(root, b'new', backups=0)
        self.assertEqual(self._logs(root), ['Servers_Rot.log'])
        self.assertEqual(self._read(root), b'new')

    def test_rotation_failure_keeps_appending(self):
        """Windows 上 rename 被占用：放弃轮转，返回句柄继续追加（不抛）。"""
        root = self.make_project()
        self._round(root, b'old' * 800)          # 2400 B ≥ 阈值
        before = self._read(root)
        self.assertGreaterEqual(len(before), self.MAX_BYTES)

        with mock.patch.object(service_logs.os, 'replace',
                               side_effect=PermissionError('in use')):
            handle = service_logs.open_service_log(
                'Servers', 'Rot', project_root=root,
                max_bytes=self.MAX_BYTES, backups=self.BACKUPS)
            self.assertIsNotNone(handle)
            handle.write(b'after')
            handle.close()

        # 旧内容保留（没有轮转），新内容追加在后面
        data = self._read(root)
        self.assertTrue(data.startswith(b'old'))
        self.assertTrue(data.endswith(b'after'))
        self.assertEqual(self._logs(root), ['Servers_Rot.log'])
