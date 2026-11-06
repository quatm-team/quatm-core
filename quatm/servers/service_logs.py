"""服务子进程的日志文件（按大小轮转），供 ProcessManager 与 Web 进程管理共用。

**为什么需要**：进程管理器以前用 ``subprocess.Popen(cmd, env=env)`` 启动服务，
子进程继承父进程的 stdout/stderr。服务被常驻拉起的场景下这有两个问题：

1. 如果父进程的 stdio 是管道（GUI 从启动器/ProcessManager 子进程拉起、Web
   服务由前台 shell 或 systemd 拉起），长期运行的服务输出会把管道填满，子进程
   写日志时**阻塞**——服务表现为“卡死”；
2. 即使不阻塞，输出也没有留存：事后排查“服务为什么崩了”只剩现场，没有日志。

这里把子进程的 stdout/stderr 重定向到 ``<项目根>/configuration/logs/`` 下的
文件，并在打开前按大小轮转，避免无限增长。

约定：

* 文件名安全：``category`` / ``name`` 里除 ``[A-Za-z0-9._-]`` 之外一律替换成
  ``_``，因此配置里带空格、``/``、``:`` 的条目名不会写出子目录或非法文件名；
* 轮转是**打开前按大小**做一次：当前文件 ≥ ``max_bytes`` 时，删掉最旧的
  ``.N``，其余 ``.k → .k+1``，当前 → ``.1``，然后以追加方式重新打开。不做
  运行中的定时轮转（服务输出量远低于此，一次启动一轮足够）；
* **尽力而为**：轮转/打开失败只往 stderr 打一条 ``tr()`` 警告并返回 ``None``，
  绝不抛异常、绝不让服务起不来（调用方拿到 None 时退回继承父进程 stdio）。
  Windows 上日志文件正被写时 rename 会 ``PermissionError``：放弃本次轮转、
  继续追加即可，不阻塞启动。

用法::

    from quatm.servers.service_logs import open_service_log

    handle = open_service_log('Servers', 'Message Hub', project_root=root)
    proc = subprocess.Popen(cmd, env=env, stdout=handle,
                            stderr=subprocess.STDOUT if handle else None)
    ...
    proc.wait()
    if handle is not None:
        handle.close()
"""

import os
import re
import sys

from quatm.utils.i18n import tr

#: 允许出现在日志文件名里的字符；其它字符统一替换成 ``_``。
_SAFE_RE = re.compile(r'[^A-Za-z0-9._-]')

#: 默认单文件上限 20 MiB、保留 5 份历史（.1 … .5）。
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUPS = 5


def _warn(text):
    """把一条已经翻译好的警告打到 stderr（清理/启动路径上绝不抛）。

    Args:
        text (str): 警告文本。
    """
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001 - 打日志失败不能反过来弄崩调用方
        pass


def _project_root(project_root=None):
    """解析项目根：显式参数优先，否则用 :mod:`quatm.servers.configreader`。

    Args:
        project_root (str|os.PathLike|None): 显式项目根。

    Returns:
        str|None: 项目根路径；无法解析时返回 None。
    """
    if project_root not in (None, ''):
        return str(project_root)
    try:
        from quatm.servers.configreader import PROJECT_ROOT
        return str(PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - 没有项目时日志目录不可用，退化即可
        return None


def _safe_component(text):
    """把标题/名字消毒成单段安全文件名（去掉路径分隔符与隐藏文件前缀）。

    Args:
        text: 原始组件（category 或服务名）。

    Returns:
        str: 只含 ``[A-Za-z0-9._-]`` 的片段；空串回退成 ``'unknown'``。
    """
    cleaned = _SAFE_RE.sub('_', str(text if text is not None else '')).strip()
    cleaned = cleaned.strip('._-')
    return cleaned or 'unknown'


def _filename(category, name):
    """由类别与服务名拼出日志文件名（例如 ``Servers_Message_Hub.log``）。

    Args:
        category: 类别（如 ``'Servers'``、``'Servers/'``）。
        name: 服务名（如 ``'Message Hub'``）。

    Returns:
        str: 单段日志文件名。
    """
    return '%s_%s.log' % (_safe_component(category), _safe_component(name))


def log_dir(project_root=None):
    """返回（并按需创建）``<项目根>/configuration/logs/``。

    Args:
        project_root (str|os.PathLike|None): 项目根；None 用当前项目根。

    Returns:
        str|None: 日志目录绝对路径；创建失败时返回 None（并打印 tr 警告）。
    """
    root = _project_root(project_root)
    if not root:
        return None
    path = os.path.join(root, 'configuration', 'logs')
    try:
        os.makedirs(path, mode=0o755, exist_ok=True)
    except OSError as exc:
        _warn(tr('[service_logs] could not create log directory %s: %s')
              % (path, exc))
        return None
    return path


def log_path(category, name, project_root=None):
    """返回某个服务日志的完整路径（纯字符串计算，不创建目录、不碰磁盘）。

    Args:
        category: 类别（``'Servers'`` / ``'GUI'`` …；末尾的 ``/`` 会被消毒成
            ``_``，建议传不带斜杠的值）。
        name: 服务/条目名。
        project_root (str|os.PathLike|None): 项目根；None 用当前项目根。

    Returns:
        str|None: 形如 ``<root>/configuration/logs/Servers_Web_Hub.log``；
        解析不到项目根时返回 None。
    """
    root = _project_root(project_root)
    if not root:
        return None
    return os.path.join(root, 'configuration', 'logs',
                        _filename(category, name))


def _rotate(path, backups):
    """把已存在的日志按 ``.1 … .N`` 轮转一次（失败返回 False，调用方继续追加）。

    Args:
        path (str): 当前日志路径。
        backups (int): 保留的历史份数（0 表示直接删掉当前文件重新开始）。

    Returns:
        bool: 轮转成功（或无需轮转的删除成功）为 True；失败为 False。
    """
    try:
        if backups <= 0:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return True
        oldest = '%s.%d' % (path, backups)
        try:
            os.remove(oldest)
        except FileNotFoundError:
            pass
        # 从最旧往最新挪：.N-1 -> .N … .1 -> .2，最后 当前 -> .1
        for index in range(backups - 1, 0, -1):
            src = '%s.%d' % (path, index)
            if os.path.exists(src):
                os.replace(src, '%s.%d' % (path, index + 1))
        os.replace(path, path + '.1')
        return True
    except PermissionError as exc:
        # Windows：文件正被另一个进程写，rename 被占用。放弃本次轮转，
        # 继续追加到当前文件（绝不因为轮转失败而让服务起不来）。
        _warn(tr('[service_logs] could not rotate %s, appending to the current '
                 'file: %s') % (path, exc))
        return False
    except OSError as exc:
        _warn(tr('[service_logs] could not rotate %s, appending to the current '
                 'file: %s') % (path, exc))
        return False


def open_service_log(category, name, *, project_root=None,
                     max_bytes=DEFAULT_MAX_BYTES, backups=DEFAULT_BACKUPS):
    """打开（必要时先轮转）某个服务的日志文件，返回可交给 Popen 的追加句柄。

    调用方把返回值直接传给 ``subprocess.Popen(stdout=handle,
    stderr=subprocess.STDOUT)``，并在进程结束后 ``close()``。返回 ``None``
    表示日志不可用（目录/文件建不出来），此时调用方应退回继承父进程 stdio，
    **不要**因此拒绝启动服务。

    Args:
        category: 类别（如 ``'Servers'``）。
        name: 服务/条目名（如 ``'Message Hub'``）。
        project_root (str|os.PathLike|None): 项目根；None 用当前项目根。
        max_bytes (int): 单文件上限；当前文件达到该大小时先轮转。
        backups (int): 保留的历史份数。

    Returns:
        io.BufferedWriter|None: 以 ``'ab'`` 打开的文件对象；失败返回 None。
    """
    folder = log_dir(project_root)
    if folder is None:
        return None
    path = os.path.join(folder, _filename(category, name))
    try:
        if max_bytes and max_bytes > 0 and os.path.exists(path) \
                and os.path.getsize(path) >= max_bytes:
            # 失败也不影响追加：_rotate 自己打印警告并返回 False。
            _rotate(path, backups)
        return open(path, 'ab')
    except OSError as exc:
        _warn(tr('[service_logs] could not open %s: %s') % (path, exc))
        return None
