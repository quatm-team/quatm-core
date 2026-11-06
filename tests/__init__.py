"""quatm 的运行时测试（stdlib ``unittest``，不引入 pytest 依赖）。

运行方式（仓库根目录）::

    python -m unittest discover -s tests -t . -v

``scripts/check.sh`` 的第 9 步会自动跑一遍（``-q``）。测试约定：

* 只用标准库 ``unittest``（环境里没有 pytest，CI 也不装——见
  ``.github/workflows/ci.yml``）；
* 需要项目目录的测试用 ``tempfile.TemporaryDirectory()``，不要写进仓库；
* 不假设已激活（``.license`` 可能不存在）：GUI/Web 的**入口门控**不会被触发，
  因为测试只 import 模块、不调 ``setup_app_theme`` / ``web.server.run``；
* 不连硬件、不连 ZMQ 网络：用假对象/Fake 替代；
* 仓库里的 ``experiments/**/*_test.py`` 是硬件自测脚本，不属于本目录。
"""

import atexit
import gc
import os

# 统一的离屏/首跑设置：CI 无显示服务器，否则 Qt 会尝试连接 xcb 并在进程
# 退出时留下半初始化的 QApplication（release CI 曾出现 finalize 期
# ``bool_dealloc`` refcount 崩溃）。各测试模块仍会 setdefault，这里提前兜底。
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QUATM_SKIP_FIRST_RUN', '1')
# 测试里关闭窗口不能弹模态确认框；启动器确认逻辑由专项测试直接验证。
os.environ.setdefault('QUATM_CONFIRM_QUIT', '0')


def _cleanup_qt_before_finalize():
    """在解释器 finalize 前关闭顶层 Qt 窗口并回收，降低 C 扩展 teardown 风险。"""
    try:
        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        # 只处理事件 + 回收，不主动 close()/deleteLater() 顶层窗口：
        # close 会触发各窗口的 closeEvent（后台线程/定时器/Properties），
        # 在解释器退出阶段做这些是 CI "QObject::~QObject: Timers cannot be
        # stopped from another thread" 后 SIGSEGV 的来源。窗口/句柄由进程
        # 退出回收；这里用 GC 提前回收 Python 环，降低 finalize refcount 风险。
        app.processEvents()
        app = None
        for _ in range(3):
            gc.collect()
    except Exception:  # noqa: BLE001 - 没有 Qt/已销毁时不处理
        pass


atexit.register(_cleanup_qt_before_finalize)


_QT_DRAIN_COUNT = 0


def _drain_qt_after_test():
    """每个测试结束后的 Qt 收尾：等待线程池空闲、处理事件、回收循环引用。

    CI（Ubuntu）在 GUI/线程池测试之间偶发 "QObject::~QObject: Timers cannot
    be stopped from another thread" 后 SIGSEGV：worker 还在跑时下一个测试
    创建/销毁 GUI 对象。这里在每个 TestCase.run 的 finally 做一次有上限的
    drain（无 Qt 的测试零开销）。
    """
    import sys as _sys
    if 'PySide6.QtWidgets' not in _sys.modules:
        return
    # 只有线程池真的还有任务时才等；多数用例没有 Qt worker，避免每次 5s
    # 上限检查+事件循环的开销（全量测试曾因此从 ~70s 涨到 ~140s）。
    try:
        from PySide6.QtCore import QThreadPool
        pool = QThreadPool.globalInstance()
        if pool.activeThreadCount():
            pool.waitForDone(5000)
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
    except Exception:  # noqa: BLE001
        pass
    # 每 25 个测试做一次 gc，兼顾环回收与全量运行时间。
    global _QT_DRAIN_COUNT
    _QT_DRAIN_COUNT += 1
    if _QT_DRAIN_COUNT % 25 == 0:
        try:
            gc.collect()
        except Exception:  # noqa: BLE001
            pass


def _install_test_cleanup_hook():
    """给 unittest.TestCase.run 包一层 Qt drain；只在导入 tests 时安装一次。"""
    import unittest as _unittest
    if getattr(_unittest.TestCase, '_quatm_qt_drain_installed', False):
        return
    _original_run = _unittest.TestCase.run

    def _run_with_qt_drain(self, result=None):
        try:
            return _original_run(self, result)
        finally:
            _drain_qt_after_test()

    _unittest.TestCase.run = _run_with_qt_drain
    _unittest.TestCase._quatm_qt_drain_installed = True


_install_test_cleanup_hook()
