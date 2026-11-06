"""ZeroMQ XSUB/XPUB proxy used by the message hubs."""

import zmq
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

from quatm import *
import argparse
import logging
from quatm.servers.configreader import ConfigReader, warn_if_exposed
from quatm.servers import zmq_security
import threading
from zmq.utils.monitor import recv_monitor_message
from quatm.servers import send_info, send_error
from quatm.utils.i18n import tr

_log = logging.getLogger('quatm.servers.xsub_xpub')

# 握手失败事件码：正常开关窗口/断开时机也会触发（如端口被 TCP 探测），
# 不能当作真实错误；只记日志，绝不进消息流。
_HANDSHAKE_FAILURE_EVENTS = (
    getattr(zmq, 'EVENT_HANDSHAKE_FAILED_NO_DETAIL', -1),
    getattr(zmq, 'EVENT_HANDSHAKE_FAILED_AUTH', -1),
    getattr(zmq, 'EVENT_HANDSHAKE_FAILED_PROTOCOL', -1),
)

EVENT_MAP = {}
# print("Event names:")
for name in dir(zmq):
    if name.startswith('EVENT_'):
        value = getattr(zmq, name)
        # print("%21s : %4i" % (name, value))
        EVENT_MAP[value] = name


def run_server(name):
    """启动一个通用的 XSUB/XPUB 转发服务器（常驻）。

    按 configfile Servers 段里 ``name`` 的 pub/sub 端点 bind 两个 socket，
    起事件监视线程后用 ``zmq.proxy`` 永久转发；绑定失败（端口被占）发
    可操作提示并以 SystemExit(1) 退出。

    Args:
        name (str): 服务器名，如 ``Servers/Property Hub``（取最后一段）。
    """
    conf = ConfigReader.getConfiguration()
    name = name.split('/')[-1]
    pubbinding = conf['Servers'][name]['pub']
    subbinding = conf['Servers'][name]['sub']
    # 安全：ZMQ 无认证/无加密。端点若被配置成 0.0.0.0 / 局域网地址，
    # 启动时给出醒目警告（只提示，不改变绑定行为）。
    warn_if_exposed(subbinding, 'XSUB %s' % name)
    warn_if_exposed(pubbinding, 'XPUB %s' % name)
    send_info(f"[Pub/Sub] {name} starting on.")
    send_info(f"[Pub/Sub] {name} XSUB subbinding.")
    send_info(f"[Pub/Sub] {name} XPUB pubbinding.")
    context = zmq.Context()
    sock_sub = context.socket(zmq.XSUB)
    sock_pub = context.socket(zmq.XPUB)
    # 本机 ZMQ 通道默认 CURVE 加密：bind 端是 server role（必须在 bind 之前设置）
    zmq_security.server_configure(sock_sub, endpoint=subbinding)
    zmq_security.server_configure(sock_pub, endpoint=pubbinding)
    try:
        sock_sub.bind(subbinding)
        sock_pub.bind(pubbinding)
    except zmq.ZMQError as e:
        # Windows 上最常见的失败：上一个 quatm 实例未退出仍占用端口
        # （或端口被其他程序占用）。直接给出可操作提示而不是裸 traceback。
        send_error(tr('[Pub/Sub] %s bind failed: %s\n  sub=%s\n  pub=%s\nAnother '
                      'Quatm instance may still hold these ports - close all '
                      'Quatm processes (or reboot) and start again.')
                    % (name, e, subbinding, pubbinding))
        context.term()
        raise SystemExit(1)

    pub_mon = sock_pub.get_monitor_socket()
    _pub_stop = threading.Event()
    pub_mon_thread = threading.Thread(
        target=event_monitor, args=(pub_mon, name, 'PUB', _pub_stop), daemon=True)
    pub_mon_thread.start()
    sub_mon = sock_sub.get_monitor_socket()
    _sub_stop = threading.Event()
    sub_mon_thread = threading.Thread(
        target=event_monitor, args=(sub_mon, name, 'SUB', _sub_stop), daemon=True)
    sub_mon_thread.start()
    _mon_threads = [pub_mon_thread, sub_mon_thread]
    _mon_stops = [_pub_stop, _sub_stop]

    try:
        # run server forever
        zmq.proxy(sock_sub, sock_pub)
    finally:
        # Ctrl-C / 异常打断 proxy 后也要干净收尾：先停并 join monitor 线程
        # （绝不跨线程关闭仍被 poll 的 monitor socket），再关 socket/context。
        for ev in _mon_stops:
            try:
                ev.set()
            except Exception:  # noqa: BLE001
                pass
        for t in _mon_threads:
            try:
                t.join(timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
        for s in (sock_sub, sock_pub):
            try:
                if s is not None and not s.closed:
                    s.close(0)
            except Exception:  # noqa: BLE001
                pass
        try:
            context.term()
        except Exception:  # noqa: BLE001
            pass
        send_info(tr('[Pub/Sub] %s server stopped and cleaned up.') % name)


def event_monitor(monitor, name, pub_sub, stop_event=None):
    """套接字事件监视线程（只写日志，不刷消息流）。

    例行 connect/disconnect 与握手失败类事件在正常开关窗口/客户端快速退出
    时也会出现，若逐条发进 Message Hub 会刷屏；因此全部只走 logging：

    * 每个事件 DEBUG 记录（默认静默，可用 logging 打开）；
    * 断连事件 INFO 记录；
    * 握手失败类事件 WARNING 记录（lastResort 下会打到 stderr）。

    Args:
        monitor: zmq monitor socket。
        name (str): 服务器名（日志用）。
        pub_sub (str): 'PUB' 或 'SUB'（日志用）。
        stop_event (threading.Event|None): 可选。给出后 poll 带短超时并定期
            检查该标志，置位即退出——关闭方借此 join 本线程，避免“跨线程
            关闭正被 poll 的 monitor socket”触发 libzmq signaler 崩溃。
    """
    while not (stop_event is not None and stop_event.is_set()):
        if not monitor.poll(timeout=50):
            # 无事件：醒来检查 stop_event（若有）后继续
            continue
        mon_evt = recv_monitor_message(monitor)
        event = mon_evt.get('event')
        desc = EVENT_MAP.get(event, 'UNKNOWN_EVENT')
        _log.debug('[Pub/Sub] %s %s: %s', name, pub_sub, desc)
        if event == zmq.EVENT_DISCONNECTED:
            _log.info('[Pub/Sub] %s %s: peer disconnected', name, pub_sub)
        elif event in _HANDSHAKE_FAILURE_EVENTS:
            # 握手失败类事件在正常开关窗口、客户端快速退出、端口被 TCP
            # 探测/占用时也会触发。Task/Process Monitor 每次刷新都会探测
            # 端口，因此这里只保留 DEBUG，避免服务日志被可预期的探测噪声
            # 刷屏；真正的长时间断连仍由 DISCONNECTED + 业务错误体现。
            _log.debug('[Pub/Sub] %s %s: %s', name, pub_sub, desc)
        if event == zmq.EVENT_MONITOR_STOPPED:
            break
    try:
        monitor.close()
    except Exception:  # noqa: BLE001
        pass
    _log.debug('[Pub/Sub] %s %s: event monitor thread done', name, pub_sub)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('name', nargs=1, help='name of this program instance')
    args = parser.parse_args()
    name = args.name[0]
    run_server(name)
