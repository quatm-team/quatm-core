"""ZMQ 非回环绑定启动警告测试（致命问题清单第 8 条）。

ZMQ **没有认证、没有加密**：端点若绑到 0.0.0.0 / 局域网地址，同网段任何主机
都能读写属性、注入命令。这里测试 ``configreader`` 里的两个 helper：

* ``is_loopback_endpoint``：127.0.0.1 / localhost / ::1 / ipc 视为安全；
  ``tcp://*`` / 0.0.0.0 / 具体 LAN IP 视为暴露；
* ``warn_if_exposed``：非回环时向 stdout 打印醒目警告，**每端点只警告一次**，
  返回 None、任何情况下不抛异常；
* ``warn_hub_if_exposed``：只检查 configfile 里存在的 Hub 端点，缺 Hub 时静默。

只用 stdlib unittest。
"""

import contextlib
import io
import unittest
from unittest import mock

from quatm.servers import configreader
from quatm.utils import i18n as _i18n


class _EnglishLanguageMixin:
    """警告文案走 tr()：测试里固定英文，避免系统语言影响断言。"""

    def setUp(self):
        super().setUp()
        saved = _i18n.language()
        _i18n.set_language('en')
        self.addCleanup(_i18n.set_language, saved)


class TestIsLoopbackEndpoint(unittest.TestCase):
    """端点 → 是否只在本机可见。"""

    def test_loopback_endpoints_are_safe(self):
        for endpoint in (
            'tcp://127.0.0.1:5605',
            'tcp://localhost:5605',
            'tcp://[::1]:5605',
            'tcp://127.0.0.2:5605',      # 127.0.0.0/8 也都是回环
            'ipc:///tmp/imagepub',       # 本地 IPC，不出主机
            'inproc://collector',
            None,
            '',
        ):
            self.assertTrue(configreader.is_loopback_endpoint(endpoint),
                            endpoint)

    def test_exposed_endpoints_are_not_safe(self):
        for endpoint in (
            'tcp://*:5605',
            'tcp://0.0.0.0:5605',
            'tcp://10.0.0.5:5605',
            'tcp://192.168.1.20:5605',
            'tcp://lab-server.local:5605',
        ):
            self.assertFalse(configreader.is_loopback_endpoint(endpoint),
                             endpoint)


class TestWarnIfExposed(_EnglishLanguageMixin, unittest.TestCase):
    """``warn_if_exposed`` 的提示内容、去重与安全性。"""

    def setUp(self):
        super().setUp()     # 先让 _EnglishLanguageMixin 固定语言
        # 每个测试从干净的"已警告"集合开始
        configreader._WARNED_EXPOSED_ENDPOINTS.clear()

    def _capture(self, endpoint, name='test endpoint'):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = configreader.warn_if_exposed(endpoint, name)
        return result, buffer.getvalue()

    def test_warns_and_does_not_raise(self):
        result, output = self._capture('tcp://0.0.0.0:5605')
        self.assertIsNone(result)
        self.assertIn('WARNING', output)
        self.assertIn('ZMQ', output)
        self.assertIn('tcp://0.0.0.0:5605', output)
        self.assertIn('127.0.0.1', output)   # 给出可操作建议
        self.assertIn('SSH', output)
        self.assertIn('authentication', output)

    def test_warns_only_once_per_endpoint(self):
        _, first = self._capture('tcp://10.0.0.5:5601', 'A')
        _, second = self._capture('tcp://10.0.0.5:5601', 'B')
        self.assertIn('WARNING', first)
        self.assertEqual(second, '', '同一端点不应重复警告')

    def test_different_endpoints_warn_separately(self):
        _, first = self._capture('tcp://10.0.0.5:5601')
        _, second = self._capture('tcp://10.0.0.6:5602')
        self.assertIn('WARNING', first)
        self.assertIn('WARNING', second)

    def test_loopback_does_not_warn(self):
        for endpoint in ('tcp://127.0.0.1:5605', 'tcp://[::1]:5605',
                         'ipc:///tmp/x'):
            result, output = self._capture(endpoint)
            self.assertIsNone(result)
            self.assertEqual(output, '', endpoint)


class TestWarnHubIfExposed(_EnglishLanguageMixin, unittest.TestCase):
    """从 configfile 读 Hub 端点并逐个检查（缺 Hub 时静默跳过）。"""

    def setUp(self):
        super().setUp()     # 先让 _EnglishLanguageMixin 固定语言
        configreader._WARNED_EXPOSED_ENDPOINTS.clear()

    def test_warns_on_non_loopback_hub_endpoints(self):
        conf = {'Servers': {'Data Hub': {'pub': 'tcp://0.0.0.0:5605',
                                         'sub': 'tcp://10.0.0.5:5606'}}}
        buffer = io.StringIO()
        with mock.patch.object(configreader, 'Config', return_value=conf), \
                contextlib.redirect_stdout(buffer):
            result = configreader.warn_hub_if_exposed('Data Hub', 'Data Manager')
        self.assertIsNone(result)
        output = buffer.getvalue()
        self.assertIn('tcp://0.0.0.0:5605', output)
        self.assertIn('tcp://10.0.0.5:5606', output)
        self.assertIn('Data Manager', output)

    def test_missing_hub_is_silent_and_safe(self):
        conf = {'Servers': {}}
        buffer = io.StringIO()
        with mock.patch.object(configreader, 'Config', return_value=conf), \
                contextlib.redirect_stdout(buffer):
            result = configreader.warn_hub_if_exposed('No Such Hub', 'X')
        self.assertIsNone(result)
        self.assertEqual(buffer.getvalue(), '')

    def test_hub_with_loopback_endpoints_is_silent(self):
        conf = {'Servers': {'Image Hub': {'pub': 'tcp://127.0.0.1:5601',
                                          'sub': 'ipc:///tmp/imagesub'}}}
        buffer = io.StringIO()
        with mock.patch.object(configreader, 'Config', return_value=conf), \
                contextlib.redirect_stdout(buffer):
            configreader.warn_hub_if_exposed('Image Hub', 'Image Logger')
        self.assertEqual(buffer.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
