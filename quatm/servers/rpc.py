"""quatm 自研轻量 RPC —— 替代 vendored sipyco.pc_rpc（LGPL）。

提供与旧 ``sipyco.pc_rpc`` 用法兼容的接口，供实验室内网把 Python 对象
暴露为远程调用服务（monitor/influxdb 服务器）或作为客户端调用它们：

    from quatm.servers.rpc import simple_server_loop   # 服务端
    from quatm.servers.rpc import Client               # 客户端

设计：
  * 纯标准库实现（socket + pickle + threading），零第三方依赖、跨平台；
  * 传输帧：4 字节大端长度前缀 + pickle 载荷；请求/响应均为 dict：
      req  = {"method": str, "args": tuple, "kwargs": dict}
      resp = {"ok": True, "result": ...}
          或 {"ok": False, "error": {"type": str, "msg": str}}
  * 支持多线程并发处理（每连接一线程），适合多个 monitor 同时查询；
  * 单个 :class:`Client` 可被多线程共用：每次请求-响应持有内部锁，帧不会
    交错；请求默认超时见 :data:`DEFAULT_RPC_TIMEOUT_S`；
  * pickle 安全性：仅用于受信任的实验室内部网络（与旧 sipyco 同类信任
    模型）。请勿暴露到不可信网络。

API 兼容性（相对 sipyco.pc_rpc 用到的子集）：
  * simple_server_loop(obj_dict, host, port)  —— 阻塞服务（KeyboardInterrupt
    或 RuntimeError("Server loop terminated") 退出）；
  * Client(host, port, target_name=None) —— 远程对象句柄，属性访问即远程
    调用；支持位置/关键字参数；close()/__enter__/__exit__ 可用。
"""
import pickle
import socket
import struct
import threading

from quatm.utils.i18n import tr

_FRAME = struct.Struct('>I')   # 4 字节大端长度前缀

#: 单次 RPC 请求-响应的默认超时 [s]（``Client(timeout=None)`` 时生效）。
#: 没有超时时，对端崩溃/掉线会让调用线程永久阻塞在 recv 上。
DEFAULT_RPC_TIMEOUT_S = 300.0


def _recv_exact(sock, n):
    """从 socket 精确读取 ``n`` 字节（处理分包/粘包）。

    Args:
        sock (socket.socket): 已连接的 TCP socket。
        n (int): 需要读取的字节数。

    Returns:
        bytes: 恰好 ``n`` 字节的数据。

    Raises:
        ConnectionError: 对端在读完前关闭连接。
    """
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('connection closed')
        buf += chunk
    return buf


def _send_frame(sock, obj):
    """发送一帧：4 字节大端长度前缀 + pickle 载荷。

    Args:
        sock (socket.socket): 已连接的 TCP socket。
        obj: 任意可 pickle 对象（请求/响应 dict）。
    """
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(_FRAME.pack(len(data)) + data)


def _recv_frame(sock):
    """读取并解析一帧（与 :func:`_send_frame` 对称）。

    Args:
        sock (socket.socket): 已连接的 TCP socket。

    Returns:
        object: 反序列化出的请求/响应 dict。
    """
    (n,) = _FRAME.unpack(_recv_exact(sock, _FRAME.size))
    return pickle.loads(_recv_exact(sock, n))


def _handle_connection(conn, targets):
    """服务一个客户端连接：循环读请求、分派到目标对象、回响应。

    客户端断开（ConnectionError/EOFError/OSError）即退出该线程并关闭连接；
    目标方法抛出的异常被包装成 ``{'ok': False, 'error': {...}}`` 回传，不会
    中断连接。

    Args:
        conn (socket.socket): 已接受的连接。
        targets (dict): ``{target_name: object}`` 目标表。
    """
    try:
        while True:
            req = _recv_frame(conn)
            method = req.get('method')
            # 用 'target:method' 定位：target 为空时匹配唯一目标
            target = targets.get(req.get('target')) or next(iter(targets.values()))
            if target is None or method is None:
                resp = {'ok': False,
                        'error': {'type': 'ValueError', 'msg': 'bad request'}}
            else:
                try:
                    fn = getattr(target, method)
                    result = fn(*req.get('args', ()), **req.get('kwargs', {}))
                    resp = {'ok': True, 'result': result}
                except Exception as e:  # noqa: BLE001 - 回传远端异常
                    resp = {'ok': False,
                            'error': {'type': type(e).__name__, 'msg': str(e)}}
            _send_frame(conn, resp)
    except (ConnectionError, EOFError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


# 连接空闲超时 [s]：客户端崩溃/网络掉线会留下半开连接，而 _recv_frame 没有
# 超时 → 该线程与 fd 会被**永久**占住（常驻监控/RPC 服务跑数周就累积）。
# 超时后 recv 抛 socket.timeout（OSError 子类），上面的 except 会正常收尾。
_CONN_IDLE_TIMEOUT_S = 300.0


def simple_server_loop(obj_dict, host='', port=0):
    """阻塞地把 obj_dict 中的对象暴露为 RPC 服务。

    每来一个连接起一个 daemon 线程服务（可并发）；Ctrl-C 退出并释放端口。

    Args:
        obj_dict (dict): ``{target_name: object}``——客户端通过 target_name
            选中目标；只有一个目标时客户端可不传名称。
        host (str): 绑定地址（默认全接口）。注意：本服务基于 pickle，
            只应在可信内网使用。
        port (int): 绑定端口。

    Returns:
        None: 仅在 KeyboardInterrupt（Ctrl-C）时返回。

    Example:
        起一个名为 ``'mon'`` 的监控服务::

            from quatm.servers.rpc import simple_server_loop

            class Monitor:
                def get_value(self):
                    return 42

            simple_server_loop({'mon': Monitor()}, port=1234)
    """
    targets = {str(k): v for k, v in (obj_dict or {}).items()}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port)))
        srv.listen(8)
        try:
            while True:
                conn, _addr = srv.accept()
                try:
                    conn.settimeout(_CONN_IDLE_TIMEOUT_S)
                except OSError:
                    pass
                t = threading.Thread(target=_handle_connection,
                                     args=(conn, targets), daemon=True)
                t.start()
        except KeyboardInterrupt:
            return


class RpcError(Exception):
    """本机侧 RPC 传输错误（区别于远端方法抛出的 :class:`RemoteError`）。"""


class RpcTimeoutError(RpcError, TimeoutError):
    """RPC 请求在超时时间内没有等到响应。

    超时后底层连接已被重置（迟到的响应无法再安全配对）；下一次调用会
    自动重连。
    """


class RpcConnectionError(RpcError, ConnectionError):
    """RPC 连接不可用：建连失败、对端关闭、网络中断或响应不可解析。

    底层连接已被重置；下一次调用会尝试重连。
    """


class Client:
    """远程对象句柄：属性访问即远程方法调用。

    用法（与旧 ``sipyco.pc_rpc`` 兼容）：

        with Client('127.0.0.1', 1234) as mon:
            value = mon.get_reading()          # 远程调用
            result = mon.compute(1, scale=2.0)  # 位置/关键字参数

    线程安全：同一个实例可被多个线程共用——每次请求-响应都在内部锁内完成，
    两个线程的请求帧不会交错（不会把 A 的响应交给 B）。

    超时与重连：每次请求都有超时（默认 :data:`DEFAULT_RPC_TIMEOUT_S`，可用
    ``timeout`` 覆盖）。超时/连接断开后本对象丢弃旧连接并抛出
    :class:`RpcTimeoutError` / :class:`RpcConnectionError`，**下一次调用会
    自动重连**。注意：超时的请求可能已在远端执行，是否重试由调用方按方法
    幂等性决定（本类不自动重试，避免重复执行带副作用的方法）。

    安全/信任模型：客户端用 ``pickle.loads`` 反序列化服务端响应，服务端同样
    用 pickle 反序列化请求——**两端互为完全信任**，任何能连上端口的一方都能
    在另一端执行任意代码。只允许在受信任的实验室内部网络使用，严禁把端口
    暴露到公网或不可信用户（旧 sipyco.pc_rpc 是同一模型）。

    Args:
        host (str): 服务端地址。
        port (int): 服务端端口。
        target_name (str|None): 目标对象名；服务端只有一个对象时可留空。
        timeout (float|None): 建连与每次请求-响应的超时（秒）；None 用
            :data:`DEFAULT_RPC_TIMEOUT_S`。

    Attributes:
        _sock (socket.socket|None): 底层连接；断开时为 None（下次调用重连）。

    Raises:
        RemoteError: 远端方法抛异常时（在调用处抛出）。
    """

    def __init__(self, host, port, target_name=None, timeout=None):
        """连接服务端。

        Args:
            host (str): 服务端地址（IP/主机名）。
            port (int): 服务端端口。
            target_name (str|None): 目标对象名。
            timeout (float|None): 建连/请求超时秒数；None 用默认值。

        Raises:
            RpcConnectionError: 建连失败。
        """
        self._host = host
        self._port = int(port)
        self._target = target_name
        self._timeout = (DEFAULT_RPC_TIMEOUT_S if timeout is None
                         else float(timeout))
        # 请求-响应全程持锁：socket 不是线程安全的，多线程并发会把帧交错。
        self._lock = threading.Lock()
        self._closed = False
        self._sock = None
        self._connect()

    def _connect(self):
        """建立（或重建）底层 TCP 连接，并把请求超时设到 socket 上。

        Raises:
            RpcConnectionError: 建连失败。
        """
        try:
            sock = socket.create_connection((self._host, self._port),
                                            timeout=self._timeout)
        except OSError as exc:
            raise RpcConnectionError(
                tr('RPC could not connect to {host}:{port}: {error}',
                   host=self._host, port=self._port, error=exc)) from exc
        try:
            sock.settimeout(self._timeout)
        except OSError:
            pass
        self._sock = sock

    def _drop_socket(self):
        """关闭并丢弃当前连接；下一次调用 :meth:`_call` 时会重连。"""
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __getattr__(self, name):
        """把属性名解析为远程方法句柄；下划线开头属性照常抛 AttributeError。"""
        if name.startswith('_'):
            raise AttributeError(name)
        return _RemoteMethod(self, name)

    def _call(self, method, args, kwargs):
        """在锁内完成一次请求-响应；传输失败时重置连接并抛明确异常。

        Args:
            method (str): 远程方法名。
            args (tuple): 位置参数。
            kwargs (dict): 关键字参数。

        Returns:
            object: 远端返回值。

        Raises:
            RpcTimeoutError: 超时没有响应（连接已重置）。
            RpcConnectionError: 连接断开/不可用/响应不可解析（连接已重置）。
            RemoteError: 远端方法抛出异常。
        """
        with self._lock:
            if self._closed:
                raise RpcConnectionError(tr('RPC client is closed.'))
            if self._sock is None:
                self._connect()
            try:
                _send_frame(self._sock, {
                    'target': self._target, 'method': method,
                    'args': args, 'kwargs': kwargs,
                })
                resp = _recv_frame(self._sock)
            except socket.timeout as exc:
                # 迟到的响应会破坏后续帧配对，因此断开重连而不是沿用。
                self._drop_socket()
                raise RpcTimeoutError(
                    tr('RPC call {method} timed out after {timeout} s; the '
                       'connection was reset. The next call will reconnect.',
                       method=method, timeout=self._timeout)) from exc
            except (pickle.UnpicklingError, EOFError, OSError) as exc:
                self._drop_socket()
                raise RpcConnectionError(
                    tr('RPC connection to {host}:{port} was lost ({error}); '
                       'the connection was reset. The next call will '
                       'reconnect.', host=self._host, port=self._port,
                       error=exc)) from exc
            if not isinstance(resp, dict):
                self._drop_socket()
                raise RpcConnectionError(
                    tr('RPC protocol error: the peer returned a response of '
                       'type {kind}.', kind=type(resp).__name__))
            if not resp.get('ok'):
                err = resp.get('error', {})
                raise RemoteError(err.get('type', 'Error'), err.get('msg', ''))
            return resp.get('result')

    def close(self):
        """关闭底层 TCP 连接并释放 socket 句柄（幂等，可重复调用）。"""
        with self._lock:
            self._closed = True
            self._drop_socket()

    def __enter__(self):
        """支持 ``with Client(...) as c:`` 用法，返回自身。"""
        return self

    def __exit__(self, *exc):
        """退出 with 块时自动调用 :meth:`close` 释放连接。"""
        self.close()


class _RemoteMethod:
    """远程方法句柄：调用它即发送一次 RPC 请求。

    通常不直接构造——由 :class:`Client` 的属性访问自动返回。

    Attributes:
        _client (Client): 所属客户端。
        _name (str): 要调用的远程方法名。
    """

    __slots__ = ('_client', '_name')

    def __init__(self, client, name):
        """绑定远程方法句柄到客户端与方法名。

        Args:
            client (Client): 所属客户端。
            name (str): 远程方法名。
        """
        self._client = client
        self._name = name

    def __call__(self, *args, **kwargs):
        """执行一次远程调用并返回远端结果。"""
        return self._client._call(self._name, args, kwargs)


class RemoteError(Exception):
    """远端抛出的异常（类型名 + 消息）。

    Attributes:
        etype (str): 远端异常类型名。
        emsg (str): 远端异常消息。
    """

    def __init__(self, etype, msg):
        """构造远端异常（消息形如 ``type: msg``）。

        Args:
            etype (str): 远端异常类型名。
            msg (str): 远端异常消息。
        """
        super().__init__('%s: %s' % (etype, msg))
        self.etype = etype
        self.emsg = msg
