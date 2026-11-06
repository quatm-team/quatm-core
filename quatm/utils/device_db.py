"""Read and generate a project ``configuration/device_db.py``.

``device_db.py`` maps the names used by experiments to driver classes and
their constructor arguments. This module parses that file with :mod:`ast` (it
never imports or executes project code), extracts instrument addresses and
generates draft entries for new devices.

The parser has no Qt, hardware or licensing dependencies and is safe to use
from the core CLI, diagnostics and the optional desktop tools.
"""

from quatm.i18n import tr

import ast
import json
import os

DEVICE_DB_REL = os.path.join('configuration', 'device_db.py')

#: ``device_db`` 条目里放设备地址的参数名（见 `quatm/drivers/README.md`）。
ADDRESS_ARGUMENT = 'address'

#: 老项目里存地址的常见参数名；找不到约定字段时按这个顺序再找一遍。
ADDRESS_KEYS = (ADDRESS_ARGUMENT, 'visa_address', 'resource', 'resource_name',
                'com_port', 'comport', 'serial', 'serial_port', 'ip', 'host',
                'hostname', 'socket', 'port')

#: 仪器地址常见的 URL 前缀（能直接交给 `comm.open_transport`）。
_ADDRESS_SCHEMES = ('serial://', 'asrl://', 'tcp://', 'socket://', 'scpi://',
                    'gpib://', 'visa://', 'instr://')

_ENTRY_TYPES = ('attr', 'generic_attr', 'driver', 'local')

#: 渲染 ``arguments`` 时的键序：与实验室现有 ``device_db.py`` 的写法对齐
#: （先通道/从设备，再单位与量程，最后是地址），未列出的键按字母序补在后面。
ARGUMENT_ORDER = ('channel', 'device', 'command', 'logic', 'display_unit',
                  'gaugefile', 'multiplier', 'minval', 'maxval', 'step',
                  'function_kwargs', 'strict_gauge', 'address')


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def project_root(root=None):
    """返回当前项目根目录（``QUATM_PROJECT_ROOT`` 优先，否则工作目录）。

    Args:
        root (str|None): 显式指定的根目录；``None`` 时读环境变量/当前目录。

    Returns:
        str: 归一化后的绝对路径。
    """
    return os.path.abspath(root or os.environ.get('QUATM_PROJECT_ROOT') or '.')


def device_db_path(root=None):
    """返回项目里 ``configuration/device_db.py`` 的完整路径（不检查存在性）。

    Args:
        root (str|None): 项目根目录；``None`` 时用 `project_root()`。

    Returns:
        str: device_db 文件路径。
    """
    return os.path.join(project_root(root), DEVICE_DB_REL)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_device_db(root=None):
    """解析项目 ``configuration/device_db.py``，返回已登记设备条目。

    在 ``ast`` 上逐条读取：能字面量求值的条目直接取；含表达式（如 ``os.path.join``
    或引用其它名字）的条目**尽力而为**——仍然取出该条的字符串字段，取不到的字段
    写成 ``'<expression>'`` 并把 ``partial`` 置真（界面会提示）。全程**不导入不执行**
    项目代码。

    Args:
        root (str|None): 项目根目录；``None`` 时用 `project_root()`。

    Returns:
        tuple[list[dict], str]: 条目列表与一句说明。每个条目含
        ``name``/``type``/``module``/``class``/``group``/``arguments``/``partial``。
    """
    path = device_db_path(root)
    if not os.path.exists(path):
        return [], 'no device_db.py at %s' % path
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            tree = ast.parse(handle.read())
    except OSError as exc:
        return [], 'cannot read device_db.py: %s' % exc
    except SyntaxError as exc:
        return [], 'device_db.py has a syntax error: %s' % exc
    table = _find_device_db_node(tree)
    if table is None:
        return [], 'no device_db = {...} table found in %s' % path
    entries = []
    partial = 0
    for key_node, value_node in zip(table.keys, table.values):
        name = _const_str(key_node)
        literal = _literal(value_node)
        is_partial = not isinstance(literal, dict)
        spec = literal if not is_partial else _shallow_dict(value_node)
        if name is None or not spec:
            partial += 1
            continue
        if is_partial:
            partial += 1
        arguments = spec.get('arguments')
        entries.append({
            'name': name,
            'type': _as_text(spec.get('type')),
            'module': _as_text(spec.get('module')),
            'class': _as_text(spec.get('class')),
            'group': _as_text(spec.get('group')),
            'arguments': arguments if isinstance(arguments, dict) else {},
            'partial': is_partial,
        })
    entries.sort(key=lambda item: (item['type'], item['name']))
    note = tr('%d device(s) registered in device_db.py') % len(entries)
    if partial:
        note += tr(' (%d entry/entries contain expressions)') % partial
    return entries, note


def entry_address(entry):
    """取出一条 device_db 条目里登记的设备地址。

    先看约定字段 ``arguments['address']``；没有时再按参数名优先级（见
    `ADDRESS_KEYS`）与地址样式（`looks_like_address`）在 ``arguments`` 里挑一个——
    老项目常把地址写成 ``com_port`` / ``resource`` / ``serial`` 之类。

    Args:
        entry (dict): `parse_device_db()` 返回的条目（或等价的字典）。

    Returns:
        str: 设备地址；没有时返回空串。
    """
    arguments = (entry or {}).get('arguments') or {}
    if not isinstance(arguments, dict):
        return ''
    explicit = arguments.get(ADDRESS_ARGUMENT, '')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for key in ADDRESS_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and looks_like_address(value):
            return value.strip()
    for value in arguments.values():
        if isinstance(value, str) and looks_like_address(value):
            return value.strip()
    return ''


def looks_like_address(value):
    """判断一个字符串是否像仪器地址（VISA / 串口 / 网络）。

    判据宁宽勿严——诊断与设备管理器只拿它挑候选，最终是否可用由 `probe()` 决定：

    * 含 ``::``（VISA 资源名，如 ``USB0::0x1AB1::…::INSTR``）；
    * 以 ``serial://`` / ``asrl://`` / ``tcp://`` / ``socket://`` / ``scpi://``
      / ``gpib://`` 开头；
    * 以 ``/dev/`` 开头（POSIX 串口设备路径）或形如 ``COM3``；
    * 形如 ``192.168.0.30`` 或 ``192.168.0.30:5025``。

    Args:
        value (str): 待判断的字符串。

    Returns:
        bool: 像设备地址时返回 ``True``。
    """
    text = str(value or '').strip()
    if not text:
        return False
    if '::' in text:
        return True
    if text.lower().startswith(_ADDRESS_SCHEMES):
        return True
    if text.startswith('/dev/'):
        return True
    return _is_com_port(text) or _is_ip_address(text)


def _is_com_port(text):
    """判断是否是 ``COM3`` 形式的 Windows 串口名。

    Args:
        text (str): 待判断的字符串。

    Returns:
        bool: 形如 ``COM`` + 数字时为 ``True``。
    """
    upper = str(text).upper()
    return upper.startswith('COM') and len(upper) > 3 and upper[3:].isdigit()


def _is_ip_address(text):
    """判断是否是 ``1.2.3.4`` 或 ``1.2.3.4:5025`` 形式的网络地址。

    Args:
        text (str): 待判断的字符串。

    Returns:
        bool: 四段点分十进制（可带数字端口）时为 ``True``。
    """
    host, _, port = str(text).partition(':')
    if port and not port.isdigit():
        return False
    parts = host.split('.')
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def addresses(entries):
    """收集条目里所有非空设备地址（去重、保持出现顺序）。

    Args:
        entries (iterable[dict]): `parse_device_db()` 返回的条目序列。

    Returns:
        list[str]: 地址列表。
    """
    seen = set()
    out = []
    for entry in entries or ():
        address = entry_address(entry)
        if address and address not in seen:
            seen.add(address)
            out.append(address)
    return out


def entry_counts(entries):
    """按条目类型统计数量，供界面/日志显示。

    Args:
        entries (iterable[dict]): `parse_device_db()` 返回的条目序列。

    Returns:
        dict[str, int]: 形如 ``{'driver': 12, 'attr': 83}``（只含出现过的类型）。
    """
    counts = {}
    for entry in entries or ():
        kind = str((entry or {}).get('type') or 'unknown')
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _find_device_db_node(tree):
    """在模块 AST 里找到 ``device_db = {...}`` 的字典节点。

    Args:
        tree (ast.Module): 已经解析好的模块语法树。

    Returns:
        ast.Dict|None: 字典节点；没找到则返回 ``None``。
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == 'device_db' \
                and isinstance(node.value, ast.Dict):
            return node.value
    return None


def _literal(node):
    """对单个 AST 节点做字面量求值，失败时返回 ``None``。

    Args:
        node (ast.AST): 待求值的节点。

    Returns:
        求值结果；含表达式/名字引用时返回 ``None``。
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _const_str(node):
    """取出常量字符串键（非字符串或非常量返回 ``None``）。

    Args:
        node (ast.AST): 字典的键节点。

    Returns:
        str|None: 字符串键，或 ``None``。
    """
    value = _literal(node)
    return value if isinstance(value, str) else None


def _shallow_dict(node):
    """在字面量求值失败时逐键尽力取值（取不到的写占位符）。

    Args:
        node (ast.AST): 期望是字典的节点。

    Returns:
        dict: 能取到的键值对；不是字典时返回空字典。
    """
    if not isinstance(node, ast.Dict):
        return {}
    out = {}
    for key_node, value_node in zip(node.keys, node.values):
        key = _const_str(key_node)
        if key is None:
            continue
        value = _literal(value_node)
        out[key] = value if value is not None else '<expression>'
    return out


def _as_text(value):
    """把字段值归一成字符串（``None`` / 占位符都变成可显示的文本）。

    Args:
        value: 任意字段值。

    Returns:
        str: 用于展示与比对的字符串（缺值时为空串）。
    """
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def qualify_module(module, package='quatm.drivers.'):
    """把驱动目录里的相对模块名补成 ``device_db.py`` 可直接解析的完整路径。

    `quatm.drivers.catalog` 给的是相对驱动目录的名字（``'rigol.dp1116a'``），
    而 device_db 条目习惯写完整包路径（``'quatm.drivers.rigol.dp1116a'``，解析时
    优先用项目内同名驱动）。已经是完整路径或项目内写法的一律原样返回。

    Args:
        module (str): 模块名，可能已是 ``quatm.drivers.…`` 或 ``drivers.…``。
        package (str): 要补上的包前缀。

    Returns:
        str: 完整模块路径（空输入返回空串）。
    """
    name = str(module or '').strip()
    if not name:
        return ''
    if name.startswith(('quatm.', 'drivers.')):
        return name
    return package + name


def device_db_entry(name, module, entry_class, address=None, entry_type='driver',
                    group=None, arguments=None):
    """组装一条 ``device_db.py`` 条目（字典形式，便于再加工或渲染成源码）。

    地址默认写进 ``arguments['address']``——这是 `quatm/drivers/README.md` 约定的
    仪器地址字段；纯属性类条目（``entry_type='attr'``）可传 ``address=None``。

    Args:
        name (str): 实验里使用的设备名（``self.<name>``）。
        module (str): 驱动模块路径，如 ``'quatm.drivers.rigol.dp1116a'``。
        entry_class (str): 入口类名，如 ``'RigolDP1116A'``。
        address (str|None): 设备地址；``None`` 时不写 address 参数。
        entry_type (str): ``'attr'`` / ``'generic_attr'`` / ``'driver'`` / ``'local'``。
        group (str|None): Dashboard 分组名；``None`` 时不写。
        arguments (dict|None): 追加的构造参数，与 address 合并。

    Returns:
        dict: ``{'name', 'type', 'module', 'class', 'group', 'arguments'}``。
    """
    merged = dict(arguments or {})
    if address:
        merged.setdefault(ADDRESS_ARGUMENT, str(address))
    return {
        'name': str(name),
        'type': str(entry_type or 'driver'),
        'module': qualify_module(module),
        'class': str(entry_class),
        'group': str(group) if group else '',
        'arguments': merged,
    }


def format_device_db_entry(entry, indent=4, inner_indent=None):
    """把 `device_db_entry()` 的结果渲染成可直接粘贴进 ``device_db.py`` 的源码。

    ``arguments`` 会先经 `ordered_arguments()` 排序（与实验室现有文件的写法
    顺序一致），再用 JSON 风格渲染（双引号，与文件里大多数条目一致）。

    Args:
        entry (dict): `device_db_entry()` 返回的条目。
        indent (int): 条目名前的缩进空格数（``device_db = {`` 内层用 4）。
        inner_indent (int|None): 字段行缩进；``None`` 时用 ``indent + 4``。
            解析现有文件时常用 12（项目里手写风格），详见 `file_style()`。

    Returns:
        str: 多行源码文本（不含外层大括号）。
    """
    pad = ' ' * indent
    inner = ' ' * (indent + 4 if inner_indent is None else inner_indent)
    lines = ['%s"%s": {' % (pad, entry.get('name', ''))]
    lines.append('%s"type": "%s",' % (inner, entry.get('type', 'driver')))
    lines.append('%s"module": "%s",' % (inner, entry.get('module', '')))
    lines.append('%s"class": "%s",' % (inner, entry.get('class', '')))
    group = entry.get('group')
    if group:
        lines.append('%s"group": "%s",' % (inner, group))
    arguments = ordered_arguments(entry.get('arguments') or {})
    rendered = json.dumps(arguments, ensure_ascii=False)
    lines.append('%s"arguments": %s,' % (inner, rendered))
    lines.append('%s},' % pad)
    return '\n'.join(lines)


def ordered_arguments(arguments):
    """按项目习惯给 ``arguments`` 排序，未列出的键按字母序排在后面。

    Args:
        arguments (dict): 原始构造参数字典。

    Returns:
        dict: 排序后的新字典（不修改入参）。
    """
    known = [key for key in ARGUMENT_ORDER if key in arguments]
    rest = sorted(key for key in arguments if key not in ARGUMENT_ORDER)
    return {key: arguments[key] for key in known + rest}


def attribute_classes():
    """列出 `quatm.experiment.attributes` 里可登记进 device_db 的属性类名。

    惰性导入（延到调用时）：`quatm.utils` 是随包分发的核心，不该在 import
    期就把实验层拉起来。

    Returns:
        list[str]: 类名列表（字母序）；实验层不可用时返回空列表。
    """
    try:
        import inspect

        from quatm.experiment import attributes
    except Exception:                 # noqa: BLE001  实验层依赖缺失时不影响 GUI
        return []
    return sorted(name for name, obj in inspect.getmembers(attributes,
                                                          inspect.isclass)
                  if obj.__module__ == attributes.__name__)


def project_groups(entries):
    """从已解析的条目里汇总出用过的分组名（含 ``hidden``）。

    Args:
        entries (list[dict]): `parse_device_db()` 的条目列表。

    Returns:
        list[str]: 分组名列表（字母序，已去重）。
    """
    groups = {entry.get('group') for entry in entries if entry.get('group')}
    groups.add('hidden')
    return sorted(groups)


def entry_names(entries):
    """从已解析的条目里取全部设备名（供“从设备”下拉框使用）。

    Args:
        entries (list[dict]): `parse_device_db()` 的条目列表。

    Returns:
        list[str]: 设备名列表（字母序，已去重）。
    """
    return sorted({entry.get('name') for entry in entries if entry.get('name')})


def file_style(path):
    """读出文件里既有的缩进风格，供插入新条目时保持版式一致。

    Args:
        path (str): ``device_db.py`` 路径。

    Returns:
        tuple[int, int]: ``(条目名缩进, 字段行缩进)``；读不到时用 ``(4, 12)``。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.read().splitlines()
    except OSError:
        return 4, 12
    keys = [line for line in lines
            if line.lstrip().startswith('"') and line.rstrip().endswith('{')]
    if not keys:
        return 4, 12
    indent = len(keys[0]) - len(keys[0].lstrip(' '))
    # 字段行缩进取“出现次数最多”的那个：老文件里 8 与 12 混着写，以多数为准。
    counted = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('"type"'):
            width = len(line) - len(line.lstrip(' '))
            counted[width] = counted.get(width, 0) + 1
    inner = max(counted, key=counted.get) if counted else indent + 4
    return indent or 4, inner


def insert_entry(path, entry, backup=True):
    """把一条条目写进 ``device_db.py`` 的 ``device_db`` 表末尾。

    安全性：① 先检查重名，重名直接报错（不会默默生成两个同名字段）；
    ② 默认先把原文件复制成 ``<path>.bak``；③ 写完立即 `ast.parse` 校验，
    语法不过就回滚成备份内容。全程不导入、不执行项目代码。

    Args:
        path (str): ``device_db.py`` 路径。
        entry (dict): `device_db_entry()` 的结果。
        backup (bool): 为真时写 ``<path>.bak`` 备份。

    Returns:
        dict: ``{'path', 'backup', 'snippet', 'entry'}``。

    Raises:
        ValueError: 条目名重复、文件里找不到 ``device_db`` 表。
        OSError: 文件读写失败。
    """
    name = str(entry.get('name') or '')
    if not name:
        raise ValueError('entry has no name')
    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError('%s has a syntax error: %s' % (path, exc)) from exc
    table = _find_device_db_node(tree)
    if table is None:
        raise ValueError('no device_db = {...} table in %s' % path)
    existing = [key.value for key in table.keys if isinstance(key, ast.Constant)]
    if name in existing:
        raise ValueError('device_db already has an entry named %r' % name)
    indent, inner = file_style(path)
    snippet = format_device_db_entry(entry, indent=indent,
                                     inner_indent=inner)
    lines = source.splitlines(keepends=True)
    # 表的右花括号所在行（ast 给的是 1-based）：新条目插到它前面。
    close_index = table.end_lineno - 1
    while close_index > 0 and not lines[close_index - 1].strip().startswith('}'):
        close_index -= 1
    insert_at = data_index = close_index
    block = snippet + '\n'
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1                      # 插到既有条目之后、空行之前
    new_source = (''.join(lines[:insert_at]) + '\n' + block
                  + ''.join(lines[insert_at:]))
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        raise ValueError('refusing to write: the result would not parse (%s)'
                         % exc) from exc
    backup_path = None
    if backup:
        backup_path = path + '.bak'
        with open(backup_path, 'w', encoding='utf-8') as handle:
            handle.write(source)
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(new_source)
    except OSError:
        if backup_path and os.path.exists(backup_path):
            with open(backup_path, 'r', encoding='utf-8') as handle:
                restored = handle.read()
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(restored)
        raise
    return {'path': path, 'backup': backup_path, 'snippet': snippet,
            'entry': entry, 'line': data_index}
