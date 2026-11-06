"""JSON 序列化辅助：把 numpy 标量/数组等对象转成标准 JSON 类型。

``quatm/web/bridge.py``、``quatm/web/server.py``（以及 ``quatm/web/runner.py``，
后者暂未并入，见任务报告）原来各有一份几乎相同的 ``_jsonable``：递归处理
dict/list，把 numpy 标量取 ``.item()``、把 ndarray 转 ``.tolist()``。逻辑分叉
后同一份数据在不同入口会序列化成不同类型（数组一侧是 list、另一侧是 str），
前端难以统一处理。本模块是唯一实现。

放在 ``quatm/servers`` 而不是 ``quatm/web``：AGPL 边界允许 web 依赖 servers
（见 ``the release checks``），且服务器侧将来也可能需要把
numpy 数据转成可 JSON 类型。本模块只依赖标准库。

另外提供两个跨平台健壮的小工具（Windows 语义与 POSIX 不同）：

* :func:`atomic_write_json`：同目录临时文件 + fsync + ``os.replace``；Windows
  上杀软/索引/OneDrive 或"读取方正打开目标文件"会让 replace 短暂报
  ``PermissionError``，这里按重试处理（之后仍失败才抛）；
* :func:`read_json`：读取时同样重试**共享冲突**（Windows 上 replace 期间读者
  ``open`` 可能短暂 ``PermissionError``），但**不**重试 JSON 解析错误（损坏
  就是损坏，交给调用方处理）。
"""

import json
import os
import tempfile
import time

#: Windows 上 ``os.replace``/``open`` 遇到文件共享冲突时的重试次数与退避基数。
_IO_RETRIES = 6
_IO_BACKOFF_S = 0.05


def jsonable(obj):
    """把对象递归转换为可被 ``json.dumps`` 序列化的 Python 类型。

    Args:
        obj: 任意对象。

    Returns:
        dict|list|str|int|float|bool|None: 序列化友好的值。``dict`` 的值、
        ``list``/``tuple`` 的元素会被递归转换；numpy ndarray 走 ``tolist()``；
        numpy 标量（及任何提供 ``item()`` 的对象）走 ``item()``；无法转换的
        对象原样返回（由调用方的 ``json.dumps`` 决定是否报错）。
    """
    if isinstance(obj, dict):
        return {key: jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(value) for value in obj]
    # ndarray（含 0 维）优先走 tolist：一次拿到嵌套 list，避免逐元素递归
    tolist = getattr(obj, 'tolist', None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:  # noqa: BLE001 - 不是数组语义的对象继续往下试
            pass
    item = getattr(obj, 'item', None)
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001 - 多元素数组等：退化成可读字符串
            return str(obj)
    return obj


def atomic_write_json(path, payload, retries=_IO_RETRIES, indent=2):
    """原子地写 JSON 文件（同目录临时文件 + fsync + ``os.replace``，带重试）。

    Args:
        path (str): 目标文件路径。
        payload: 任意可 JSON 序列化对象。
        retries (int): ``os.replace`` 遇到 Windows ``PermissionError`` 时的
            重试次数（目标文件被其它进程短暂打开）。
        indent (int): ``json.dump`` 缩进。

    Raises:
        OSError|TypeError|ValueError: 最终仍失败时原样抛出；目标文件保持旧内容。
    """
    path = str(path)
    folder = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + '.', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        last = None
        for attempt in range(max(1, int(retries))):
            try:
                os.replace(tmp_path, path)
                last = None
                break
            except PermissionError as exc:  # Windows 共享冲突：退避重试
                last = exc
                time.sleep(_IO_BACKOFF_S * (attempt + 1))
        if last is not None:
            raise last
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # POSIX 上 fsync 目录项（Windows 打开目录会失败，忽略）
    try:
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json(path, default=None, retries=_IO_RETRIES):
    """读取 JSON 文件，Windows 共享冲突时短暂重试。

    Args:
        path (str): 文件路径。
        default: 文件不存在时的返回值。
        retries (int): ``PermissionError``（Windows 上 replace 期间的共享冲突）
            的重试次数。

    Returns:
        object: 解析后的 JSON；文件不存在返回 ``default``。

    Raises:
        json.JSONDecodeError|UnicodeDecodeError: 内容损坏（**不**重试）。
        OSError: 重试后仍无法打开（权限/IO 真错误）。
    """
    path = str(path)
    last = None
    for attempt in range(max(1, int(retries))):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except PermissionError as exc:  # Windows 共享冲突：退避重试
            last = exc
            time.sleep(_IO_BACKOFF_S * (attempt + 1))
    raise last
