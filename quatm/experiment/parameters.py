# -*- coding: utf-8 -*-
"""实验参数存档：GUI Browser 与 Web 后端共用的**唯一实现**。

历史上 GUI（``GUI/browser/experiment_window.py``）和 Web
（``web/experiments.py``）各写了一套“保存/加载参数”，目录、文件名与 JSON
序列化格式逐渐漂移（Web 曾用 ``indent=2`` + ``ensure_ascii=False``，且同一分钟
重名时追加序号；GUI 用 ``json.dump`` 默认格式并覆盖）。本模块把这条规则收拢到
一处，两边都只调用这里的纯函数。

目录与文件名（与 GUI 现有存档**逐字节兼容**）::

    <configuration>/experiment_params/<实验名>/<YYYY_MM_DD>/<HHhMM>_<label>.json
    <configuration>/experiment_params/<实验名>/<YYYY_MM_DD>/backup.json

* 时间戳取本地时间（``datetime.today()``），可用 ``when`` 参数注入以测试；
* 文件名里的 label 先按 Windows/macOS/Linux 的非法字符规则清洗（``/``、``:``
  等替换成 ``_``）；空 label 记作 ``unlabelled``；
* JSON 用 ``json.dumps`` 的**默认**参数（紧凑单行、``ensure_ascii=True``），
  与 GUI 的 ``json.dump(...)`` 输出完全一致——仓库里 ``configuration/
  experiment_params`` 的真实存档（紧凑格式的样例）可逐字节回归；
* 同一分钟内同名保存采用**覆盖**语义（与 GUI 一致；Web 以前追加序号，现已统一）。

备份 ``backup.json`` 的语义与 GUI 相同：写“加载前的当前参数 + ``label`` 为
``backup``”，供用户一键回到加载前状态。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from quatm.utils.i18n import tr

#: 空 label 写进文件名与 JSON 时使用的默认值（与 GUI 一致）。
DEFAULT_LABEL = 'unlabelled'

#: 备份文件的 label 与文件名（与 GUI 一致）。
BACKUP_LABEL = 'backup'
BACKUP_FILENAME = 'backup.json'

#: ``experiment_params`` 目录名（在 ``<configuration>/`` 下）。
PARAMS_DIRNAME = 'experiment_params'

#: 文件名里必须替换掉的字符（Windows 禁止 '/'、':' 等；其余系统保持一致行为）。
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

#: 日期目录的匹配模式（列表时只认 ``YYYY_MM_DD`` 这种目录）。
_DATE_DIR_GLOB = '[0-9]*'


def params_root(project):
    """返回项目的参数存档根目录（``<project>/configuration/experiment_params``）。

    Args:
        project (str|Path): 用户项目根目录。

    Returns:
        Path: 参数存档根目录（不保证存在）。
    """
    return Path(project) / 'configuration' / PARAMS_DIRNAME


def safe_label(label):
    """把 label 清洗成任意系统都能用的文件名片段（与 GUI 的 ``_safe_filename`` 相同）。

    Args:
        label (str): 参数集标签；空值也接受。

    Returns:
        str: 清洗后的标签；全部字符都被删掉时返回 ``unlabelled``。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub('_', str(label or ''))
    return cleaned.strip() or DEFAULT_LABEL


def date_stamp(when=None):
    """返回日期目录名 ``YYYY_MM_DD``。

    Args:
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        str: 形如 ``2024_11_04`` 的目录名。
    """
    return (when or datetime.today()).strftime('%Y_%m_%d')


def time_stamp(when=None):
    """返回文件名里的时间戳 ``HHhMM``（GUI 一直用 ``h`` 分隔，不要改成冒号）。

    Args:
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        str: 形如 ``16h51`` 的时间戳。
    """
    return (when or datetime.today()).strftime('%Hh%M')


def experiment_dir(param_root, experiment_name):
    """返回某实验的参数目录（``<param_root>/<实验名>``），并校验实验名安全。

    Args:
        param_root (str|Path): 参数存档根目录（见 :func:`params_root`）。
        experiment_name (str): 实验名（实验文件的 stem，不含路径）。

    Returns:
        Path: 该实验的参数目录（不保证存在）。

    Raises:
        ValueError: 实验名为空或包含路径分隔符/``..``（防止写出参数根目录）。
    """
    name = str(experiment_name or '').strip()
    # 只允许单级目录名：实验名来自 Path(file).stem / 窗口名，正常不会带分隔符。
    separators = {'/', '\\', '.', '..'}
    if (not name or name in separators
            or os.sep in name or (os.altsep and os.altsep in name)):
        raise ValueError(tr('invalid experiment name'))
    return Path(param_root) / name


def date_dir(param_root, experiment_name, when=None):
    """返回某实验某天的参数目录（``.../<实验名>/<YYYY_MM_DD>``）。

    Args:
        param_root (str|Path): 参数存档根目录。
        experiment_name (str): 实验名。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        Path: 日期目录（不保证存在）。
    """
    return experiment_dir(param_root, experiment_name) / date_stamp(when)


def params_filename(label='', when=None):
    """生成参数文件名 ``<HHhMM>_<label>.json``（不包含目录）。

    Args:
        label (str): 参数集标签（会被 :func:`safe_label` 清洗）。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        str: 文件名，如 ``16h51_Dark ion test.json``。
    """
    return '%s_%s.json' % (time_stamp(when), safe_label(label))


def params_path(param_root, experiment_name, label='', when=None):
    """返回本次保存将写入的参数文件路径（目录按需由 :func:`write_params` 创建）。

    Args:
        param_root (str|Path): 参数存档根目录。
        experiment_name (str): 实验名。
        label (str): 参数集标签。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        Path: 参数文件路径。
    """
    return date_dir(param_root, experiment_name, when) / params_filename(label, when)


def backup_path(param_root, experiment_name, when=None):
    """返回备份文件路径 ``.../<实验名>/<YYYY_MM_DD>/backup.json``。

    Args:
        param_root (str|Path): 参数存档根目录。
        experiment_name (str): 实验名。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        Path: 备份文件路径。
    """
    return date_dir(param_root, experiment_name, when) / BACKUP_FILENAME


def serialize_params(values):
    """把参数字典序列化成**与 GUI 逐字节相同**的 JSON 文本。

    GUI 用的是 ``json.dump(values, file)``（默认参数）：紧凑单行、
    ``ensure_ascii=True``、分隔符 ``", "`` / ``": "``、无尾随换行。仓库里
    ``configuration/experiment_params`` 下所有真实存档都符合其中一种历史格式
    （紧凑或 ``indent=4``），这里固定为“当前 GUI 写出的紧凑格式”。

    Args:
        values (dict): 参数字典。

    Returns:
        str: JSON 文本。
    """
    return json.dumps(values)


def write_params(path, values):
    """把参数字典写入 JSON 文件（自动建目录，覆盖同名文件）。

    Args:
        path (str|Path): 目标文件路径。
        values (dict): 参数字典。

    Returns:
        Path: 实际写入的路径。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialize_params(values), encoding='utf-8')
    return target


def save_params(param_root, experiment_name, values, label='', when=None):
    """保存一组实验参数（文件名带标签，格式与 GUI 一致）。

    Args:
        param_root (str|Path): 参数存档根目录。
        experiment_name (str): 实验名。
        values (dict): 参数名 → 值。
        label (str): 参数集标签；空串记作 ``unlabelled``。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        Path: 写入的参数文件路径。
    """
    data = dict(values or {})
    data['label'] = label or DEFAULT_LABEL
    return write_params(params_path(param_root, experiment_name, label, when),
                        data)


def save_backup(param_root, experiment_name, values, when=None):
    """写 ``backup.json``：保存“加载前的当前参数”，label 固定为 ``backup``。

    Args:
        param_root (str|Path): 参数存档根目录。
        experiment_name (str): 实验名。
        values (dict): 加载前的当前参数。
        when (datetime|None): 注入的时间；None 表示当前本地时间。

    Returns:
        Path: 写入的备份文件路径。
    """
    data = dict(values or {})
    data['label'] = BACKUP_LABEL
    return write_params(backup_path(param_root, experiment_name, when), data)


def read_params(path):
    """读取一个参数 JSON 文件并校验其为对象。

    Args:
        path (str|Path): 参数文件路径（GUI 来自文件对话框；Web 来自校验后的路径）。

    Returns:
        dict: 参数名 → 值（可能含 ``label``）。

    Raises:
        ValueError: 文件内容不是 JSON 对象。
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(tr('parameter file must contain a JSON object'))
    return data


def resolve_params_path(param_root, project_root, experiment_name, relpath):
    """把 Web 客户端传来的“相对项目根”的路径解析成实验目录下的绝对路径。

    只允许落在 ``<param_root>/<实验名>/`` 之内（防 ``../`` 越界读取）。

    Args:
        param_root (str|Path): 参数存档根目录。
        project_root (str|Path): 用户项目根目录（relpath 相对它）。
        experiment_name (str): 实验名。
        relpath (str): 参数文件相对项目根的路径。

    Returns:
        Path: 解析后的绝对路径。

    Raises:
        ValueError: 路径越出该实验的参数目录。
    """
    base = experiment_dir(param_root, experiment_name).resolve()
    target = (Path(project_root) / str(relpath or '')).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(tr('invalid parameter path'))
    return target


def list_params(param_root, project_root, experiment_name):
    """列出某实验已保存的参数集（含备份），新日期在前（与 Web 原来一致）。

    Args:
        param_root (str|Path): 参数存档根目录。
        project_root (str|Path): 用户项目根目录（用于生成相对路径）。
        experiment_name (str): 实验名。

    Returns:
        list[dict]: 每项含 ``path``（相对项目根）/``name``/``date``/``mtime``。
    """
    base = experiment_dir(param_root, experiment_name)
    if not base.is_dir():
        return []
    out = []
    for date_directory in sorted(base.glob(_DATE_DIR_GLOB), reverse=True):
        if not date_directory.is_dir():
            continue
        for path in sorted(date_directory.glob('*.json'), reverse=True):
            out.append({
                'path': str(path.relative_to(project_root)),
                'name': path.stem,
                'date': date_directory.name,
                'mtime': path.stat().st_mtime,
            })
    return out
