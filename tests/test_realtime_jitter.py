"""Python 实时回放抖动度量测试（不接硬件、不真的 sleep）。

背景（「致命问题清单」第 5 条的非致命优化）：``Experiment._run_realtime_stack``
在 Python 线程里用 ``time.sleep(t + starttime - time.time())`` 等到目标时刻再
``setattr``。这是**非硬实时**：唤醒精度受 OS 调度影响（典型 ms 级抖动）；
µs 级、多通道严格并发时序必须走控制器的硬件序列（Adwin 等）。

本次只加“旁路观测”：
* 逐条用 ``time.monotonic()`` 量“实际执行 - 目标时刻”的滞后，取 ``max_lag``；
* 超过 ``Experiment.REALTIME_JITTER_WARN_S``（默认 2 ms）时**每次 run 只发
  一条** ``send_warning``，内容含最大 lag（ms）、命令条数与“不是硬实时 /
  µs 级请用 Adwin 硬件序列”的提示；
* 记录 ``_realtime_max_lag`` / ``_realtime_command_count`` 供调试/测试读取；
* 模拟模式（``_simulate_cmd_stack``）跳过测量与告警。

**时序语义冻结**：sleep 目标表达式与命令顺序不得改变——测试用假时钟断言
``sleep()`` 收到的正是原来的 ``t + starttime - time.time()``。
"""

import os
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from quatm.experiment import experiment as experiment_module  # noqa: E402
from quatm.experiment.experiment import Experiment  # noqa: E402

#: 测试里固定的 starttime 基准（wall 时间）；sleep 目标应等于 t。
START_WALL = 1000.0

#: 阈值（与生产默认一致）与用于模拟“迟到”的额外滞后。
THRESHOLD = 0.002


class _FakeClock(object):
    """假时钟：``time``/``monotonic``/``sleep`` 三件套，可注入唤醒滞后。"""

    def __init__(self, lag_per_sleep=0.0):
        self.wall = START_WALL
        self.mono = 50.0
        self.lag_per_sleep = lag_per_sleep
        self.sleep_calls = []

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        if seconds < 0:
            # 与真实 time.sleep 一致：负值抛 ValueError（现有行为，不改）
            raise ValueError('sleep length must be non-negative')
        self.wall += seconds
        # 模拟被 OS 延迟唤醒：实际耗时比期望多 lag_per_sleep
        self.mono += seconds + self.lag_per_sleep


class _FakeExperiment(object):
    """只实现 ``_run_realtime_stack`` 需要的属性（不建 Experiment 实例）。"""

    REALTIME_JITTER_WARN_S = THRESHOLD

    def __init__(self, commands, simulate=False):
        self._cmd_stack = [list(item) for item in commands]
        self._simulate_cmd_stack = simulate


class _ExplodingExperiment(_FakeExperiment):
    """第 2 条命令 setattr 时抛异常：验证 finally/异常传播语义。"""

    @property
    def boom(self):
        raise AttributeError("can't set attribute")


class TestRealtimeJitterMeasurement(unittest.TestCase):
    """抖动度量 + 告警行为。"""

    def setUp(self):
        self._warnings = []

    def _run(self, experiment, clock):
        """在假时钟/假 send_warning 下执行真实 ``_run_realtime_stack``。"""
        run = types.MethodType(Experiment._run_realtime_stack, experiment)
        with mock.patch.object(experiment_module, 'time', clock), \
                mock.patch.object(experiment_module, 'starttime', START_WALL), \
                mock.patch.object(experiment_module, 'tr', lambda text: text), \
                mock.patch.object(experiment_module, 'send_warning',
                                  self._warnings.append):
            run()

    def _clock(self, lag):
        return _FakeClock(lag_per_sleep=lag)

    def test_class_default_threshold(self):
        """默认阈值是 2 ms，且可被子类/实例覆盖。"""
        self.assertEqual(Experiment.REALTIME_JITTER_WARN_S, THRESHOLD)

    def test_no_warning_below_threshold(self):
        """正常情况（0.5 ms 抖动 < 2 ms）：不发 warning，但记录度量。"""
        clock = self._clock(0.0005)
        experiment = _FakeExperiment([
            [0.0, 'dac_0', 1.0],
            [0.001, 'dac_1', 2.0],
        ])
        self._run(experiment, clock)

        self.assertEqual(self._warnings, [])
        self.assertEqual(experiment._realtime_command_count, 2)
        self.assertAlmostEqual(experiment._realtime_max_lag, 0.0005)
        # 时序语义冻结：sleep 目标仍是 t + starttime - time.time()（= t）
        self.assertEqual(len(clock.sleep_calls), 2)
        self.assertAlmostEqual(clock.sleep_calls[0], 0.0)
        self.assertAlmostEqual(clock.sleep_calls[1], 0.001)
        # 命令按时间戳顺序执行
        self.assertEqual(experiment.dac_0, 1.0)
        self.assertEqual(experiment.dac_1, 2.0)
        self.assertEqual(experiment._cmd_stack, [])

    def test_warning_when_lag_over_threshold(self):
        """10 ms 抖动 > 2 ms：发一条 warning，含 ms 数值/条数/硬实时提示。"""
        clock = self._clock(0.010)
        experiment = _FakeExperiment([
            [0.0, 'dac_0', 1.0],
            [0.001, 'dac_1', 2.0],
        ])
        self._run(experiment, clock)

        self.assertEqual(len(self._warnings), 1)
        message = self._warnings[0]
        self.assertIn('10.0 ms', message)
        self.assertIn('2 commands', message)
        self.assertIn('NOT hard realtime', message)
        self.assertIn('Adwin hardware sequence', message)
        self.assertAlmostEqual(experiment._realtime_max_lag, 0.010)
        self.assertEqual(experiment._realtime_command_count, 2)

    def test_at_most_one_warning_per_run(self):
        """3 条命令全部超阈值：每次 run 仍只发 1 条（不刷屏）。"""
        clock = self._clock(0.020)
        commands = [[0.0, 'a', 1], [0.001, 'b', 2], [0.002, 'c', 3]]
        experiment = _FakeExperiment(commands)
        self._run(experiment, clock)
        self.assertEqual(len(self._warnings), 1)

        # 第二次 run（新的一次 starttime/时钟）再发且仅发一条：一次 run 一条
        experiment._cmd_stack = [list(item) for item in commands]
        self._run(experiment, self._clock(0.020))
        self.assertEqual(len(self._warnings), 2)

    def test_threshold_override_disables_warning(self):
        """阈值可被实例属性覆盖（测试注入/项目自定义）。"""
        clock = self._clock(0.010)
        experiment = _FakeExperiment([[0.0, 'dac_0', 1.0]])
        experiment.REALTIME_JITTER_WARN_S = 0.5
        self._run(experiment, clock)
        self.assertEqual(self._warnings, [])
        self.assertAlmostEqual(experiment._realtime_max_lag, 0.010)

    def test_simulated_mode_skips_measurement_and_warning(self):
        """模拟模式（_simulate_cmd_stack）不 sleep、不度量、不告警。"""
        clock = self._clock(0.010)
        experiment = _FakeExperiment(
            [[0.0, 'dac_0', 1.0], [0.001, 'dac_1', 2.0]], simulate=True)
        self._run(experiment, clock)

        self.assertEqual(clock.sleep_calls, [])       # 不真实回放
        self.assertEqual(self._warnings, [])
        self.assertEqual(experiment._realtime_max_lag, 0.0)
        self.assertEqual(experiment._realtime_command_count, 0)
        # 模拟期命令仍然通过 setattr 记录
        self.assertEqual(experiment.dac_0, 1.0)
        self.assertEqual(experiment._cmd_stack, [])

    def test_empty_stack_records_zero(self):
        """空命令栈：不 sleep、不发 warning、度量清零。"""
        clock = self._clock(0.010)
        experiment = _FakeExperiment([])
        self._run(experiment, clock)
        self.assertEqual(clock.sleep_calls, [])
        self.assertEqual(self._warnings, [])
        self.assertEqual(experiment._realtime_command_count, 0)
        self.assertEqual(experiment._realtime_max_lag, 0.0)

    def test_warning_failure_does_not_affect_run(self):
        """观测本身失败（send_warning 抛异常）不能影响回放/异常传播。"""
        clock = self._clock(0.010)
        experiment = _FakeExperiment([[0.0, 'dac_0', 1.0]])
        run = types.MethodType(Experiment._run_realtime_stack, experiment)
        with mock.patch.object(experiment_module, 'time', clock), \
                mock.patch.object(experiment_module, 'starttime', START_WALL), \
                mock.patch.object(experiment_module, 'tr', lambda text: text), \
                mock.patch.object(experiment_module, 'send_warning',
                                  side_effect=RuntimeError('message hub down')):
            run()   # 不应抛异常
        self.assertEqual(experiment.dac_0, 1.0)
        self.assertEqual(experiment._cmd_stack, [])
        self.assertAlmostEqual(experiment._realtime_max_lag, 0.010)


class TestRealtimeJitterExceptionSemantics(unittest.TestCase):
    """命令栈异常时的既有 finally 语义：清空栈 + 原异常照常传播。"""

    def setUp(self):
        self._warnings = []

    def _run(self, experiment, clock):
        run = types.MethodType(Experiment._run_realtime_stack, experiment)
        with mock.patch.object(experiment_module, 'time', clock), \
                mock.patch.object(experiment_module, 'starttime', START_WALL), \
                mock.patch.object(experiment_module, 'tr', lambda text: text), \
                mock.patch.object(experiment_module, 'send_warning',
                                  self._warnings.append):
            run()

    def test_exception_clears_stack_and_propagates(self):
        clock = _FakeClock()
        experiment = _ExplodingExperiment([
            [0.0, 'dac_0', 1.0],
            [0.0, 'boom', 2.0],
        ])
        with self.assertRaises(AttributeError):
            self._run(experiment, clock)
        self.assertEqual(experiment._cmd_stack, [])
        self.assertEqual(self._warnings, [])
        self.assertEqual(experiment._realtime_command_count, 2)

    def test_warning_does_not_mask_exception(self):
        """抖动超阈值 + 命令异常：warning 照发，但原异常不能被吞掉。"""
        clock = _FakeClock(lag_per_sleep=0.010)
        experiment = _ExplodingExperiment([
            [0.0, 'dac_0', 1.0],
            [0.0, 'boom', 2.0],
        ])
        with self.assertRaises(AttributeError):
            self._run(experiment, clock)
        self.assertEqual(len(self._warnings), 1)
        self.assertIn('NOT hard realtime', self._warnings[0])
        self.assertEqual(experiment._cmd_stack, [])


if __name__ == '__main__':
    unittest.main()
