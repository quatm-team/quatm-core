"""Read the project configuration and resolve user-project paths.

``QUATM_PROJECT_ROOT`` is resolved in this order:

1. the ``QUATM_PROJECT_ROOT`` environment variable;
2. the nearest parent containing ``configuration/configfile/configfile.json``
   or a ``.quatm_project`` marker;
3. a legacy repository-layout fallback.

The module is usable from any working directory once the package is installed;
it does not require the old ``<repo>/quatm`` layout.
"""


import os
import json
import sys
import threading

import datetime
import ipaddress
import logging
import shutil

from pathlib import Path

# 激活检查**不在这里**（AGPL 子包不得在 import 期依赖commercial licensing）：
# 入口层统一调用 quatm.licensing.require_license()，边界见
# the release checks。

# ---------------------------------------------------------------------------
# package-internal locations (always derived from this file's own position)
# ---------------------------------------------------------------------------
QUATM_DIR = Path(__file__).resolve().parent.parent          # the quatm package
driverpath = os.path.realpath(str(QUATM_DIR / 'drivers'))
iconpath = os.path.realpath(str(QUATM_DIR / 'icons'))

# ---------------------------------------------------------------------------
# user-project locations (resolved as described above)
# ---------------------------------------------------------------------------
_PROJECT_MARKERS = (
    Path('configuration') / 'configfile' / 'configfile.json',
    Path('.quatm_project'),
)
_CONFIGFILE_MARKER = Path('configuration') / 'configfile' / 'configfile.json'

# 最近使用的项目根状态文件：许可工具（license_manager 等）从任意目录运行
# 时用它定位项目根读取 .license 激活状态（core.pyx find_license_file 与
# licensing.tool._project_root 读同一个文件）。
_PROJECT_ROOT_HINT = os.path.expanduser('~/.quatm_project_root')


def _remember_project_root(p):
    """把项目根写入用户级状态文件（写失败不影响主流程）。

    Args:
        p (pathlib.Path): 用户项目根目录。
    """
    try:
        with open(_PROJECT_ROOT_HINT, 'w', encoding='utf-8') as f:
            f.write(str(p))
    except OSError:  # noqa: BLE001
        pass


def _find_project_root():
    """定位当前用户项目根目录。

    解析顺序（按可靠性从高到低）：

    1. 环境变量 ``QUATM_PROJECT_ROOT``——但会验证该目录下确有项目标记，
       否则多半是残留/指错的环境变量，忽略并继续；
    2. 从 cwd 向上逐层找 **真实存在的 configfile.json**（自下而上取最近）；
    3. 仍找不到时，从 cwd 向上找 ``.quatm_project`` 标记；
    4. 兜底：包目录的父目录的父目录（旧仓库布局）。

    注意：configfile.json 比 .quatm_project 优先级高。用户可能在项目子目录
    （如 analysis/）残留了 .quatm_project 或旧的 configuration 目录，若把
    两者同权处理，项目根会被误判到子目录，导致 Config() 打不开文件。
    """
    env = os.environ.get('QUATM_PROJECT_ROOT', '').strip()
    if env:
        p = Path(env).resolve()
        if any((p / marker).is_file() for marker in _PROJECT_MARKERS):
            _remember_project_root(p)
            return p

    start = Path.cwd().resolve()
    candidates = (start, *start.parents)
    for candidate in candidates:
        if (candidate / _CONFIGFILE_MARKER).is_file():
            _remember_project_root(candidate)
            return candidate
    for candidate in candidates:
        if (candidate / '.quatm_project').is_file():
            _remember_project_root(candidate)
            return candidate
    # 首次运行向导选定的项目根（用户级提示文件）：从任意目录启动都能回到它。
    # 放在 cwd 搜索之后：当前目录里确实有项目时，以“就地运行”为准。
    try:
        with open(_PROJECT_ROOT_HINT, encoding='utf-8') as hint:
            remembered = Path(hint.read().strip())
        if remembered.is_dir() and any((remembered / m).is_file()
                                       for m in _PROJECT_MARKERS):
            return remembered
    except OSError:
        pass

    # legacy repo layout: quatm lives at <repo>/quatm
    return QUATM_DIR.parent.parent


PROJECT_ROOT = _find_project_root()
workpath = os.path.realpath(str(PROJECT_ROOT))

CONFIG_DIR = PROJECT_ROOT / 'configuration'
EXPERIMENTS_DIR = PROJECT_ROOT / 'experiments'
DATA_DIR = PROJECT_ROOT / 'Data'
IMAGES_DIR = PROJECT_ROOT / 'images'

configpath = os.path.realpath(str(CONFIG_DIR))
datapath = os.path.realpath(str(DATA_DIR))
experiment_path = os.path.realpath(str(EXPERIMENTS_DIR))

configfilename = str(CONFIG_DIR / 'configfile' / 'configfile.json')
propertyfilename = str(CONFIG_DIR / 'properties' / 'properties.json')

# 默认 tcp 端口表：以 Servers 端点 ipc 路径的 basename 为键（如 imagepub）。
# Windows 上 pyzmq 默认不支持 ipc://，由 Config() 自动改写为仅回环的
# tcp://127.0.0.1:<port>；端口集中在 56xx 段避免冲突，可用 configfile
# 顶层 "_tcp_ports": {...} 按项目覆盖（多项目同机运行时用不同端口错开）。
# 与 quatm/web/endpoints.py 共用同一张表（该模块从此处导入）。
DEFAULT_TCP_PORTS = {
    'imagepub': 5601, 'imagesub': 5602,
    'controlpub': 5603, 'controlsub': 5604,
    'datapub': 5605, 'datasub': 5606,
    'propertypub': 5607, 'propertysub': 5608,
    'messagepub': 5609, 'messagesub': 5610,
    'propertyrep': 5611,
}


def server_endpoints(conf, name):
    """向后兼容地查找 Servers 条目。

    新版使用带空格的键名（'Data Hub' / 'Property Logger'），旧部署的
    configfile 用无空格键名（'Datahub' / 'Propertylogger'）。查找顺序：
    精确名 → 去空格名 → 大小写不敏感的去空格名。

    Args:
        conf (dict): configfile 配置字典。
        name (str): 服务名（如 'Property Hub'）。

    Returns:
        dict: 该服务的端点字典（如 ``{'pub': ..., 'sub': ...}``）。

    Raises:
        KeyError: 找不到对应 Servers 条目。
    """
    servers = conf.get('Servers') or {}
    entry = servers.get(name)
    if entry is None:
        compact = name.replace(' ', '')
        entry = servers.get(compact)
    if entry is None:
        low = compact.lower()
        for k, v in servers.items():
            if k.replace(' ', '').lower() == low:
                entry = v
                break
    if entry is None:
        raise KeyError(
            'no Servers entry %r (or %r) in configfile.json'
            % (name, name.replace(' ', '')))
    return entry


def _tcp_endpoint(endpoint, configuration):
    """Windows 上把 ipc:// 端点改写为仅回环的 tcp 端点。

    安全要求：ZMQ 无认证无加密，必须绑定 127.0.0.1，绝不对局域网/外网
    开放（否则同网段任何人可订阅相机图像/数据并注入命令）。非 ipc
    端点（已有 tcp:// 或 None）原样返回。

    Args:
        endpoint (str|None): 原始端点字符串。
        configuration (dict): configfile 配置（读 ``_tcp_ports`` 覆盖表）。

    Returns:
        str|None: 改写后的端点。

    Raises:
        ValueError: ipc 端点没有已知的 tcp 端口映射。
    """
    if not (isinstance(endpoint, str) and endpoint.startswith('ipc://')):
        return endpoint
    name = Path(endpoint[len('ipc://'):]).name
    override = (configuration.get('_tcp_ports') or {}) if isinstance(configuration, dict) else {}
    port = override.get(name, DEFAULT_TCP_PORTS.get(name))
    if port is None:
        raise ValueError(
            'no tcp port known for endpoint "%s"; add it to "_tcp_ports" in '
            'configfile.json' % name)
    return 'tcp://127.0.0.1:%d' % int(port)


# ---------------------------------------------------------------------------
# ZMQ 安全边界：非回环绑定的启动警告
#
# ZMQ 本身**没有认证/没有加密**：端点一旦绑到 0.0.0.0 / 局域网地址，
# 同网段任何主机都能订阅数据流、改写属性、注入命令（= 控制设备）。
# 默认配置都绑回环（见 _tcp_endpoint 的说明），但 configfile 允许用户改成
# LAN 地址，因此各服务器启动时用本节的 helper 做一次**醒目但不阻塞**的警告。
# ---------------------------------------------------------------------------

#: 已经警告过的端点字符串（同一端点只警告一次，避免每个客户端/重连都刷屏）。
_WARNED_EXPOSED_ENDPOINTS = set()

_log = logging.getLogger(__name__)


def _tr(text):
    """延迟导入 i18n 的 ``tr``（configreader 是底层模块，不能模块级 import）。

    ``quatm.utils.__init__`` 会经 :mod:`quatm.utils.paths` 反向 import 本模块，
    所以这里**不能**在模块级 ``from quatm.utils.i18n import tr``（会循环导入）；
    警告属于低频路径，函数内延迟 import 没有性能问题。

    Args:
        text (str): 英文原文（同时是 zh.json 的 key）。

    Returns:
        str: 当前语言下的译文；缺 key 时回退英文原文。
    """
    from quatm.utils.i18n import tr
    return tr(text)


def is_loopback_endpoint(endpoint):
    """判断 ZMQ 端点是否只在本机可见（安全）。

    规则：

    * ``tcp://127.0.0.1:...`` / ``tcp://localhost:...`` / ``tcp://[::1]:...``
      （以及其它 ``127.0.0.0/8``、IPv6 回环）→ True；
    * ``tcp://*:...`` / ``tcp://0.0.0.0:...`` / 具体局域网 IP / 主机名 → False；
    * 非 tcp 传输（``ipc://``、``inproc://`` 等本地 IPC）→ True（不出主机）；
    * None/空字符串 → True（没有可暴露的地址）。

    Args:
        endpoint (str|None): ZMQ 端点字符串，如 ``tcp://127.0.0.1:5605``。

    Returns:
        bool: True=只在本机可见；False=可能被局域网/外网访问。
    """
    if endpoint is None:
        return True
    text = str(endpoint).strip()
    if not text:
        return True
    if not text.lower().startswith('tcp://'):
        # ipc://、inproc:// 等本地传输不经过网络，不构成暴露面。
        return True
    address = text[len('tcp://'):]
    address = address.split('/', 1)[0]          # 容错：忽略可能出现的路径部分
    if address.startswith('['):                 # IPv6 写法 tcp://[::1]:5555
        host = address[1:].split(']', 1)[0]
    else:
        host = address.rsplit(':', 1)[0] if ':' in address else address
    host = host.strip().strip('[]').lower()
    if not host:
        return True
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # '*':5555、0.0.0.0、具体 LAN IP、主机名等一律按“可能暴露”处理。
        return False


def warn_if_exposed(endpoint, name='ZMQ endpoint'):
    """非回环 ZMQ 端点启动警告（每个端点只警告一次，**绝不抛异常**）。

    Args:
        endpoint (str|None): 实际 bind（或 connect）的端点字符串。
        name (str): 端点用途，用于警告文本（如 ``'Property Logger (rep)'``）。

    Returns:
        None: 永远不抛异常，保证服务器启动不被安全提示打断。
    """
    if is_loopback_endpoint(endpoint):
        return None
    key = str(endpoint)
    if key in _WARNED_EXPOSED_ENDPOINTS:
        return None
    _WARNED_EXPOSED_ENDPOINTS.add(key)
    message = '\n'.join((
        '=' * 78,
        _tr('WARNING: ZMQ endpoint "%s" (%s) is NOT bound to loopback.')
        % (endpoint, name),
        _tr('  ZMQ has NO authentication and NO encryption: any host on the '
            'LAN can read/alter properties and inject commands (= control '
            'devices).'),
        _tr('  Bind 127.0.0.1 (or use ipc://), or reach the port through an '
            'SSH tunnel. Only expose it on a network you fully trust.'),
        '=' * 78,
    ))
    print(message)
    return None


def warn_hub_if_exposed(hub_name, role):
    """检查 configfile 里某个 Hub 的端点，非回环时逐个警告。

    各服务器入口（Data/Image/Message/Property Hub 的使用方）在启动时调用：
    优先检查该 Hub 的 ``pub`` / ``sub`` / ``rep`` 端点；没有该 Hub 条目或没有
    configfile 时静默跳过（不能因为安全提示让服务起不来）。

    Args:
        hub_name (str): configfile ``Servers`` 里的 Hub 名（如 ``'Data Hub'``）。
        role (str): 调用方角色名，用于警告文本（如 ``'Data Manager'``）。

    Returns:
        None
    """
    try:
        entry = server_endpoints(Config(), hub_name)
    except Exception as exc:  # noqa: BLE001 - 缺 Hub/缺 configfile 时跳过
        # 不能静默吞：没有配置就无从判断暴露面，但要留下可排查的记录。
        _log.debug('cannot inspect %s endpoints for exposure: %s',
                   hub_name, exc)
        return None
    if not isinstance(entry, dict):
        return None
    for key in ('pub', 'sub', 'rep'):
        endpoint = entry.get(key)
        if isinstance(endpoint, str) and endpoint.strip():
            warn_if_exposed(endpoint, '%s (%s/%s)' % (role, hub_name, key))
    return None


def _first_run_fallback():
    """缺配置时的兜底：图形主线程里跑一遍首次运行向导，成功后可重试读配置。

    入口自检（:mod:`quatm` 包导入时的钩子）按“启动方式”判断要不要弹向导，
    启动方式奇怪时可能没命中（例如启动器改名、``-m`` 拉起）；而这里是**已经
    确定缺配置**的时刻，条件更硬、更可靠，正好当第二道保险。

    只在：主线程 + 入口在 ``quatm/GUI/`` 下（:func:`quatm.licensing.is_gui_process`
    同一规则）+ 未设 ``QUATM_SKIP_FIRST_RUN`` 时才动手；服务器/worker 线程
    照旧直接报错。

    Returns:
        bool: 向导跑完、项目可用了为 True。
    """
    if os.environ.get('QUATM_SKIP_FIRST_RUN'):
        return False
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        from quatm.licensing import is_gui_process
        if not is_gui_process():
            return False
        from quatm.project_setup import ensure_project_ready
        return bool(ensure_project_ready(force=True))
    except Exception:  # noqa: BLE001 - 兜底失败不影响“报出可执行的错误说明”
        return False


def Config():
    """读取 configfile.json 并返回配置字典。

    文件不存在时，如果在图形主线程里，会先把**首次运行向导**跑一遍（新机器上
    还没建项目的情形），再重试一次读取。

    Returns:
        dict: 完整配置字典（Windows 下会先把 Servers 端点改写为回环 tcp）。

    Raises:
        FileNotFoundError: 找不到 ``configuration/configfile/configfile.json``。
    """
    try:
        with open(configfilename, encoding='utf-8') as inputfile:
            configuration = json.load(inputfile)
    except FileNotFoundError:
        if _first_run_fallback():
            try:
                with open(configfilename, encoding='utf-8') as inputfile:
                    configuration = json.load(inputfile)
            except FileNotFoundError:
                pass
            else:
                return _apply_windows_endpoints(configuration)
        raise FileNotFoundError(
            '找不到配置文件 %r。\n'
            '当前项目根 PROJECT_ROOT = %r（可用环境变量 '
            'QUATM_PROJECT_ROOT 覆盖）。\n'
            '请检查：1) 项目根目录下是否有 '
            'configuration/configfile/configfile.json；\n'
            '2) 是否在项目目录或其子目录里启动了脚本/图形界面；\n'
            '3) 若子目录（如 analysis/）里有残留的 .quatm_project 标记或'
            '旧 configuration 目录，请删除，以免项目根被误判到子目录。\n'
            '全新机器还没建项目时：直接启动任一图形界面会自动弹出首次运行'
            '向导；也可以先跑 \n'
            '    python -m quatm.project_setup --create --root "<项目目录>"\n'
            '把最小配置建好（只创建缺失文件，不覆盖已有内容）。'
            % (configfilename, str(PROJECT_ROOT))) from None
    return _apply_windows_endpoints(configuration)


def _apply_windows_endpoints(configuration):
    """Windows 上把 Servers 端点改写为仅回环 tcp（其它平台原样返回）。

    Args:
        configuration (dict): configfile 内容。

    Returns:
        dict: 处理后的配置（同一对象）。
    """
    if sys.platform != 'win32':
        return configuration
    servers = configuration.get('Servers')
    if isinstance(servers, dict):
        for hub in servers.values():
            if not isinstance(hub, dict):
                continue
            for key, value in list(hub.items()):
                if isinstance(value, str):
                    hub[key] = _tcp_endpoint(value, configuration)
    return configuration


class ConfigReader():
    """读取静态配置文件（configfile.json）的只读封装。"""
    @staticmethod
    def getConfiguration():
        """返回当前 configfile 的内容（dict）。

        Returns:
            dict: 配置字典。
        """

        return Config()


# ---------------------------------------------------------------------------
# properties.json 损坏保护
#
# 旧实现是 ``try: json.load(...) except: return {}``：文件损坏/半截 JSON 时
# 无声无息用空树启动，随后 PropertyLogger 把默认树写回，原始配置就丢了。
# 现在：**存在但读/解析失败**时先把原文件另存为
# ``properties.json.corrupt-<UTC时间戳>``，再在 stderr 醒目警告并尽力上报
# error 消息；返回值仍是 {}（行为兼容，不能让服务起不来）。
# ---------------------------------------------------------------------------

def _corrupt_backup_name(filename):
    """生成损坏属性文件的备份路径。

    Args:
        filename (str): 原属性文件路径。

    Returns:
        str: ``<filename>.corrupt-<UTC YYYYmmdd-HHMMSS>``。
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y%m%d-%H%M%S')
    return '%s.corrupt-%s' % (filename, stamp)


def _report_corrupt_properties(filename, backup, error):
    """醒目上报 properties.json 损坏（stderr + 尽力发 error 消息）。

    只打印**路径与非敏感错误信息**，不含任何属性值。``send_error`` 用延迟
    import（configreader 是底层模块，避免 import 期的循环依赖）；消息中枢
    不可用时**不静默吞**——记 warning 日志（stderr 已有完整提示），且坏文件
    绝不能阻止服务启动。

    Args:
        filename (str): 损坏的属性文件路径。
        backup (str|None): 备份文件路径；复制失败时为 None。
        error (BaseException): 触发损坏判定的异常。
    """
    lines = [
        '=' * 78,
        _tr('WARNING: [Properties] configuration file is corrupt or unreadable:'),
        '  %s' % filename,
        _tr('  error: %s: %s') % (type(error).__name__, error),
    ]
    if backup:
        lines.append(_tr('  the original file was copied to: %s') % backup)
    else:
        lines.append(_tr('  WARNING: could not create a backup copy of the '
                         'corrupt file.'))
    lines.extend((
        _tr('  Loading an EMPTY property tree; the property logger may '
            'overwrite the file with defaults. Restore the settings from '
            'the backup before running experiments.'),
        '=' * 78,
    ))
    message = '\n'.join(lines)
    print(message, file=sys.stderr)
    try:  # 延迟 import：只有真的损坏时才拉起消息层
        from quatm.servers import send_error
        send_error(message)
    except Exception as exc:  # noqa: BLE001 - 消息中枢不可用时不阻塞启动
        _log.warning('could not report corrupt properties to the message hub: '
                     '%s', exc)


def _load_properties(filename):
    """从 ``filename`` 读取属性树；损坏时备份+上报并返回 {}。

    * 文件**不存在** → 返回 {}（首次启动属正常，不报警、不备份）；
    * 文件存在但 JSON 损坏/编码错误/IO 错误/根不是对象 → 备份原文件到
      ``<filename>.corrupt-<UTC时间戳>``（复制失败忽略）、stderr 醒目警告、
      尽力发 error 消息，返回 {}。

    Args:
        filename (str): properties.json 路径。

    Returns:
        dict: 解析后的属性树；缺失/损坏时为空字典。
    """
    if not filename or not os.path.exists(filename):
        return {}
    try:
        with open(filename, encoding='utf-8') as inputfile:
            prop = json.load(inputfile)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        backup = None
        try:
            backup = _corrupt_backup_name(filename)
            shutil.copy2(filename, backup)
        except OSError:
            backup = None
        _report_corrupt_properties(filename, backup, exc)
        return {}
    if not isinstance(prop, dict):
        # 合法 JSON 但不是对象（[] / "x" / 数字）同样无法当属性树用，按损坏处理。
        error = ValueError('properties root must be an object, got %s'
                           % type(prop).__name__)
        backup = None
        try:
            backup = _corrupt_backup_name(filename)
            shutil.copy2(filename, backup)
        except OSError:
            backup = None
        _report_corrupt_properties(filename, backup, error)
        return {}
    return prop


def Properties():
    """读取属性盘文件（properties.json）并返回字典。

    文件不存在时返回 {}（首次启动属正常）。文件**存在但损坏/不可读**时：

    1. 把原文件另存为 ``properties.json.corrupt-<UTC时间戳>``
       （``shutil.copy2``；复制失败则忽略，继续启动）；
    2. 在 stderr 打印醒目警告，并尽力通过 ``quatm.servers.send_error``
       上报（延迟 import；消息层不可用则只保留 stderr 输出）；
    3. **仍返回 {}**——坏文件绝不能阻止服务启动（行为与旧版兼容）。

    调用方若希望更进一步（例如弹窗、拒绝启动），应在自己的入口层处理；
    本函数只负责"不丢原文件 + 醒目提示 + 兼容返回"。

    Returns:
        dict: 启动属性；文件不存在或损坏时返回 {}。
    """
    return _load_properties(propertyfilename)


if __name__ == "__main__":
    pass
