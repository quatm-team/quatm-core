"""构建身份（build identity）：让“源码已修、部署机旧构建”一眼可见。

受保护构建（Cython / ``.c`` 自动编译分发）在打包时会把 commit / 构建时间 /
mode / dirty 写进 ``quatm/_build_info.py``（由
``tools/protect/build_protected.py`` 生成，**勿手改**）；普通源码树没有这个
文件。本模块把两种情形统一成一个 dict，供 GUI「关于」、GUI/Web 启动日志与
``GET /api/health`` 展示：

    源码树：     {'version': '1.0.1', 'commit': None, 'build_time': None,
                  'mode': 'source', 'dirty': None}
    保护构建：   {'version': '1.0.1', 'commit': '6c2e8fb',
                  'build_time': '2026-11-04T20:40:12Z', 'mode': 'protected',
                  'dirty': False}

设计约束（有意为之）：

* **不 import Qt、不做网络、不跑 git 子进程**：这段信息在 GUI 启动与 Web 健康
  检查的最早路径上被读取，必须廉价；源码树也**不猜** commit——猜出来的值只会
  误导排障（旧构建恰恰是这次要暴露的问题）；
* 不做缓存：同一进程内构建信息不变，但缓存会让测试注入假 ``_build_info`` 变难。
"""

import importlib

import quatm

#: 受保护构建生成的包内模块名（普通源码树里不存在）。
_BUILD_MODULE = 'quatm._build_info'

#: :func:`get_build_info` 返回的固定键（展示代码与测试都依赖这个形状）。
BUILD_INFO_KEYS = ('version', 'commit', 'build_time', 'mode', 'dirty')


def _generated_fields():
    """读取受保护构建嵌入的 ``quatm._build_info`` 字段。

    Returns:
        dict|None: ``{'commit', 'build_time', 'mode', 'dirty'}``；没有该模块
        （源码树）或读取失败时返回 ``None``。
    """
    try:
        module = importlib.import_module(_BUILD_MODULE)
    except ImportError:
        return None
    return {
        'commit': getattr(module, 'BUILD_COMMIT', None),
        'build_time': getattr(module, 'BUILD_TIME', None),
        'mode': getattr(module, 'BUILD_MODE', None),
        'dirty': getattr(module, 'BUILD_DIRTY', None),
    }


def get_build_info():
    """返回当前运行实例的构建身份。

    Returns:
        dict: ``{'version': str, 'commit': str|None, 'build_time': str|None,
        'mode': 'source'|'protected', 'dirty': bool|None}``。
        ``commit`` / ``build_time`` 为 ``None`` 表示未嵌入构建（源码树）；
        ``dirty`` 为 ``None`` 表示未知。
    """
    info = {
        'version': str(getattr(quatm, '__version__', 'unknown')),
        'commit': None,
        'build_time': None,
        'mode': 'source',
        'dirty': None,
    }
    generated = _generated_fields()
    if generated is None:
        return info
    commit = generated.get('commit')
    if commit:
        info['commit'] = str(commit)
    build_time = generated.get('build_time')
    if build_time:
        info['build_time'] = str(build_time)
    dirty = generated.get('dirty')
    if isinstance(dirty, bool):
        info['dirty'] = dirty
    elif dirty is not None:
        info['dirty'] = bool(dirty)
    info['mode'] = str(generated.get('mode') or 'protected')
    return info


def _format_build_time(value):
    """把 ISO 构建时间压成 ``YYYY-MM-DD HH:MM``（不可解析时原样返回）。

    Args:
        value (str|None): 构建时间（UTC ISO 8601）。

    Returns:
        str: 人类可读时间；空值返回空串。
    """
    text = str(value).strip() if value is not None else ''
    if not text:
        return ''
    try:
        from datetime import datetime
        # Python 3.9 的 fromisoformat 不认 'Z' 后缀，先换成显式时区。
        stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return stamp.strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return text


def format_build_info(info=None):
    """把构建信息格式化成单行人类可读文本（关于对话框/启动日志/健康检查共用）。

    Args:
        info (dict|None): :func:`get_build_info` 的结果；``None`` 时现取。

    Returns:
        str: 源码树形如 ``'1.0.1+source (no build identity)'``；保护构建形如
        ``'1.0.1+protected commit 6c2e8fb, built 2026-11-04 20:40 (dirty)'``。
    """
    data = get_build_info() if info is None else dict(info)
    version = str(data.get('version') or 'unknown')
    mode = str(data.get('mode') or 'source')
    line = '%s+%s' % (version, mode)
    commit = data.get('commit')
    if commit:
        line += ' commit %s' % commit
    built = _format_build_time(data.get('build_time'))
    if built:
        line += ', built %s' % built
    if data.get('dirty'):
        line += ' (dirty)'
    if mode == 'source' and not commit:
        line += ' (no build identity)'
    return line
