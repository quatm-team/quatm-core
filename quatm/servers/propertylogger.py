"""Property logger service."""

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


from quatm.servers.properties import Properties, consume_pending_file
from quatm.servers import configreader  as cr
from quatm.servers import zmq_security
from quatm.servers import zmqcontext
import atexit
import signal
import time
import argparse
import json
import os
import threading
import zmq

# 有变更后停手多久就落盘（秒）。旧实现固定等 ``logginginterval[s]``（默认
# 30 s），于是“改完属性就停服务/重启/关机”必然丢掉这一段；现在停手 1.5 s
# 就写。写整棵树（~0.9 MB）在 SSD 上只要几毫秒，值得。
DEBOUNCE_S = 1.5
# 变更连续不断时的兑底上限（秒）：防抖永远触发不了，靠它保证最多丢这么久。
# 实际取 ``min(logginginterval[s], MAX_LATENCY_S)``——用户配的间隔仍是上限，
# 不会写得比用户想要的还勤。
MAX_LATENCY_S = 10.0


def _write_tree(snapshot, filename):
    """把属性树原子写入盘文件（临时文件 + flush/fsync + 原子替换）。

    Args:
        snapshot (dict): 要写入的整棵属性树。
        filename (str): 目标文件路径。

    Raises:
        OSError: 写文件或替换失败（调用方决定重试/保留快照）。
    """
    tmp = filename + '.%d.tmp' % os.getpid()
    with open(tmp, 'w', encoding='utf-8') as outfile:
        json.dump(snapshot, outfile, indent=4)
        outfile.flush()
        os.fsync(outfile.fileno())
    # Windows 上 os.replace 会因杀软/索引/OneDrive 短暂占用目标文件而报
    # Access denied (WinError 5)——短暂重试几次
    last_err = None
    for attempt in range(6):
        try:
            os.replace(tmp, filename)
            last_err = None
            break
        except OSError as e:
            last_err = e
            time.sleep(0.2 * (attempt + 1))
    if last_err is not None:
        raise last_err

def run_propertylogger(name):
    """运行 PropertyLogger 服务器（属性树落盘 + INIT/FLUSH 应答）。

    主循环：绑定 configfile 中 "Property Logger" 的 rep 端点；收到 ``INIT?``
    时返回序列化的整棵属性树（带缓存，树变化才重建）；``FLUSH?`` 表示“立刻把
    当前属性树写盘”（Property Editor 的 Save now 与 ProcessManager 停服前
    都会发），应答 ``OK``/``ERR``；未知消息回 ``ERR`` 保持 REP 状态机干净，
    畸形帧（recv 异常）时重建 REP socket 继续服务。

    **落盘时机**（旧实现只有“每 logginginterval[s] 且树有变更”这一条，默认
    30 s，导致“改完属性就停服务/重启/关机”必丢这一段）：

    * 有变更后停手 :data:`DEBOUNCE_S` 秒 → 写；
    * 变更连续不断时最多等 :data:`MAX_LATENCY_S`（且不超过用户配的
      ``logginginterval[s]``）→ 写；
    * 收到 ``FLUSH?`` → 同步写（对方要的是“确实已经写好了”）；
    * 收到 SIGTERM/SIGINT（Linux/macOS 上 ProcessManager 的停止、Ctrl-C）→
      退出前最后写一次；解释器正常退出走 ``atexit`` 同一路径。Windows 的
      ``TerminateProcess`` 无法捕获，靠上面的防抖把丢失窗口压到 ~2 s。

    启动时还会合并 :func:`quatm.servers.properties.consume_pending_file`：
    Propertylogger 没在跑时客户端把改动记在本地 pending 文件里，这里读回来
    写进属性树（**成功落盘后才删**，所以中途崩了也不会丢），于是“服务没开时
    改的属性”也不会丢。

    写盘在独立线程执行以免阻塞 REP 应答；上一轮写盘未完成时不重新起线程
    （留待下轮），写失败也不退出进程、保留快照待重试。绑定失败（端口被占）
    即退出。

    Args:
        name (str): 进程/属性名（Properties 实例名）。

    Note:
        常驻运行，由 ProcessManager 作为服务器拉起；Ctrl-C 结束。
    """
    conf=cr.Config()
    p=Properties(name,initfromfile=True)
    rep_socket = zmqcontext.socket(zmq.REP)
    #rep_socket.RCVTIMEO = p.get('reptimeout',100)
    # 向后兼容旧 configfile 的无空格键名（Propertylogger）
    rep_endpoint = cr.server_endpoints(conf, 'Property Logger')['rep']
    # 安全：ZMQ 无认证/无加密；非回环 REP 端点（0.0.0.0/LAN）启动时醒目警告。
    cr.warn_if_exposed(rep_endpoint, 'Property Logger (rep)')
    # 本机 ZMQ 通道默认 CURVE 加密：REP bind 端是 server role
    zmq_security.server_configure(rep_socket, endpoint=rep_endpoint)
    try:
        rep_socket.bind(rep_endpoint)
    except zmq.ZMQError as e:
        # Windows 上最常见的失败：旧实例仍占用端口
        print('[Property Logger] bind failed: %s\n'
              'endpoint: %s\n'
              '另一个 quatm 实例可能仍占用该端口——请先关闭所有 quatm 进程'
              '（或重启机器）再启动。'
              % (e, rep_endpoint))
        raise SystemExit(1)
    lasttime=0
    _write_running = threading.Event()
    # 写盘失败时保留快照，下个周期继续重试（旧实现先清 recent_changes，
    # 一旦失败这批属性更新就静默丢失）
    _pending_snapshot = None
    # 缓存 INIT? 应答的序列化 JSON：大量进程同时启动时（每个子进程都来
    # 握手一次），每次都 deepcopy + json.dumps 整棵树会非常慢。树变化时
    # 才重建。
    _init_payload = None
    # 上次“树发生变化”的时刻（防抖用）、上一轮看到的变更集合与退出标志
    _last_change = 0.0
    _seen_changes = set()
    _stop = threading.Event()

    def _write_now(snapshot=None):
        """同步写一次盘（``FLUSH?``、SIGTERM/atexit 退出路径共用）。

        Args:
            snapshot (dict|None): 要写的树；None 表示现取一份。

        Returns:
            bool: 写成功为 True（失败时保留快照待重试）。
        """
        nonlocal _pending_snapshot, _init_payload, _last_change, lasttime
        if snapshot is None:
            snapshot = _pending_snapshot
            if snapshot is None:
                snapshot = p.get('/', {})
                p.recent_changes.clear()
        try:
            _write_tree(snapshot, cr.propertyfilename)
        except Exception as e:  # noqa: BLE001 - 写失败保留快照待重试
            _pending_snapshot = snapshot
            print('[Property Logger] write failed: %s (will retry)' % e)
            return False
        _pending_snapshot = None
        _init_payload = json.dumps(snapshot)
        lasttime = time.time()
        _last_change = 0.0
        # 树已落盘 → 客户端 pending 文件里的改动已经进树，可以删了
        consume_pending_file(clear=True)
        return True

    def _final_write(*_args):
        """退出前把尚未落盘的变更写一次（SIGTERM/SIGINT/atexit 共用，幂等）。"""
        if _stop.is_set():
            return
        _stop.set()
        if not (p.recent_changes or _pending_snapshot is not None):
            return
        if _write_now():
            print('[Property Logger] final write on exit: %s'
                  % cr.propertyfilename)

    atexit.register(_final_write)
    for _sig in (getattr(signal, 'SIGTERM', None),
                 getattr(signal, 'SIGINT', None)):
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _final_write)
        except (ValueError, OSError, RuntimeError):
            # 非主线程/不支持的平台：忽略（还有 atexit 与防抖兑底）
            pass

    # ---- 启动时合并“服务没跑期间的改动”（客户端 pending 文件）---------
    _pending_restored = consume_pending_file()
    if _pending_restored:
        _applied = 0
        for _key, _value in _pending_restored.items():
            try:
                p.set('/' + str(_key).lstrip('/'), _value)
                _applied += 1
            except Exception as e:  # noqa: BLE001 - 单条坏了不影响其它
                print('[Property Logger] pending key %r failed: %s'
                      % (_key, e))
        print('[Property Logger] merged %d pending property change(s) from '
              'properties_pending.json' % _applied)
        _last_change = time.time()
    _configured = 30.0          # logginginterval[s] 缓存（见循环内说明）
    while not _stop.is_set():
        #handle requests
        # 旧实现在这里无条件 ``time.sleep(0.01)``：即使请求已在排队，也先睡
        # 10 ms 才处理，等于给每一次 INIT? 握手固定加 10 ms 延迟（Browser
        # 启动时十几个属性客户端串行握手，白白多花 ~0.2 s，还没算序列化
        # 整棵树的时间）。改成只在轮询到空时才退避。
        _has_request = rep_socket.poll(1) == zmq.POLLIN
        if not _has_request:
            time.sleep(0.005)
        # 树有变化 → INIT 缓存失效（否则客户端会拿到陈旧树，新写入的
        # 属性直到下一轮写盘后才可见）。
        # 防抖时刻只能在**真的来了新变更**时刷新：旧写法在“还有未落盘变更”
        # 期间每轮都刷 _last_change（循环 ~5 ms 一次），导致防抖永远不触发，
        # 只能靠 max_latency 兜底（实测单次改动要等 10 s 才落盘）。
        if p.recent_changes:
            _init_payload = None
            if p.recent_changes != _seen_changes:
                _seen_changes = set(p.recent_changes)
                _last_change = time.time()
                # 只在“真的来了新变更”时重读配置
                try:
                    _configured = float(p.get('logginginterval[s]', 30) or 30)
                except (TypeError, ValueError):
                    _configured = 30.0
        elif _seen_changes:
            # 变更已被快照/写盘消费掉 → 重置“已见”集合，下次有新变更才算
            _seen_changes = set()
        if _has_request:
            try:
                message = rep_socket.recv_string()
            except zmq.ZMQError as e:
                # 畸形帧/客户端中途断开会把 REP 状态机带入 EFSM：重建
                # REP socket 继续服务，而不是让整个进程崩溃
                print('[Property Logger] recv failed (%s); rebuilding REP '
                      'socket' % e)
                try:
                    rep_socket.close(0)
                except Exception:
                    pass
                rep_socket = zmqcontext.socket(zmq.REP)
                # 重连后的 socket 同样要配置 CURVE（server role）
                zmq_security.server_configure(rep_socket, endpoint=rep_endpoint)
                try:
                    rep_socket.bind(rep_endpoint)
                except zmq.ZMQError:
                    pass
                message = None
            if message == 'INIT?':
                #print('propertylogger.py: someone wants data',message)
                if _init_payload is None:
                    _init_payload = json.dumps(p.get('/', {}))
                try:
                    rep_socket.send_string('INIT?', zmq.SNDMORE)
                    rep_socket.send_string(_init_payload)
                except zmq.ZMQError as e:
                    print('[Property Logger] send failed: %s' % e)
            elif message == 'FLUSH?':
                # 立刻落盘（Property Editor 的 Save now、ProcessManager 停服
                # 之前都发这条）——同步写，因为对方要的是“确实写好了”
                _ok = _write_now()
                try:
                    rep_socket.send_string('OK' if _ok else 'ERR')
                except zmq.ZMQError as e:
                    print('[Property Logger] send failed: %s' % e)
            elif message == 'PING?':
                # 轻量心跳：客户端（Properties.check 线程）用它判断服务是否
                # 还活着，从而决定要不要把改动记进本地 pending 文件。
                # 不复用 INIT?——那会序列化整棵树回来，做心跳太重。
                try:
                    rep_socket.send_string('PONG')
                except zmq.ZMQError as e:
                    print('[Property Logger] send failed: %s' % e)
            elif message is not None:
                # 未知消息也回一个应答，保持 REP 状态机干净（不回包会让
                # 客户端下一次 send 触发 EFSM）
                print('propertylogger.py unknown message: ', message)
                try:
                    rep_socket.send_string('ERR')
                except zmq.ZMQError as e:
                    print('[Property Logger] send failed: %s' % e)

        #do logging
        # 落盘时机：① 停手 DEBOUNCE_S 秒（旧实现要等 logginginterval[s]，默认
        # 30 s，改完就停服务/关机必丢）；② 变更连续不断时最多等 max_latency
        # （用户配的 logginginterval[s] 仍是上限，不会写得比他要的还勤）。
        # _configured 只在真有新变更时重读（见上面的 _seen_changes 分支）：
        # 旧写法每轮循环都 get 一次，而本循环约 5 ms/轮 → 空闲时每秒约 200 次
        # 加锁 + 整树 deepcopy，纯属空转。
        _max_latency = max(0.5, min(_configured, MAX_LATENCY_S))
        _debounce = min(DEBOUNCE_S, _max_latency)
        if not p.recent_changes and _pending_snapshot is None:
            continue
        _now = time.time()
        if (_now - _last_change < _debounce
                and _now - lasttime < _max_latency):
            continue
        # 上一轮写盘仍在进行：不清 recent_changes 也不再起新线程，
        # 留到下一轮重试——否则写盘期间新增的整段更新会被丢掉
        if _write_running.is_set():
            continue
        lasttime = _now
        # snapshot（deepcopy）与序列化都放在写盘线程里做，REP 请求循环
        # 不会被大树阻塞（否则期间的 INIT? 全部超时，客户端只能回退到
        # 读文件，更慢）
        def _write():
            nonlocal _init_payload, _pending_snapshot
            snapshot = _pending_snapshot
            try:
                if snapshot is None:
                    snapshot = p.get('/', {})
                    # 快照已捕获：只清掉已进入快照的部分；此刻之后的新
                    # 变更留给下一轮写盘（避免 p.get 与清空之间的更新丢失）
                    p.recent_changes.clear()
                _init_payload = json.dumps(snapshot)          # INIT? 用紧凑格式
                _write_tree(snapshot, cr.propertyfilename)
                _pending_snapshot = None
                # 树已落盘 → 客户端 pending 文件里的改动已经进树，可以删了
                consume_pending_file(clear=True)
            except Exception as e:
                # 写盘失败不退出进程（REP 仍应答）；保留快照，下个周期
                # 即使没有新变更也会重试，避免静默丢数据
                _pending_snapshot = snapshot
                print('[Property Logger] write failed: %s (will retry)' % e)
            finally:
                _write_running.clear()
        _write_running.set()
        threading.Thread(target=_write, daemon=True).start()

    # 主循环退出（SIGTERM/SIGINT）→ 最后写一次
    _final_write()


if __name__ == "__main__":
#    parser = argparse.ArgumentParser()
#    parser.add_argument('name', nargs=1, help='name of this program instance')
#    args = parser.parse_args()
#    name=args.name[0]
    name='Servers/ProperyLogger'
    run_propertylogger(name)

