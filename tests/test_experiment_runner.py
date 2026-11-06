# -*- coding: utf-8 -*-
"""隔离子进程实验 runner 的协议/语义回归测试（task-37）。

覆盖：

1. **执行语义与 GUI ExperimentManager 一致**：``start_measurement`` →
   每 repetition ``start_sequence`` → 每 run 写扫描值/设 ``_run``/调
   ``_start_run`` → ``end_sequence`` → ``end_measurement`` → ``close``；
   ``nReps`` 是“额外重复次数”（实际 sequence 数 = nReps+1，与 GUI
   ``nRepsQSB`` 缺省 0 = 跑一次一致）；``scansequence`` 决定取值顺序。
2. **事件协议**：stdout 只有 JSON 行；``started``/``run_started``/``run_done``/
   ``finished``/``error``；``run``/``rep``/``task`` 字段齐全。
3. **控制通道**：``pause``/``resume`` 在 run 之间生效（不打断当前 run）；
   ``terminate`` 优雅收尾（end_sequence/end_measurement/close）后退出码 0。
4. **失败语义**：``run()`` 抛异常 → ``error`` 事件带 traceback、退出码 1、
   ``close()`` 仍被调用；坏任务 JSON → 明确 ``error`` + 退出码 2。
5. **参数 schema 校验**：未知 ``args``/``scanpars`` 被拒绝（错误信息含未知名字
   与实验名），设备名允许出现在 ``args``；无登记表的对象 fail-open。
6. **保存回执接入点**（task-36）：``off`` 不调用、``warn``/``strict`` 的
   API 未就绪/失败语义（纯函数单测，子进程不依赖 API）。

测试用临时项目 + 假实验模块（覆盖 ``__init__`` 不连 Properties/ZMQ 服务），
子进程方式启动 ``python -m quatm.experiment.runner``，不接硬件、不连服务器。
约定见 ``tests/__init__.py``：stdlib unittest、不新增依赖。
"""

import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import quatm.experiment.runner as runner

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 假实验模块：``Experiment`` 子类，但覆盖 ``__init__`` 不连 Properties/DataClient
#: （真实 ``Experiment.__init__`` 会连 ZMQ 服务；本测试只验证协议/执行语义）。
#: 生命周期各步写入 ``QUATM_RUNNER_TEST_LOG`` 指向的 JSONL 文件，供父进程断言
#: 调用顺序与参数值。
DUMMY_MODULE = '''# -*- coding: utf-8 -*-
"""runner 测试用假实验（不接硬件/不连服务）。"""
import json
import os
import time

from quatm.experiment.experiment import Experiment, NumberValue


def _log(kind, **extra):
    """把一次生命周期调用追加到测试日志（JSONL，父进程读取断言）。"""
    path = os.environ.get('QUATM_RUNNER_TEST_LOG')
    if not path:
        return
    record = {'kind': kind}
    record.update(extra)
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\\n')


class Dummy(Experiment):
    """最小实验：只记录调用；不连接任何服务/设备。"""

    # 覆盖 Experiment 的 PropertyAttribute 描述符，避免 __init__ 被跳过后写属性报错。
    _run = 0
    _repetition = 0

    def __init__(self):
        self.name = type(self).__name__
        self.expName = self.name
        self._props = None
        self._arguments = []
        self._declared_arguments = {}
        self._device_db = {'dev': {'type': 'attr'}}
        self._attributes = {}

    def build(self):
        _log('build')
        self.setattr_argument('p', NumberValue(ndecimals=3, step=0.1, value=0.0))

    def start_measurement(self):
        _log('start_measurement')

    def end_measurement(self):
        _log('end_measurement')

    def start_sequence(self):
        _log('start_sequence', rep=self._repetition)

    def end_sequence(self):
        _log('end_sequence', rep=self._repetition)

    def _cleanup_after_failed_run(self):
        _log('cleanup')

    def close(self):
        _log('close')

    def _start_run(self, task=None):
        _log('run', run=self._run, rep=self._repetition, task=task,
             p=getattr(self, 'p', None))
        sleep_s = float(os.environ.get('QUATM_RUNNER_TEST_SLEEP', '0') or 0)
        if sleep_s:
            time.sleep(sleep_s)
        fail_run = os.environ.get('QUATM_RUNNER_TEST_FAIL')
        if fail_run is not None and str(self._run) == str(fail_run):
            raise RuntimeError('boom at run %s' % fail_run)
'''


class _EventReader:
    """后台读取子进程 stdout 的 JSON 事件行（跨平台：不用 select）。"""

    def __init__(self, stream):
        self._stream = stream
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        """逐行读取；EOF 时结束。"""
        try:
            for line in self._stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.queue.put(json.loads(line))
                except Exception:  # noqa: BLE001 - 非法行也要让测试看见
                    self.queue.put({'event': '<non-json>', 'raw': line})
        except Exception:  # noqa: BLE001 - 管道关闭
            pass

    def wait_for(self, event_name, timeout=60.0):
        """等待某个事件；收集途中事件并返回 ``(事件, 途中事件列表)``。"""
        seen = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError('timeout waiting for %r; seen=%r'
                                     % (event_name, seen))
            try:
                item = self.queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            event = item.get('event')
            if event == '<non-json>':
                raise AssertionError('non-JSON stdout line: %r' % item['raw'])
            seen.append(item)
            if event == event_name:
                return item, seen

    def assert_no_event(self, names, duration=0.4):
        """断言 ``duration`` 秒内没有出现给定事件（用于验证 pause 真的停住）。"""
        deadline = time.time() + duration
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                item = self.queue.get(timeout=remaining)
            except queue.Empty:
                return
            if item.get('event') in names:
                raise AssertionError('unexpected event %r' % item)


class _RunnerTestCase(unittest.TestCase):
    """提供临时项目 + 假实验模块 + 子进程启动工具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = pathlib.Path(self._tmp.name)
        (self.project / 'experiments').mkdir()
        (self.project / 'experiments' / 'dummy.py').write_text(
            DUMMY_MODULE, encoding='utf-8')
        self.trace_path = self.project / 'trace.jsonl'

    def _env(self, **extra):
        """子进程环境：项目根、PYTHONPATH、跳过首次运行、测试日志路径。"""
        env = os.environ.copy()
        existing = env.get('PYTHONPATH')
        env['PYTHONPATH'] = (str(REPO_ROOT) + os.pathsep + existing
                             if existing else str(REPO_ROOT))
        env['QUATM_PROJECT_ROOT'] = str(self.project)
        env['QUATM_SKIP_FIRST_RUN'] = '1'
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['PYTHONUTF8'] = '1'
        # 消息文案固定英文，断言不受机器语言（zh/en）影响。
        env['QUATM_LANG'] = 'en'
        env['QUATM_RUNNER_TEST_LOG'] = str(self.trace_path)
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def _command(self, task=None):
        """构造 runner 子进程命令；task 不为 None 时用 --task 传任务。"""
        command = [sys.executable, '-m', 'quatm.experiment.runner', 'run',
                   '--project', str(self.project), '--file', 'dummy.py',
                   '--class', 'Dummy']
        if task is not None:
            command += ['--task', json.dumps(task)]
        return command

    def _run(self, task, env_extra=None, stdin_data='', timeout=180):
        """一次性跑完子进程并返回 ``CompletedProcess``。"""
        return subprocess.run(
            self._command(task), cwd=str(self.project),
            env=self._env(**(env_extra or {})), input=stdin_data,
            capture_output=True, text=True, timeout=timeout)

    def _events(self, stdout):
        """把 stdout 解析成事件列表；遇到非 JSON 行直接失败。"""
        events = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except Exception as exc:  # noqa: BLE001
                self.fail('non-JSON stdout line %r (%s)' % (line, exc))
        return events

    def _trace(self):
        """读假实验写入的生命周期日志。"""
        if not self.trace_path.exists():
            return []
        records = []
        for line in self.trace_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def _kinds(self):
        """返回 trace 里的 kind 序列。"""
        return [record['kind'] for record in self._trace()]

    def _popen(self, task, env_extra=None):
        """以交互方式启动子进程（stdin 用于控制命令），返回 proc/事件读取器。"""
        proc = subprocess.Popen(
            self._command(task), cwd=str(self.project),
            env=self._env(**(env_extra or {})), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1)
        reader = _EventReader(proc.stdout)
        # stderr 必须持续排空，否则大量输出可能把管道写满卡住子进程。
        stderr_chunks = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read()),
            daemon=True)
        stderr_thread.start()
        proc._quatm_stderr_chunks = stderr_chunks  # noqa: SLF001 - 测试辅助
        proc._quatm_stderr_thread = stderr_thread  # noqa: SLF001
        return proc, reader

    def _send(self, proc, command):
        """向子进程 stdin 发一条控制命令。"""
        proc.stdin.write(json.dumps({'cmd': command}) + '\n')
        proc.stdin.flush()


class TestRunnerProtocol(_RunnerTestCase):
    """正常完成：事件顺序、scan 顺序、生命周期调用。"""

    def test_success_events_and_scan_order(self):
        """3 个 run、scansequence 打乱：事件顺序/字段与写入参数都正确。"""
        task = {
            'task': 7,
            'args': {'p': 0.25},
            'scanpars': ['p'],
            'scanvals': [[1.0], [2.0], [3.0]],
            'scansequence': [2, 0, 1],
            'nRuns': 3,
            'nReps': 0,
            'scan_name': 'scan1',
        }
        proc = self._run(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        names = [event['event'] for event in events]
        self.assertEqual(names, [
            'started',
            'run_started', 'run_done',
            'run_started', 'run_done',
            'run_started', 'run_done',
            'finished',
        ])
        started = events[0]
        self.assertEqual(started['task'], 7)
        self.assertEqual(started['task'], events[1]['task'],
                         '事件都要带 task 字段便于 manager 映射')
        self.assertEqual(started['experiment'], 'Dummy')
        self.assertEqual(started['class'], 'Dummy')
        self.assertEqual(started['protocol'], 1)
        self.assertEqual(started['n_runs'], 3)
        self.assertEqual(started['n_reps'], 0)
        self.assertEqual(started['scanpars'], ['p'])
        self.assertEqual(started['scan_name'], 'scan1')
        run_starts = [event for event in events if event['event'] == 'run_started']
        self.assertEqual([(event['run'], event['rep']) for event in run_starts],
                         [(0, 0), (1, 0), (2, 0)])
        self.assertTrue(events[-1]['ok'])

        trace = self._trace()
        kinds = [record['kind'] for record in trace]
        self.assertEqual(kinds[0], 'build')
        self.assertEqual(kinds[1], 'start_measurement')
        self.assertEqual(kinds[2], 'start_sequence')
        self.assertEqual(kinds[-3:], ['end_sequence', 'end_measurement', 'close'])
        runs = [record for record in trace if record['kind'] == 'run']
        self.assertEqual([record['p'] for record in runs],
                         [3.0, 1.0, 2.0],
                         'scansequence 决定取值顺序（与 GUI _runloop 相同）')
        self.assertTrue(all(record['task'] == 7 for record in runs))
        self.assertTrue(all(record['rep'] == 0 for record in runs))
        self.assertEqual([record['run'] for record in runs], [0, 1, 2])
        self.assertEqual(kinds.count('close'), 1, 'close 只能调一次')

    def test_repetitions_match_gui_extra_repeats_semantics(self):
        """nReps 与 GUI 一致是“额外重复次数”：nReps=2 → 3 个 sequence。"""
        task = {'task': 1, 'nRuns': 1, 'nReps': 2}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        started = events[0]
        self.assertEqual(started['n_reps'], 2)
        reps = [event['rep'] for event in events if event['event'] == 'run_done']
        self.assertEqual(reps, [0, 1, 2])
        trace = self._trace()
        sequences = [record for record in trace if record['kind'] == 'start_sequence']
        self.assertEqual([record['rep'] for record in sequences], [0, 1, 2])
        self.assertEqual(sum(1 for r in trace if r['kind'] == 'run'), 3)
        self.assertEqual([record['kind'] for record in trace][-3:],
                         ['end_sequence', 'end_measurement', 'close'])

    def test_no_control_flag_runs_without_stdin(self):
        """``--no-control``：不读 stdin 控制命令，任务照常完成（手动运行用）。"""
        command = self._command({'task': 0, 'nRuns': 1, 'nReps': 0})
        command.append('--no-control')
        proc = subprocess.run(
            command, cwd=str(self.project), env=self._env(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._events(proc.stdout)[-1]['event'], 'finished')

    def test_args_are_applied_before_run(self):
        """args 在 start_measurement 前写入（与 manager 一致）。"""
        task = {'task': 0, 'args': {'p': 1.25}, 'nRuns': 1, 'nReps': 0}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        runs = [r for r in self._trace() if r['kind'] == 'run']
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]['p'], 1.25)


class TestRunnerFailures(_RunnerTestCase):
    """失败语义：error 事件/traceback/退出码 1、close 仍执行；坏 JSON → 退出码 2。"""

    def test_run_exception_reports_error_and_closes(self):
        """``_start_run`` 抛异常：error+traceback、退出码 1、cleanup+close 都调用。"""
        task = {'task': 3, 'nRuns': 3, 'nReps': 0}
        proc = self._run(task, env_extra={'QUATM_RUNNER_TEST_FAIL': '1'})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        kinds = [event['event'] for event in events]
        self.assertEqual(kinds[-1], 'error')
        error = events[-1]
        self.assertIn('boom at run 1', error['message'])
        self.assertIn('RuntimeError', error['traceback'])
        self.assertIn('run_started', kinds)
        self.assertNotIn('finished', kinds)
        trace_kinds = self._kinds()
        self.assertIn('cleanup', trace_kinds,
                      '失败后要调 _cleanup_after_failed_run（同 manager）')
        self.assertIn('close', trace_kinds)
        self.assertEqual(trace_kinds[-1], 'close', 'close 必须最后执行')
        runs = [r for r in self._trace() if r['kind'] == 'run']
        self.assertEqual(len(runs), 2, 'run 0 完成、run 1 失败后停止')

    def test_bad_task_json_is_protocol_error(self):
        """stdin 首行非 JSON：error 事件 + 退出码 2。"""
        proc = subprocess.run(
            self._command(None), cwd=str(self.project), env=self._env(),
            input='this is not json\n', capture_output=True, text=True,
            timeout=180)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'error')
        self.assertIn('invalid task JSON', events[0]['message'])
        self.assertEqual(self._trace(), [], '坏任务不应加载/执行实验')

    def test_task_json_must_be_object(self):
        """任务 JSON 不是对象 → 协议错误退出码 2。"""
        proc = subprocess.run(
            self._command(None), cwd=str(self.project), env=self._env(),
            input='[1, 2, 3]\n', capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        self.assertEqual(events[0]['event'], 'error')
        self.assertIn('must be an object', events[0]['message'])


class TestRunnerControl(_RunnerTestCase):
    """控制通道：pause/resume 在 run 之间生效；terminate 优雅收尾退出码 0。"""

    def test_pause_blocks_until_resume(self):
        """pause 后不再开始新 run；resume 后继续到完成。"""
        task = {'task': 9, 'nRuns': 4, 'nReps': 0}
        proc, reader = self._popen(
            task, env_extra={'QUATM_RUNNER_TEST_SLEEP': '0.15'})
        try:
            reader.wait_for('run_done', timeout=120)
            self._send(proc, 'pause')
            reader.wait_for('paused', timeout=30)
            reader.assert_no_event(('run_started',), duration=0.4)
            self.assertIsNone(proc.poll(), 'pause 期间进程不能退出')
            self._send(proc, 'resume')
            reader.wait_for('resumed', timeout=30)
            finished, _ = reader.wait_for('finished', timeout=120)
            self.assertTrue(finished['ok'])
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=60), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    def test_terminate_finishes_gracefully_with_exit_zero(self):
        """terminate：停在 run 边界，补齐 end_sequence/end_measurement/close，退出 0。

        给每 run 加一点耗时：CI 负载高时 10 个"瞬时" run 可能在测试把
        terminate 写进 stdin 之前就跑完了，导致偶发 timeout（CI 实测）。
        """
        task = {'task': 11, 'nRuns': 10, 'nReps': 0}
        proc, reader = self._popen(
            task, env_extra={'QUATM_RUNNER_TEST_SLEEP': '0.2'})
        try:
            reader.wait_for('run_done', timeout=120)
            self._send(proc, 'terminate')
            terminated, _ = reader.wait_for('terminated', timeout=60)
            self.assertEqual(terminated['task'], 11)
            finished, _ = reader.wait_for('finished', timeout=60)
            self.assertTrue(finished['ok'])
            self.assertTrue(finished.get('terminated'))
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=60), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
        trace_kinds = self._kinds()
        self.assertIn('end_sequence', trace_kinds)
        self.assertIn('end_measurement', trace_kinds,
                      '优雅终止也要做测量收尾（task-37 要求）')
        self.assertEqual(trace_kinds[-1], 'close')
        runs = [r for r in self._trace() if r['kind'] == 'run']
        self.assertGreaterEqual(len(runs), 1)
        self.assertLess(len(runs), 10, 'terminate 后不应继续跑完剩余 run')

    def test_unknown_control_command_is_logged_not_fatal(self):
        """未知控制命令只发 log warning，任务照常完成。"""
        task = {'task': 5, 'nRuns': 2, 'nReps': 0}
        proc, reader = self._popen(
            task, env_extra={'QUATM_RUNNER_TEST_SLEEP': '0.2'})
        try:
            self._send(proc, 'dance')
            warning, _seen = reader.wait_for('log', timeout=60)
            self.assertEqual(warning.get('level'), 'warning')
            self.assertIn('dance', warning.get('message', ''))
            finished, _seen = reader.wait_for('finished', timeout=120)
            self.assertTrue(finished['ok'])
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=60), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


class TestRunnerSchemaValidation(_RunnerTestCase):
    """参数 schema 校验：未知名字被拒绝；设备名允许；无登记表 fail-open。"""

    def test_unknown_args_rejected(self):
        """未知 args 键：error（含未知名字+实验名）+ 退出码 1，且不执行 run。"""
        task = {'task': 4, 'args': {'p': 1.0, 'nope': 2.0},
                'nRuns': 1, 'nReps': 0}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        self.assertEqual(events[-1]['event'], 'error')
        message = events[-1]['message']
        self.assertIn('nope', message)
        self.assertIn('Dummy', message)
        self.assertNotIn('start_measurement', self._kinds(),
                         '校验失败不能开始测量/写硬件')

    def test_unknown_scan_parameter_rejected(self):
        """扫描了未声明参数：error + 退出码 1。"""
        task = {'task': 4, 'scanpars': ['p', 'ghost'],
                'scanvals': [[1.0, 2.0]], 'scansequence': [0],
                'nRuns': 1, 'nReps': 0}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        self.assertEqual(events[-1]['event'], 'error')
        self.assertIn('ghost', events[-1]['message'])

    def test_device_name_allowed_in_args(self):
        """``_device_db`` 里的设备名允许出现在 args（与 manager 规则一致）。"""
        task = {'task': 0, 'args': {'p': 0.5, 'dev': 1.0},
                'nRuns': 1, 'nReps': 0}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._events(proc.stdout)[-1]['event'], 'finished')

    def test_none_scanpar_is_ignored(self):
        """``--NONE--`` 槽位不参与校验（也不写入）。"""
        task = {'task': 0, 'scanpars': ['p', '--NONE--'],
                'scanvals': [[1.0, 7.0]], 'scansequence': [0],
                'nRuns': 1, 'nReps': 0}
        proc = self._run(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_schema_helpers_match_manager_semantics(self):
        """纯函数：未知 scan/args 顺序、设备名放行、无登记表 fail-open。"""
        class _Exp:
            """只有登记表/设备表的最小实验替身。"""
            name = 'Exp'
            _declared_arguments = {'p': object()}
            _arguments = []
            _device_db = {'dev': {'type': 'attr'}}
            _attributes = {'dev': 0}

        experiment = _Exp()
        unknown_scan, unknown_args = runner.task_schema_unknown(
            experiment, {'scanpars': ['p', '--NONE--', 'ghost'],
                         'args': {'p': 1, 'dev': 2, 'other': 3}})
        self.assertEqual(unknown_scan, ['ghost'])
        self.assertEqual(unknown_args, ['other'])
        self.assertEqual(runner.declared_parameter_names(experiment), {'p'})
        self.assertEqual(runner.experiment_device_names(experiment), {'dev'})
        # 没有登记表的对象：fail-open（不误判全部未知）
        self.assertEqual(runner.task_schema_unknown(
            object(), {'scanpars': ['whatever'], 'args': {'x': 1}}), ([], []))


class TestSaveAckIntegration(unittest.TestCase):
    """task-36 保存回执接入点：配置解析 + 等待语义（纯函数单测）。

    task-36 的 API 是 ``quatm.servers.datamgr.save_ack(token, expected_seq,
    timeout=...)``，返回 ``{'available','ok','reason',...}``；runner 用与 GUI
    manager 相同的 hook/配置语义（详见 runner.save_ack_config/wait_save_ack）。
    """

    def setUp(self):
        self.events = []
        # 消息断言固定英文：runner.tr 直接返回源文案（不受本机语言影响）。
        self._tr_patch = mock.patch.object(
            runner, 'tr', lambda text, **kwargs: text)
        self._tr_patch.start()
        self.addCleanup(self._tr_patch.stop)

    def _emit(self, event, **fields):
        """收集事件。"""
        record = {'event': event}
        record.update(fields)
        self.events.append(record)

    class _Props:
        """最小属性库替身：``get(path, default)``。"""

        def __init__(self, values):
            self.values = values

        def get(self, path, default=None):
            """读属性（未知键返回默认值）。"""
            return self.values.get(path, default)

    class _Experiment:
        """只带 ``_props`` 的实验替身。"""

        def __init__(self, props=None):
            self._props = props

    def test_config_defaults_to_off_without_manager_context(self):
        """没有任务配置/属性库时缺省 off（隔离 runner 不猜着等回执）。"""
        config = runner.save_ack_config(self._Experiment(), {})
        self.assertEqual(config['mode'], 'off')
        self.assertTrue(config['strict'])
        self.assertEqual(config['timeout'], 5.0)
        self.assertTrue(config['token'].startswith('runner-'))

    def test_config_reads_gui_properties_like_manager(self):
        """能从属性库读到 GUI 的 mode/strict/timeout（与 manager 语义一致）。"""
        props = self._Props({
            runner.SAVE_ACK_MODE_PROP: 'run',
            runner.SAVE_ACK_STRICT_PROP: False,
            runner.SAVE_ACK_TIMEOUT_PROP: 2.5,
        })
        config = runner.save_ack_config(self._Experiment(props), {})
        self.assertEqual(config['mode'], 'run')
        self.assertFalse(config['strict'])
        self.assertEqual(config['timeout'], 2.5)

    def test_task_config_overrides_properties(self):
        """任务里的 ``save_ack`` 段优先于属性库（task-38 可显式下发）。"""
        props = self._Props({runner.SAVE_ACK_MODE_PROP: 'task'})
        config = runner.save_ack_config(
            self._Experiment(props),
            {'save_ack': {'mode': 'run', 'strict': True,
                          'timeout_s': 1.5, 'token': 'tok-1'}})
        self.assertEqual((config['mode'], config['strict'],
                          config['timeout'], config['token']),
                         ('run', True, 1.5, 'tok-1'))

    def test_invalid_mode_falls_back_to_off(self):
        """非法 mode 退回 off（避免拼错配置导致意外阻塞/失败）。"""
        config = runner.save_ack_config(
            self._Experiment(), {'save_ack': {'mode': 'sometimes'}})
        self.assertEqual(config['mode'], 'off')

    def test_off_mode_short_circuits_without_lookup(self):
        """mode=off 不查 API、不调用、不告警。"""
        config = runner.save_ack_config(
            self._Experiment(), {'save_ack': {'mode': 'off'}})
        with mock.patch.object(runner, 'find_save_ack') as finder:
            result = runner.wait_save_ack(config, 3, self._emit)
        finder.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertEqual(self.events, [])

    def test_missing_api_degrades_with_warning(self):
        """API 未就绪：log warning + available=False（strict 也不判失败）。"""
        config = runner.save_ack_config(
            self._Experiment(),
            {'save_ack': {'mode': 'run', 'strict': True}})
        state = {'failure': None, 'unavailable': False}
        with mock.patch.object(runner, 'find_save_ack', return_value=None):
            result = runner.wait_save_ack(config, 2, self._emit, state)
        self.assertFalse(result['available'])
        self.assertIsNone(state['failure'], '回执不可用是降级，不是数据丢失')
        self.assertTrue(state['unavailable'])
        self.assertEqual(self.events[-1]['level'], 'warning')
        self.assertIn('save-ack', self.events[-1]['message'])

    def test_unavailable_result_degrades_even_in_strict(self):
        """API 返回 available=False（旧 Data Manager/端点不可达）：strict 也降级。"""
        config = runner.save_ack_config(
            self._Experiment(),
            {'save_ack': {'mode': 'run', 'strict': True}})
        state = {'failure': None, 'unavailable': False}
        api = mock.Mock(return_value={'available': False, 'ok': False,
                                      'reason': 'unavailable'})
        with mock.patch.object(runner, 'find_save_ack', return_value=api):
            runner.wait_save_ack(config, 0, self._emit, state)
        self.assertIsNone(state['failure'])
        self.assertTrue(state['unavailable'])

    def test_strict_nack_sets_failure_state(self):
        """明确回执失败 + strict：写 state.failure（调用方转 error/退出码 1）。"""
        config = runner.save_ack_config(
            self._Experiment(),
            {'save_ack': {'mode': 'run', 'strict': True}})
        state = {'failure': None, 'unavailable': False}
        api = mock.Mock(return_value={'available': True, 'ok': False,
                                      'reason': 'timeout'})
        with mock.patch.object(runner, 'find_save_ack', return_value=api):
            runner.wait_save_ack(config, 0, self._emit, state)
        self.assertIsNotNone(state['failure'])
        self.assertIn('strict', state['failure'])
        self.assertEqual(self.events[-1]['level'], 'error')
        self.assertIn('timeout', self.events[-1]['message'])

    def test_warn_nack_does_not_set_failure(self):
        """非 strict：回执失败只 log error，继续（state.failure 保持 None）。"""
        config = runner.save_ack_config(
            self._Experiment(),
            {'save_ack': {'mode': 'run', 'strict': False}})
        state = {'failure': None, 'unavailable': False}
        api = mock.Mock(return_value={'available': True, 'ok': False,
                                      'reason': 'timeout'})
        with mock.patch.object(runner, 'find_save_ack', return_value=api):
            runner.wait_save_ack(config, 0, self._emit, state)
        self.assertIsNone(state['failure'])
        self.assertEqual(self.events[-1]['level'], 'error')

    def test_success_passes_token_seq_timeout(self):
        """成功路径把 token/expected_seq/timeout 传给共用 API。"""
        config = runner.save_ack_config(
            self._Experiment(),
            {'save_ack': {'mode': 'task', 'strict': True, 'token': 'tok-9',
                          'timeout_s': 3.0}})
        api = mock.Mock(return_value={'available': True, 'ok': True,
                                      'reason': 'ok'})
        with mock.patch.object(runner, 'find_save_ack', return_value=api):
            result = runner.wait_save_ack(config, 42, self._emit)
        self.assertTrue(result['ok'])
        api.assert_called_once_with('tok-9', 42, timeout=3.0)
        self.assertEqual(self.events, [])

    def test_api_exception_is_treated_as_nack(self):
        """API 自身抛异常：strict 时写 failure，warn 时只记录。"""
        for strict in (True, False):
            with self.subTest(strict=strict):
                self.events = []
                config = runner.save_ack_config(
                    self._Experiment(),
                    {'save_ack': {'mode': 'run', 'strict': strict}})
                state = {'failure': None, 'unavailable': False}
                api = mock.Mock(side_effect=RuntimeError('boom'))
                with mock.patch.object(runner, 'find_save_ack',
                                       return_value=api):
                    runner.wait_save_ack(config, 0, self._emit, state)
                self.assertEqual(bool(state['failure']), strict)
                self.assertEqual(self.events[-1]['level'], 'error')

    def test_execute_task_aborts_on_strict_ack_failure(self):
        """端到端（进程内）：strict 回执失败 → error 事件 + 退出码 1 + close。

        用临时项目 + 假实验模块直接调用 ``runner.execute_task``（不 spawn），
        把 ``wait_save_ack`` 换成“写 state.failure”，验证 abort 接线。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp)
            (project / 'experiments').mkdir()
            (project / 'experiments' / 'dummy.py').write_text(
                DUMMY_MODULE, encoding='utf-8')
            old_log = os.environ.get('QUATM_RUNNER_TEST_LOG')
            os.environ['QUATM_RUNNER_TEST_LOG'] = str(project / 'trace.jsonl')
            self.addCleanup(self._restore_env, 'QUATM_RUNNER_TEST_LOG', old_log)
            events = []

            def emit(event, **fields):
                record = {'event': event}
                record.update(fields)
                events.append(record)

            def failing_wait(config, expected_seq, emit_fn, state=None):
                if state is not None:
                    state['failure'] = 'save-ack failed in strict mode'
                return {'available': True, 'ok': False, 'reason': 'timeout'}

            task = {'task': 1, 'nRuns': 1, 'nReps': 0,
                    'save_ack': {'mode': 'run', 'strict': True}}
            with mock.patch.object(runner, 'wait_save_ack', failing_wait):
                code = runner.execute_task(
                    str(project), 'dummy.py', 'Dummy', task,
                    control=None, emit=emit)
            self.assertEqual(code, 1)
            self.assertEqual(events[-1]['event'], 'error',
                             'strict 回执失败要发 error 事件')
            self.assertIn('save-ack', events[-1]['message'])

    @staticmethod
    def _restore_env(name, value):
        """恢复环境变量（addCleanup 用）。"""
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_install_save_ack_sets_data_token_context(self):
        """实验支持 task-36 接口时：把任务 token 挂到数据流上（回执按 token 统计）。"""
        calls = []

        class _HookedExperiment:
            """记录 set_ack_context 的实验替身。"""

            def set_ack_context(self, token):
                calls.append(token)

        experiment = _HookedExperiment()
        config = runner.save_ack_config(
            self._Experiment(), {'save_ack': {'mode': 'run', 'token': 'tok'}})
        installed = runner._install_save_ack(
            experiment, config, self._emit, {'failure': None})
        self.assertTrue(installed)
        self.assertEqual(calls, ['tok'])
        # 收尾清理必须把上下文清掉（None），避免污染同进程后续任务。
        runner._clear_save_ack(experiment)
        self.assertEqual(calls, ['tok', None])

    def test_install_save_ack_requires_api_on_experiment(self):
        """老实验对象没有 ``set_ack_context``：返回 False（调用方降级 expected_seq=0）。"""
        class _PlainExperiment:
            """没有 task-36 ack 接口的旧实验替身。"""

        config = runner.save_ack_config(
            self._Experiment(), {'save_ack': {'mode': 'run'}})
        self.assertFalse(runner._install_save_ack(
            _PlainExperiment(), config, self._emit, {'failure': None}))

    def test_install_save_ack_off_mode_does_not_touch_experiment(self):
        """mode=off 不挂上下文。"""
        class _HookedExperiment:
            """记录 set_ack_context 调用。"""

            def __init__(self):
                self.calls = []

            def set_ack_context(self, token):
                self.calls.append(token)

        experiment = _HookedExperiment()
        config = runner.save_ack_config(self._Experiment(), {})
        self.assertFalse(runner._install_save_ack(
            experiment, config, self._emit, {'failure': None}))
        self.assertEqual(experiment.calls, [])


class TestSaveAckSubprocess(_RunnerTestCase):
    """子进程端到端：保存回执不可用时降级为 warning，任务仍成功。"""

    def test_unavailable_ack_degrades_and_task_finishes(self):
        """任务显式要求 per-run 回执，但本机没有 Data Manager：降级不失败。"""
        task = {'task': 2, 'nRuns': 1, 'nReps': 0,
                'save_ack': {'mode': 'run', 'strict': True,
                             'timeout_s': 0.5}}
        proc = self._run(task, env_extra={
            # 指向必然拒绝连接的端点，确保回执迅速判为 unavailable（不真等）。
            'QUATM_SAVE_ACK_ENDPOINT': 'tcp://127.0.0.1:9',
        })
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self._events(proc.stdout)
        self.assertEqual(events[-1]['event'], 'finished')
        self.assertTrue(events[-1]['ok'])
        warning = [event for event in events
                   if event['event'] == 'log'
                   and event.get('level') == 'warning']
        self.assertTrue(warning, '回执不可用要发 log warning（降级）')
        self.assertIn('save-ack', warning[0]['message'])


class TestSaveAckUnavailableWarning(unittest.TestCase):
    """回执 API 不可用时每次任务只提示一条 warning（不按 run 刷屏）。"""

    def test_unavailable_warns_once_per_task(self):
        from unittest import mock

        from quatm.experiment import runner

        logs = []

        def _emit(kind, **payload):
            logs.append((kind, payload))

        state = {'failure': None, 'unavailable': False}
        with mock.patch.object(runner, 'find_save_ack', return_value=None):
            first = runner.wait_save_ack(
                {'mode': 'run', 'strict': True, 'timeout': 0.1, 'token': 't'},
                0, _emit, state)
            second = runner.wait_save_ack(
                {'mode': 'run', 'strict': True, 'timeout': 0.1, 'token': 't'},
                0, _emit, state)
        self.assertFalse(first['available'])
        self.assertFalse(second['available'])
        warnings = [payload for kind, payload in logs
                    if kind == 'log' and payload.get('level') == 'warning']
        self.assertEqual(len(warnings), 1, logs)


if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------------------
# 父进程死亡优雅收尾（task-52）
# ---------------------------------------------------------------------------
#: 假父进程：以 ``QUATM_PARENT_PID=<自己 pid>`` 启动 runner 子进程并长睡，
#: 供测试 kill -9。runner 子进程的看门狗随后应执行注册的优雅钩子再退出。
RUNNER_SUPERVISOR = r'''# -*- coding: utf-8 -*-
"""测试用假父进程：spawn runner 子进程后等待被 kill -9。"""
import json
import os
import subprocess
import sys
import time

command = json.loads(sys.argv[1])
cwd = sys.argv[2]
pid_file = sys.argv[3]
env = os.environ.copy()
env['QUATM_PARENT_PID'] = str(os.getpid())
proc = subprocess.Popen(command, cwd=cwd, env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
with open(pid_file, 'w', encoding='utf-8') as handle:
    handle.write(str(proc.pid))
while True:
    time.sleep(1)
'''


class TestRunnerParentDeath(_RunnerTestCase):
    """父进程被 kill 后 runner 的 close/end 收尾（不接硬件）。"""

    def test_normal_run_closes_exactly_once(self):
        """正常完成路径：close 只调用一次（守卫不引入二次关闭）。"""
        proc = self._run({'task': 1, 'nRuns': 1, 'nReps': 0})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        kinds = self._kinds()
        self.assertEqual(kinds.count('close'), 1)
        self.assertEqual(kinds[-1], 'close')

    def test_parent_killed_during_blocked_run_calls_close(self):
        """父进程被 kill -9 时：runner 请求终止并尽力 close，随后硬退。"""
        supervisor = self.project / 'supervisor.py'
        supervisor.write_text(RUNNER_SUPERVISOR, encoding='utf-8')
        pid_file = self.project / 'runner.pid'
        env = self._env(QUATM_RUNNER_TEST_SLEEP=30)
        parent = subprocess.Popen(
            [sys.executable, str(supervisor),
             json.dumps(self._command({'task': 3, 'nRuns': 5, 'nReps': 0})),
             str(self.project), str(pid_file)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill_if_alive, parent)
        child_pid = None
        self.addCleanup(self._kill_pid_if_alive, lambda: child_pid)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if pid_file.exists() and any(
                    record.get('kind') == 'run' for record in self._trace()):
                child_pid = int(pid_file.read_text(encoding='utf-8').strip())
                break
            time.sleep(0.02)
        self.assertIsNotNone(child_pid, 'runner 未进入 run()')
        started = time.monotonic()
        parent.kill()
        parent.wait(timeout=5)
        from quatm.servers.heartbeat import pid_alive
        while time.monotonic() - started < 14.0:
            if not pid_alive(child_pid):
                break
            time.sleep(0.02)
        self.assertFalse(pid_alive(child_pid),
                         'runner 子进程未在 10s 预算内退出')
        self.assertLess(time.monotonic() - started, 12.0)
        kinds = self._kinds()
        self.assertEqual(kinds.count('close'), 1,
                         '父死收尾必须调用且只调用一次 close')
        self.assertIn('run', kinds)

    def test_parent_death_at_safe_point_reuses_normal_cleanup(self):
        """父死发生在 run 边界：主线程走正常收尾（end_measurement+close），钩子不重复关。"""
        from unittest import mock

        import quatm.servers as servers
        from quatm.experiment import runner

        class _FakeControl:
            """模拟 ControlChannel：request_terminate 后 wait_until_ready 返回 True。"""

            def __init__(self):
                self.terminated = False
                self.requests = 0

            def request_terminate(self):
                """请求优雅终止。"""
                self.requests += 1
                self.terminated = True

            def wait_until_ready(self):
                """run 边界：父死请求后返回 True（停止后续 run）。"""
                return self.terminated

        events = []

        def emit(kind, **payload):
            record = {'event': kind}
            record.update(payload)
            events.append(record)

        task = {'task': 9, 'nRuns': 5, 'nReps': 0}
        control = _FakeControl()
        result = {}

        def _run_task():
            result['code'] = runner.execute_task(
                str(self.project), 'dummy.py', 'Dummy', task,
                control=control, emit=emit)

        thread = threading.Thread(target=_run_task, daemon=True)
        # 进程内调用 execute_task：假实验的日志路径/每次 run 的 sleep 都读环境变量，
        # 必须给本进程也设上（子进程测试由 _env 注入）。
        with mock.patch.dict(os.environ, {
                'QUATM_RUNNER_TEST_LOG': str(self.trace_path),
                'QUATM_RUNNER_TEST_SLEEP': '0.05'}):
            thread.start()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if any(record.get('kind') == 'run' for record in self._trace()):
                    break
                time.sleep(0.01)
            # 模拟父进程死亡钩子（servers 看门狗线程会这样调用）
            servers._run_parent_death_hooks(total_timeout=5.0)
            thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive(), '任务未在钩子请求后收尾')
        self.assertEqual(result.get('code'), 0)
        kinds = self._kinds()
        self.assertEqual(kinds.count('close'), 1, 'close 必须只一次')
        self.assertIn('end_measurement', kinds, '安全点必须复用正常收尾路径')
        self.assertLess(kinds.index('end_measurement'), kinds.index('close'))
        finished = [event for event in events if event['event'] == 'finished']
        self.assertTrue(finished and finished[-1].get('terminated'))

    def test_cleanup_guard_idempotent_and_mutually_exclusive(self):
        """守卫：父死路径 close 后，正常路径的 mark_main_done 不再二次 close。"""
        from quatm.experiment import runner

        class _Exp:
            """记录 close 次数。"""

            def __init__(self):
                self.closed = 0

            def close(self):
                """记录关闭。"""
                self.closed += 1

        class _Control:
            """记录终止请求。"""

            def __init__(self):
                self.terminated = False

            def request_terminate(self):
                """记录终止请求。"""
                self.terminated = True

        exp = _Exp()
        control = _Control()
        guard = runner._HardwareSafeCleanup(exp, control=control)
        with mock.patch.object(runner, 'PARENT_DEATH_MAIN_WAIT_S', 0.05):
            guard()                      # 模拟父死钩子（主线程未收尾）
        self.assertEqual(exp.closed, 1)
        self.assertTrue(control.terminated)
        self.assertTrue(guard.mark_main_done())   # 正常路径随后收尾
        self.assertEqual(exp.closed, 1, '不得二次 close')
        guard()                                    # 重复调用钩子也幂等
        self.assertEqual(exp.closed, 1)

    @staticmethod
    def _kill_if_alive(proc):
        """tearDown：杀掉仍在跑的假父进程。"""
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _kill_pid_if_alive(pid_getter):
        """tearDown：杀掉仍在跑的 runner 子进程（避免测试泄漏）。"""
        try:
            pid = pid_getter()
            if not pid:
                return
            from quatm.servers.heartbeat import pid_alive
            if pid_alive(pid):
                os.kill(pid, 9)
        except Exception:  # noqa: BLE001
            pass
