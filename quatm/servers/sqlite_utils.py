"""SQLite 写操作的共用工具：锁错误判定 + 退避重试。

背景：``messagestore``（消息库）与 ``pushstore``（推送 outbox）原来各有一份
几乎相同的实现——``locked``/``busy`` 判定、失败回滚、指数退避、以及“重试期间
是否释放进程内锁”。两份逻辑一旦漂移，同一个 ``database is locked`` 在两边的
表现就不一样。本模块是唯一实现，两边通过传参保留各自的预算（重试次数/退避）。

典型用法::

    with self._lock:
        sqlite_utils.run_with_retry(self._conn, self._lock, self._write_locked,
                                    retries=WRITE_RETRIES,
                                    backoff=WRITE_RETRY_BACKOFF_S)

``func`` 内部负责完整的“写 + 提交”；本模块只在遇到锁错误时回滚并重试。
"""

import sqlite3
import time


def is_locked(exc):
    """判断异常是不是 SQLite 的锁/忙错误（这类错误值得重试）。

    Args:
        exc (BaseException): SQLite 抛出的异常。

    Returns:
        bool: 异常文本含 ``locked`` 或 ``busy`` 时为 True。
    """
    message = str(exc).lower()
    return 'locked' in message or 'busy' in message


def run_with_retry(conn, lock, func, retries=4, backoff=0.5):
    """执行一个写操作；遇 ``database is locked`` 先回滚再指数退避重试。

    这是 Windows 上 ``database is locked`` 的主要对策：SQLite 自带的
    ``timeout`` 只管“等锁”，超时后抛异常；重试能把“另一个进程正好在批量
    写/迁移”的短暂争用熬过去。

    Args:
        conn (sqlite3.Connection): 数据库连接（失败重试前会在它上面 rollback）。
        lock (threading.Lock|None): 调用方持有的进程内锁。给出时，退避 sleep
            期间会**释放它**——否则同进程的 GUI 读查询会被挡在锁外好几秒
            （退避总共可达数秒），表现为“界面卡一下”。
        func (callable): 无参可调用对象，内部做完整的“写 + 提交”。
        retries (int): 最多尝试次数（含首次），默认 4。
        backoff (float): 首次退避秒数，之后每次翻倍、封顶 2 s，默认 0.5。

    Returns:
        object: ``func()`` 的返回值。

    Raises:
        sqlite3.OperationalError: 重试耗尽后仍被锁时抛出最后一次异常；
            非锁类错误原样抛出（不重试）。
    """
    attempts = max(1, int(retries))
    delay = max(0.0, float(backoff))
    last = None
    for attempt in range(attempts):
        try:
            return func()
        except sqlite3.OperationalError as exc:
            if not is_locked(exc):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            # 锁失败会让连接停在一个“半开”的事务里，不回滚的话下一次尝试
            # 可能报别的错（例如 PRAGMA synchronous=NORMAL 会直接抛
            # “Safety level may not be changed inside a transaction”）。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            released = False
            if lock is not None:
                try:
                    lock.release()
                    released = True
                except RuntimeError:  # 并非本线程持有：继续持锁退避
                    released = False
            try:
                time.sleep(delay)
            finally:
                if released:
                    lock.acquire()
            delay = min(delay * 2, 2.0)
    raise last
