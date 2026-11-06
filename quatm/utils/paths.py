# -*- coding: utf-8 -*-
"""项目根感知的路径辅助（对权威定义的再导出）。

权威的 ``PROJECT_ROOT`` / ``CONFIG_DIR`` / ``EXPERIMENTS_DIR`` /
``DATA_DIR`` / ``IMAGES_DIR`` 常量定义在 :mod:`quatm.servers.configreader`
（总是随包部署的模块），即使本便捷模块在客户端缺失也能正常工作。本模块只是
再导出它们，因此 ``from quatm.utils.paths import PROJECT_ROOT`` 对旧代码
依旧可用。

例::

    from quatm.utils.paths import PROJECT_ROOT, CONFIG_DIR

    filename = PROJECT_ROOT / 'configuration' / 'trap_old_paras.json'
    savefile = IMAGES_DIR / (name + '.png')

所有常量均为 :class:`pathlib.Path` 对象（可直接用于 ``open()`` 与 ``os.*``）。
"""
from quatm.servers.configreader import (PROJECT_ROOT, CONFIG_DIR,
                                        EXPERIMENTS_DIR, DATA_DIR, IMAGES_DIR,
                                        QUATM_DIR)

import os

__all__ = ['PROJECT_ROOT', 'CONFIG_DIR', 'EXPERIMENTS_DIR', 'DATA_DIR', 'IMAGES_DIR',
           'QUATM_DIR', 'resolve_experiment_path', 'paths_equal']


def _norm_path(p):
    """路径规范化：绝对化 + realpath + 平台大小写/分隔符归一。

    带进程级缓存：``os.path.realpath`` 会对每个路径组件做一次 ``lstat``（在
    Windows 上还更贵），而 :func:`paths_equal` 每次比较要规范化 2 个路径、
    Looper 查找实验窗口时又要对每个已打开窗口比较一次——实测一个循环条目
    构建就能累积上百次系统调用（这是“每个 Task 条目 ~5 ms、每个 List 条目
    ~55 ms”的主要来源之一）。路径在进程生命周期内不会变（除非用户替换
    符号链接），因此按输入字符串缓存，并用 ``setdefault`` 让已规范化的
    字符串也直接命中。

    Args:
        p (str|Path): 待规范化路径。

    Returns:
        str: 规范化后的路径字符串。
    """
    if not p:
        return ''
    src = str(p)
    hit = _NORM_PATH_CACHE.get(src)
    if hit is not None:
        return hit
    try:
        norm = os.path.normcase(os.path.realpath(os.path.abspath(src)))
    except Exception:  # noqa: BLE001 - 规范化失败时退回原字符串
        norm = os.path.normcase(src)
    _NORM_PATH_CACHE[src] = norm
    _NORM_PATH_CACHE.setdefault(norm, norm)
    return norm


def paths_equal(a, b):
    """判断两个实验文件路径是否指向同一文件（跨平台稳健比较）。

    旧的字符串直接比较在 Windows 上不可靠：大小写不同（路径比较是
    大小写不敏感但 Python 字符串比较敏感）、斜杠方向、prepfile 里的
    陈旧绝对路径经 resolve_experiment_path 重定位后与窗口存储的原始
    路径不一致——任何差异都会让 Looper 每任务**新开一个实验窗口**，
    每个窗口泄露约 16 个文件描述符（Properties socket + 线程），
    几百个任务后 Windows 的 512 上限耗尽 → “Too many open files”。

    Args:
        a (str|Path): 路径 1。
        b (str|Path): 路径 2。

    Returns:
        bool: 是否指向同一文件。
    """
    return _norm_path(a) == _norm_path(b)

def norm_path(p):
    """对外暴露的路径规范化（带进程级缓存，见 :func:`_norm_path`）。

    用途：需要“把一批路径规范化后放进 set/dict 做多次比较”的场景（如
    Loop 条目反复判断“该实验窗口是否已打开”）。逐次调用 :func:`paths_equal`
    是 O(窗口数)，而先规范化一次再查集合是 O(1)，且跳过了 Windows 上
    大小写/分隔符不一致导致的误判。

    Args:
        p (str|Path): 待规范化路径。

    Returns:
        str: 规范化后的路径字符串。
    """
    return _norm_path(p)
# per-process cache of experiment-name -> resolved script path (positive hits)
_EXPERIMENT_PATH_CACHE = {}

# _norm_path() 的进程级缓存：{输入字符串: 规范化结果}。见 _norm_path 文档。
_NORM_PATH_CACHE = {}

# per-process cache of (expname, filepath) -> resolved absolute path (positive
# hits only). 同一个实验脚本会在 loopfile 的多个 List 条目、以及 prepfile 的多个
# 条目里反复出现（一个 List 条目就有几十个子任务）；而解析的第一站是
# ``os.path.isfile``，在 Windows（实时防护扫描）上每次 1~3 ms——实测一个
# List 条目能在这上面耗掉 ~50 ms。只缓存“确实存在”的正值：文件后来被删只是
# 多扫一次目录，不会让“新建的文件永远扫不到”。
_RESOLVED_PATH_CACHE = {}


def resolve_experiment_path(expname, filepath=''):
    """解析实验脚本的绝对路径，对陈旧/相对 filepath 稳健。

    用于 prepfile/loopfile 从旧部署或别的机器拷来、其中存的绝对路径已不存在
    的情形。解析顺序：

    1. 相对 ``filepath`` 拼到 ``EXPERIMENTS_DIR`` 下；
    2. 若该绝对路径存在则返回；
    3. 否则在 ``EXPERIMENTS_DIR`` 内递归搜索 ``<expname>.py``（首个命中，
       按进程缓存）；
    4. 最后兜底：原样返回（绝对）``filepath``。

    Args:
        expname (str): 实验名。
        filepath (str): 存储的文件路径（可为空/相对/陈旧）。

    Returns:
        str: 解析出的绝对路径；无可用结果返回 ''。
    """
    fp = (filepath or '').strip()
    name = (expname or '').strip()
    if fp and not os.path.isabs(fp):
        fp = os.path.join(str(EXPERIMENTS_DIR), fp)
    _key = (name, fp)
    _hit = _RESOLVED_PATH_CACHE.get(_key)
    if _hit:
        return _hit
    if fp and os.path.isfile(fp):
        _resolved = os.path.abspath(fp)
        _RESOLVED_PATH_CACHE[_key] = _resolved
        return _resolved
    found = _find_experiment_by_name(name)
    if found:
        _RESOLVED_PATH_CACHE[_key] = found
        return found
    return os.path.abspath(fp) if fp else ''


def _find_experiment_by_name(expname):
    """在 ``EXPERIMENTS_DIR`` 内递归搜索 ``<expname>.py``（带进程缓存）。

    Args:
        expname (str): 实验名（可带 .py 后缀）。

    Returns:
        str: 找到的绝对路径；未找到返回 ''。
    """
    if not expname:
        return ''
    target = expname if expname.endswith('.py') else expname + '.py'
    hit = _EXPERIMENT_PATH_CACHE.get(target)
    if hit:
        return hit
    root = str(EXPERIMENTS_DIR)
    if not root or not os.path.isdir(root):
        return ''
    for dirpath, _dirnames, filenames in os.walk(root):
        if target in filenames:
            found = os.path.abspath(os.path.join(dirpath, target))
            _EXPERIMENT_PATH_CACHE[target] = found
            return found
    return ''
