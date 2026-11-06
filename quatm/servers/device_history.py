# -*- coding: utf-8 -*-
"""设备健康历史（JSONL）——探针/自检结果的本地时间线。

设备健康视图（``quatm/GUI/devicemanager/health.py``）每次 probe / code check /
周期轮询都会把结果追加到 ``<项目根>/configuration/devices/health_history.jsonl``，
用于 History 页签、跨会话的“上次最终状态”（状态跃迁告警）以及事后排查。
``configuration/devices/`` 整个目录是运行时状态，已在 ``.gitignore`` 里。

文件格式
========

JSONL：每行一个 JSON 对象（UTF-8），字段与设备健康视图一致::

    {'ts': 1762345678.123,          # epoch 秒
     'address': 'tcp://host:5025',  # 设备地址（空地址条目为 ''）
     'name': 'psu_main',            # device_db 里的名字
     'kind': 'probe' | 'code',      # 哪类检查
     'ok': True | False | None,     # 通过/失败/不适用（skipped、in-use）
     'status': 'ok' | 'error' | 'skipped' | 'in-use' | 'unknown',
     'latency_ms': 12.5,            # probe 延迟（code 检查为 None）
     'error': '...',                # 失败原因/无应答说明
     'idn': '*IDN? 应答',           # probe 才有
     'source': 'manual' | 'poll' | 'code'}

设计约束
========

* **只写本地文件**：不依赖 Properties/消息系统；单 GUI 进程写入用模块级
  :data:`_LOCK` 串行化，追加用 ``os.open(..., O_APPEND)``（一次 ``os.write``
  写完整行，避免并发交错）；不跨进程加锁（同一项目由 Device Health 窗口
  单实例写入）。
* **轮转**：默认超过 :data:`DEFAULT_MAX_BYTES`（2 MB）或 :data:`DEFAULT_MAX_LINES`
  （5000 行）就重写文件、只保留最近 :data:`DEFAULT_MAX_LINES` 行（原子
  ``os.replace``）。文件因此有界，不会无限增长。
* **坏行跳过**：手工编辑/半截写入/旧格式行在 :func:`read` 里直接忽略，
  不让一条坏记录拖垮 History 页。
* **失败不抛**：磁盘只读/权限不足/路径被占时 :func:`append`/:func:`clear`
  返回 ``False`` 并打印一条 warning——健康视图是诊断工具，不能因为写不了
  历史把界面弄崩。
"""

import json
import os
import threading
import time

from quatm.servers.configreader import PROJECT_ROOT

#: 默认单文件字节上限（超过就轮转）。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
#: 默认保留的最大行数（轮转后保留最近这么多行）。
DEFAULT_MAX_LINES = 5000
#: 历史文件在项目根下的相对路径。
HISTORY_REL = os.path.join('configuration', 'devices', 'health_history.jsonl')

#: 进程内写锁（GUI 一次只开一个健康窗口；跨进程不加锁，见模块说明）。
_LOCK = threading.Lock()


def _tr(text):
    """延迟导入 i18n 的 ``tr``（servers 包内避免 import 期循环）。"""
    from quatm.utils.i18n import tr
    return tr(text)


def history_path(root=None):
    """返回历史文件的绝对路径（不创建目录）。

    Args:
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`（可被测试替换）。

    Returns:
        str: ``<root>/configuration/devices/health_history.jsonl``。
    """
    base = root or PROJECT_ROOT
    return os.path.join(str(base), HISTORY_REL)


def _record_line(record):
    """把记录序列化成一行 JSON（无法序列化的字段转 str，绝不抛）。"""
    return json.dumps(record, ensure_ascii=False, default=str) + '\n'


def _line_count(path):
    """统计文件行数（读失败返回 0）。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return sum(1 for _line in handle)
    except OSError:
        return 0


def _rotate(path, max_lines):
    """把文件重写成“只保留最近 ``max_lines`` 行”（原子替换）。

    Args:
        path (str): 历史文件路径。
        max_lines (int): 保留的最大行数。

    Returns:
        bool: 轮转成功（或文件不存在）为 True。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()
    except OSError:
        return False
    keep = lines[-max_lines:] if max_lines > 0 else []
    temp = '%s.tmp.%d' % (path, os.getpid())
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ''.join(keep).encode('utf-8'))
        finally:
            os.close(fd)
        os.replace(temp, path)
        return True
    except OSError as exc:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        print(_tr('[Device History] cannot rotate %s: %s') % (path, exc))
        return False


def _maybe_rotate(path, max_bytes, max_lines):
    """超过字节/行数上限时轮转，保留最近 ``max_lines`` 行。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    too_big = size > max_bytes
    too_many = False
    if not too_big:
        too_many = _line_count(path) > max_lines
    if too_big or too_many:
        _rotate(path, max_lines)


def append(record, root=None, max_bytes=DEFAULT_MAX_BYTES,
           max_lines=DEFAULT_MAX_LINES):
    """追加一条历史记录；失败只打印 warning，不抛异常。

    Args:
        record (dict): 记录（字段见模块 docstring）；非 dict 直接返回 False。
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`。
        max_bytes (int): 文件字节上限（超过即轮转，测试可传小值）。
        max_lines (int): 保留的最大行数（测试可传小值）。

    Returns:
        bool: 写入成功为 True。
    """
    if not isinstance(record, dict):
        return False
    payload = dict(record)
    payload.setdefault('ts', time.time())
    path = history_path(root)
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        line = _record_line(payload).encode('utf-8')
    except (OSError, TypeError, ValueError) as exc:
        print(_tr('[Device History] cannot append to %s: %s') % (path, exc))
        return False
    with _LOCK:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as exc:
            print(_tr('[Device History] cannot append to %s: %s')
                  % (path, exc))
            return False
        try:
            _maybe_rotate(path, int(max_bytes), int(max_lines))
        except OSError as exc:  # noqa: BLE001 - 轮转失败不影响本条已写入
            print(_tr('[Device History] cannot rotate %s: %s') % (path, exc))
    return True


def read(limit=500, address=None, root=None):
    """读取最近的记录（新的在后），坏行跳过。

    Args:
        limit (int): 最多返回多少条（``<=0`` 表示不限）。
        address (str|None): 只返回该地址的记录（精确匹配）。
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`。

    Returns:
        list[dict]: 解析成功的记录（文件不存在/不可读时为空列表）。
    """
    path = history_path(root)
    records = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # 坏行/半截写入：跳过
                if not isinstance(record, dict):
                    continue
                records.append(record)
    except OSError:
        return []
    if address is not None:
        wanted = str(address)
        records = [record for record in records
                   if str(record.get('address') or '') == wanted]
    if limit is not None and int(limit) > 0:
        records = records[-int(limit):]
    return records


def latest_by_address(root=None):
    """返回每个地址的最后一条记录（跨会话的“上次最终状态”）。

    Args:
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`。

    Returns:
        dict: ``{address: record}``；没有历史/文件不可读时为空 dict。
    """
    latest = {}
    for record in read(limit=0, root=root):
        address = str(record.get('address') or '')
        if address:
            latest[address] = record
    return latest


def clear(root=None):
    """删除历史文件（文件本来不存在也算成功）。

    Args:
        root (str|None): 项目根；None 用 :data:`PROJECT_ROOT`。

    Returns:
        bool: 删除成功（或原本不存在）为 True；删除失败为 False。
    """
    path = history_path(root)
    with _LOCK:
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            print(_tr('[Device History] cannot remove %s: %s') % (path, exc))
            return False
