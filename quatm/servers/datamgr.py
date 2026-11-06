"""Data-manager service for archival and save acknowledgements."""

import sys
import os
# robust import bootstrap (independent of CWD / interpreter): add the repo
# root and <repo>/quatm to sys.path, derived from this file's own location
_here = os.path.dirname(os.path.abspath(__file__))
_repo = _here
while not os.path.isdir(os.path.join(_repo, 'quatm')):
    _parent = os.path.dirname(_repo)
    if _parent == _repo:
        break
    _repo = _parent
for _p in (_repo, os.path.join(_repo, 'quatm')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _here, _repo, _p


from quatm.servers import (Properties, DataClient, ImageClient, send_msg,
                           send_info, zmqcontext)
from quatm.servers.configreader import warn_hub_if_exposed
from quatm.servers import zmq_security
import time
import threading
import numpy as np
import h5py
import re
import zmq
from datetime import datetime
from quatm.utils.i18n import tr


# ---------------------------------------------------------------------------
# Pure HDF5 write helpers.
#
# These take *snapshots* of the collected data (plain lists) and never touch the
# DataSummary objects, so they can safely run on a background worker thread
# while the GUI thread keeps receiving new data. They return True on success.
# ---------------------------------------------------------------------------

# Monotonic counter so two saves in the same second never try to create a
# dataset with the same name (h5py raises ValueError if the name exists).
_save_counter = [0]


# ---------------------------------------------------------------------------
# 数据落盘健康状态（致命问题 2：任务成功 ≠ 数据落盘成功）
#
# h5 写入发生在后台 worker 线程，失败时旧逻辑只发一条 warning，任务照样标
# Done——磁盘满 / Data Manager 崩溃时用户“以为有数据”。这里把失败次数与
# 最近一条错误发布到属性树：
#
#   /DataManager/save_errors            累计失败次数（int）
#   /DataManager/last_save_error        最近一条失败原因（str）
#   /DataManager/last_save_error_time   该失败的本地时间（ISO 字符串）
#   /DataManager/alive                  Data Manager 是否在运行（bool）
#
# Experiment Manager 在任务开始/结束各读一次 save_errors，本任务期间增加就
# 把任务标 Failed（“事后发现 + 阻断后续”，不是强一致）。发布全部 best-effort：
# 没有属性系统时只 print，任何发布异常都不允许影响写盘流程。
# ---------------------------------------------------------------------------

_SAVE_HEALTH = {'errors': 0, 'last_error': '', 'last_time': ''}
_SAVE_HEALTH_LOCK = threading.Lock()
#: 显式注入的属性客户端（测试 / 嵌入 / sim 用）；None = 自动模式。
_health_props = None
#: 未注入客户端时是否自动创建 ``Properties('DataManager')``。
_health_props_auto = True
_health_print_once = [False]


def set_health_props(props, auto=None):
    """注入/清除保存健康状态发布用的属性客户端（测试、sim、嵌入场景）。

    Args:
        props: 具备 ``set(key, value)`` 的对象；None 表示回到自动模式。
        auto (bool|None): 是否允许自动创建 Properties 客户端；None 表示不变。
    """
    global _health_props, _health_props_auto
    _health_props = props
    if auto is not None:
        _health_props_auto = bool(auto)


def reset_save_health():
    """清空进程内保存健康状态（测试 / 重新开始记录时用）。"""
    with _SAVE_HEALTH_LOCK:
        _SAVE_HEALTH.update({'errors': 0, 'last_error': '', 'last_time': ''})


def save_health():
    """返回当前保存健康快照 ``{'errors','last_error','last_time'}``。

    Returns:
        dict: 计数、最近错误文本与时间（副本，可安全修改）。
    """
    with _SAVE_HEALTH_LOCK:
        return dict(_SAVE_HEALTH)


def save_error_count():
    """返回进程内累计的 h5 落盘失败次数。

    Returns:
        int: 失败次数。
    """
    with _SAVE_HEALTH_LOCK:
        return int(_SAVE_HEALTH['errors'])


def _health_client():
    """返回发布健康状态用的属性客户端（显式注入优先，否则自动创建并缓存）。

    Returns:
        具备 ``set`` 的对象；没有属性客户端且不允许自动创建时返回 None。
    """
    global _health_props
    client = _health_props
    if client is not None:
        return client
    if not _health_props_auto:
        return None
    try:
        _health_props = Properties('DataManager', enable_monitors=False)
    except Exception as exc:  # noqa: BLE001 - 没有属性系统：只打印一次
        if not _health_print_once[0]:
            _health_print_once[0] = True
            print('[Data Manager] save health is not published (no property '
                  'client): %s' % exc)
        return None
    return _health_props


def _publish_health(props=None):
    """尽力把保存健康状态发布到 ``/DataManager/*``（失败绝不影响写盘流程）。

    Args:
        props: 可选属性客户端；None 时用模块级客户端（或自动创建）。
    """
    client = props if props is not None else _health_client()
    with _SAVE_HEALTH_LOCK:
        state = dict(_SAVE_HEALTH)
    if client is None:
        # 没有属性系统：按任务要求只 print（不改变原有 send_msg 告警）
        print('[Data Manager] save error #%d: %s'
              % (state['errors'], state['last_error']))
        return
    try:
        client.set('/DataManager/save_errors', int(state['errors']))
        client.set('/DataManager/last_save_error', str(state['last_error']))
        client.set('/DataManager/last_save_error_time', str(state['last_time']))
    except Exception:  # noqa: BLE001 - 只是可观测性，发布失败就算了
        pass


def set_manager_alive(alive, props=None):
    """尽力把 ``/DataManager/alive`` 置 True/False（属性不可用只 print/忽略）。

    供 Data Manager 启动成功 / 退出 / 致命错误时调用；Experiment Manager 的
    预检会读这个属性，但只在它明确为 False 时告警（缺失 = 未知，不误杀）。

    Args:
        alive (bool): True=Data Manager 正在运行；False=已退出/启动失败。
        props: 可选属性客户端；None 时用模块级客户端（或自动创建）。

    Returns:
        bool: 确实发布成功为 True。
    """
    client = props if props is not None else _health_client()
    if client is None:
        print('[Data Manager] alive=%s (no property client)' % bool(alive))
        return False
    try:
        client.set('/DataManager/alive', bool(alive))
        return True
    except Exception:  # noqa: BLE001 - 发布失败只影响可观测性
        return False


def _note_save_error(message, props=None):
    """记录一次 h5 落盘失败：计数 +1、保存最近错误/时间，并尽力发布到属性。

    调用点保持原有的 ``send_msg(..., 'warning')`` 用户告警；本函数只负责
    “可观测”，因此内部任何异常都必须吞掉——绝不能因为记健康状态而影响
    写盘/清理流程。

    Args:
        message (str): 失败原因（可读文本，调用方已本地化）。
        props: 可选属性客户端；None 时用模块级客户端（或自动创建）。
    """
    text = str(message)
    stamp = datetime.now().isoformat(sep=' ', timespec='seconds')
    with _SAVE_HEALTH_LOCK:
        _SAVE_HEALTH['errors'] += 1
        _SAVE_HEALTH['last_error'] = text
        _SAVE_HEALTH['last_time'] = stamp
    try:
        _publish_health(props)
    except Exception:  # noqa: BLE001 - 记健康状态绝不能抛出
        pass


# ---------------------------------------------------------------------------
# 保存回执（per-run save ack）：Data Manager 侧
#
# 目的：把数据健康从“任务结束后比较 save_errors 增量（事后发现）”升级为
# “每个 run/task 标 Done 前确认 Data Manager 已经把数据收到并处理”。实验侧
# 给数据消息挂 ``_ack={'token','seq'}``（见 quatm/servers/clients.py），本模块
# 按 token 记录收到的 seq 集合与写盘失败数，并通过一个独立控制 REP socket
# 回答 flush_ack 请求：
#
#   REQ : {'op': 'flush_ack', 'token': str, 'expected_seq': int}
#   REP : {'ok': True, 'token': str, 'received_seq': int, 'messages': int,
#          'missing': int, 'saved': int, 'errors': int, 'last_error': str}
#         或 {'ok': False, 'token': str, 'error': str}
#
# 端点：环境变量 QUATM_SAVE_ACK_ENDPOINT > configfile 的
# ``Servers/Data Manager/ack`` > 由 Data Hub 的 ``sub`` 端点派生
# （ipc 路径加 ``_ack`` 后缀；tcp 端口 +1）。派生失败 = 回执不可用，调用方
# （ExperimentManager / 子进程 runner）应降级到旧的事后检查，不误报。
# ---------------------------------------------------------------------------

_ACK_LOCK = threading.Lock()
#: token -> {'max_seq': int, 'seqs': set[int], 'saved': set[int]}
_ACK_TOKENS = {}
_ACK_SERVER_LOCK = threading.Lock()
_ACK_SERVER = {'socket': None, 'thread': None, 'stop': None,
               'endpoint': None}


def reset_ack_tracker():
    """清空保存回执统计（测试/重新开始记录用）。"""
    with _ACK_LOCK:
        _ACK_TOKENS.clear()


def _ack_token_state(token):
    """返回（必要时创建）某个 token 的统计容器。

    Args:
        token (str): 任务 token。

    Returns:
        dict: 该 token 的统计容器（调用方需持有 :data:`_ACK_LOCK`）。
    """
    return _ACK_TOKENS.setdefault(str(token),
                                  {'max_seq': 0, 'seqs': set(), 'saved': set()})


def _note_ack(stamp):
    """记录一条收到的带 ack 标记的数据消息。

    Args:
        stamp (dict|None): ``{'token': str, 'seq': int}``；非法时忽略。
    """
    if not isinstance(stamp, dict):
        return
    token = stamp.get('token')
    if not token:
        return
    try:
        seq = int(stamp.get('seq'))
    except (TypeError, ValueError):
        return
    with _ACK_LOCK:
        state = _ack_token_state(token)
        state['seqs'].add(seq)
        if seq > state['max_seq']:
            state['max_seq'] = seq


def _pop_ack(newdata, keep=False):
    """从收到的数据里取出 ``_ack`` 标记并记账。

    数据消息的 ``_ack`` 在统计后删掉（避免污染 run 记录/单流数据字典）；
    ``.start``/``.end`` 标记消息保留它，供写盘时统计 ``saved``（写盘侧会
    跳过 ``_ack`` 键，不写进 h5 schema）。

    Args:
        newdata (dict): 收到的数据字典（原地修改）。
        keep (bool): True=保留 ``_ack``（标记消息用）。

    Returns:
        dict|None: 取到的 ack 标记。
    """
    if not isinstance(newdata, dict):
        return None
    stamp = newdata.get('_ack')
    if not keep:
        newdata.pop('_ack', None)
    _note_ack(stamp)
    return stamp


def _note_saved_acks(data):
    """把已成功写入 h5 的记录里的 ``_ack`` 标记计为 saved。

    Args:
        data (list[dict]|None): 本次真正写入的数据集记录列表。
    """
    if not isinstance(data, (list, tuple)):
        return
    for record in data:
        if not isinstance(record, dict):
            continue
        stamp = record.get('_ack')
        if not isinstance(stamp, dict):
            continue
        token = stamp.get('token')
        if not token:
            continue
        try:
            seq = int(stamp.get('seq'))
        except (TypeError, ValueError):
            continue
        with _ACK_LOCK:
            _ack_token_state(token)['saved'].add(seq)


def ack_snapshot(token):
    """返回某个 token 的保存回执统计（含全局写盘失败数/最近错误）。

    Args:
        token (str): 任务 token。

    Returns:
        dict: ``{'ok': True, 'token', 'received_seq', 'messages', 'missing',
        'saved', 'errors', 'last_error'}``。
    """
    with _ACK_LOCK:
        state = _ACK_TOKENS.get(str(token))
        if state is None:
            seqs, max_seq, saved = set(), 0, set()
        else:
            seqs = set(state['seqs'])
            max_seq = int(state['max_seq'])
            saved = set(state['saved'])
    health = save_health()
    return {'ok': True,
            'token': str(token),
            'received_seq': max_seq,
            'messages': len(seqs),
            'missing': max(0, max_seq - len(seqs)),
            'saved': len(saved),
            'errors': int(health['errors']),
            'last_error': str(health['last_error'] or '')}


def _flush_ack_h5():
    """flush 仍未关闭的 h5 文件（当前写盘路径每个 ``with`` 块结束即关闭）。

    保留这个钩子是为了将来的长连接写入；没有任何打开文件时是空操作。

    Returns:
        bool: 恒为 True。
    """
    return True


def _handle_ack_request(request):
    """处理一条控制请求，返回应答 dict（GUI/线程/主循环共用）。

    Args:
        request (dict): ``{'op': 'flush_ack', 'token': str, 'expected_seq': int}``。

    Returns:
        dict: ack 统计或 ``{'ok': False, 'error': ...}``。
    """
    if not isinstance(request, dict):
        return {'ok': False, 'error': 'bad request'}
    if request.get('op') != 'flush_ack':
        return {'ok': False,
                'error': 'unsupported op %r' % (request.get('op'),)}
    token = request.get('token')
    if not token:
        return {'ok': False, 'error': 'missing token'}
    _flush_ack_h5()
    return ack_snapshot(token)


def _derive_ack_endpoint(data_hub_endpoint):
    """由 Data Hub 端点派生控制 REP 端点（不可派生时返回 None）。

    Args:
        data_hub_endpoint (str): Data Hub 的 sub/pub 端点。

    Returns:
        str|None: 控制端点；ipc/tcp 之外的 scheme 返回 None。
    """
    endpoint = str(data_hub_endpoint or '').strip()
    if endpoint.startswith('ipc://'):
        return endpoint.rstrip('/') + '_ack'
    if endpoint.startswith('tcp://'):
        hostport = endpoint[len('tcp://'):]
        host, sep, port_text = hostport.rpartition(':')
        if not sep or not port_text.isdigit():
            return None
        if host in ('0.0.0.0', '*', ''):
            host = '127.0.0.1'
        port = int(port_text)
        if port <= 0 or port >= 65535:
            return None
        return 'tcp://%s:%d' % (host, port + 1)
    return None


def ack_endpoint():
    """返回保存回执控制端点（环境变量 > 配置 > 由 Data Hub 派生）。

    Returns:
        str|None: 控制端点；无法确定时 None（= 回执不可用，调用方降级）。
    """
    env = os.environ.get('QUATM_SAVE_ACK_ENDPOINT', '').strip()
    if env:
        return env
    try:
        from quatm.servers.configreader import ConfigReader, server_endpoints
        conf = ConfigReader.getConfiguration()
        servers = conf.get('Servers') or {}
        entry = None
        for key in ('Data Manager', 'Datamanager', 'DataManager'):
            candidate = servers.get(key)
            if isinstance(candidate, dict):
                entry = candidate
                break
        if isinstance(entry, dict):
            configured = str(entry.get('ack') or '').strip()
            if configured:
                return configured
        hub = server_endpoints(conf, 'Data Hub')
        return _derive_ack_endpoint(hub.get('sub') or hub.get('pub'))
    except Exception:  # noqa: BLE001 - 配置不可用：回执不可用即可
        return None


def _serve_one_ack(sock):
    """处理一个已就绪的控制请求并回包（异常一律吞掉，不能打断循环）。

    Args:
        sock (zmq.Socket): REP socket。
    """
    try:
        request = sock.recv_json()
    except Exception:  # noqa: BLE001 - 坏帧：丢弃该请求
        return
    try:
        reply = _handle_ack_request(request)
    except Exception as exc:  # noqa: BLE001 - 应答内部错误
        reply = {'ok': False, 'error': 'internal error: %s' % exc}
    try:
        sock.send_json(reply)
    except Exception:  # noqa: BLE001 - 对端已断开
        pass


def _ack_serve_loop(sock, stop):
    """后台线程：每 50 ms poll 一次控制请求（不阻塞数据路径）。

    Args:
        sock (zmq.Socket): REP socket。
        stop (threading.Event): 停止信号。
    """
    while not stop.is_set():
        try:
            if not sock.poll(50):
                continue
        except Exception:  # noqa: BLE001 - socket 已关闭
            break
        _serve_one_ack(sock)


def start_ack_server(endpoint=None, threaded=None):
    """启动保存回执控制服务（幂等；绑定失败/端点不可用时返回 False）。

    Args:
        endpoint (str|None): 覆盖端点；None 用 :func:`ack_endpoint`。
        threaded (bool|None): True=后台线程 poll；False=由
            :func:`run_forever` 主循环调用 :func:`poll_ack_requests`；
            None 默认 True（GUI 的 Data Manager 进程没有 run_forever）。

    Returns:
        bool: 已启动/已在运行为 True。
    """
    with _ACK_SERVER_LOCK:
        if _ACK_SERVER['socket'] is not None:
            return True
        target = endpoint or ack_endpoint()
        if not target:
            return False
        try:
            sock = zmqcontext.socket(zmq.REP)
            # 本机 ZMQ 默认 CURVE：REP bind 端是 server role
            zmq_security.server_configure(sock, endpoint=str(target))
            sock.setsockopt(zmq.LINGER, 0)
            sock.bind(str(target))
            sock.RCVTIMEO = 100
        except Exception as exc:  # noqa: BLE001 - 端点被占/不可绑：回执不可用
            print('[Data Manager] save ack endpoint %r unavailable: %s'
                  % (target, exc))
            return False
        _ACK_SERVER['socket'] = sock
        _ACK_SERVER['endpoint'] = str(target)
        if threaded is None or threaded:
            stop = threading.Event()
            thread = threading.Thread(target=_ack_serve_loop,
                                      args=(sock, stop), daemon=True,
                                      name='quatm-save-ack')
            _ACK_SERVER['stop'] = stop
            _ACK_SERVER['thread'] = thread
            thread.start()
        return True


def poll_ack_requests(timeout_ms=0):
    """处理一个挂起的控制请求（供 ``run_forever`` 主循环调用）。

    只有 :func:`start_ack_server` 以 ``threaded=False`` 启动时才有意义；
    无请求/未启动时立即返回 0，不阻塞数据路径。

    Args:
        timeout_ms (int): 最多等多久 [ms]（通常 0）。

    Returns:
        int: 处理了 1 个请求返回 1，否则 0。
    """
    sock = _ACK_SERVER.get('socket')
    if sock is None or _ACK_SERVER.get('thread') is not None:
        return 0
    try:
        if not sock.poll(max(0, int(timeout_ms))):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    _serve_one_ack(sock)
    return 1


def stop_ack_server():
    """停止保存回执服务（测试/进程退出用；幂等）。"""
    with _ACK_SERVER_LOCK:
        stop = _ACK_SERVER.get('stop')
        sock = _ACK_SERVER.get('socket')
        thread = _ACK_SERVER.get('thread')
        _ACK_SERVER.update({'socket': None, 'thread': None, 'stop': None})
    if stop is not None:
        stop.set()
    # 先让服务线程退出 poll，再关 socket：跨线程关闭一个正在 poll 的 ZMQ
    # socket 不安全（libzmq 可能断言）。poll 超时 50 ms，join 很快返回。
    if thread is not None and thread is not threading.current_thread():
        try:
            thread.join(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
    if sock is not None:
        try:
            sock.close(0)
        except Exception:  # noqa: BLE001
            pass


def _save_ack_request(token, expected_seq=0, endpoint=None):
    """发一次 flush_ack 请求，返回应答 dict；端点不可达/超时返回 None。

    Args:
        token (str): 任务 token。
        expected_seq (int): 期望已收到的最大 seq（信息字段）。
        endpoint (str|None): 控制端点；None 用 :func:`ack_endpoint`。

    Returns:
        dict|None: Data Manager 的应答；不可用时 None。
    """
    target = endpoint or ack_endpoint()
    if not target:
        return None
    try:
        sock = zmqcontext.socket(zmq.REQ)
    except Exception:  # noqa: BLE001
        return None
    try:
        # 本机 ZMQ 默认 CURVE：REQ connect 端是 client role
        zmq_security.client_configure(sock)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 300)
        sock.connect(str(target))
        sock.send_json({'op': 'flush_ack', 'token': str(token),
                        'expected_seq': int(expected_seq)})
        reply = sock.recv_json()
        return reply if isinstance(reply, dict) else None
    except Exception:  # noqa: BLE001 - 超时/旧版无端点/对端错误
        return None
    finally:
        try:
            sock.close(0)
        except Exception:  # noqa: BLE001
            pass


def save_ack(token, expected_seq, timeout=5.0, baseline_errors=None,
             endpoint=None, poll_interval=0.05):
    """等待 Data Manager 确认本 token 的数据已被收到（GUI/runner 共用 API）。

    在 ``timeout`` 内反复向控制端点发 ``flush_ack``：直到
    ``received_seq >= expected_seq``、``missing == 0`` 且没有新增写盘错误。
    Data Manager 不可达/旧版本没有控制端点 → 返回 ``available=False``，
    调用方应降级到“事后 save_errors 检查”，不要误判失败。

    Args:
        token (str): 任务 token（数据消息里的 ``_ack.token``）。
        expected_seq (int): 本 run/task 结束时发出的最大 seq。
        timeout (float): 最长等待 [s]。
        baseline_errors (int|None): 开始等待前的写盘失败计数；None 不比较。
        endpoint (str|None): 控制端点；None 用 :func:`ack_endpoint`。
        poll_interval (float): 重试间隔 [s]。

    Returns:
        dict: ``{'available': bool, 'ok': bool, 'reason': str,
        'response': dict|None}``；``response`` 含 received_seq/messages/
        missing/saved/errors/last_error。
    """
    expected = max(0, int(expected_seq))
    deadline = time.monotonic() + max(0.0, float(timeout))
    last_response = None
    while True:
        response = _save_ack_request(token, expected, endpoint=endpoint)
        if response is None:
            return {'available': False, 'ok': False,
                    'reason': 'unavailable', 'response': None}
        last_response = response
        if not response.get('ok'):
            return {'available': True, 'ok': False, 'reason': 'error',
                    'response': response}
        received = int(response.get('received_seq') or 0)
        missing = int(response.get('missing') or 0)
        errors = int(response.get('errors') or 0)
        if baseline_errors is not None and errors > int(baseline_errors):
            return {'available': True, 'ok': False,
                    'reason': 'save-errors', 'response': response}
        if received >= expected and missing == 0:
            return {'available': True, 'ok': True, 'reason': 'ok',
                    'response': response}
        if time.monotonic() >= deadline:
            return {'available': True, 'ok': False, 'reason': 'timeout',
                    'response': last_response}
        time.sleep(max(0.0, float(poll_interval)))


def _unique_dataset_suffix():
    """生成每个保存唯一的数据集名字：时间戳 + 单调计数器。

    同一秒内两次保存不会撞名（同名数据集 h5py 会抛 ValueError）。
    """
    _save_counter[0] += 1
    return '%d_%d' % (int(time.time()), _save_counter[0])


def _ensure_h5_dir(h5file):
    """打开 .h5 文件前确保其所在目录存在。

    ``h5py.File(h5file, 'a')`` 在父目录缺失时会抛 FileNotFoundError（例如
    过时的数据路径或全新安装还没建数据文件夹）。保存数据不应因此崩溃，
    改为按需创建目录。

    Args:
        h5file (str): 目标 .h5 文件路径。
    """
    parent = os.path.dirname(h5file)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _determine_formatstring(data):
    """根据一组 run 字典列表构造结构化 numpy dtype。

    逐 key 汇总**所有 run** 中出现过的值类型（不只最后一个 run），避免异构
    数据丢字段：数值（int/float/np 标量）映射为对应数值字段，int/float 跨
    run 混用时升级 float64；bool 映射为 ``?`` 字段；list 按全局最大长度拆成
    编号数值字段（如 key0, key1…，短 run 缺的列写入时补零）；字符串映射为
    定宽 S 字节字段（按最长 utf-8 字节数取宽，中文实验名也能放下）。
    None/dict 等无法表示的类型不建字段（写入侧跳过并告警）。

    Args:
        data (list[dict]): 各 run 的字典列表。

    Returns:
        np.dtype: 结构化 dtype。
    """
    # 逐 key 汇总所有 run 中出现过的值类型（而不是只看最后一个 run），
    # 避免异构数据被静默丢字段：bool 此前被丢掉、跨 run 类型不同的标量
    # 只按最后出现者定类型、跨 run 长度不同的列表只按最后一个 run 拆列
    # （长一点的 run 的多余列会被静默丢弃）。
    strlen = {}          # str 标量 key -> 最长 utf-8 字节宽
    ftypes = {}          # 数值/布尔标量 key -> 统一后的 dtype
    listlen = {}         # list key -> 全局最大长度（跨 run）
    list_is_float = {}   # list key -> 元素出现过 float（列用 float64）
    for run in data:
        for k, v in run.items():
            if k == '_ack':
                # 保存回执元数据（token/seq）：不是测量字段，不建 h5 列
                continue
            if isinstance(v, str):
                # S<n> is a BYTE width: use the utf-8 byte length so non-ASCII
                # strings (e.g. Chinese experiment names) fit in the field
                strlen[k] = max(strlen.get(k, 0), len(v.encode('utf-8')))
            elif isinstance(v, bool):
                ftypes[k] = np.dtype('?')
            elif isinstance(v, (int, float, np.integer, np.floating)):
                want = (np.dtype('f8') if isinstance(v, (float, np.floating))
                        else np.dtype('i8'))
                cur = ftypes.get(k)
                if cur is None:
                    ftypes[k] = want
                elif cur.kind != want.kind:
                    # int/float 跨 run 混用：升级为 float64 以免丢精度
                    ftypes[k] = np.dtype('f8')
            elif isinstance(v, (list, tuple)):
                listlen[k] = max(listlen.get(k, 0), len(v))
                for item in v:
                    if isinstance(item, (float, np.floating)):
                        list_is_float[k] = True
    flist = []
    for k in sorted(ftypes):
        flist.append((k, ftypes[k]))
    # unpack all lists into numbered individual values, they should be
    # short as the head is not used to send data; 列数取全局最大长度，短
    # 一点的 run 缺的列在写入时补零
    for k in sorted(listlen):
        dt = np.dtype('f8') if list_is_float.get(k) else np.dtype('i8')
        for i in range(listlen[k]):
            flist.append(('{}{}'.format(k, i), dt))
    # scalar string values (e.g. _expName) as fixed-width BYTES fields
    # (h5py does not support unicode '<U' inside compound dtypes)
    for k, n in sorted(strlen.items()):
        if k not in ftypes:
            flist.append((k, 'S%d' % max(1, n)))
    return np.dtype(flist)


def _write_data_summary(h5file, folder, name, data, props=None):
    """把一组 run 字典作为一个结构化数据集写入 H5 文件。

    数据集名为 ``folder/name+唯一后缀``。成功返回 True；数据集过大或路径
    无权限时发 warning 并返回 False（同时记入 ``/DataManager/save_errors``
    健康计数，供 Experiment Manager 在任务结束时判定“数据没写进去”）。
    空数据直接返回 True（空复合 dtype 无法建零行数据集）。记录先用
    np.zeros 在内存组装（未赋值字段一律为 0，绝不把未初始化内存写进文件）
    再一次性批量写入：bool 值写入 ``?`` 字段；跨 run 类型异构/None/dict 等
    无法表示的值跳过并在 dropped 里发 warning；批量写入失败会删除已建数据集
    （不留半成品）再返回 False。

    Args:
        h5file (str): 目标 .h5 文件路径。
        folder (str): 文件内组名。
        name (str): 数据集名前缀。
        data (list[dict]): run 字典快照。
        props: 可选属性客户端（用于发布保存健康状态）；None 时用模块级自动
            客户端。

    Returns:
        bool: 是否写入成功。
    """
    if len(data) == 0:
        # nothing to save: h5py cannot create a zero-row structured dataset
        # with an empty compound dtype (it raises ValueError), so just no-op.
        return True
    try:
        _ensure_h5_dir(h5file)
    except OSError as exc:
        message = tr('[Data Manager] H5 file failed to save, please check the permission of the path.')
        _note_save_error('%s (%s)' % (message, exc), props=props)
        send_msg(message, 'warning')
        return False
    try:
        tabletype = _determine_formatstring(data)
    except Exception as exc:  # noqa: BLE001 - 全部字段都无法表示等
        message = tr('[Data Manager] saving summary %r failed: %s') % (name, exc)
        _note_save_error(message, props=props)
        send_msg(message, 'warning')
        return False
    dataset_name = folder + '/' + name + _unique_dataset_suffix()
    try:
        file = h5py.File(h5file, 'a')
    except Exception as exc:  # noqa: BLE001 - 权限/磁盘满/文件损坏
        message = tr('[Data Manager] H5 file failed to save, please check the permission of the path.')
        _note_save_error('%s (%s)' % (message, exc), props=props)
        send_msg(message, 'warning')
        return False
    with file:
        try:
            dataset = file.create_dataset(dataset_name, (len(data),), tabletype)
        except ValueError as exc:
            message = tr('[Data Manager] H5 file failed to save, please check the size of the dataset.')
            _note_save_error('%s (%s)' % (message, exc), props=props)
            send_msg(message, 'warning')
            return False
        except FileNotFoundError as exc:
            message = tr('[Data Manager] H5 file failed to save, please check the permission of the path.')
            _note_save_error('%s (%s)' % (message, exc), props=props)
            send_msg(message, 'warning')
            return False
        except Exception as exc:  # noqa: BLE001 - 其它 h5py 写入错误
            message = tr('[Data Manager] saving summary %r failed: %s') % (name, exc)
            _note_save_error(message, props=props)
            send_msg(message, 'warning')
            return False
        # Build the records in memory and write them in ONE bulk operation.
        # The old per-element loop did one tiny H5 write per (row, column),
        # which is orders of magnitude slower for large datasets.
        # np.zeros：所有字段先归零。之前用 np.empty，异构/跨 run 数据里
        # 未赋值的字段会把未初始化内存当真实数据写进文件。
        records = np.zeros(len(data), dtype=tabletype)
        dropped = set()
        for index, values in enumerate(data):
            rec = records[index]
            for k, v in values.items():
                if k == '_ack':
                    # 保存回执元数据：只为统计 saved，不写进 h5 数据集
                    continue
                try:
                    if isinstance(v, str):
                        rec[k] = v.encode('utf-8')
                    elif isinstance(v, bool):
                        rec[k] = bool(v)
                    elif isinstance(v, (int, float, np.integer, np.floating)):
                        rec[k] = v
                    elif isinstance(v, (list, tuple)):
                        # unpack lists, they should usually be short
                        for i, v_i in enumerate(v):
                            if isinstance(v_i, (int, float, np.integer, np.floating)):
                                rec['{}{}'.format(k, i)] = v_i
                    else:
                        # None/dict/其它不支持类型：无法表示，跳过并在
                        # dropped 里向用户告警
                        dropped.add(k)
                except (TypeError, ValueError, KeyError):
                    # 跨 run 类型异构/字段缺失：跳过该值，别让单条坏记录
                    # 报废整份数据
                    dropped.add(k)
        if dropped:
            send_msg(tr('[Data Manager] saving summary dropped unsupported '
                     'value(s): %s') % ', '.join(sorted(dropped)[:10]), 'warning')
        try:
            dataset[:] = records
        except Exception as e:
            # 不留半成品数据集在文件里：删除再返回失败
            try:
                del file[dataset_name]
            except Exception:
                pass
            message = tr('[Data Manager] saving summary %r failed: %s') % (name, e)
            _note_save_error(message, props=props)
            send_msg(message, 'warning')
            return False
    # 数据集已写入并关闭：把其中的保存回执标记计为 saved（供 flush_ack 报告）
    _note_saved_acks(data)
    return True


def _write_single_data(h5file, folder, name, data, arrays, props=None):
    """写入一组 1D 数组块及对应的 run 字典。

    数组先规整为 2D（1D 单列自动 reshape），再打包成 (n, ...) 单数据集
    写入；随后调用 _write_data_summary 写字典部分。失败时除原有 warning
    外还记入 ``/DataManager/save_errors`` 健康计数。

    Args:
        h5file (str): 目标 .h5 文件路径。
        folder (str): 文件内组名。
        name (str): 数据集名前缀。
        data (list[dict]): run 字典快照。
        arrays (list[np.ndarray]): 1D 数组块列表。
        props: 可选属性客户端（用于发布保存健康状态）。

    Returns:
        bool: 是否全部写入成功。
    """
    success = True
    if len(arrays) > 0:
        try:
            _ensure_h5_dir(h5file)
            # normalize to 2D so both 1D (single column) and 2D blocks save fine;
            # the old code indexed a.shape[1] unconditionally -> IndexError on 1D
            arrays = [np.asarray(a) for a in arrays]
            arrays = [a.reshape(-1, 1) if a.ndim == 1 else a for a in arrays]
            shapes = np.array([a.shape for a in arrays])
            newshape = tuple(int(max(i)) for i in shapes.T)
            alldata = np.zeros((len(arrays), *newshape))
            for n, a in enumerate(arrays):
                alldata[n, :a.shape[0], :a.shape[1]] = a
            with h5py.File(h5file, 'a') as file:
                file.create_dataset(
                    folder + '/' + name + 'dat' + _unique_dataset_suffix(),
                    data=alldata)
        except Exception as exc:  # noqa: BLE001 - 数组/文件写入失败
            message = tr('[Data Manager] saving single data {name!r} failed: '
                         '{error}', name=name, error=exc)
            _note_save_error(message, props=props)
            send_msg(message, 'warning')
            success = False
    success &= _write_data_summary(h5file, folder, name, data, props=props)
    return success


def _write_images(h5file, folder, name, arrays, axis, props=None):
    """把图像及其 x/y 坐标轴写入 H5 文件的一个组。

    每张图存为 ``{:04d}`` 数据集，对应轴存为 ``{:04d}x``/``{:04d}y``。
    失败时除原有 warning 外还记入 ``/DataManager/save_errors`` 健康计数。

    Args:
        h5file (str): 目标 .h5 文件路径。
        folder (str): 文件内组名。
        name (str): 组名前缀。
        arrays (list[np.ndarray]): 图像数组列表。
        axis (list[tuple]): 每张图的 (x轴, y轴)。
        props: 可选属性客户端（用于发布保存健康状态）。

    Returns:
        bool: 是否写入成功。
    """
    try:
        _ensure_h5_dir(h5file)
        with h5py.File(h5file, 'a') as file:
            group = file.create_group(folder + '/' + name + 'dat'
                                      + _unique_dataset_suffix())
            for i, a in enumerate(arrays):
                group.create_dataset("{:04d}".format(i), data=a)
                if i < len(axis):
                    group.create_dataset("{:04d}x".format(i), data=axis[i][0])
                    group.create_dataset("{:04d}y".format(i), data=axis[i][1])
    except Exception as exc:  # noqa: BLE001 - 图像写入失败
        message = tr('[Data Manager] saving images {name!r} failed: {error}',
                     name=name, error=exc)
        _note_save_error(message, props=props)
        send_msg(message, 'warning')
        return False
    return True



class DataSummary:
    """汇总多个输入通道的数据流，整理成每个 run 的完整字典。

    收到的数据先累积在 incompleteData；当所有订阅通道的数据都已到达
    （_streams 覆盖 streamlist）时，整条记录移入 completeData。若 run 结束
    后 timeout 秒仍有数据未到，缺失部分以零补齐。实验从未发 Experiment.end
    时（中断/崩溃），超过 stalltimeout[s]（默认 300 s）没有数据活动的 run
    会被强制结案，避免永久滞留 incompleteData。

    Args:
        name (str): 本汇总器的名字，用作属性命名空间与 DataClient 名。
        props (Properties|None): 属性树实例；None 时自行创建。

    Attributes:
        incompleteData (list[dict]): 尚未收齐的 run 记录。
        completeData (list[dict]): 已收齐（或超时补齐）的 run 记录。
        streamlist (list[str]): 订阅的通道列表。
        task (int): 当前任务序号。
        dataq (DataClient): 数据订阅客户端。
    """

    def __init__(self, name, props=None):
        """构造数据汇总器：创建属性、DataClient 并初始化订阅。

        Args:
            name (str): 汇总器名字。
            props (Properties|None): 属性树实例；None 时用 name 自建。
        """
        if props is None:
            props = Properties(name)
        self.name = name
        self.props = props
        self.incompleteData = []
        self.completeData = []
        self.streamlist = []
        self.task = -1
        self.expIsRunning = False
        # 轮询计数与“本轮收到过数据”必须是**实例**状态：GUI 会同时存在
        # 多个 DataSummary（汇总 + 多个 SingleDataSummary/对齐 plotter），
        # 共用类级变量时一个实例的轮询会清掉/吞掉另一个实例的计数与告警。
        self.data_update_checks = 0
        self.got_data = False
        self.dataq = DataClient(self.name)
        self.initDataq()
        self.lastupdate = 0
        # h5storage 的主汇总器（名字固定 'Summary'）就是实际 Data Manager 写入
        # 方所在的进程：在这里尽力启动保存回执控制服务，供 GUI / 子进程 runner
        # 查询“数据到没到”。其他 DataSummary（对齐 plotter/单流）不启动。
        if str(name) == 'Summary':
            try:
                start_ack_server()
            except Exception:  # noqa: BLE001 - 回执服务失败不影响收数据
                pass

    def initDataq(self):
        """（重新）订阅属性里配置的通道加实验开始/结束事件。

        读取属性 ``datastreams`` 并与当前订阅比较，变化时重置订阅。
        """
        new_streamlist = self.props.get('datastreams', [])+['Experiment.start', 'Experiment.end']
        if new_streamlist != self.streamlist:
            self.streamlist = new_streamlist
            send_info(tr('[Data Manager] datamgr DataSummary: subscription reset'))
            self.dataq.unsubscribe()
            self.dataq.subscribe(self.streamlist)

    def recvData(self):
        """接收并分发数据队列中的全部消息（非阻塞轮询）。

        每 ~1000 次调用若始终无数据则发一次 warning（约 50 秒无数据）。
        收到一帧就把 (msg, dic, A) 交给 processIncomingData。
        """
        self.data_update_checks += 1
        if self.data_update_checks % 1000 == 0:
            self.data_update_checks = 0
            if not self.got_data:
                send_msg(tr('[RecvData] No data arrived within approx. 50 sec.'), 'warning')
            self.got_data = False
        while self.dataq.has_new_data():
            # send_msg('[RecvData] Received new data.', 'info')
            recvmsg = self.dataq.recv()
            A = None
            if len(recvmsg) == 2:
                msg, dic = recvmsg
            elif len(recvmsg) == 3:
                msg, dic, A = recvmsg
            else:
                send_msg(tr('[RecvData] an error happened in datamgr.'), 'error')
                return
            self.got_data = True
            self.processIncomingData(msg, dic, A)

    def processIncomingData(self, msg, newdata, A):
        """处理一帧收到的数据，归并到最近的 run 记录。

        'Experiment.start' 开启新 run；'Experiment.end' 结束当前 run；普通
        通道数据按时间戳归入最后开始的 run（键加通道名前缀，_ 开头的元
        数据键不并入）。每帧数据会刷新命中 run 的 ``_lastactive`` 时间戳
        （供停滞兜底判定）。处理完调用 _processCompleteData。

        Args:
            msg (str): 通道/消息名。
            newdata (dict): 数据字典。
            A: 可选的附加数组（DataSummary 不使用）。
        """

        # 保存回执：记录本帧的 ack（token/seq）。标记消息保留 _ack 供写盘
        # 侧统计 saved；普通数据消息取出后丢弃（不污染 run 记录）。
        _pop_ack(newdata, keep=msg in ('Experiment.start', 'Experiment.end'))
        incomp = self.incompleteData
        if msg == 'Experiment.start':
            self.expIsRunning = True
            if newdata['_task'] > self.task:
                self.task = newdata['_task']
                self.newTask()
            incomp.append(newdata)
            #print(repr(newdata))
            incomp[-1]['_streams'] = [msg]
            incomp[-1]['_lastactive'] = time.time()
        elif len(incomp) > 0:    #process data only after experiment start has been detected
            if msg == 'Experiment.end':
                incomp[-1].update(newdata)
                self.expIsRunning = False
                incomp[-1]['_streams'].append(msg)
            else:
                for line in reversed(incomp):
                    #print(line['_starttime'],newdata['timestamp'])                 #take last run starting before image was recorded
                    if line['_starttime'] < newdata['timestamp']:                 #take last run starting before image was recorded
                        dic = {msg + '.' + k: v for k, v in newdata.items() if k[0] != '_'}   #don't take parameters starting with _,prepend channelname to key
                        line.update(dic)
                        line['_streams'].append(msg)
                        line['_lastactive'] = time.time()
                        #print('break')
                        break
                else:
                    send_msg(tr('[Data Manager] data discarded because: data arrived after timeout or more than once per run ')+msg, 'warning')#,'incomp', repr(incomp))
        self._processCompleteData()

    def _processCompleteData(self):
        """把已收齐（或超时/停滞）的 run 从 incompleteData 移入 completeData。

        满足三条件之一即判定可移出：1) _streams 覆盖全部订阅通道；2) 已收到
        _endtime 且超过 timeout 秒仍未收齐（缺失以零补齐）；3) 从未收到
        _endtime（中断/崩溃）且超过 stalltimeout[s]（默认 300 s）没有数据
        活动——兜底结案，避免 run 永久滞留 incompleteData。
        """
        # sort out complete data
        incomp = self.incompleteData
        if len(incomp) == 0:
            return
        #print('dsdad',set(self.streamlist),set(incomp[0]['_streams']))
        while len(incomp) > 0 and set(self.streamlist) == set(incomp[0]['_streams']):
            self.completeData.append(incomp.pop(0))
            self.lastupdate = time.time()
            # print('complete!!!!!!!!!!!!',self.completeData[-1])
        while len(incomp) > 0 and '_endtime' in incomp[0] and time.time() > incomp[0]['_endtime'] + self.props.get('timeout',1):
            # ('datamgr.py Warning: timeout (some data has not arrived) ',set(self.streamlist),
                    # set(incomp[0]['_streams']),set(self.streamlist)==set(incomp[0]['_streams']))
            self.completeData.append(self.incompleteData.pop(0))
            self.lastupdate = time.time()
        # 兜底：实验从未发 Experiment.end（手动中断/崩溃/忘记 stop）时，
        # 上面两条都不会触发，run 会永久滞留 incompleteData 造成内存
        # 增长与数据丢失。超过 stalltimeout[s]（默认 300 s）没有任何
        # 数据或 end 的 run 强制结案。新 run 开始后旧 run 不再收到数据
        # （数据归入更新的 run），其 _lastactive 停止刷新，也会在这里被
        # 结案；正在进行的 run 只要有数据帧就会刷新 _lastactive，不会
        # 被误结案。
        stall = self.props.get('stalltimeout[s]', 300)
        while (len(incomp) > 0
               and time.time() - incomp[0].get('_lastactive',
                                               incomp[0].get('_starttime', 0)) > stall):
            self.completeData.append(self.incompleteData.pop(0))
            self.lastupdate = time.time()
    def newTask(self):
        """新任务开始时的钩子函数（当前为空实现，保留给子类扩展使用）。"""
        #print(self.completeData)
        pass

    def savetoFile(self, filename, folder='folder', data=None, clear_after=True):
        """把收集到的 run 字典写入 HDF5 结构化数据集。

        Args:
            filename: 目标 .h5 文件。
            folder (str): 文件内的组名。
            data (list[dict]|None): 待保存快照；None 时用 self.completeData。
            clear_after (bool): 保存后是否清空已收集数据。

        Returns:
            bool: 是否保存成功。
        """
        if data is None:
            data = self.completeData
        success = _write_data_summary(filename, folder, self.name, data,
                                      props=self.props)
        if clear_after:
            self.clear()
        return success

    def loadFromFile(self, filepath, name, summary):
        """从 HDF5 文件读回一组已存的 run 字典。

        读取 ``filepath`` 内 ``name/summary`` 数据集（:meth:`savetoFile` 写
        出的格式）到 self.completeData，使已存测量无需重新采集即可绘图/检查。

        Args:
            filepath (str): .h5 文件路径。
            name (str): 组名。
            summary (str): 数据集名。

        Returns:
            list[dict]: 读回的 run 字典列表。
        """
        with h5py.File(filepath, 'r') as file:
            node = file[name][summary]
            if not isinstance(node, h5py.Dataset):
                send_msg(tr('[Data Manager] loadFromFile(): %r is not a data '
                         'summary dataset, cannot load it.') % summary, 'warning')
                return []
            fields = node.dtype.names or ()
            rows = node[:]

        data = []
        for row in rows:
            run = {}
            list_parts = {}   # field prefix -> {index: value}
            scalars = {}      # plain scalar field -> value
            for field in fields:
                value = row[field]
                if isinstance(value, np.generic):
                    value = value.item()
                if isinstance(value, bytes):   # S-width string fields -> str
                    value = value.decode('utf-8', errors='replace')
                m = re.match(r'^(.*[^0-9])([0-9]+)$', field)
                if m:
                    # candidates for list elements unpacked from `key0, key1, ...`
                    prefix = m.group(1)
                    list_parts.setdefault(prefix, {})[int(m.group(2))] = value
                else:
                    scalars[field] = value

            for prefix, parts in list_parts.items():
                if prefix in scalars or len(parts) < 2:
                    # ambiguous with a genuine scalar: keep the unpacked form
                    for idx, val in sorted(parts.items()):
                        run['{}{}'.format(prefix, idx)] = val
                else:
                    # reconstruct the list when we have a full 0..N-1 run
                    idxs = sorted(parts)
                    if idxs == list(range(len(idxs))):
                        run[prefix] = [parts[i] for i in idxs]
                    else:
                        for i in idxs:
                            run['{}{}'.format(prefix, i)] = parts[i]
            run.update(scalars)
            data.append(run)

        self.incompleteData = []
        self.completeData = data
        self.task = -1
        self.lastupdate = time.time()
        send_info(tr('[Data Manager] loadFromFile(): loaded %d runs from %s/%s.')
                  % (len(data), name, summary))
        return data

    def _determineFormatstring(self, data):
        """为给定数据计算结构化 dtype（转发给模块级 _determine_formatstring）。

        Args:
            data (list[dict]): run 字典列表。

        Returns:
            np.dtype: 结构化 dtype。
        """
        return _determine_formatstring(data)

    def clear(self):
        """清空已收集的全部数据（incomplete/complete 列表与任务计数）。"""
        self.incompleteData = []
        self.lastupdate = time.time()
        self.completeData = []
        self.task = -1

    def run_forever(self):
        """循环调用 recvData（每 50 ms 一次）并轮询保存回执控制请求。

        控制请求走独立 REP socket、非阻塞 poll，不与数据 recv 抢道；独立
        Data Manager 服务进程没有 GUI 定时器，所以在这里同时处理回执请求。
        """
        try:
            start_ack_server(threaded=False)
        except Exception:  # noqa: BLE001 - 回执服务失败不影响收数据
            pass
        while True:
            time.sleep(0.05)
            self.recvData()
            poll_ack_requests(0)


class SingleDataSummary(DataSummary):
    """汇总单一数据通道：每个 run 存一个字典加一个 1D 数组块。

    Attributes:
        datastream (str|list): 订阅的数据流。
        arrays (list[np.ndarray|None]): 与每个 run 对应的数组块。
    """
    def __init__(self, datastream, name, props=None):
        """构造单通道数据汇总器。

        Args:
            datastream (str|list): 要订阅的数据流。
            name (str): 汇总器名字。
            props (Properties|None): 属性树实例。
        """
        self.datastream=datastream
        super().__init__(name, props)
        self.arrays = []
        self.streamlist = []
        # print("SINGLE DATA SUMMARY INITIALIZED")


    def initDataq(self):
        """用 DataClient 订阅 datastream（订阅变化时重置）。"""
        self.dataq = DataClient(self.name)
        if self.streamlist != self.datastream:
            send_msg(tr('[Datamanager Initialisation] SingleDataSummary: subscription reset'), 'info')
            self.dataq.unsubscribe()
            self.streamlist = self.datastream
            self.dataq.subscribe(self.streamlist)

    def processIncomingData(self, msg, newdata, A):
        """把每个 run 的字典与数组分别追加到 completeData 与 arrays。

        Args:
            msg (str): 通道/消息名。
            newdata (dict): 数据字典。
            A: 与数据配套的 1D 数组。
        """
        _pop_ack(newdata)   # 保存回执：记账后移除，不污染数据字典
        self.completeData.append(newdata)
        self.arrays.append(A)
        self.lastupdate = time.time()

    def savetoFile(self, filename, folder='folder', data=None, arrays=None, clear_after=True):
        """保存 1D 数组块与对应的 run 字典到 H5 文件。

        Args:
            filename: 目标 .h5 文件。
            folder (str): 文件内组名。
            data (list[dict]|None): run 字典快照；None 用 self.completeData。
            arrays (list|None): 数组块快照；None 用 self.arrays。
            clear_after (bool): 保存后是否清空。

        Returns:
            bool: 是否全部保存成功。
        """
        if data is None:
            data = self.completeData
        if arrays is None:
            arrays = self.arrays
        success = _write_single_data(filename, folder, self.name, data, arrays,
                                     props=self.props)
        if clear_after:
            self.clear()
        return success

    def clear(self):
        """清空全部数据：调用父类 clear 并清空 arrays 数组块。"""
        super().clear()
        self.arrays = []
        self.lastupdate = time.time()

    def _check_datablocks(self, arrays=None):
        """把数组块规整为等形状的 2D/3D 单一块并返回（用于调试/预览）。

        Args:
            arrays (list|None): 数组块；None 用 self.arrays。

        Returns:
            np.ndarray: 规整后的数组；无数据时返回 shape (0,) 的空数组。
        """
        if arrays is None:
            arrays = self.arrays
        if len(arrays) == 0:
            return np.zeros((0,))
        arrays = [np.asarray(a) for a in arrays]
        arrays = [a.reshape(-1, 1) if a.ndim == 1 else a for a in arrays]
        shapes = np.array([a.shape for a in arrays])
        newshape = tuple(int(max(i)) for i in shapes.T)
        alldata = np.zeros((len(arrays), *newshape))
        for n, a in enumerate(arrays):
            alldata[n, :a.shape[0], :a.shape[1]] = a
        return alldata


class SingleImageSummary(SingleDataSummary):
    """汇总图像数据流：每个 run 存图像数组、坐标轴与元数据字典。

    Attributes:
        axis (list[tuple]): 每张图对应的 (x轴, y轴)。
    """
    def __init__(self, datastream, name, props=None):
        """构造图像数据汇总器。

        Args:
            datastream (str|list): 要订阅的图像流。
            name (str): 汇总器名字。
            props (Properties|None): 属性树实例。
        """
        super().__init__(datastream, name, props)
        self.axis = []

        # print("SINGLE IMAGE SUMMARY INITIALIZED: {}".format(name))

    def initDataq(self):
        """用 ImageClient 订阅图像流（本类存在的意义：用图像客户端收大块数据）。"""
        self.dataq = ImageClient(self.name) #this is the reason for this class
        self.dataq.unsubscribe()
        self.streamlist = self.datastream
        self.dataq.subscribe(self.streamlist)

    def savetoFile(self, filename, folder, data=None, arrays=None, axis=None, clear_after=True):
        """保存图像、坐标轴与 run 字典到 H5 文件。

        Args:
            filename: 目标 .h5 文件。
            folder (str): 文件内组名。
            data (list[dict]|None): run 字典快照；None 用 self.completeData。
            arrays (list|None): 图像快照；None 用 self.arrays。
            axis (list|None): 坐标轴快照；None 用 self.axis。
            clear_after (bool): 保存后是否清空。

        Returns:
            bool: 是否全部保存成功。
        """
        if arrays is None:
            arrays = self.arrays
        if axis is None:
            axis = self.axis
        if data is None:
            data = self.completeData
        success = _write_images(filename, folder, self.name, arrays, axis,
                                props=self.props)
        success &= _write_data_summary(filename, folder, self.name, data,
                                       props=self.props)
        if clear_after:
            self.clear()
        return success

    def computeImageAxis(self, newdata):
        """由图像元数据计算正确的 x/y 坐标轴。

        Args:
            newdata (dict): 消息头，含 shape/_offset/_imgresolution 等元数据。

        Returns:
            list[np.ndarray]: [x轴, y轴]。
        """
        # get image metadata from message head
        shape = newdata['shape']
        offset = newdata['_offset']
        res = newdata['_imgresolution']

        # calculate x and y axis
        axis = [(np.arange(0, shape[i]) + offset[i]) * res[i] for i in range(2)]

        return axis

    def processIncomingData(self, msg, newdata, A):
        """处理图像帧：计算坐标轴存入 self.axis 后交给父类归并。

        元数据不全时记录错误，仍尝试把数据交给父类。

        Args:
            msg (str): 通道/消息名。
            newdata (dict): 图像头部元数据。
            A (np.ndarray): 图像数组。
        """
        _pop_ack(newdata)   # 保存回执：记账后移除（父类会再把数据字典入账）
        try:
            axis = self.computeImageAxis(newdata)
            self.axis.append(axis)

        except Exception:  # noqa: BLE001 - 元数据不全：记健康状态后继续尝试保存
            message = tr("[Data Manager] Incomplete image metadata!")
            _note_save_error(message, props=getattr(self, 'props', None))
            send_msg(message, 'error')
        SingleDataSummary.processIncomingData(self, msg, newdata, A)


def main(name):
    """启动数据管理器：创建 DataSummary 并阻塞式持续收数据。

    Args:
        name (str): 数据汇总器的名字（也用作属性命名空间）。
    """
    # 安全：ZMQ 无认证/无加密；若 Data Hub 端点被配置到局域网，启动时警告。
    warn_hub_if_exposed('Data Hub', 'Data Manager')
    set_manager_alive(True)
    try:
        mgr = DataSummary(name)
        mgr.run_forever()
    finally:
        # 退出 / 致命错误（run_forever 抛出）都标 False，任务侧预检才能发现
        # “Data Manager 已不在”；发布失败只影响可观测性。
        set_manager_alive(False)


## Start Qt event loop unless running in interactive mode or using pyside.
if __name__ == '__main__':
    import sys
#    if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
#        QtGui.QApplication.instance().exec()
    main('datamgr')
