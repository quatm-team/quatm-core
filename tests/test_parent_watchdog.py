# -*- coding: utf-8 -*-
"""父进程死亡看门狗“优雅钩子”测试（task-52，纯 stdlib + subprocess）。

背景：``quatm.servers`` 在 import 期启动父进程看门狗；旧行为是父进程（Browser/
ProcessManager）被 kill -9 后子进程直接 ``os._exit(0)``，设备/文件来不及收尾。
现在子进程可以 ``register_parent_death_hook(callback, timeout=...)`` 注册收尾
钩子：父死时按注册**逆序**执行，各自独立线程 + join(timeout)，总预算
``PARENT_DEATH_TOTAL_TIMEOUT_S`` 秒，异常吞掉，最后 ``os._exit(0)``。

测试结构（不接硬件、不依赖 psutil）：测试进程启动一个“假父进程”，假父进程再
以 ``QUATM_PARENT_PID=<自己的 pid>`` 启动被测子进程；测试 kill 掉假父进程后
检查子进程的钩子证据与退出时间。``pid_alive`` 来自 ``quatm.servers.heartbeat``
（跨平台实现）。
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from quatm.servers.heartbeat import pid_alive

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 被测子进程：import quatm.servers（会启动看门狗）→ 按 mode 注册钩子 → 长睡。
CHILD_SCRIPT = r'''# -*- coding: utf-8 -*-
"""父进程死亡钩子被测子进程（纯 stdlib）。"""
import os
import sys
import time

from quatm.servers import register_parent_death_hook

MODE = os.environ.get('WATCHDOG_TEST_MODE', 'hook')
OUT = os.environ['WATCHDOG_TEST_OUT']


def write(text):
    """追加一行证据到输出文件（父进程死后仍可写普通文件）。"""
    try:
        with open(OUT, 'a', encoding='utf-8') as handle:
            handle.write(str(text) + '\n')
            handle.flush()
    except Exception:
        pass


if MODE == 'hook':
    register_parent_death_hook(lambda: write('hook1'))
    register_parent_death_hook(lambda: write('hook2'))
elif MODE == 'raising':
    def boom():
        write('boom-raise')
        raise RuntimeError('hook exploded')
    register_parent_death_hook(lambda: write('first'))
    register_parent_death_hook(boom)
elif MODE == 'timeout':
    def slow():
        write('slow-start')
        time.sleep(30)
    register_parent_death_hook(slow, timeout=1.0)
    register_parent_death_hook(lambda: write('fast'))
elif MODE == 'none':
    pass
else:
    raise SystemExit('unknown mode %r' % MODE)

write('ready')
while True:
    time.sleep(1)
'''

#: 假父进程：以 QUATM_PARENT_PID=<自己 pid> 启动子进程，写子进程 pid，然后长睡。
SUPERVISOR_SCRIPT = r'''# -*- coding: utf-8 -*-
"""假父进程：spawn 被测子进程后长睡，供测试 kill -9。"""
import os
import subprocess
import sys
import time

child = sys.argv[1]
child_pid_file = sys.argv[2]
child_out = sys.argv[3]
mode = sys.argv[4]

env = os.environ.copy()
env['QUATM_PARENT_PID'] = str(os.getpid())
env['WATCHDOG_TEST_MODE'] = mode
env['WATCHDOG_TEST_OUT'] = child_out
env['PYTHONUNBUFFERED'] = '1'
existing = env.get('PYTHONPATH')
env['PYTHONPATH'] = (str(%(repo)r) + os.pathsep + existing
                     if existing else str(%(repo)r))

proc = subprocess.Popen([sys.executable, child], env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
with open(child_pid_file, 'w', encoding='utf-8') as handle:
    handle.write(str(proc.pid))
while True:
    time.sleep(1)
'''


class TestParentDeathHooks(unittest.TestCase):
    """父进程被 kill 后：钩子逆序执行、异常/超时兜底、无钩子行为不变。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.child = self.tmp / 'child.py'
        self.child.write_text(CHILD_SCRIPT, encoding='utf-8')
        self.supervisor = self.tmp / 'supervisor.py'
        self.supervisor.write_text(
            SUPERVISOR_SCRIPT % {'repo': str(REPO_ROOT)}, encoding='utf-8')
        self.out = self.tmp / 'evidence.txt'
        self.pid_file = self.tmp / 'child.pid'
        self._parents = []
        self._children = []

    def tearDown(self):
        for proc in self._parents:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        for pid in self._children:
            try:
                if pid_alive(pid):
                    os.kill(pid, 9)
            except Exception:  # noqa: BLE001
                pass

    def _start_child(self, mode, timeout=20.0):
        """启动假父进程+被测子进程，返回子进程 pid（等 ready 后）。"""
        proc = subprocess.Popen(
            [sys.executable, str(self.supervisor), str(self.child),
             str(self.pid_file), str(self.out), mode],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._parents.append(proc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.pid_file.exists() and self.out.exists()
                    and 'ready' in self.out.read_text(encoding='utf-8')):
                return int(self.pid_file.read_text(encoding='utf-8').strip())
            time.sleep(0.02)
        raise AssertionError('child did not become ready (mode=%s)' % mode)

    def _kill_parent_and_wait_child(self, child_pid, timeout=14.0):
        """kill -9 假父进程，等子进程自行退出；返回耗时秒数。"""
        self._children.append(child_pid)
        parent = self._parents.pop()
        started = time.monotonic()
        parent.kill()
        parent.wait(timeout=5)
        deadline = started + timeout
        while time.monotonic() < deadline:
            if not pid_alive(child_pid):
                return time.monotonic() - started
            time.sleep(0.02)
        raise AssertionError('child %d still alive %.1fs after parent death'
                             % (child_pid, time.monotonic() - started))

    def _evidence(self):
        """读子进程写入的钩子证据（按写入顺序）。"""
        if not self.out.exists():
            return []
        return [line for line in
                self.out.read_text(encoding='utf-8').splitlines() if line]

    def test_hooks_run_in_reverse_order_then_exit(self):
        child_pid = self._start_child('hook')
        elapsed = self._kill_parent_and_wait_child(child_pid)
        self.assertLess(elapsed, 10.0, '退出应受总预算 10s 限制')
        self.assertEqual(self._evidence(), ['ready', 'hook2', 'hook1'],
                         '钩子必须按注册逆序执行（后注册先跑）')

    def test_raising_hook_is_swallowed_and_others_still_run(self):
        child_pid = self._start_child('raising')
        elapsed = self._kill_parent_and_wait_child(child_pid)
        self.assertLess(elapsed, 10.0)
        evidence = self._evidence()
        # 逆序：boom 先于 first；boom 抛异常不能阻止 first 执行。
        self.assertEqual(evidence, ['ready', 'boom-raise', 'first'])

    def test_hook_timeout_does_not_block_exit(self):
        child_pid = self._start_child('timeout')
        elapsed = self._kill_parent_and_wait_child(child_pid, timeout=14.0)
        evidence = self._evidence()
        self.assertIn('fast', evidence)
        self.assertIn('slow-start', evidence)
        self.assertLess(elapsed, 10.0,
                        '单钩子 timeout=1s + 总预算 10s：不能真的等 30s')

    def test_no_hooks_behaves_like_before(self):
        """没有注册钩子：检测到父死立即退出（旧行为不变）。"""
        child_pid = self._start_child('none')
        elapsed = self._kill_parent_and_wait_child(child_pid)
        self.assertLess(elapsed, 8.0)
        self.assertEqual(self._evidence(), ['ready'])


if __name__ == '__main__':
    unittest.main()
