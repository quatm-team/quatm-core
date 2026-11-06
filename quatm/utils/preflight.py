"""Preflight checks for a Quatm project and its runtime environment.

The command inspects configuration, stale processes, bus locks and optional
resources, then emits JSON so it can be used interactively or by CI/deployment
scripts. It deliberately avoids importing Qt, drivers, the web console or the
licensing layer.
"""

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

#: 项目标记（与 configreader 的根定位规则保持一致）。
PROJECT_MARKERS = ('configuration/configfile/configfile.json',
                   '.quatm_project')

#: 项目根/configuration 的约定路径。
CONFIG_SUBDIR = 'configuration'

#: 磁盘阈值 [字节]：低于 1 GiB 视为 error，低于 2 GiB 视为 warn。
DISK_ERROR_BYTES = 1024 ** 3
DISK_WARN_BYTES = 2 * 1024 ** 3

#: 单个历史文件超过该大小给 warn（避免长期运行无限增长）。
HISTORY_WARN_BYTES = 100 * 1024 ** 2

#: 消息库超过该大小给 warn（提示 prune/VACUUM）。
MESSAGES_DB_WARN_BYTES = 2 * 1024 ** 3

#: tcp 端点探测超时（秒）——维护窗口里必须“短平快”。
TCP_PROBE_TIMEOUT = 0.3

#: 快照脚本路径（仓库根下）。
SNAPSHOT_SCRIPT = os.path.join('scripts', 'snapshot.sh')

#: 快照超时（秒）；大 Data 目录打包可能较慢。
SNAPSHOT_TIMEOUT = 600

#: CURVE 密钥文件名（与 ``quatm.servers.zmq_security.KEY_FILES`` 一致；
#: 延迟 import 失败时的兜底副本，测试会断言两者一致）。
CURVE_KEY_FILES_FALLBACK = ('server.key', 'server.key_secret',
                            'client.key', 'client.key_secret')

#: ZMQ CURVE 开关环境变量（与 zmq_security 一致）。
ENV_CURVE = 'QUATM_ZMQ_CURVE'

#: CURVE 密钥目录覆盖环境变量（与 zmq_security 一致）。
ENV_KEY_DIR = 'QUATM_ZMQ_KEY_DIR'


class UsageError(Exception):
    """命令行用法错误；main 转成退出码 2 并在 stdout 最后一行给 JSON。"""


class _JsonArgumentParser(argparse.ArgumentParser):
    """不 ``SystemExit`` 的参数解析器：错误抛 :class:`UsageError`。

    维护脚本可能被 GUI/CI 调用，用法错误也要尽量给出可解析的 JSON，而不是
    只有 argparse 的 usage 文本。
    """

    def error(self, message):
        """把 argparse 的错误转成 :class:`UsageError`。"""
        raise UsageError(message)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _project_root():
    """返回项目根：``QUATM_PROJECT_ROOT``（带标记校验）优先，其次 configreader。

    Returns:
        str: 项目根绝对路径。
    """
    env = os.environ.get('QUATM_PROJECT_ROOT', '').strip()
    if env:
        candidate = os.path.abspath(env)
        if any(os.path.isfile(os.path.join(candidate, marker))
               for marker in PROJECT_MARKERS):
            return candidate
    try:
        from quatm.servers import configreader
        return str(configreader.PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - 维护脚本不能因为项目配置读不了就崩
        return os.getcwd()


def _config_dir(root):
    """项目配置文件目录（``<root>/configuration``）。"""
    return os.path.join(root, CONFIG_SUBDIR)


def _repo_root():
    """本仓库根（``quatm/utils/preflight.py`` 往上三层）；不在仓库里时为 None。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.dirname(os.path.dirname(here))
    return candidate if os.path.isdir(os.path.join(candidate, 'quatm')) else None


def _short(text, limit=300):
    """把可能很长的文本压成一行（JSON detail 用）。"""
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def _human_bytes(size):
    """把字节数转成 ``12.3 MiB`` 之类的可读字符串。"""
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024.0 or unit == 'TiB':
            return ('%d %s' % (value, unit)
                    if unit == 'B' else '%.1f %s' % (value, unit))
        value /= 1024.0
    return '%.1f TiB' % value


def _check(name, level, detail, fix=''):
    """组装一条检查结果（统一字段，缺失 fix 用空串）。"""
    return {'name': name, 'level': level, 'detail': _short(detail),
            'fix': _short(fix)}


def _file_size(path):
    """文件大小（不存在/不可读时返回 None）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _load_json_file(path):
    """读一个 JSON 文件；返回 ``(value, error)``，error 为空表示成功。"""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle), ''
    except OSError as exc:
        return None, 'cannot read %s: %s' % (path, exc)
    except ValueError as exc:
        return None, '%s is not valid JSON: %s' % (path, exc)


def _parse_switch(value):
    """把开关值解析成 ``True``/``False``/``None``（None=未配置/无法判定）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == '':
            return None
        if text in ('0', 'false', 'no', 'off', 'disabled'):
            return False
        if text in ('1', 'true', 'yes', 'on', 'enabled'):
            return True
    return bool(value)


# ---------------------------------------------------------------------------
# 各检查项（独立函数，便于测试/维护；每项返回 {'level','detail','fix'}）
# ---------------------------------------------------------------------------
def check_project(ctx):
    """检查项目根/配置目录/device_db.py 是否就绪且可静态解析。"""
    root = ctx['root']
    config_dir = _config_dir(root)
    if not os.path.isdir(root):
        return {'level': 'error',
                'detail': 'project root does not exist: %s' % root,
                'fix': 'set QUATM_PROJECT_ROOT to the project directory'}
    if not os.path.isdir(config_dir):
        return {'level': 'error',
                'detail': 'project has no configuration/ directory: %s'
                          % config_dir,
                'fix': 'run the first-run setup wizard, or point '
                       'QUATM_PROJECT_ROOT at the right project'}
    device_db = os.path.join(config_dir, 'device_db.py')
    if not os.path.isfile(device_db):
        return {'level': 'warn',
                'detail': 'no configuration/device_db.py (empty project?)',
                'fix': 'run the first-run setup wizard / add devices in the '
                       'Device Manager'}
    from quatm.utils.device_db import parse_device_db
    entries, note = parse_device_db(root)
    lower_note = str(note).lower()
    if ('syntax error' in lower_note or 'cannot read' in lower_note
            or 'no device_db table' in lower_note):
        return {'level': 'error',
                'detail': 'device_db.py could not be parsed: %s' % note,
                'fix': 'fix the syntax of configuration/device_db.py '
                       '(python -m py_compile configuration/device_db.py) '
                       'or restore it from backup'}
    return {'level': 'ok',
            'detail': 'project=%s, device_db entries=%d (%s)'
                      % (root, len(entries), note),
            'fix': ''}


def _classify_endpoint(endpoint):
    """把端点字符串分类成 ``(transport, target)``。

    Returns:
        tuple[str, object]: ('tcp', (host, port)) / ('ipc', path) /
        ('other', None)。
    """
    text = str(endpoint or '').strip()
    if not text:
        return 'other', None
    if text.startswith('tcp://'):
        rest = text[len('tcp://'):]
        for sep in ('/', '?'):
            rest = rest.split(sep, 1)[0]
        host, _, port = rest.rpartition(':')
        if host and port.isdigit():
            return 'tcp', (host or '127.0.0.1', int(port))
        return 'other', None
    if text.startswith('ipc://'):
        return 'ipc', text[len('ipc://'):]
    return 'other', None


def _tcp_reachable(host, port, timeout=TCP_PROBE_TIMEOUT):
    """短超时 TCP 连接探测（成功返回 True；任何异常按不可达处理）。"""
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _endpoints_of(conf, name):
    """取某服务的端点字典（优先 configreader.server_endpoints 的兼容规则）。"""
    try:
        from quatm.servers.configreader import server_endpoints
        return server_endpoints(conf, name)
    except Exception:  # noqa: BLE001 - 兼容旧/畸形配置
        return (conf.get('Servers') or {}).get(name) or {}


def check_services(ctx):
    """检查 configfile 里各服务端点（tcp 连接 / ipc 路径）。"""
    config_dir = _config_dir(ctx['root'])
    configfile = os.path.join(config_dir, 'configfile', 'configfile.json')
    conf, error = _load_json_file(configfile)
    if error:
        return {'level': 'warn', 'detail': error,
                'fix': 'restore configuration/configfile/configfile.json '
                       'from backup (or run the first-run setup wizard)'}
    servers = (conf or {}).get('Servers') or {}
    configured = 0
    reachable = 0
    unreachable = []
    inactive = 0
    no_network = bool(ctx['opts'].get('no_network'))
    for name in sorted(servers):
        entry = _endpoints_of(conf, name)
        if not isinstance(entry, dict):
            continue
        if entry.get('active') is False:
            inactive += 1
            continue
        for key, value in entry.items():
            if key in ('active', 'script', 'args') or not isinstance(value, str):
                continue
            transport, target = _classify_endpoint(value)
            if transport == 'tcp':
                configured += 1
                if no_network:
                    continue
                if _tcp_reachable(target[0], target[1]):
                    reachable += 1
                else:
                    unreachable.append('%s/%s' % (name, key))
            elif transport == 'ipc':
                configured += 1
                if os.path.exists(target):
                    reachable += 1
                else:
                    unreachable.append('%s/%s' % (name, key))
    if no_network:
        return {'level': 'ok',
                'detail': 'network checks skipped (--no-network); %d endpoint(s) '
                          'configured in %d service(s)' % (configured,
                                                           len(servers)),
                'fix': 're-run without --no-network from the machine that '
                       'hosts the services'}
    if configured == 0:
        return {'level': 'warn',
                'detail': 'no active tcp/ipc endpoints found in configfile '
                          '(%d service(s) configured)' % len(servers),
                'fix': 'check Servers entries in '
                       'configuration/configfile/configfile.json'}
    if unreachable:
        return {'level': 'warn',
                'detail': 'reachable %d/%d endpoint(s); unreachable: %s'
                          % (reachable, configured, ', '.join(unreachable)),
                'fix': 'start the missing services from the Process Manager, '
                       'then re-run preflight'}
    return {'level': 'ok',
            'detail': 'reachable %d/%d endpoint(s) (%d inactive service(s))'
                      % (reachable, configured, inactive),
            'fix': ''}


def _scan_bus_locks(config_dir):
    """扫描 ``configuration/devices/leases/bus_*.json`` 短时总线锁。

    ``device_lease.list_leases()`` 有意跳过 bus 锁（它们是打开设备那一小段
    的短时锁，TTL 很短、下次争用时自动回收）；维护自检仍要把“卡住的旧 bus
    锁”报告出来，方便判断某台设备是否被半死的进程挡住。

    Returns:
        tuple[int, int]: (bus 锁总数, 其中疑似陈旧的数量)。
    """
    leases_dir = os.path.join(config_dir, 'devices', 'leases')
    if not os.path.isdir(leases_dir):
        return 0, 0
    try:
        names = sorted(os.listdir(leases_dir))
    except OSError:
        return 0, 0
    total = 0
    stale = 0
    for name in names:
        if not (name.startswith('bus_') and name.endswith('.json')):
            continue
        total += 1
        path = os.path.join(leases_dir, name)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                record = json.load(handle)
            pid = int(record.get('pid'))
            age = time.time() - float(record.get('ts') or 0.0)
        except (OSError, ValueError, TypeError):
            stale += 1
            continue
        try:
            from quatm.servers import heartbeat
            alive = heartbeat.pid_alive(pid, unknown=True)
        except Exception:  # noqa: BLE001 - 旧包没有 heartbeat：按年龄判断
            alive = True
        if not alive or age > 600:
            stale += 1
    return total, stale


def check_leases(ctx):
    """检查设备租约（陈旧/占用）；默认只报告，``--clean-stale`` 才清理。"""
    try:
        from quatm.servers import device_lease
    except Exception as exc:  # noqa: BLE001 - 旧版本/被裁剪的包
        return {'level': 'warn',
                'detail': 'device_lease module unavailable: %s' % exc,
                'fix': 'upgrade to a build that includes '
                       'quatm.servers.device_lease'}
    try:
        leases = device_lease.list_leases()
    except Exception as exc:  # noqa: BLE001 - 租约目录不可读
        return {'level': 'warn',
                'detail': 'cannot read device leases: %s' % exc,
                'fix': 'check permissions of configuration/devices/leases'}
    stale = [lease for lease in leases if lease.get('stale')]
    cleaned = None
    clean_error = ''
    if stale and ctx['opts'].get('clean_stale'):
        try:
            cleaned = device_lease.clean_stale()
        except Exception as exc:  # noqa: BLE001
            clean_error = str(exc)
    bus_total, bus_stale_count = _scan_bus_locks(_config_dir(ctx['root']))
    bus_note = (', bus locks: %d (%d stale)'
                % (bus_total, bus_stale_count)) if bus_total else ''
    if bus_stale_count:
        bus_note += ('; stale bus locks are auto-reclaimed on the next '
                     'acquire_bus, but a hung device open may be blocking one')
    if clean_error:
        return {'level': 'warn',
                'detail': 'stale leases=%d but clean_stale() failed: %s'
                          % (len(stale), clean_error),
                'fix': 'check permissions of configuration/devices/leases; '
                       'remove stale *.json manually'}
    if cleaned is not None:
        if cleaned:
            return {'level': 'ok',
                    'detail': 'cleaned %d stale lease(s); remaining leases=%d%s'
                              % (cleaned, len(leases) - cleaned, bus_note),
                    'fix': ''}
        return {'level': 'ok',
                'detail': 'no stale lease needed cleaning (%d lease(s))%s'
                          % (len(leases), bus_note),
                'fix': ''}
    if stale:
        return {'level': 'warn',
                'detail': '%d stale device lease(s) (of %d)%s'
                          % (len(stale), len(leases), bus_note),
                'fix': 'python -m quatm.utils.preflight --clean-stale'}
    if bus_stale_count:
        return {'level': 'warn',
                'detail': 'device leases ok (%d active)%s'
                          % (len(leases), bus_note),
                'fix': 'stale bus lock(s) are reclaimed automatically on the '
                       'next acquire_bus; if a device open is stuck, stop that '
                       'process and remove configuration/devices/leases/'
                       'bus_*.json'}
    return {'level': 'ok',
            'detail': 'device leases ok (%d active)%s'
                      % (len(leases), bus_note),
            'fix': ''}


def _iter_processes():
    """枚举本机进程的 ``(pid, ppid, cmdline)`` 列表；不支持时返回 None。

    POSIX：优先读 ``/proc/*/cmdline``（Linux），没有 /proc 时（macOS）用
    ``ps -axo pid=,ppid=,command=``。Windows：``tasklist`` 不提供命令行，
    返回 None（调用方按“跳过并说明”处理）。
    """
    if os.path.isdir('/proc'):
        processes = []
        try:
            names = os.listdir('/proc')
        except OSError:
            return None
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open('/proc/%d/cmdline' % pid, 'rb') as handle:
                    raw = handle.read()
                with open('/proc/%d/stat' % pid, 'r',
                          encoding='utf-8', errors='replace') as handle:
                    stat_fields = handle.read().split()
                ppid = int(stat_fields[3]) if len(stat_fields) > 3 else 0
            except (OSError, ValueError):
                continue
            cmdline = [part.decode('utf-8', 'replace')
                       for part in raw.split(b'\0') if part]
            processes.append((pid, ppid, cmdline))
        return processes
    if os.name == 'posix':
        try:
            proc = subprocess.run(
                ['ps', '-axo', 'pid=,ppid=,command='],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        processes = []
        for line in proc.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 3 or not fields[0].isdigit() \
                    or not fields[1].isdigit():
                continue
            processes.append((int(fields[0]), int(fields[1]),
                              fields[2].split()))
        return processes
    return None


def _looks_like_task_runner(cmdline):
    """命令行是否是 quatm 实验任务 runner（隔离子进程执行）。"""
    text = ' '.join(cmdline) if not isinstance(cmdline, str) else cmdline
    return 'quatm.experiment.runner' in text


def check_orphans(ctx):
    """报告遗留的 quatm 实验 runner 子进程（只报告，不杀进程）。"""
    processes = _iter_processes()
    if processes is None:
        return {'level': 'ok',
                'detail': 'process scan unavailable on this platform/'
                          'sandbox (%s); skipped' % sys.platform,
                'fix': 'on Windows use Process Manager to check for leftover '
                       'python processes'}
    pids = set(pid for pid, _ppid, _cmd in processes)
    runners = [(pid, ppid, cmd) for pid, ppid, cmd in processes
               if _looks_like_task_runner(cmd)]
    orphans = [(pid, ppid) for pid, ppid, _cmd in runners
               if ppid == 1 or ppid not in pids]
    if not runners:
        return {'level': 'ok',
                'detail': 'no quatm task runner process found',
                'fix': ''}
    if not orphans:
        return {'level': 'ok',
                'detail': '%d task runner process(es), all with a live parent'
                          % len(runners),
                'fix': ''}
    orphan_pids = ', '.join(str(pid) for pid, _ppid in orphans)
    return {'level': 'warn',
            'detail': '%d orphan task runner process(es) (pid %s); parent is '
                      'gone (GUI crashed?)' % (len(orphans), orphan_pids),
            'fix': 'terminate them conservatively: kill -TERM %s  (or use '
                   'Process Manager); then run preflight again'
                   % ' '.join(str(pid) for pid, _ppid in orphans)}


def _disk_usage(path):
    """``shutil.disk_usage`` 的薄包装（测试可替换）。"""
    return shutil.disk_usage(path)


def _resource_monitor_summary():
    """若 ``resource_monitor`` 已就绪，返回它的资源摘要（否则 None）。

    不 import GUI；the resource-monitor integration 交付的模块若有 ``collect_resources()`` 就复用，
    任何不兼容/异常都静默降级（preflight 自己已有轻量 getsize 兜底）。
    """
    try:
        from quatm.servers import resource_monitor
    except Exception:  # noqa: BLE001
        return None
    collect = getattr(resource_monitor, 'collect_resources', None)
    if not callable(collect):
        return None
    try:
        summary = collect()
    except Exception:  # noqa: BLE001
        return None
    return summary if isinstance(summary, dict) else None


def check_disk(ctx):
    """检查项目盘剩余空间与关键 DB/属性文件大小。"""
    root = ctx['root']
    config_dir = _config_dir(root)
    try:
        usage = _disk_usage(root)
    except Exception as exc:  # noqa: BLE001 - 网络盘/权限问题
        return {'level': 'error',
                'detail': 'cannot read disk usage for %s: %s' % (root, exc),
                'fix': 'check that the project directory is mounted and '
                       'readable'}
    free = int(usage.free)
    sizes = []
    for label, path in (
            ('messages.db',
             os.path.join(config_dir, 'messagelist', 'messages.db')),
            ('push.db', os.path.join(config_dir, 'push', 'push.db')),
            ('properties.json',
             os.path.join(config_dir, 'properties', 'properties.json'))):
        size = _file_size(path)
        if size is not None:
            sizes.append('%s=%s' % (label, _human_bytes(size)))
    summary = _resource_monitor_summary()
    if summary:
        sizes.append('resource_monitor=available')
    detail = 'free %s; %s' % (_human_bytes(free),
                              ', '.join(sizes) if sizes else 'no databases yet')
    if free < DISK_ERROR_BYTES:
        return {'level': 'error', 'detail': detail,
                'fix': 'free disk space before running experiments '
                       '(move old Data/*.h5 to another disk / prune databases)'}
    if free < DISK_WARN_BYTES:
        return {'level': 'warn', 'detail': detail,
                'fix': 'disk is getting full; archive old Data/*.h5 and prune '
                       'Message/Push databases'}
    messages_size = _file_size(
        os.path.join(config_dir, 'messagelist', 'messages.db'))
    if messages_size is not None and messages_size > MESSAGES_DB_WARN_BYTES:
        return {'level': 'warn', 'detail': detail,
                'fix': 'messages.db is large; let MessageStore prune run, or '
                       'run VACUUM manually during a maintenance window'}
    return {'level': 'ok', 'detail': detail, 'fix': ''}


def _sqlite_quick_check(path):
    """对 SQLite 文件执行 ``PRAGMA quick_check``。

    Returns:
        tuple[bool, str]: (正常为 True, 说明文本)。
    """
    connection = None
    try:
        connection = sqlite3.connect('file:%s?mode=ro' % path, uri=True,
                                     timeout=1.0)
        row = connection.execute('PRAGMA quick_check').fetchone()
        result = str(row[0]) if row else ''
        if result.lower() == 'ok':
            return True, 'ok'
        return False, 'quick_check returned %r' % result
    except sqlite3.Error as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def check_db(ctx):
    """检查消息/推送 SQLite 与 properties.json 的可读性/完整性。"""
    config_dir = _config_dir(ctx['root'])
    problems = []
    warnings = []
    details = []
    for label, path in (
            ('messages.db',
             os.path.join(config_dir, 'messagelist', 'messages.db')),
            ('push.db', os.path.join(config_dir, 'push', 'push.db'))):
        if not os.path.isfile(path):
            details.append('%s=not initialized' % label)
            continue
        ok, message = _sqlite_quick_check(path)
        lower = message.lower()
        if ok:
            details.append('%s=ok (%s)'
                           % (label, _human_bytes(_file_size(path) or 0)))
        elif 'locked' in lower or 'unable to open' in lower:
            warnings.append('%s busy/locked: %s' % (label, message))
            details.append('%s=busy' % label)
        else:
            problems.append('%s corrupt: %s' % (label, message))
            details.append('%s=CORRUPT' % label)
    properties = os.path.join(config_dir, 'properties', 'properties.json')
    if not os.path.isfile(properties):
        warnings.append('properties.json not found (first run will create it)')
        details.append('properties.json=missing')
    else:
        _value, error = _load_json_file(properties)
        if error:
            problems.append('properties.json invalid: %s' % error)
            details.append('properties.json=INVALID')
        else:
            details.append('properties.json=ok (%s)'
                           % _human_bytes(_file_size(properties) or 0))
    detail = '; '.join(details)
    if problems:
        return {'level': 'error', 'detail': detail,
                'fix': 'stop services first; copy the .db (+ .db-wal/.db-shm) '
                       'or properties.json to a backup, then use sqlite3 '
                       '".recover" / restore the last known-good copy'}
    if warnings:
        return {'level': 'warn', 'detail': detail, 'fix': '; '.join(warnings)}
    return {'level': 'ok', 'detail': detail, 'fix': ''}


def _curve_key_files():
    """CURVE 密钥文件名（尽量与 zmq_security 保持一致）。"""
    try:
        from quatm.servers import zmq_security
        names = getattr(zmq_security, 'KEY_FILES', None)
        if names:
            return tuple(names)
    except Exception:  # noqa: BLE001 - 旧版本/裁剪包
        pass
    return CURVE_KEY_FILES_FALLBACK


def _curve_key_dir(config_dir):
    """CURVE 密钥目录：``QUATM_ZMQ_KEY_DIR`` 覆盖，否则 ``configuration/zmq``。"""
    override = os.environ.get(ENV_KEY_DIR, '').strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(config_dir, 'zmq')


def check_curves(ctx):
    """检查 CURVE 密钥目录/权限/开关一致性；提示轮换需全进程重启。"""
    config_dir = _config_dir(ctx['root'])
    env_value = _parse_switch(os.environ.get(ENV_CURVE))
    config_value = None
    config_error = ''
    configfile = os.path.join(config_dir, 'configfile', 'configfile.json')
    if os.path.isfile(configfile):
        conf, error = _load_json_file(configfile)
        if error:
            config_error = error
        else:
            config_value = _parse_switch(
                (conf.get('Servers') or {}).get('zmq_curve'))
    else:
        config_error = 'configfile.json not found'
    rotation_hint = ('CURVE key rotation requires restarting ALL quatm '
                     'processes together (old processes cannot talk to new '
                     'keys); back up the current key directory first')
    warnings = []
    details = []
    if config_error:
        warnings.append('cannot read CURVE switch from configfile: %s'
                        % config_error)
    if env_value is not None:
        details.append('env %s=%s' % (ENV_CURVE, env_value))
    if config_value is not None:
        details.append('config Servers/zmq_curve=%s' % config_value)
    if env_value is not None and config_value is not None \
            and env_value != config_value:
        warnings.append('CURVE switch mismatch: env=%s vs config=%s'
                        % (env_value, config_value))
    effective = (env_value if env_value is not None
                 else (config_value if config_value is not None else True))
    details.append('effective=%s' % ('on' if effective else 'off'))
    key_dir = _curve_key_dir(config_dir)
    key_files = _curve_key_files()
    if effective:
        if not os.path.isdir(key_dir):
            warnings.append('key directory missing (%s); it is generated on '
                            'the first encrypted service start' % key_dir)
        else:
            missing = [name for name in key_files
                       if not os.path.isfile(os.path.join(key_dir, name))]
            if missing:
                warnings.append('incomplete CURVE key set (missing %s); this '
                                'is worse than no keys: stop all services and '
                                'delete the key dir to regenerate'
                                % ', '.join(missing))
            if os.name == 'posix':
                for name in key_files:
                    path = os.path.join(key_dir, name)
                    try:
                        mode = os.stat(path).st_mode & 0o777
                    except OSError:
                        continue
                    if mode & 0o077:
                        warnings.append('key file %s is group/world readable '
                                        '(mode %o, expected 600)'
                                        % (name, mode))
                try:
                    dir_mode = os.stat(key_dir).st_mode & 0o777
                except OSError:
                    dir_mode = None
                if dir_mode is not None and dir_mode & 0o077:
                    warnings.append('key directory %s is group/world '
                                    'accessible (mode %o, expected 700)'
                                    % (key_dir, dir_mode))
    else:
        has_keys = os.path.isdir(key_dir) and any(
            os.path.isfile(os.path.join(key_dir, name))
            for name in key_files)
        if has_keys:
            warnings.append('CURVE disabled but key directory exists (%s); '
                            'some processes may still be encrypted and fail '
                            'to connect' % key_dir)
    detail = '; '.join(details)
    if warnings:
        return {'level': 'warn', 'detail': detail + '; ' + '; '.join(warnings),
                'fix': rotation_hint}
    return {'level': 'ok', 'detail': detail + '; keys ok',
            'fix': rotation_hint}


def check_history(ctx):
    """检查 health/resource 历史 JSONL 的可读/可写与大小。"""
    config_dir = _config_dir(ctx['root'])
    warnings = []
    details = []
    for label, path in (
            ('health_history',
             os.path.join(config_dir, 'devices', 'health_history.jsonl')),
            ('resource_history',
             os.path.join(config_dir, 'health', 'resource_history.jsonl'))):
        if not os.path.isfile(path):
            details.append('%s=not created yet' % label)
            continue
        size = _file_size(path) or 0
        details.append('%s=%s' % (label, _human_bytes(size)))
        try:
            with open(path, 'rb') as handle:
                handle.read(1)
        except OSError as exc:
            warnings.append('%s unreadable: %s' % (label, exc))
            continue
        directory = os.path.dirname(path)
        if os.name == 'posix' and not os.access(directory, os.W_OK):
            warnings.append('%s directory not writable (%s); history records '
                            'will be lost' % (label, directory))
        if size > HISTORY_WARN_BYTES:
            warnings.append('%s is large (%s); rotate/compress during the '
                            'maintenance window' % (label, _human_bytes(size)))
        try:
            with open(path, 'rb') as handle:
                handle.seek(max(0, size - 4096))
                tail = handle.read().decode('utf-8', 'replace')
            lines = [line for line in tail.splitlines() if line.strip()]
            if lines:
                json.loads(lines[-1])
        except (OSError, ValueError) as exc:
            warnings.append('%s last line is not valid JSON (partial write?): '
                            '%s' % (label, exc))
    detail = '; '.join(details)
    if warnings:
        return {'level': 'warn', 'detail': detail + '; ' + '; '.join(warnings),
                'fix': 'fix permissions / rotate large history files during '
                       'the maintenance window; a bad last line is usually a '
                       'partial write and can be truncated'}
    return {'level': 'ok', 'detail': detail, 'fix': ''}


def _run_snapshot(script, project_root):
    """执行快照脚本，返回 ``(returncode, output_tail, error)``（测试可替换）。"""
    try:
        proc = subprocess.run(
            ['bash', script], cwd=project_root, capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=SNAPSHOT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, '', '%s: %s' % (type(exc).__name__, exc)
    output = (proc.stdout or '') + (proc.stderr or '')
    return proc.returncode, output, ''


def check_snapshot(ctx):
    """执行现有 ``scripts/snapshot.sh``（仅 --snapshot 时加入检查列表）。"""
    repo_root = _repo_root()
    if repo_root is None:
        return {'level': 'warn',
                'detail': 'scripts/snapshot.sh not found (not running from a '
                          'quatm source checkout)',
                'fix': 'run preflight from the repository checkout, or run '
                       'scripts/snapshot.sh manually'}
    script = os.path.join(repo_root, SNAPSHOT_SCRIPT)
    if not os.path.isfile(script):
        return {'level': 'warn',
                'detail': 'snapshot script missing: %s' % script,
                'fix': 'restore scripts/snapshot.sh from the repository'}
    returncode, output, error = _run_snapshot(script, ctx['root'])
    if error:
        return {'level': 'error',
                'detail': 'snapshot failed: %s' % error,
                'fix': 'check disk space and permissions, then run '
                       'scripts/snapshot.sh manually'}
    if returncode != 0:
        return {'level': 'error',
                'detail': 'snapshot exited %d: %s' % (returncode,
                                                      _short(output, 200)),
                'fix': 'run scripts/snapshot.sh manually to see the full '
                       'error'}
    return {'level': 'ok',
            'detail': 'snapshot ok: %s' % _short(output, 200),
            'fix': ''}


# ---------------------------------------------------------------------------
# 执行/输出
# ---------------------------------------------------------------------------
def _check_registry():
    """检查项注册表（name → 函数）；测试可替换其中的函数。"""
    return (
        ('project', check_project),
        ('services', check_services),
        ('leases', check_leases),
        ('orphans', check_orphans),
        ('disk', check_disk),
        ('db', check_db),
        ('curves', check_curves),
        ('history', check_history),
    )


def run_checks(ctx):
    """依次执行所有检查；单项异常被隔离成 ``error`` 结果。

    Returns:
        list[dict]: 每条含 ``name``/``level``/``detail``/``fix``。
    """
    checks = []
    for name, func in _check_registry():
        try:
            result = func(ctx) or {}
        except Exception as exc:  # noqa: BLE001 - 单项检查不能拖垮整份报告
            import traceback as _traceback
            result = {
                'level': 'error',
                'detail': 'check %r crashed: %s: %s'
                          % (name, type(exc).__name__, exc),
                'fix': 'please report this; traceback: %s'
                       % _short(_traceback.format_exc(), 400),
            }
        level = str(result.get('level') or 'error')
        if level not in ('ok', 'warn', 'error'):
            level = 'error'
        checks.append({'name': name, 'level': level,
                       'detail': _short(result.get('detail', '')),
                       'fix': _short(result.get('fix', ''))})
    if ctx['opts'].get('snapshot'):
        try:
            result = check_snapshot(ctx)
        except Exception as exc:  # noqa: BLE001
            result = {'level': 'error',
                      'detail': 'snapshot check crashed: %s: %s'
                                % (type(exc).__name__, exc),
                      'fix': ''}
        checks.append({'name': 'snapshot',
                       'level': result.get('level', 'error'),
                       'detail': _short(result.get('detail', '')),
                       'fix': _short(result.get('fix', ''))})
    return checks


def _emit(payload, human):
    """输出：人读摘要（可选）后，最后一行永远是单个 JSON 对象。"""
    if human:
        for check in payload['checks']:
            sys.stdout.write('[%s] %s: %s\n'
                             % (check['level'].upper(), check['name'],
                                check['detail']))
            if check['fix']:
                sys.stdout.write('       fix: %s\n' % check['fix'])
        if payload.get('error'):
            sys.stdout.write('error: %s\n' % payload['error'])
    sys.stdout.write(json.dumps(payload, ensure_ascii=False,
                                sort_keys=True) + '\n')
    sys.stdout.flush()


def build_parser():
    """构造命令行解析器。"""
    parser = _JsonArgumentParser(
        prog='python -m quatm.utils.preflight',
        description='Read-only maintenance preflight checks (JSON on the '
                    'last stdout line).')
    parser.add_argument('--json', action='store_true',
                        help='only print the machine-readable JSON line')
    parser.add_argument('--no-network', action='store_true',
                        help='skip tcp endpoint probes (CI/offline)')
    parser.add_argument('--snapshot', action='store_true',
                        help='also run scripts/snapshot.sh')
    parser.add_argument('--strict', action='store_true',
                        help='treat warnings as failures (exit code 1)')
    parser.add_argument('--clean-stale', action='store_true',
                        help='actually remove stale device leases')
    return parser


def _payload(checks, project, error=''):
    """组装最终 JSON 负载。"""
    payload = {
        'ok': (not error
               and not any(check['level'] == 'error' for check in checks)),
        'checks': checks,
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'project': project,
    }
    if error:
        payload['error'] = error
    return payload


def main(argv=None):
    """CLI 入口：跑检查、输出 JSON、返回退出码。

    Args:
        argv (list[str]|None): 参数列表（None 用 ``sys.argv[1:]``）。

    Returns:
        int: 0 无 error（`--strict` 下也无 warn）/ 1 有 error 或 strict-warn /
        2 用法错误。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    project = _project_root()
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        payload = _payload([], project, error='usage: %s' % exc)
        _emit(payload, human=not ('--json' in argv))
        return 2
    except SystemExit as exc:   # --help
        return int(exc.code or 0)
    ctx = {
        'root': project,
        'opts': {
            'no_network': bool(args.no_network),
            'clean_stale': bool(args.clean_stale),
            'snapshot': bool(args.snapshot),
            'strict': bool(args.strict),
        },
    }
    checks = run_checks(ctx)
    payload = _payload(checks, project)
    if args.strict:
        payload['ok'] = payload['ok'] and not any(
            check['level'] == 'warn' for check in checks)
    _emit(payload, human=not args.json)
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
