"""CommandAttr / CommandClient 类属性句柄泄漏的回归测试（stdlib ``unittest``）。

背景：``CommandAttr`` 是**类属性描述符**，每条持有一个 ``CommandClient``
（2 个 ZMQ socket）；``Experiment.close()`` 过去只遍历 ``vars(self)``，看不到
它们，反复开合实验窗口后 fd/连接线性增长（用户 watchdog：fd≈2255、
conns≈537）。修复后 ``Experiment.close()`` 会按 ``type(self).__mro__`` 的类
``__dict__`` 关闭这些描述符。

本测试不连真实 ZMQ/硬件：用 fake ``CommandClient`` 计数；fd 用例用真实的
``os.open(os.devnull)`` 模拟句柄（macOS/Linux 的 ``/dev/fd`` 可统计），
不依赖 pyzmq 连接。运行方式（仓库根目录）::

    ./python_venv/bin/python -m unittest tests.test_attribute_leaks -v
"""

import gc
import os
import unittest
from unittest import mock

from quatm.experiment import attributes
from quatm.experiment import experiment as experiment_module
from quatm.experiment.experiment import Experiment


class FakeCommandClient:
    """假的 CommandClient：只记录 close 次数，不建任何 socket。"""

    #: 所有构造出来的 fake 实例（按构造顺序）。
    instances = []

    def __init__(self, device='test'):
        """记录设备名并把自己登记到类列表，供断言取用。"""
        self.device = device
        self.close_calls = 0
        self.sent = []
        self.fail_on_close = False
        FakeCommandClient.instances.append(self)

    def send(self, cmd='', data=None, **kwargs):
        """记录发送内容（测试只关心 close 回收，不关心命令语义）。"""
        self.sent.append((cmd, data))

    def close(self):
        """记录关闭次数；``fail_on_close`` 为真时抛错以测试容错。"""
        self.close_calls += 1
        if self.fail_on_close:
            raise RuntimeError('boom')


class BoomAttr(attributes.AttributeBaseClass):
    """一个 close() 会抛错的属性描述符（模拟坏掉的回收路径）。"""

    def close(self):
        """故意抛错，验证 Experiment.close() 不中断其它回收。"""
        raise RuntimeError('attribute close boom')


def _make_experiment(namespace):
    """构造一个不调用 ``Experiment.__init__`` 的假实验类。

    真正的 ``Experiment.__init__`` 会连接 Properties/DataClient（ZMQ），本测试
    只验证 ``close()`` 的资源回收路径，不需要这些连接。用 ``type()`` 动态建类
    便于每个用例挂不同的属性描述符；``close()`` 只依赖实例字典与类字典，不会
    因为跳过 ``__init__`` 而报错。

    Args:
        namespace (dict): 类命名空间（属性名 → 描述符/值）。

    Returns:
        type: 新实验类（``Experiment`` 子类）。
    """
    namespace = dict(namespace)
    namespace.setdefault('__init__', lambda self: None)
    return type('FakeExperiment', (Experiment,), namespace)


class TestCommandAttrClose(unittest.TestCase):
    """类属性 Description：close 幂等、失败不阻断、实例设备仍关闭。"""

    def setUp(self):
        FakeCommandClient.instances = []
        patcher = mock.patch.object(attributes, 'CommandClient',
                                    FakeCommandClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _two_attr_experiment(self):
        """含 2 个 CommandAttr 的假实验类（并断言 client 已构造）。"""
        klass = _make_experiment({
            'attr_a': attributes.CommandAttr(None, device='dev_a',
                                             command='A {}'),
            'attr_b': attributes.CommandAttr(None, device='dev_b',
                                             command='B {}'),
        })
        self.assertEqual(len(FakeCommandClient.instances), 2)
        return klass

    def test_class_attributes_are_closed(self):
        """Experiment.close() 必须关掉类属性上的两个 CommandClient。"""
        klass = self._two_attr_experiment()
        exp = klass()
        exp.close()
        clients = FakeCommandClient.instances
        self.assertEqual([client.close_calls for client in clients], [1, 1])
        self.assertEqual([client.device for client in clients],
                         ['dev_a', 'dev_b'])

    def test_close_is_idempotent(self):
        """重复 close() 不会重复关闭，也不抛异常。"""
        klass = self._two_attr_experiment()
        exp = klass()
        exp.close()
        exp.close()
        exp.close()
        self.assertEqual([client.close_calls
                          for client in FakeCommandClient.instances], [1, 1])

    def test_failing_client_still_closes_others_and_warns(self):
        """一个 client.close 抛错：另一个仍被关闭，并发出 warning。"""
        klass = self._two_attr_experiment()
        FakeCommandClient.instances[0].fail_on_close = True
        exp = klass()
        with mock.patch.object(attributes, 'send_warning') as warn:
            exp.close()          # 不能抛
        self.assertEqual([client.close_calls
                          for client in FakeCommandClient.instances], [1, 1])
        self.assertEqual(warn.call_count, 1)
        self.assertIn('attr_a', str(warn.call_args))

    def test_failing_attribute_does_not_stop_others(self):
        """描述符自己的 close() 抛错：Experiment.close() 记 warning 后继续。"""
        klass = _make_experiment({
            'bad': BoomAttr(),
            'attr_a': attributes.CommandAttr(None, device='dev_a',
                                             command='A {}'),
        })
        exp = klass()
        with mock.patch.object(experiment_module, 'send_warning') as warn:
            exp.close()          # 不能抛
        self.assertEqual(FakeCommandClient.instances[0].close_calls, 1)
        self.assertEqual(warn.call_count, 1)
        self.assertIn('bad', str(warn.call_args))

    def test_shared_descriptor_closed_once(self):
        """同一个描述符对象出现在多个类的字典里时，按 id() 去重只关一次。"""
        descriptor = attributes.CommandAttr(None, device='dev_shared',
                                            command='S {}')
        base = _make_experiment({'shared': descriptor})
        child = type('FakeChild', (base,), {'shared': descriptor})
        exp = child()
        exp.close()
        self.assertEqual(FakeCommandClient.instances[0].close_calls, 1)

    def test_instance_device_is_still_closed(self):
        """实例字典里的普通设备（有 close）仍按既有行为关闭。"""
        class Device:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        klass = _make_experiment({})
        exp = klass()
        device = Device()
        exp.some_device = device
        exp.close()
        self.assertEqual(device.close_calls, 1)

    def test_repeated_build_close_cycles_do_not_accumulate_clients(self):
        """循环“建类→实例→close”：每个 client 恰好被关一次，没有未关的。"""
        for _ in range(5):
            klass = self._two_attr_experiment_quiet()
            exp = klass()
            exp.close()
        clients = FakeCommandClient.instances
        self.assertEqual(len(clients), 10)
        self.assertEqual([client.close_calls for client in clients],
                         [1] * 10)

    def _two_attr_experiment_quiet(self):
        """同 `_two_attr_experiment`，但不断言实例数（循环用）。"""
        return _make_experiment({
            'attr_a': attributes.CommandAttr(None, device='dev_a',
                                             command='A {}'),
            'attr_b': attributes.CommandAttr(None, device='dev_b',
                                             command='B {}'),
        })


class FakeFdClient:
    """持有一个真实文件描述符的 fake CommandClient（用于 fd 泄漏测试）。"""

    def __init__(self, device='test'):
        """打开一个 ``/dev/null`` fd 充当“socket”句柄。"""
        self.device = device
        self.fd = os.open(os.devnull, os.O_RDONLY)
        self._closed = False

    def send(self, cmd='', data=None, **kwargs):
        """空实现：本测试只关心 fd 回收。"""
        pass

    def close(self):
        """关闭 fd（幂等）。"""
        if self._closed:
            return
        self._closed = True
        os.close(self.fd)


@unittest.skipUnless(os.path.isdir('/dev/fd'),
                     '需要 /dev/fd 才能统计进程 fd（macOS/Linux）')
class TestNoFdGrowth(unittest.TestCase):
    """循环“建类→实例→close”后 fd 不线性增长（用真实 fd 模拟 socket）。"""

    def _fd_count(self):
        """返回当前进程打开的 fd 数量（/dev/fd 条目数）。"""
        return len(os.listdir('/dev/fd'))

    def test_fd_does_not_grow_with_close_cycles(self):
        """修复后 30 轮循环前后 fd 数量应基本不变（容差 4）。"""
        with mock.patch.object(attributes, 'CommandClient', FakeFdClient):
            cycles = 30
            gc.collect()
            baseline = self._fd_count()
            for _ in range(cycles):
                klass = _make_experiment({
                    'attr_a': attributes.CommandAttr(None, device='dev_a',
                                                     command='A {}'),
                    'attr_b': attributes.CommandAttr(None, device='dev_b',
                                                     command='B {}'),
                })
                exp = klass()
                exp.close()
            gc.collect()
            after = self._fd_count()
        self.assertLessEqual(after - baseline, 4,
                             'fd 从 %d 涨到 %d：CommandAttr 的句柄没有被关闭'
                             % (baseline, after))


if __name__ == '__main__':
    unittest.main()
