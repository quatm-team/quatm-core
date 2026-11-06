"""`properties.json` 损坏保护测试（致命问题清单第 3 条）。

覆盖：

* 合法 JSON → 正常返回字典；
* 文件不存在 → 返回 {}、**不**报警、**不**生成备份（首次启动属正常）；
* 半截/非法 JSON、非法 UTF-8、根不是对象 → 返回 {}、生成
  ``properties.json.corrupt-<UTC时间戳>`` 备份（内容与原文件一致）、
  stderr 有醒目提示；
* 备份复制失败（``shutil.copy2`` 抛 OSError）→ 不抛、仍返回 {}；
* ``send_error`` 上报失败也不影响返回（消息层不可用不能阻塞启动）；
* 时间戳格式可断言：``\\.corrupt-\\d{8}-\\d{6}$``（UTC）。

只用 stdlib unittest + tempfile。
"""

import contextlib
import io
import os
import re
import tempfile
import unittest
from unittest import mock

from quatm.servers import configreader
from quatm.utils import i18n as _i18n

CORRUPT_SUFFIX = re.compile(r'\.corrupt-\d{8}-\d{6}$')


class _EnglishLanguageMixin:
    """警告文案走 tr()：测试里固定英文，避免系统语言影响断言。"""

    def setUp(self):
        super().setUp()
        saved = _i18n.language()
        _i18n.set_language('en')
        self.addCleanup(_i18n.set_language, saved)


class _TempPropertiesMixin:
    """在临时目录里准备一个 properties.json 路径。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, 'properties.json')

    def write_bytes(self, data):
        """把原始字节写进属性文件（用于非法 UTF-8 场景）。"""
        with open(self.path, 'wb') as handle:
            handle.write(data)

    def write_text(self, text):
        """把文本写进属性文件。"""
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def corrupt_backups(self):
        """返回目录里所有 ``.corrupt-*`` 文件名（排序）。"""
        return sorted(name for name in os.listdir(self.dir)
                      if '.corrupt-' in name)

    def load(self):
        """调用被测函数并返回 (结果, stderr 文本)。"""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = configreader._load_properties(self.path)
        return result, stderr.getvalue()


class TestLoadProperties(_EnglishLanguageMixin, _TempPropertiesMixin,
                         unittest.TestCase):
    """``_load_properties(filename)`` 的行为契约。"""

    def test_valid_json_returns_dict(self):
        self.write_text('{"Experiments": {"a": 1}}')
        result, stderr = self.load()
        self.assertEqual(result, {'Experiments': {'a': 1}})
        self.assertEqual(stderr, '')
        self.assertEqual(self.corrupt_backups(), [])

    def test_missing_file_returns_empty_without_alarm(self):
        result, stderr = self.load()
        self.assertEqual(result, {})
        self.assertEqual(stderr, '')
        self.assertEqual(self.corrupt_backups(), [])

    def test_truncated_json_is_backed_up_and_reported(self):
        original = b'{"Experiments": {"a": 1}, "truncated": '
        self.write_bytes(original)
        result, stderr = self.load()
        self.assertEqual(result, {})
        backups = self.corrupt_backups()
        self.assertEqual(len(backups), 1, backups)
        backup_name = backups[0]
        self.assertRegex(backup_name, CORRUPT_SUFFIX)
        self.assertTrue(backup_name.startswith('properties.json.corrupt-'))
        with open(os.path.join(self.dir, backup_name), 'rb') as handle:
            self.assertEqual(handle.read(), original)
        # 醒目提示：stderr + 关键词 + 备份路径
        self.assertIn('WARNING', stderr)
        self.assertIn('corrupt', stderr)
        self.assertIn('Properties', stderr)
        self.assertIn(backup_name, stderr)

    def test_invalid_utf8_is_backed_up(self):
        original = b'\xff\xfe{"a": 1}'
        self.write_bytes(original)
        result, stderr = self.load()
        self.assertEqual(result, {})
        backups = self.corrupt_backups()
        self.assertEqual(len(backups), 1, backups)
        with open(os.path.join(self.dir, backups[0]), 'rb') as handle:
            self.assertEqual(handle.read(), original)
        self.assertIn('UnicodeDecodeError', stderr)

    def test_non_object_json_is_treated_as_corrupt(self):
        self.write_text('[]')
        result, stderr = self.load()
        self.assertEqual(result, {})
        self.assertEqual(len(self.corrupt_backups()), 1)
        self.assertIn('must be an object', stderr)

    def test_backup_copy_failure_does_not_raise(self):
        self.write_text('{broken')
        with mock.patch.object(configreader.shutil, 'copy2',
                               side_effect=OSError('disk full')):
            result, stderr = self.load()
        self.assertEqual(result, {})
        self.assertEqual(self.corrupt_backups(), [])
        self.assertIn('WARNING', stderr)
        self.assertIn('could not create a backup', stderr)

    def test_send_error_failure_does_not_raise(self):
        self.write_text('{broken')
        with mock.patch('quatm.servers.send_error',
                        side_effect=RuntimeError('no message hub')):
            result, stderr = self.load()
        self.assertEqual(result, {})
        self.assertIn('WARNING', stderr)
        # 备份照常生成
        self.assertEqual(len(self.corrupt_backups()), 1)


class TestPropertiesEntry(_EnglishLanguageMixin, _TempPropertiesMixin,
                          unittest.TestCase):
    """公共入口 ``Properties()`` 使用模块级 ``propertyfilename``。"""

    def test_properties_uses_propertyfilename_and_protects_corruption(self):
        self.write_text('{not json')
        with mock.patch.object(configreader, 'propertyfilename', self.path):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = configreader.Properties()
        self.assertEqual(result, {})
        self.assertEqual(len(self.corrupt_backups()), 1)
        self.assertIn('corrupt', stderr.getvalue())

    def test_properties_returns_defaults_for_missing_file(self):
        with mock.patch.object(configreader, 'propertyfilename', self.path):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = configreader.Properties()
        self.assertEqual(result, {})
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(self.corrupt_backups(), [])


if __name__ == '__main__':
    unittest.main()
