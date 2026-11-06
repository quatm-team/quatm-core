"""
clients —— 统一的进程间通信（IPC）标准接口。

方便地处理与数据通道的交互。目前有四类客户端：

* **CommandClient** —— 用于控制驱动/进程（命令通道）；
* **DataClient**    —— 用于小数据（1D 曲线等）；
* **ImageClient**   —— 用于大数据块（主要是 2D 图像）；
* **MessageClient** —— 用于 error/warning/info/debug 消息。

例：从某个流接收图像::

    from quatm.servers import ImageClient
    imageq = ImageClient(name)          # name 只在发送数据时有意义
    imageq.subscribe('teststream')
    msg = imageq.recv()
    if msg is not None:
        msgstr, dict_info, array_data = msg

例：向某个流发送数据::

    from quatm.servers import DataClient
    dataq = DataClient('teststream')
    dataq.send(info_dictionary, data_array, 'substream1_')
"""

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


from quatm.servers import zmqcontext
from quatm.servers import zmq_security
import zmq


def _zmq_opt(name, default):
    """读一个 ZMQ socket 选项的取值（环境变量可覆盖，非法值退回默认）。

    Args:
        name (str): 环境变量名。
        default (int): 默认值。

    Returns:
        int: 生效值。
    """
    try:
        return int(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        return int(default)
import numpy
import json
import threading
from quatm.servers.configreader import ConfigReader, server_endpoints
import numpy as np


# ---------------------------------------------------------------------------
# 保存回执（save ack）上下文：给“本任务/本 run”发出的数据消息挂上
# ``datadict['_ack'] = {'token': ..., 'seq': ...}``，供 Data Manager 统计
# “收到了哪些消息”，从而在任务/run 标 Done 前确认数据确实被处理。
#
# 为什么放在模块级而不是每个 DataClient 实例上：实验脚本通常自建
# ``DataClient(self.name)``（每个实验一个），Manager 拿不到全部实例；模块级
# 上下文能让**本进程内所有客户端**在同一 token 下共用一条递增 seq。
# 旧 Data Manager 只合并非 ``_`` 开头的键（数据消息里的 ``_ack`` 被忽略），
# 标记消息里多出的 ``_ack`` 会被当不支持字段丢弃，因此保持向后兼容。
# ---------------------------------------------------------------------------
_ACK_CONTEXT = {'token': None, 'seq': 0}
_ACK_LOCK = threading.Lock()


def set_ack_context(token):
    """设置/清除本进程数据消息的保存回执上下文。

    Args:
        token (str|None): 任务 token；None/空串表示停止给消息挂 ``_ack``。

    Returns:
        int: 设置后的起始序号（清空时为 0）。
    """
    with _ACK_LOCK:
        _ACK_CONTEXT['token'] = str(token) if token else None
        _ACK_CONTEXT['seq'] = 0
        return 0


def current_ack_context():
    """返回当前保存回执上下文副本 ``{'token': str|None, 'seq': int}``。"""
    with _ACK_LOCK:
        return dict(_ACK_CONTEXT)


def ack_context_seq():
    """返回当前上下文里已发出的、带 ack 标记的消息条数（seq 最大值）。

    Returns:
        int: 已发送条数（无上下文时 0）。
    """
    with _ACK_LOCK:
        return int(_ACK_CONTEXT['seq'])


def _ack_stamp():
    """为下一条数据消息生成 ``{'token','seq'}`` 标记（无上下文时返回 None）。

    Returns:
        dict|None: ack 标记；不受 ack 跟踪时返回 None。
    """
    with _ACK_LOCK:
        token = _ACK_CONTEXT['token']
        if not token:
            return None
        _ACK_CONTEXT['seq'] += 1
        return {'token': token, 'seq': int(_ACK_CONTEXT['seq'])}


def _drain_multipart(socket, frames_read=0):
    """读完当前多帧消息的剩余帧（尽力而为），保证下一次 recv 从新消息开始。

    ZMQ 的订阅流以「消息」为单位，一条消息由若干帧组成：``recv_string()``
    只取走第一帧。若之后解码/校验失败就直接返回，dict/数组帧仍留在 socket
    里，下一次 ``recv()`` 会把数组帧当成主题 —— 该订阅流从此永久错位。
    这个辅助用 ``RCVMORE`` 把当前消息读空；异常一律吞掉（排空只是尽力而为，
    常驻线程不能因为坏帧或对端断开而退出）。

    Args:
        socket: 已接收过当前消息至少一帧的 ZMQ socket（测试可传等价的 Fake）。
        frames_read (int): 当前消息已读帧数（仅用于返回值记账）。

    Returns:
        int: 当前消息一共读过的帧数（``frames_read`` + 本次丢弃的帧数）。
    """
    total = frames_read
    try:
        while socket.getsockopt(zmq.RCVMORE):
            socket.recv(copy=False)
            total += 1
    except Exception:  # noqa: BLE001 - 排空失败也必须让调用方安全返回
        pass
    return total


class NpEncoder(json.JSONEncoder):
    """支持 NumPy 类型序列化的 JSONEncoder。

    处理默认 JSONEncoder 无法序列化的 numpy 类型：numpy.integer→int、
    numpy.floating→float、numpy.ndarray→list；其余交给父类。

    Example::

        import numpy as np, json
        json.dumps({'a': np.int64(1), 'c': np.array([1, 2, 3])},
                   cls=NpEncoder)
    """
    def default(self, obj):
        """把 numpy 标量/数组转成可 JSON 序列化的 Python 类型。

        Args:
            obj: 待序列化对象。

        Returns:
            int|float|list|object: 转换结果。
        """
        if isinstance(obj, numpy.integer):
            return int(obj)
        if isinstance(obj, numpy.floating):
            return float(obj)
        if isinstance(obj, numpy.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)

class GenericClient():
    """通用 ZMQ 客户端：标准化的收发数据接口。

    连接目标由中央配置决定（Data/Image/Command Hub）。适用于小数据（<10k），
    大数据（图像）请用 ImageClient。

    Args:
        name (str): 发送数据时使用的名字（数据发布在以此为前缀的通道下）。
        recvtimeout (int): 接收超时毫秒数。
        subscribe (str|list|None): 可选，构造后立即订阅的通道。
    """
    #: 该客户端的消息是否进入 Data Manager 订阅的"数据面"（Data Hub）。
    #: 只有数据面的消息才消耗保存回执 seq：Command Hub/Image Hub 的消息
    #: Data Manager 根本收不到，若也计入 seq 会出现永久缺失空洞，导致
    #: ``save_ack`` 永远失败（真机 CommandAttr 路径即如此）。子类可覆盖。
    ACK_TRACKED = True

    def __init__(self, name='noname', recvtimeout=1000, subscribe=None):
        """按配置连接相应 Hub 的 pub/sub socket。

        Args:
            name (str): 发送时使用的名字。
            recvtimeout (int): 接收超时（毫秒）。
            subscribe (str|list|None): 可选初始订阅。
        """
        self.name = name
        self.subscriptions = []
        # ZMQ sockets are not thread-safe: guard the PUB socket so concurrent
        # send() calls from different threads cannot corrupt it. MessageClient
        # and ImageClient inherit this lock and use it for their own send().
        self._send_lock = threading.Lock()
        # close() 标志：置位后 send/recv/subscribe 等一律安全返回，不访问
        # 已置 None 的 socket（避免 AttributeError / 对已关闭 socket 的调用）。
        self._closed = False
        # subscribe/unsubscribe 共享 sub_socket 的 setsockopt，用独立锁串行化。
        self._sub_lock = threading.Lock()
        # initialize sockets
        self.pub_socket = zmqcontext.socket(zmq.PUB)
        self.sub_socket = zmqcontext.socket(zmq.SUB)
        # 本机 ZMQ 通道默认 CURVE 加密：connect 端是 client role
        #（bind 端由对应 Hub 服务器配置 server role，两端必须成对）。
        zmq_security.client_configure(self.pub_socket)
        zmq_security.client_configure(self.sub_socket)
        self.sub_socket.RCVTIMEO = recvtimeout
        # 背压：ZMQ 的 HWM=0 就是“无上限”——慢订阅者（图像面板、Web bridge、
        # 各种 logger）会让发布端/中继端的队列无界增长，常驻进程跑数周就是
        # OOM；而 100 MB 的 SND/RCVBUF 在 Windows 上还会额外占内核缓冲。
        # 这里给有限值（窗口很大，正常负载根本到不了），可用环境变量覆盖。
        self.pub_socket.setsockopt(
            zmq.SNDBUF, _zmq_opt('QUATM_ZMQ_SNDBUF', 4 * 1024 * 1024))
        self.pub_socket.setsockopt(
            zmq.SNDHWM, _zmq_opt('QUATM_ZMQ_SNDHWM', 100000))
        self.sub_socket.setsockopt(
            zmq.RCVBUF, _zmq_opt('QUATM_ZMQ_RCVBUF', 4 * 1024 * 1024))
        self.sub_socket.setsockopt(
            zmq.RCVHWM, _zmq_opt('QUATM_ZMQ_RCVHWM', 100000))
        self._connect()
        if subscribe is not None:
            self.subscribe(subscribe)

    def unsubscribe(self, channel=None):
        """取消订阅指定通道。

        Args:
            channel (str|None): 要取消订阅的通道；若为 None 则取消全部订阅。
        """
        if self._closed or self.sub_socket is None:
            return
        with self._sub_lock:
            if channel is None:
                for entry in list(self.subscriptions):
                    try:
                        self.sub_socket.setsockopt_string(zmq.UNSUBSCRIBE, entry)
                    except zmq.ZMQError:  # noqa: BLE001
                        pass
                self.subscriptions = []
            else:
                try:
                    self.sub_socket.setsockopt_string(zmq.UNSUBSCRIBE, channel)
                except zmq.ZMQError:  # noqa: BLE001
                    pass
                if channel in self.subscriptions:
                    self.subscriptions.remove(channel)

    def subscribe(self, channel):
        """订阅一个通道（可传单个字符串或字符串列表）。

        Args:
            channel (str|list[str]): 要订阅的通道名，或一组通道名。
        """
        if self._closed or self.sub_socket is None:
            return
        with self._sub_lock:
            if type(channel) == str:
                if channel not in self.subscriptions:
                    self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, channel)
                    self.subscriptions.append(channel)
            elif type(channel) == list:
                for ch in channel:
                    if ch not in self.subscriptions:
                        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, ch)
                        self.subscriptions.append(ch)

    def send(self, datadict, A=None, channel='', flags=0, copy=True, track=False, prefix=None):
        """通过 ZMQ 发布数据（可附带一个 numpy 数组）。

        Args:
            datadict (dict): 要发送的数据（须可 JSON 序列化）。
            A (np.array|None): 可选的附加数据数组；给出时会随消息附带其 dtype/shape。
            channel (str): 子通道名，发送时会拼接到 self.name 之后。
            flags (int): ZMQ 发送标志，参见 ZMQ 文档。
            copy (bool): 是否复制发送数据。
            track (bool): 是否跟踪消息发送完成。
            prefix (str|None): 若给定，改用 prefix+'.'+channel 作为主题前缀。
        """
        if self._closed or self.pub_socket is None:
            return None
        # 保存回执：任务/run 上下文活跃时给消息挂 {'token','seq'}。只有活跃
        # 时才复制 dict（避免污染调用方复用的 head；无 ack 时保持旧行为）。
        # 只有数据面（Data Hub）消息消耗 seq——见 ACK_TRACKED 注释。
        stamp = _ack_stamp() if getattr(self, 'ACK_TRACKED', True) else None
        payload = datadict
        if stamp is not None and isinstance(datadict, dict):
            payload = dict(datadict)
            payload['_ack'] = stamp
        with self._send_lock:
            if prefix is None:
                self.pub_socket.send_string(self.name+channel,flags|zmq.SNDMORE)
            else:
                self.pub_socket.send_string(prefix+'.'+channel,flags|zmq.SNDMORE)

            if not isinstance(A, np.ndarray):

                return self.pub_socket.send_json(payload, flags, cls=NpEncoder)
            else:
                payload['dtype'] = str(A.dtype)
                payload['shape'] = A.shape
                self.pub_socket.send_json(payload, flags|zmq.SNDMORE, cls=NpEncoder)
                return self.pub_socket.send(A, flags, copy=copy, track=track)


    def recv(self, flags=0, copy=True, track=False):
        """接收一帧消息：返回 (主题, 字典) 或 (主题, 字典, numpy 数组)。

        超时（无新数据）时返回 None。

        Args:
            flags (int): ZMQ 接收标志。
            copy (bool): 是否复制接收数据。
            track (bool): 是否跟踪消息接收。

        Returns:
            (str, dict)|(str, dict, np.ndarray)|None: 视消息是否附带数组而定；超时返回 None。
        """
        if self._closed or self.sub_socket is None:
            return None
        try:
            message = self.sub_socket.recv_string(flags=flags)
            if self.sub_socket.getsockopt(zmq.RCVMORE):
                dict_ = self.sub_socket.recv_json(flags=flags)
                if not self.sub_socket.getsockopt(zmq.RCVMORE):
                    return message, dict_
                msg = self.sub_socket.recv(flags=flags, copy=copy, track=track)
                buf = memoryview(msg)
                A = numpy.frombuffer(buf, dtype=dict_['dtype'])
                return message,dict_, A.reshape(dict_['shape'])
        except (zmq.ZMQError, KeyError, ValueError, TypeError):
            # 超时 / 已关闭 / 对端发送了不完整或畸形帧：视为无数据返回。
            # 关键：先把本消息的剩余帧读空（主题帧已读走，frames_read=1），
            # 否则下一次 recv 会把数组帧当成主题，该订阅流永久错位。
            _drain_multipart(self.sub_socket, 1)
            return None

    def recv_topic_only(self, timeout_ms=None):
        """只取回消息主题并丢弃其余帧（供只关心“有没有数据”的订阅方使用）。

        典型用途：Image/Data Logger 只需要通道名与帧数，用 :meth:`recv`
        会把整帧图像（数 MB）也拷进本进程再丢掉。这里用 ``copy=False``
        读帧体，不产生 numpy 数组。

        顺便解决“空闲时永不醒”的问题：给了 ``timeout_ms`` 后，即使完全
        无数据，调用方也会周期性返回 ``(None, 0)``，常驻服务因此能按时
        做超时清理（旧实现无数据就永远阻塞在 recv 里）。

        Args:
            timeout_ms (int|None): 临时设置的接收超时 [ms]；None 时用 socket
                当前设置（构造时给的 recvtimeout）。

        Returns:
            tuple: ``(主题, 帧数)``；无数据/超时时为 ``(None, 0)``。
        """
        if self._closed or self.sub_socket is None:
            return None, 0
        if timeout_ms is not None:
            try:
                self.sub_socket.RCVTIMEO = int(timeout_ms)
            except zmq.ZMQError:  # noqa: BLE001
                pass
        try:
            topic = self.sub_socket.recv_string()
        except (zmq.ZMQError, ValueError, TypeError):
            # 主题帧读不出来（超时 / 非 UTF-8 主题 / 已关闭）：同样排空本条
            # 消息的剩余帧，避免下一次 recv 把载荷帧当成主题。
            _drain_multipart(self.sub_socket)
            return None, 0
        return topic, _drain_multipart(self.sub_socket, 1)

    def has_new_data(self):
        """检查流中是否有未处理的新数据。

        Returns:
            bool: 若有数据则返回 True（下一次 recv 不会超时等待）。
        """
        if self._closed or self.sub_socket is None:
            return False
        # poll(0): non-blocking. The old poll(1) blocked the calling (GUI)
        # thread for 1 ms on every check, which - called from 10-100 Hz timers -
        # starved the Qt event loop and made the cursor spin while idle.
        return self.sub_socket.poll(0) == zmq.POLLIN

    def close(self):
        """关闭本客户端持有的 PUB/SUB socket（幂等、线程安全）。

        GenericClient 没有运行线程，socket 只能靠对象 GC 时的 pyzmq __del__
        释放；被长生命周期对象（如 Experiment._dataq）持有时会一直占用
        fd。显式 close 让持有方（Experiment.close 等）能确定性释放。

        置位 _closed 后 send/recv/subscribe/has_new_data 一律安全返回；
        先与 _send_lock 同步，确保没有在途 send 正在使用 pub_socket，再以
        linger=0 立即关闭（不等待积压队列）。close 后不要再调用 send/recv。
        """
        if self._closed:
            return
        self._closed = True
        try:
            with self._send_lock:
                pass   # 发送屏障：等待在途 send 结束
        except Exception:  # noqa: BLE001
            pass
        for sock in (getattr(self, 'pub_socket', None),
                     getattr(self, 'sub_socket', None)):
            if sock is not None:
                try:
                    sock.close(0)
                except Exception:  # noqa: BLE001
                    pass
        self.pub_socket = None
        self.sub_socket = None
        self.subscriptions = []

class DataClient(GenericClient):
    """数据客户端：连接 Data Hub，用于普通数据的发布与订阅。

    使用方式与 GenericClient 相同，区别仅在于按中央配置连接到 Data Hub
    的 pub/sub 端口。

    Args:
        name (str): 发送数据时使用的名字。
        **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
    """
    def __init__(self, name, **kwargs):
        """构造并连接 Data Hub。

        Args:
            name (str): 发送数据时使用的名字。
            **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
        """
        super().__init__(name, **kwargs)

    def _connect(self):
        """从配置读取 Data Hub 端点并连接 PUB/SUB socket。"""
        conf = ConfigReader.getConfiguration()
        hub = server_endpoints(conf, 'Data Hub')
        self.pub_socket.connect(hub['sub'])
        self.sub_socket.connect(hub['pub'])


class CommandClient(DataClient):
    """命令客户端：向驱动发送控制命令。

    与 DataClient 复用相同机制，但连接独立的 Command Hub，因此命令与
    数据通道彼此分开，便于单独设定端点。

    Args:
        name (str): 发送命令时使用的名字。
        **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
    """
    def __init__(self, name, **kwargs):
        """构造并连接 Command Hub。

        Args:
            name (str): 发送命令时使用的名字。
            **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
        """
        super().__init__(name, **kwargs)

    #: 命令走 Command Hub，Data Manager 不订阅 → 不消耗保存回执 seq。
    ACK_TRACKED = False

    def _connect(self):
        """从配置读取 Command Hub 端点并连接 PUB/SUB socket。"""
        conf = ConfigReader.getConfiguration()
        hub = server_endpoints(conf, 'Command Hub')
        self.pub_socket.connect(hub['sub'])
        self.sub_socket.connect(hub['pub'])

    def send(self, cmd='', data={}, A=None, flags=0, copy=True, track=False, prefix=None):
        """发送一条命令（命令名拼接在通道主题中）。

        Args:
            cmd (str): 命令名，会作为主题的一部分（' '+cmd）发送。
            data (dict): 命令附带的数据（须可 JSON 序列化）。
            A (np.array|None): 可选附带的数据数组。
            flags (int): ZMQ 发送标志。
            copy (bool): 是否复制发送数据。
            track (bool): 是否跟踪发送完成。
            prefix (str|None): 主题前缀覆盖。
        """
        super().send(data, A, ' ' + cmd, flags, copy, track, prefix)

class ImageClient(DataClient):
    """图像客户端：连接 Image Hub，用于大尺寸（图像级）数据的发送。

    与 GenericClient/DataClient 的差别在于 send 接口直接接收 numpy 数组
    （而非塞进 dict），避免大数据的序列化开销。

    Args:
        name (str): 发送图像数据时使用的名字。
        **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
    """
    def __init__(self, name, **kwargs):
        """构造并连接 Image Hub。

        Args:
            name (str): 发送图像数据时使用的名字。
            **kwargs: 透传给 GenericClient 的参数（recvtimeout 等）。
        """
        super().__init__(name, **kwargs)
        # 图像走 Image Hub，Data Manager 不订阅 → 不消耗保存回执 seq。
        ACK_TRACKED = False
        # 图像流：一帧就是数 MB，用“消息条数”当上限会立刻变成 GB 级内存
        # （100k 条 x 8 MB）。图像查看/记录只需要“足够新”的帧，所以把上限
        # 压到很小：到顶后 ZMQ 丢弃旧帧，而不是让内存无限增长。
        try:
            self.sub_socket.setsockopt(
                zmq.RCVHWM, _zmq_opt('QUATM_ZMQ_IMG_RCVHWM', 20))
        except (zmq.ZMQError, AttributeError):
            pass

    def _connect(self):
        """从配置读取 Image Hub 端点并连接 PUB/SUB socket。"""
        conf = ConfigReader.getConfiguration()
        hub = server_endpoints(conf, 'Image Hub')
        self.pub_socket.connect(hub['sub'])
        self.sub_socket.connect(hub['pub'])

    def send(self, data, header={}, channel='', flags=0, copy=True, track=False):
        """发送一张图像（numpy 数组）及其头部信息。

        Args:
            data (np.ndarray): 要发送的图像数据。
            header (dict): 头部元数据（须可 JSON 序列化），会自动加入 dtype/shape。
            channel (str): 子通道名，发送时会拼接到 self.name 之后。
            flags (int): ZMQ 发送标志。
            copy (bool): 是否复制发送数据。
            track (bool): 是否跟踪发送完成。
        """
        if self._closed or self.pub_socket is None:
            return None
        # 图像消息不进 Data Manager 的数据面，因此不消耗保存回执 seq。
        stamp = None
        if stamp is not None and isinstance(header, dict):
            header = dict(header)
            header['_ack'] = stamp
        with self._send_lock:
            self.pub_socket.send_string(self.name+channel, flags | zmq.SNDMORE)

            header['dtype'] = str(data.dtype)
            header['shape'] = data.shape
            self.pub_socket.send_json(header, flags|zmq.SNDMORE, cls=NpEncoder)
            return self.pub_socket.send(data, flags, copy=copy, track=track)






if __name__ == "__main__":
    pass
