"""实验运行时回归测试（P1-3/P1-4）：设备回收、失败可见、描述符按实例隔离。

仓库测试约定见 ``tests/__init__.py``：只用标准库 ``unittest``；不连硬件、
不连 ZMQ 服务、不需要授权——全部用假 controller / 设备 / Properties 替身。

覆盖点：

* ``Experiment.close()`` 必须对**已解析**设备调用 ``close()``；未解析的惰性
  设备不能被构造；单个设备关闭失败不阻断其它设备/Properties；可重复调用。
* 真实运行中 ``genericAttr`` 驱动调用失败、实时命令提交失败都要抛
  ``ExperimentDeviceError``（否则任务照旧标 Done、数据静默错）；模拟运行
  （``_simulate_cmd_stack`` / ``_simulating``）保持不抛。
* ``DAC`` / ``TTLOut`` / ``genericAttr`` / ``CommandAttr`` 描述符不得把
  experiment/controller 绑定到某个实例：同一实验类的两个实例互不影响。
"""

import unittest
from unittest import mock

import quatm.experiment.attributes as attributes_module
import quatm.experiment.experiment as experiment_module
from quatm.experiment.attributes import (
    CommandAttr, DAC, ExperimentDeviceError, TTLOut, _adwin_driver_error,
    genericAttr)
from quatm.experiment.experiment import Experiment, _LazyDevice


class FakeController:
    """假实时控制器：记录 push/start_realtime，可按需在提交时抛错。"""

    def __init__(self, submit_error=None):
        self.commands = []
        self.submitted = 0
        self.submit_error = submit_error
        self.stopped = 0
        self.discarded = 0

    def push(self, timestamp, command, value):
        """记录一条待执行命令。"""
        self.commands.append((timestamp, command, value))

    def start_realtime(self, precise=False):
        """记录一次提交；构造时给了 submit_error 就抛它。"""
        self.submitted += 1
        if self.submit_error is not None:
            raise self.submit_error

    def stop(self):
        """记录停止。"""
        self.stopped += 1

    def discard(self):
        """记录丢弃未提交命令。"""
        self.discarded += 1


class FakeProps:
    """最小 Properties 替身：get/set + ``close_all()`` 用的并发关闭协议。"""

    def __init__(self):
        self.data = {}
        self.close_calls = 0
        self.closed = False

    def get(self, path, default=None):
        """读属性（未知键返回默认值）。"""
        return self.data.get(path, default)

    def set(self, path, value):
        """写属性。"""
        self.data[path] = value

    def close(self):
        """单实例关闭（``close_all`` 不可用时的回退路径）。"""
        self.close_calls += 1
        self.closed = True

    def _request_close(self):
        """``properties.close_all`` 协议：返回是否需要进行关闭。"""
        return not self.closed

    def _wait_threads(self, timeout=2.0):
        """``properties.close_all`` 协议：替身没有线程要等。"""
        return None

    def _close_sockets(self):
        """``properties.close_all`` 协议：真正“关 socket”的地方。"""
        self.close_calls += 1
        self.closed = True


class FakeDataClient:
    """最小 DataClient 替身：只记录 close()。"""

    def __init__(self):
        self.close_calls = 0

    def close(self):
        """记录关闭次数。"""
        self.close_calls += 1


class FakeDevice:
    """假设备：记录 close() 调用；``fail_close`` 模拟坏驱动。"""

    def __init__(self, name='fake_device', fail_close=False):
        self.name = name
        self.close_calls = 0
        self.fail_close = fail_close

    def close(self):
        """记录关闭；fail_close 时抛 OSError（验证 close 不会因此中断）。"""
        self.close_calls += 1
        if self.fail_close:
            raise OSError('%s close failed' % self.name)


class LazyTarget:
    """惰性代理指向的假驱动：记录是否被构造/关闭。"""

    constructed = 0

    def __init__(self, **kwargs):
        type(self).constructed += 1
        self.closed = False

    def close(self):
        """记录关闭。"""
        self.closed = True


class FakeCommandClient:
    """CommandAttr 用的假命令客户端：只记录，不建 ZMQ 连接。"""

    def __init__(self, device):
        self.device = device
        self.sent = []
        self.closed = False

    def send(self, command, data):
        """记录一次命令发送。"""
        self.sent.append((command, data))

    def close(self):
        """记录关闭。"""
        self.closed = True


class FakeDriver:
    """假驱动：记录 genericAttr 调用到的值与附加参数。"""

    def __init__(self):
        self.calls = []

    def set_voltage(self, value, channel=None, **kwargs):
        """记录一次函数调用。"""
        self.calls.append((value, channel))


class FailingDriver:
    """总是抛错的假驱动（模拟设备离线/驱动异常）。"""

    def set_voltage(self, value):
        """直接抛 ValueError。"""
        raise ValueError('supply is offline')


class FakeExperiment:
    """实验替身：只提供属性描述符读写所需的运行时接口。"""

    name = 'FakeExperiment'

    def __init__(self, controller=None, simulating=False,
                 simulate_cmd_stack=False):
        self.controller = controller
        self.parsing = False
        self._building = False
        self._simulate_cmd_stack = simulate_cmd_stack
        self._simulating = simulating
        self._props = FakeProps()
        self._cmd_stack = []
        self._simulated_cmd_stack = []
        self._missing_gaugefiles = set()

    def _add_realtime_cmd(self, name, value):
        """记录一条实时命令（模拟 Experiment._add_realtime_cmd）。"""
        self._cmd_stack.append([0, name, value])

    def _add_simulated_cmd(self, name, value):
        """记录一条模拟命令（模拟 Experiment._add_simulated_cmd）。"""
        self._simulated_cmd_stack.append([0, name, value])


def _harness_class():
    """新建一个实验类：每个测试独享，避免描述符挂到共享类上互相污染。"""
    return type('RuntimeHarness', (FakeExperiment,), {})


def _blank_experiment(**attributes):
    """构造一个只填了 ``__dict__`` 的 Experiment 实例（不跑 __init__）。

    用于 close() 测试：``Experiment.close()`` 只读 ``self.__dict__``，
    不需要真的连 Properties/ZMQ。
    """
    exp = object.__new__(Experiment)
    exp.__dict__.update(attributes)
    return exp


class TestCloseDeviceTeardown(unittest.TestCase):
    """P1-3：Experiment.close() 必须回收设备句柄，而不只是 Properties。"""

    def test_close_closes_devices_properties_and_data_client(self):
        props = FakeProps()
        dataq = FakeDataClient()
        device = FakeDevice()
        exp = _blank_experiment(_props=props, _dataq=dataq, camera=device,
                                no_close_object=object())
        exp.close()
        self.assertEqual(device.close_calls, 1, '设备 close() 必须被调用')
        self.assertEqual(props.close_calls, 1, 'Properties 仍要关闭')
        self.assertEqual(dataq.close_calls, 1, 'DataClient 仍要关闭')

    def test_close_is_idempotent(self):
        props = FakeProps()
        dataq = FakeDataClient()
        device = FakeDevice()
        exp = _blank_experiment(_props=props, _dataq=dataq, camera=device)
        exp.close()
        exp.close()  # 窗口 closeEvent 与 Browser 清理可能各调一次
        self.assertEqual(device.close_calls, 1, '重复 close 不得再次关设备')
        self.assertEqual(dataq.close_calls, 1, '重复 close 不得再次关 DataClient')
        self.assertEqual(props.close_calls, 1, '重复 close 不得再次关 Properties')

    def test_close_continues_after_one_device_failure(self):
        props = FakeProps()
        good = FakeDevice('good')
        bad = FakeDevice('bad', fail_close=True)
        exp = _blank_experiment(_props=props, _dataq=FakeDataClient(),
                                good=good, bad=bad)
        with mock.patch.object(experiment_module, 'send_warning') as warning:
            exp.close()  # 单个设备失败只 warning，绝不向外抛
        self.assertEqual(bad.close_calls, 1)
        self.assertEqual(good.close_calls, 1, '坏设备不能让其它设备漏关')
        self.assertEqual(props.close_calls, 1)
        warning.assert_called_once()
        self.assertIn('bad', warning.call_args[0][0],
                      'warning 要指明哪个设备关闭失败')

    def test_close_skips_unresolved_lazy_device(self):
        LazyTarget.constructed = 0
        exp = _blank_experiment(_props=FakeProps(), _dataq=FakeDataClient())
        lazy = _LazyDevice(LazyTarget, {}, 'lazy_dev', exp)
        exp.__dict__['lazy_dev'] = lazy
        exp.close()
        self.assertEqual(LazyTarget.constructed, 0,
                         '从未解析的惰性设备不能被 close() 触发构造')

    def test_close_closes_resolved_lazy_device(self):
        LazyTarget.constructed = 0
        exp = _blank_experiment(_props=FakeProps(), _dataq=FakeDataClient())
        lazy = _LazyDevice(LazyTarget, {}, 'lazy_dev', exp)
        exp.__dict__['lazy_dev'] = lazy
        real = lazy.resolve()
        exp.close()
        self.assertTrue(real.closed, '已解析的惰性设备必须被 close()')

    def test_close_dedupes_same_device_under_two_names(self):
        device = FakeDevice()
        exp = _blank_experiment(_props=FakeProps(), _dataq=FakeDataClient(),
                                controller=device, adwin=device)
        exp.close()
        self.assertEqual(device.close_calls, 1, '同一对象的别名只关一次')


class TestDeviceErrorsAreVisible(unittest.TestCase):
    """P1-4：设备操作失败必须抛 ExperimentDeviceError，而不是静默吞掉。"""

    def test_generic_attr_failure_raises(self):
        cls = _harness_class()
        exp = cls()
        exp.hv = FailingDriver()
        attribute = genericAttr(exp)
        attribute.set_class('hv')
        attribute.set_function('set_voltage')
        attribute.__name__ = 'hv_voltage'
        setattr(cls, 'hv_voltage', attribute)
        with mock.patch.object(attributes_module, 'send_error') as send_error, \
                mock.patch.object(attributes_module.traceback, 'print_exc'):
            with self.assertRaises(ExperimentDeviceError) as caught:
                exp.hv_voltage = 100.0
        send_error.assert_called_once()
        self.assertIsInstance(caught.exception, RuntimeError,
                              'GUI 面板按 RuntimeError 处理写入被拒绝')
        self.assertIsInstance(caught.exception.__cause__, ValueError,
                              '原始驱动异常要保留在异常链里')

    def test_generic_attr_simulated_does_not_raise(self):
        cls = _harness_class()
        exp = cls(simulate_cmd_stack=True)
        exp.hv = FailingDriver()
        attribute = genericAttr(exp)
        attribute.set_class('hv')
        attribute.set_function('set_voltage')
        attribute.__name__ = 'hv_voltage'
        setattr(cls, 'hv_voltage', attribute)
        exp.hv_voltage = 100.0  # 模拟运行只记录，不调用驱动
        self.assertEqual(exp._simulated_cmd_stack, [[0, 'hv_voltage', 100.0]])

    def test_dac_realtime_submit_failure_raises(self):
        ctrl = FakeController(
            submit_error=AssertionError('command table too long'))
        cls = _harness_class()
        exp = cls(ctrl)
        setattr(cls, 'dac_1', DAC(exp, channel=1))
        with mock.patch.object(attributes_module, 'send_error') as send_error:
            with self.assertRaises(ExperimentDeviceError):
                exp.dac_1 = 1.0
        send_error.assert_called_once()
        self.assertEqual(len(ctrl.commands), 1, '命令仍应已 push 给控制器')

    def test_dac_realtime_driver_error_raises(self):
        driver_error = _adwin_driver_error()
        if driver_error is Exception:
            self.skipTest('本环境没有 AdWinDriverError（退化为通用异常）')
        ctrl = FakeController(submit_error=driver_error('board offline'))
        cls = _harness_class()
        exp = cls(ctrl)
        setattr(cls, 'dac_1', DAC(exp, channel=1))
        with mock.patch.object(attributes_module, 'send_error'):
            with self.assertRaises(ExperimentDeviceError):
                exp.dac_1 = 1.0

    def test_realtime_submit_failure_simulated_does_not_raise(self):
        ctrl = FakeController(
            submit_error=AssertionError('command table too long'))
        cls = _harness_class()
        exp = cls(ctrl, simulating=True)
        setattr(cls, 'dac_1', DAC(exp, channel=1))
        with mock.patch.object(attributes_module, 'send_error') as send_error:
            exp.dac_1 = 1.0  # 模拟运行：提交失败只上报，不标 Failed
        send_error.assert_called_once()
        self.assertEqual(len(ctrl.commands), 1)

    def test_run_failure_uses_framework_cleanup_and_propagates(self):
        class RunHarness(FakeExperiment):
            """模拟一次 run() 里的设备异常，验证 _start_run 的失败链路。"""

            def __init__(self):
                super().__init__(FakeController())
                self.cleaned = False
                self.published = []

            def _publish_startvalues(self, task):
                self.published.append('start')

            def _publish_endvalues(self):
                self.published.append('end')

            def _cleanup_after_failed_run(self):
                self.cleaned = True

            def run(self):
                raise ExperimentDeviceError('device failed during run')

        exp = RunHarness()
        with self.assertRaises(ExperimentDeviceError):
            Experiment._start_run(exp, task=7)
        self.assertTrue(exp.cleaned,
                        'run() 失败后必须走 _cleanup_after_failed_run')
        self.assertEqual(exp.published, ['start', 'end'])


class TestDescriptorsArePerInstance(unittest.TestCase):
    """P1-4：描述符挂在类上，但 experiment/controller 必须按实例解析。"""

    def test_dac_controller_follows_instance(self):
        cls = _harness_class()
        ctrl1, ctrl2 = FakeController(), FakeController()
        exp1, exp2 = cls(ctrl1), cls(ctrl2)
        # 模拟两个窗口各自的 build()：描述符挂到同一个实验类上，后者覆盖前者
        setattr(cls, 'dac_1', DAC(exp1, channel=1))
        setattr(cls, 'dac_1', DAC(exp2, channel=1))

        exp1.dac_1 = 1.0
        exp2.dac_1 = 2.0

        self.assertEqual([c[1] for c in ctrl1.commands], ['dac_1'])
        self.assertEqual([c[1] for c in ctrl2.commands], ['dac_1'])
        self.assertEqual(ctrl1.commands[0][2], int((1.0 + 10) * 2 ** 16 / 20))
        self.assertEqual(ctrl2.commands[0][2], int((2.0 + 10) * 2 ** 16 / 20))

    def test_ttl_controller_follows_instance(self):
        cls = _harness_class()
        ctrl1, ctrl2 = FakeController(), FakeController()
        exp1, exp2 = cls(ctrl1), cls(ctrl2)
        setattr(cls, 'ttl_1', TTLOut(exp1, channel=1))
        setattr(cls, 'ttl_1', TTLOut(exp2, channel=1))

        exp1.ttl_1 = True
        exp2.ttl_1 = False

        self.assertEqual(len(ctrl1.commands), 1)
        self.assertEqual(ctrl1.commands[0][1], 'set_bits')
        self.assertEqual(len(ctrl2.commands), 1)
        self.assertEqual(ctrl2.commands[0][1], 'clear_bits')

    def test_generic_attr_uses_instance_driver(self):
        cls = _harness_class()
        exp1, exp2 = cls(), cls()
        driver1, driver2 = FakeDriver(), FakeDriver()
        exp1.ps = driver1
        exp2.ps = driver2
        # 同一份 device_db 条目：两个实例各自 build() 出同参数描述符
        setattr(cls, 'ps_voltage', genericAttr(exp1, function_kwargs={}))
        setattr(cls, 'ps_voltage', genericAttr(exp2, function_kwargs={}))
        for attribute in (cls.__dict__['ps_voltage'],):
            attribute.set_class('ps')
            attribute.set_function('set_voltage')

        exp1.ps_voltage = 1.5
        exp2.ps_voltage = 2.5

        self.assertEqual(driver1.calls, [(1.5, None)])
        self.assertEqual(driver2.calls, [(2.5, None)])

    def test_descriptors_keep_no_experiment_reference(self):
        exp = FakeExperiment()
        attributes = (
            DAC(exp, channel=1),
            TTLOut(exp, channel=1),
            genericAttr(exp),
        )
        for attribute in attributes:
            with self.subTest(attribute=type(attribute).__name__):
                self.assertNotIn(
                    'experiment', vars(attribute),
                    '%s 不得保存 experiment 引用（会跨实例串台）'
                    % type(attribute).__name__)
                self.assertNotIn(
                    'controller', vars(attribute),
                    '%s 不得保存 controller 引用（会跨实例串台）'
                    % type(attribute).__name__)

    def test_command_attr_writes_go_to_own_simulated_stack(self):
        cls = _harness_class()
        exp1 = cls(simulate_cmd_stack=True)
        exp2 = cls(simulate_cmd_stack=True)
        with mock.patch.object(attributes_module, 'CommandClient',
                               FakeCommandClient):
            attribute = CommandAttr(exp1, device='cmd_test', command='SET {}',
                                    defaultval=0.0)
        attribute.__name__ = 'cmd_attr'
        setattr(cls, 'cmd_attr', attribute)
        exp1.cmd_attr = 11
        exp2.cmd_attr = 22
        self.assertEqual(exp1._simulated_cmd_stack, [[0, 'cmd_attr', 11]])
        self.assertEqual(exp2._simulated_cmd_stack, [[0, 'cmd_attr', 22]])
        self.assertEqual(attribute._comq.device, 'cmd_test')
        self.assertEqual(attribute._comq.sent, [],
                         '模拟运行不得把命令真发给设备')


if __name__ == '__main__':
    unittest.main()
