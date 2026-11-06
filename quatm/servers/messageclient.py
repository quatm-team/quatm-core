"""Client-side message publishing and logging helpers."""

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


from quatm.servers.clients import GenericClient
from quatm.servers.configreader import ConfigReader, server_endpoints
from quatm.servers import zmqcontext
import zmq
import json
import threading
import datetime
import inspect

class MessageClient(GenericClient):
    """标准化的“消息流”客户端：把消息发到 Message Hub。

    一般不要直接用——请用 :func:`quatm.servers.send_info/send_warning/
    send_error/send_debug`。若直接构造，需提供流名。

    Args:
        name (str): 消息流名（发送时会在 channel 前出现）。
    """
    def __init__(self,name):
        """初始化消息客户端（连接在 :meth:`_connect` 中建立）。

        Args:
            name (str): 消息流名。
        """
        super().__init__(name)

    def _connect(self):
        """按配置连接 Message Hub 的 pub/sub 端点。"""
        conf = ConfigReader.getConfiguration()
        hub = server_endpoints(conf, 'Message Hub')
        self.pub_socket.connect(hub['sub'])
        self.sub_socket.connect(hub['pub'])


    def send(self, channel='info', message='', flags=0):    
        """把一条消息发到 Message Hub（PUB）。

        注意：PUB 在连接尚未建立时可能丢包。

        Args:
            channel (str): 子通道（如 info/Error/Warning）。
            message (str): 消息文本。
            flags (int): ZMQ 发送标志。
        """
        # guard the PUB socket: send_msg() is called from many threads
        # (GUI thread + every worker thread) and ZMQ sockets are not
        # thread-safe
        with self._send_lock:
            self.pub_socket.send_string(channel + ': ' + message, flags)


    def recv(self, flags=0, copy=True, track=False):
        """从消息流接收一条消息。

        Args:
            flags, copy, track: ZMQ 接收选项（一般用默认值即可）。

        Returns:
            tuple|None: ``(message_str, {})``；无数据(EAGAIN)时返回 None。
        """
        try:
            message = self.sub_socket.recv_string(flags=flags)
            return message, {}
        except zmq.ZMQError as e:
            print('error')
            if e.errno == zmq.EAGAIN:
                return None
            else:
                raise e

    def has_new_data(self):
        """非阻塞地判断是否有新消息可读。

        Returns:
            bool: 有可读消息时为 True。
        """
        # non-blocking check; see clients.py for why poll(1) is avoided
        return self.sub_socket.poll(0) == zmq.POLLIN

# 懒加载：import quatm.servers 不再要求 configfile 存在、也不在导入期
# 建立 ZMQ 连接。只有第一次真正发消息时才创建 MessageClient；初始化失败
# （如找不到配置文件）只打印一次诊断，之后消息静默丢弃，不拖垮调用方。
_mgsc = None
_mgsc_lock = threading.Lock()
_mgsc_broken = False
_mgsc_sent = 0  # 已发出的消息条数（诊断用，见 sent_count）


def sent_count():
    """返回本进程已成功发到 Message Hub 的消息条数。

    用途：Browser 启动诊断——把“某个阶段发了多少条消息”一起输出。消息
    发送本身不便宜（格式化 + PUB 发送，旧实现还要做文件系统访问），启动
    期的消息风暴会直接反映为开窗变慢。

    Returns:
        int: 自进程启动以来 send_msg 成功发出的条数。
    """
    return _mgsc_sent


def _get_mgsc():
    """懒加载全局 MessageClient 单例（带线程锁）。

    首次发消息才创建；初始化失败（如无 configfile）只报一次，之后返回 None
    （消息静默丢弃，不拖垮调用方）。

    Returns:
        MessageClient|None: 全局客户端实例。
    """
    global _mgsc, _mgsc_broken
    # 快路径：已建好（绝大多数情况）时完全不加锁
    client = _mgsc
    if client is not None:
        return client
    if _mgsc_broken:
        return None
    with _mgsc_lock:
        if _mgsc is None:
            try:
                _mgsc = MessageClient('testname')
            except Exception as e:
                _mgsc_broken = True
                print('quatm MessageClient 初始化失败，消息将只打印到本地: %s' % e)
        return _mgsc


def _source_name(frame):
    """从调用帧取源文件名（等价旧的 ``inspect.getmodule(frame).__file__`` 基名）。

    旧实现对每条消息都做 ``inspect.getmodule(frame)``：它会经 getsourcefile
    路径做 ``os.path.exists``/``stat``——在 Windows（尤其带实时防护扫描）上
    每次文件系统调用是毫秒级，而启动恢复/实时运行期间消息数量可观（实测
    每条消息 2 次文件系统调用）。直接取调用帧的 ``co_filename`` 基名，语义
    相同（都是“发出这条消息的代码所在文件”）却零文件系统访问。

    Args:
        frame: 调用帧（应已由 :func:`_source_frame` 跳过包装层）。

    Returns:
        str: 源文件名；取不到时为 ``'<unknown>'``。
    """
    try:
        name = frame.f_code.co_filename.replace('\\', '/').rsplit('/', 1)[-1]
        return name or '<unknown>'
    except Exception:  # noqa: BLE001 - 诊断信息绝不拖垮调用方
        return '<unknown>'


# 发送时需跳过的“包装层”模块名：``send_error/send_warning/send_info/send_debug``
# 本身就定义在 messageclient 里（并被 re-export 到 ``quatm.servers``），因此
# ``sys._getframe(1)`` 拿到的是本文件——上报的 source 于是永远是
# ``'messageclient.py'``（用户报告：日志里 2102 条消息的 source 全是它）。
# 真正的来源应该是**调用 send_* 的那个脚本/模块**。
_WRAPPER_MODULES = ('quatm.servers.messageclient', 'quatm.servers')
_MAX_FRAME_HOPS = 8


def _source_frame(frame, hops=_MAX_FRAME_HOPS):
    """向上找到“真正的调用者”帧（跳过本模块/包的包装函数）。

    Args:
        frame: 起始帧（``send_msg`` 里的 ``sys._getframe(1)``）。
        hops (int): 最多向上跳几层，避免病态调用链拖慢发送。

    Returns:
        帧对象: 第一个不属于包装模块的帧；找不到时返回起始帧。
    """
    for _ in range(hops):
        try:
            name = frame.f_globals.get('__name__')
        except Exception:  # noqa: BLE001 - 取不到就当找到
            return frame
        if name not in _WRAPPER_MODULES:
            return frame
        nxt = frame.f_back
        if nxt is None:
            break
        frame = nxt
    return frame


def send_msg(msg, errlevel, extra=None):
    """把带时间戳/来源文件的消息发到 Message Hub。

    线格式：``'<Level>: <时间戳>$<来源文件>$<内容>[$<JSON 附加字段>]'``。
    第四段是**可选**的结构化附加字段（如 ``{'device': 'dds_0'}``）：不传时
    格式与历史完全一致，老监视器照旧能用；传的时候旧解析器只会把它当内容
    的尾巴显示，不会报错。监视器侧用
    :func:`quatm.servers.messagestore.MessageRow.parse` 解析（第四段不是合法
    JSON 时会当成内容里的 ``$``，不会误判）。

    Args:
        msg (str): 消息内容。
        errlevel (str): 级别（Error/Warning/Info/Debug）。
        extra (dict|None): 可选的结构化附加字段。
    """
    global _mgsc_sent
    # 先看 hub 是否可用：旧实现即使 hub 不可用（或本进程从未连上），也会先
    # 做 inspect.getmodule + 时间戳格式化再丢弃——纯浪费，且带文件系统访问
    client = _get_mgsc()
    if client is None:
        return
    # 性能：inspect.stack() 会遍历整个调用栈（实测 ~0.1ms/次），
    # sys._getframe(1) 直接取上一帧，几乎零开销；再向上跳过 messageclient/
    # quatm.servers 这层包装，才能真正定位到发消息的脚本。
    frame = _source_frame(sys._getframe(1))
    filename = _source_name(frame)
    # 带毫秒：同一秒内会有多条消息（旧实现只有秒，监视器里时间戳末尾永远是
    # '.000'，无法区分先后），MessageRow.parse 对旧格式仍兼容。
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    payload = timestamp + '$' + filename + '$' + msg
    if extra:
        try:
            payload += '$' + json.dumps(extra, ensure_ascii=False,
                                        separators=(',', ':'))
        except Exception:  # noqa: BLE001 - 附加字段不可序列化就不带它
            pass
    client.send(errlevel, payload)
    _mgsc_sent += 1

def send_error(msg, extra=None):
    """发送 Error 级消息：进入消息流并触发错误通知。

    Args:
        msg (str): 要发送的消息文本。
        extra (dict|None): 可选的结构化附加字段（见 :func:`send_msg`）。
    """
    send_msg(msg, 'Error', extra)

def send_warning(msg, extra=None):
    """发送 Warning 级消息：进入消息流并触发警告通知。

    Args:
        msg (str): 要发送的消息文本。
        extra (dict|None): 可选的结构化附加字段（见 :func:`send_msg`）。
    """
    send_msg(msg, 'Warning', extra)

def send_info(msg, extra=None):
    """发送 Info 级消息：进入消息流作为普通通知。

    Args:
        msg (str): 要发送的消息文本。
        extra (dict|None): 可选的结构化附加字段（见 :func:`send_msg`）。
    """
    send_msg(msg, 'Info', extra)

def send_debug(msg, extra=None):
    """发送 Debug 级消息：仅在调试通道中显示。

    Args:
        msg (str): 要发送的消息文本。
        extra (dict|None): 可选的结构化附加字段（见 :func:`send_msg`）。
    """
    send_msg(msg, 'Debug', extra)