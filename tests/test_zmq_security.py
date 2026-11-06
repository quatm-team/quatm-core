"""本机 ZMQ CURVE 加密测试（stdlib unittest，tcp loopback / ipc，不接硬件）。

覆盖：

* 密钥管理：首次自动生成、目录 0700 / 文件 0600、二次解析复用同一组密钥；
  损坏/不完整密钥、目录是文件、目录不可写、libzmq 无 CURVE 支持 →
  警告 + 明文回退（绝不抛异常）；``QUATM_ZMQ_CURVE=0`` / configfile
  ``Servers/zmq_curve: false`` → 明文；
* 加密通道：CURVE server(bind) ↔ client(connect) 在 tcp loopback 与 ipc 上
  PUB/SUB、REQ/REP 均可用；
* 失败模式：明文客户端连 CURVE server、错误 server key 的客户端、
  CURVE 客户端连明文 server → 收不到消息（RCVTIMEO/poll 断言，不卡死）；
* 回退：关闭加密后明文 PUB/SUB 可用；非回环绑定 + 明文回退有醒目警告。

测试全部用临时密钥目录（``QUATM_ZMQ_KEY_DIR``）+ 每个测试后 reset 模块缓存，
不读取/不污染仓库里的真实密钥。
"""

import contextlib
import io
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

import zmq

from quatm.servers import zmq_security as sec
from quatm.utils import i18n as _i18n


def _mode(path):
    """返回文件/目录权限位（0o600 / 0o700）。"""
    return stat.S_IMODE(os.stat(path).st_mode)


class _SecurityTestBase(unittest.TestCase):
    """环境/语言/模块缓存隔离 + ZMQ context 清理。"""

    def setUp(self):
        super().setUp()
        self._saved_env = {name: os.environ.get(name)
                           for name in (sec.ENV_CURVE, sec.ENV_KEY_DIR)}
        os.environ.pop(sec.ENV_CURVE, None)
        os.environ.pop(sec.ENV_KEY_DIR, None)
        self._saved_lang = _i18n.language()
        _i18n.set_language('en')
        self._ctx = None
        self.addCleanup(self._cleanup)
        sec.reset_for_tests()

    def _cleanup(self):
        if self._ctx is not None:
            try:
                self._ctx.destroy(linger=0)
            except Exception:  # noqa: BLE001 - 清理不能掩盖测试失败
                pass
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _i18n.set_language(self._saved_lang)
        sec.reset_for_tests()

    def _temp_key_dir(self):
        """建临时目录并把它设为密钥目录，返回路径。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'zmq')
        os.environ[sec.ENV_KEY_DIR] = path
        sec.reset_for_tests()
        return path

    def _context(self):
        self._ctx = zmq.Context()
        return self._ctx

    @contextlib.contextmanager
    def _capture_stdout(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            yield buffer


class TestKeyManagement(_SecurityTestBase):
    """密钥生成/复用/回退。"""

    def test_generates_keys_with_secure_permissions(self):
        directory = self._temp_key_dir()
        self.assertTrue(sec.curve_enabled())
        self.assertEqual(sorted(os.listdir(directory)), sorted(sec.KEY_FILES))
        if os.name == 'posix':  # Windows 没有 POSIX 模式位（ACL 需 icacls，见 ADR）
            self.assertEqual(_mode(directory), 0o700)
            for name in sec.KEY_FILES:
                self.assertEqual(_mode(os.path.join(directory, name)), 0o600,
                                 name)
        for name in sec.KEY_FILES:
            path = os.path.join(directory, name)
            with open(path, encoding='ascii') as handle:
                self.assertEqual(len(handle.read().strip()), 40, name)

    def test_existing_keys_are_reused(self):
        directory = self._temp_key_dir()
        self.assertTrue(sec.curve_enabled())
        before = {}
        for name in sec.KEY_FILES:
            with open(os.path.join(directory, name), 'rb') as handle:
                before[name] = handle.read()
        sec.reset_for_tests()
        self.assertTrue(sec.curve_enabled())
        for name in sec.KEY_FILES:
            with open(os.path.join(directory, name), 'rb') as handle:
                self.assertEqual(handle.read(), before[name], name)

    def test_corrupt_key_is_regenerated_with_warning(self):
        """损坏的密钥触发一次原子重建 + 警告（而不是让整套服务留在明文）。"""
        directory = self._temp_key_dir()
        self.assertTrue(sec.curve_enabled())
        self.assertTrue(os.path.isfile(os.path.join(directory, 'server.key')))
        with open(os.path.join(directory, 'server.key'), 'w',
                  encoding='ascii') as handle:
            handle.write('not-a-valid-curve-key')
        sec.reset_for_tests()
        with self._capture_stdout() as output:
            self.assertTrue(sec.curve_enabled())
        text = output.getvalue()
        self.assertIn('[ZMQ]', text)
        self.assertIn('CURVE', text)
        self.assertIn(directory, text)
        self.assertIn('generated a fresh key set', text)
        self.assertTrue(all(os.path.isfile(os.path.join(directory, name))
                            for name in sec.KEY_FILES))

    def test_partially_present_keys_are_regenerated_with_warning(self):
        """半套密钥（并发生成遗留）会被原子重建，不再回退明文。"""
        directory = self._temp_key_dir()
        self.assertTrue(sec.curve_enabled())
        for name in ('server.key_secret', 'client.key', 'client.key_secret'):
            os.remove(os.path.join(directory, name))
        sec.reset_for_tests()
        with self._capture_stdout() as output:
            self.assertTrue(sec.curve_enabled())
        self.assertIn('generated a fresh key set', output.getvalue())
        self.assertTrue(all(os.path.isfile(os.path.join(directory, name))
                            for name in sec.KEY_FILES))
        sec.reset_for_tests()
        with self._capture_stdout() as output:
            self.assertTrue(sec.curve_enabled())
        self.assertNotIn('generated a fresh key set', output.getvalue())

    def test_key_dir_path_is_a_file_falls_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'zmq')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('not a directory')
        os.environ[sec.ENV_KEY_DIR] = path
        sec.reset_for_tests()
        with self._capture_stdout() as output:
            self.assertFalse(sec.curve_enabled())
        self.assertIn('is not a directory', output.getvalue())

    def test_unwritable_key_dir_falls_back(self):
        """父目录只读导致密钥目录无法创建 → 回退明文 + 警告。"""
        if os.name != 'posix':
            self.skipTest('Windows 上 chmod 0500 不会阻止创建子目录')
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('root 不受目录权限限制')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        parent = os.path.join(tmp.name, 'readonly')
        os.makedirs(parent, mode=0o500)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        os.environ[sec.ENV_KEY_DIR] = os.path.join(parent, 'zmq')
        sec.reset_for_tests()
        with self._capture_stdout() as output:
            self.assertFalse(sec.curve_enabled())
        text = output.getvalue()
        self.assertIn('[ZMQ]', text)
        self.assertIn('CURVE', text)

    def test_disabled_by_environment(self):
        directory = self._temp_key_dir()
        os.environ[sec.ENV_CURVE] = '0'
        sec.reset_for_tests()
        self.assertFalse(sec.curve_enabled())
        self.assertFalse(os.path.exists(directory), '禁用时不应生成密钥')

    def test_disabled_by_configfile(self):
        self._temp_key_dir()
        conf = {'Servers': {'zmq_curve': False}}
        with mock.patch('quatm.servers.configreader.ConfigReader'
                        '.getConfiguration', return_value=conf):
            sec.reset_for_tests()
            self.assertFalse(sec.curve_enabled())

    def test_environment_overrides_configfile(self):
        self._temp_key_dir()
        os.environ[sec.ENV_CURVE] = '1'
        conf = {'Servers': {'zmq_curve': False}}
        with mock.patch('quatm.servers.configreader.ConfigReader'
                        '.getConfiguration', return_value=conf):
            sec.reset_for_tests()
            self.assertTrue(sec.curve_enabled())

    def test_missing_libzmq_curve_support_falls_back(self):
        self._temp_key_dir()
        with mock.patch.object(sec.zmq, 'has', return_value=False):
            sec.reset_for_tests()
            with self._capture_stdout() as output:
                self.assertFalse(sec.curve_enabled())
        self.assertIn('CURVE', output.getvalue())


class TestEncryptedChannels(_SecurityTestBase):
    """CURVE server(bind) ↔ client(connect) 回环可用。"""

    def _bind_tcp(self, sock, endpoint='tcp://127.0.0.1:*'):
        sock.bind(endpoint)
        return sock.getsockopt(zmq.LAST_ENDPOINT).decode('ascii')

    def test_encrypted_pubsub_tcp(self):
        self._temp_key_dir()
        ctx = self._context()
        server = ctx.socket(zmq.PUB)
        self.assertTrue(sec.server_configure(server))
        endpoint = self._bind_tcp(server)
        client = ctx.socket(zmq.SUB)
        self.assertTrue(sec.client_configure(client))
        client.setsockopt_string(zmq.SUBSCRIBE, '')
        client.connect(endpoint)
        try:
            deadline = time.time() + 3.0
            received = None
            while time.time() < deadline and received is None:
                server.send_string('secret-topic')
                if client.poll(100):
                    received = client.recv_string()
            self.assertEqual(received, 'secret-topic')
        finally:
            server.close(0)
            client.close(0)

    def test_encrypted_reqrep_tcp(self):
        self._temp_key_dir()
        ctx = self._context()
        server = ctx.socket(zmq.REP)
        self.assertTrue(sec.server_configure(server))
        endpoint = self._bind_tcp(server)
        client = ctx.socket(zmq.REQ)
        self.assertTrue(sec.client_configure(client))
        client.connect(endpoint)
        client.RCVTIMEO = 2000
        try:
            client.send_string('ping')
            self.assertEqual(server.recv_string(), 'ping')
            server.send_string('pong')
            self.assertEqual(client.recv_string(), 'pong')
        finally:
            server.close(0)
            client.close(0)

    def test_encrypted_pubsub_ipc(self):
        if os.name != 'posix':
            self.skipTest('Windows 的 libzmq 不提供 ipc:// 传输')
        self._temp_key_dir()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        endpoint = 'ipc://' + os.path.join(tmp.name, 'curve.ipc')
        ctx = self._context()
        server = ctx.socket(zmq.PUB)
        self.assertTrue(sec.server_configure(server, endpoint=endpoint))
        server.bind(endpoint)
        client = ctx.socket(zmq.SUB)
        self.assertTrue(sec.client_configure(client))
        client.setsockopt_string(zmq.SUBSCRIBE, '')
        client.connect(endpoint)
        try:
            deadline = time.time() + 3.0
            received = None
            while time.time() < deadline and received is None:
                server.send_string('ipc-encrypted')
                if client.poll(100):
                    received = client.recv_string()
            self.assertEqual(received, 'ipc-encrypted')
        finally:
            server.close(0)
            client.close(0)


class TestHandshakeFailures(_SecurityTestBase):
    """加密边界：没有正确密钥的对端收不到消息。"""

    def _curve_rep(self, ctx):
        server = ctx.socket(zmq.REP)
        self.assertTrue(sec.server_configure(server))
        server.bind('tcp://127.0.0.1:*')
        endpoint = server.getsockopt(zmq.LAST_ENDPOINT).decode('ascii')
        return server, endpoint

    def test_plaintext_client_cannot_talk_to_curve_server(self):
        self._temp_key_dir()
        ctx = self._context()
        server, endpoint = self._curve_rep(ctx)
        client = ctx.socket(zmq.REQ)     # 故意不配置 CURVE
        client.RCVTIMEO = 500
        client.connect(endpoint)
        try:
            client.send_string('plaintext')
            with self.assertRaises(zmq.Again):
                client.recv_string()
            self.assertEqual(server.poll(300), 0)
        finally:
            server.close(0)
            client.close(0)

    def test_wrong_server_key_client_is_rejected(self):
        self._temp_key_dir()
        ctx = self._context()
        server, endpoint = self._curve_rep(ctx)
        client = ctx.socket(zmq.REQ)
        client.curve_server = False
        client.curve_publickey = sec._state['keys']['client_public']
        client.curve_secretkey = sec._state['keys']['client_secret']
        wrong_public, _wrong_secret = zmq.curve_keypair()
        client.curve_serverkey = wrong_public
        client.RCVTIMEO = 500
        client.connect(endpoint)
        try:
            client.send_string('wrong-server-key')
            with self.assertRaises(zmq.Again):
                client.recv_string()
            self.assertEqual(server.poll(300), 0)
        finally:
            server.close(0)
            client.close(0)

    def test_curve_client_cannot_talk_to_plaintext_server(self):
        self._temp_key_dir()
        ctx = self._context()
        server = ctx.socket(zmq.REP)     # 明文 server
        server.bind('tcp://127.0.0.1:*')
        endpoint = server.getsockopt(zmq.LAST_ENDPOINT).decode('ascii')
        client = ctx.socket(zmq.REQ)
        self.assertTrue(sec.client_configure(client))
        client.RCVTIMEO = 500
        client.connect(endpoint)
        try:
            client.send_string('curve-to-plain')
            with self.assertRaises(zmq.Again):
                client.recv_string()
            self.assertEqual(server.poll(300), 0)
        finally:
            server.close(0)
            client.close(0)


class TestPlaintextFallback(_SecurityTestBase):
    """关闭加密后的明文路径与暴露警告。"""

    def test_disabled_falls_back_to_plaintext(self):
        self._temp_key_dir()
        os.environ[sec.ENV_CURVE] = '0'
        sec.reset_for_tests()
        self.assertFalse(sec.curve_enabled())
        ctx = self._context()
        pub = ctx.socket(zmq.PUB)
        sub = ctx.socket(zmq.SUB)
        self.assertFalse(sec.server_configure(pub))
        self.assertFalse(sec.client_configure(sub))
        pub.bind('tcp://127.0.0.1:*')
        endpoint = pub.getsockopt(zmq.LAST_ENDPOINT).decode('ascii')
        sub.setsockopt_string(zmq.SUBSCRIBE, '')
        sub.connect(endpoint)
        try:
            deadline = time.time() + 3.0
            received = None
            while time.time() < deadline and received is None:
                pub.send_string('plaintext-ok')
                if sub.poll(100):
                    received = sub.recv_string()
            self.assertEqual(received, 'plaintext-ok')
        finally:
            pub.close(0)
            sub.close(0)

    def test_plaintext_nonloopback_bind_warns(self):
        self._temp_key_dir()
        os.environ[sec.ENV_CURVE] = '0'
        sec.reset_for_tests()
        ctx = self._context()
        sock = ctx.socket(zmq.PUB)
        try:
            with self._capture_stdout() as output:
                self.assertFalse(
                    sec.server_configure(sock, endpoint='tcp://0.0.0.0:5555'))
            text = output.getvalue()
            self.assertIn('[ZMQ]', text)
            self.assertIn('tcp://0.0.0.0:5555', text)
            self.assertIn('plaintext', text)
            # 回环端点不产生额外警告
            with self._capture_stdout() as output2:
                self.assertFalse(
                    sec.server_configure(sock, endpoint='tcp://127.0.0.1:5555'))
            self.assertNotIn('plaintext fallback', output2.getvalue())
        finally:
            sock.close(0)


class TestConcurrentKeyGeneration(unittest.TestCase):
    """多进程并发首启必须收敛到同一组密钥。

    背景（验收复现）：ProcessManager 首启会并发拉起多个服务，旧实现用
    ``makedirs`` + 原地写四个文件：多个进程同时生成时会出现
    "File exists"/"partially present"，部分进程回退明文 → 同机部分服务
    CURVE、部分明文，互相连不上。现在改为"锁 + 临时目录原子 rename"。
    """

    def test_parallel_processes_share_one_key_set(self):
        import json
        import shutil
        import subprocess
        import sys
        tmp = tempfile.mkdtemp(prefix='quatm-zmq-keys-')
        self.addCleanup(shutil.rmtree, tmp, True)
        key_dir = os.path.join(tmp, 'zmq')
        child = (
            'import json;'
            'from quatm.servers import zmq_security as z;'
            'z.reset_for_tests();'
            'enabled = z.curve_enabled();'
            'keys = z._state.get("keys") or {};'
            'print("RESULT " + json.dumps({'
            '"enabled": bool(enabled),'
            '"server_public": (keys.get("server_public") or b"").decode("ascii"),'
            '"reason": z._state.get("reason")}))')
        env = dict(os.environ)
        env['QUATM_ZMQ_KEY_DIR'] = key_dir
        env['QUATM_ZMQ_CURVE'] = '1'
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        procs = [subprocess.Popen(
            [sys.executable, '-c', child], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=repo)
            for _ in range(8)]
        results = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, stderr[-2000:])
            lines = [ln for ln in stdout.splitlines()
                     if ln.startswith('RESULT ')]
            self.assertEqual(len(lines), 1,
                             (stdout[-2000:] + stderr[-2000:]))
            results.append(json.loads(lines[0][len('RESULT '):]))
        self.assertTrue(all(r['enabled'] for r in results),
                        'some process fell back to plaintext: %r' % results)
        keys = {r['server_public'] for r in results}
        self.assertEqual(len(keys), 1, 'processes generated different keys')
        self.assertTrue(os.path.isfile(os.path.join(key_dir, 'server.key')))
        # 锁文件用完即删（没有残留）
        self.assertFalse(os.path.exists(key_dir + '.lock'))


if __name__ == '__main__':
    unittest.main()
