"""A1 安全回归：推送服务配置镜像进 Properties 之前必须脱敏。

背景：:func:`quatm.servers.notifications.save_config` 会把 ``services`` 镜像到
``Properties('Notifications')``，而常驻 Propertylogger 又把属性树落盘到**被 git
跟踪的** ``configuration/properties/properties.json``。如果原样镜像，用户填的
``token`` / ``password`` 就会经这条路径重新进入版本库（凭据泄漏回归）。

覆盖：

* :func:`quatm.servers.notifications._sanitize_services` 纯函数：递归掩码敏感键、
  大小写不敏感、保留非敏感结构、绝不修改入参；
* ``save_config`` 镜像到 mock Properties 的值里不含任何原始 secret，但磁盘上的
  ``notifications.json`` 仍保留真 token（投递需要）；
* 仓库里被 git 跟踪的 ``configuration/properties/properties.json`` 不存在明文
  敏感键值。

测试约定见 ``tests/__init__.py``：只用 stdlib ``unittest`` + ``tempfile``；
不连属性系统/ZMQ（mock ``_properties_client``）。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from quatm.servers import notifications

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_TOKEN = 'SECRET-TOKEN-abcdef123456'
SECRET_PASSWORD = 'SECRET-PASSWORD-654321'
SECRET_API_KEY = 'SECRET-APIKEY-xyz789'


class _FakeProps:
    """假的 Properties 客户端：只记录 ``set()`` 调用，不连 ZMQ。"""

    def __init__(self):
        self.values = {}

    def set(self, key, value):
        """记录一次属性写入。

        Args:
            key (str): 属性键。
            value: 属性值。
        """
        self.values[key] = value


class TestSanitizeServices(unittest.TestCase):
    """``_sanitize_services`` 是纯函数：脱敏敏感键、保留结构、不改入参。"""

    def test_masks_token_and_credential_urls(self):
        services = [
            {'name': 'Mattermost', 'type': 'mattermost', 'enabled': True,
             'config': {'url': 'chat.example.com', 'port': 443,
                        'token': SECRET_TOKEN, 'team_name': 'T',
                        'channel_name': 'C'}},
            {'name': 'Gotify', 'type': 'gotify',
             'config': {'url': 'https://gotify.example.com',
                        'Token': SECRET_TOKEN}},
            {'name': 'Hook', 'type': 'webhook',
             'config': {'url': 'https://hooks.example.com/services/'
                                + SECRET_TOKEN, 'timeout': 5}},
            {'name': 'Bark', 'type': 'bark',
             'config': {'base_url': 'https://bark.example.com/' + SECRET_TOKEN}},
        ]
        original = json.loads(json.dumps(services))

        sanitized = notifications._sanitize_services(services)

        blob = json.dumps(sanitized)
        self.assertNotIn(SECRET_TOKEN, blob)
        # 非敏感字段与整体结构必须保留
        self.assertEqual(sanitized[0]['name'], 'Mattermost')
        self.assertEqual(sanitized[0]['type'], 'mattermost')
        self.assertTrue(sanitized[0]['enabled'])
        self.assertEqual(sanitized[0]['config']['url'], 'chat.example.com')
        self.assertEqual(sanitized[0]['config']['port'], 443)
        self.assertEqual(sanitized[0]['config']['team_name'], 'T')
        self.assertEqual(sanitized[1]['config']['url'],
                         'https://gotify.example.com')
        self.assertEqual(sanitized[2]['config']['timeout'], 5)
        # 敏感值被掩码成占位串（保留“这里有凭据”的信息）
        self.assertEqual(sanitized[0]['config']['token'], notifications._MASK)
        self.assertEqual(sanitized[2]['config']['url'], notifications._MASK)
        self.assertEqual(sanitized[3]['config']['base_url'], notifications._MASK)
        # 纯函数：入参绝不被修改
        self.assertEqual(services, original)

    def test_masks_password_apikey_and_nested_keys(self):
        services = [
            {'name': 'X', 'type': 'mattermost',
             'config': {'password': SECRET_PASSWORD,
                        'api_key': SECRET_API_KEY,
                        'headers': {'Authorization': SECRET_TOKEN},
                        'nested': [{'client_secret': SECRET_TOKEN,
                                    'plain': 'visible'}]}},
        ]
        sanitized = notifications._sanitize_services(services)
        blob = json.dumps(sanitized)
        for secret in (SECRET_PASSWORD, SECRET_API_KEY, SECRET_TOKEN):
            self.assertNotIn(secret, blob)
        # 结构保留：嵌套 list/dict 还在，非敏感值可见
        nested = sanitized[0]['config']['nested'][0]
        self.assertEqual(nested['plain'], 'visible')
        self.assertEqual(nested['client_secret'], notifications._MASK)

    def test_case_insensitive_key_match(self):
        services = [{'name': 'X', 'type': 'gotify',
                     'config': {'API_TOKEN': SECRET_TOKEN,
                                'Password': SECRET_PASSWORD,
                                'app_secret': SECRET_API_KEY}}]
        blob = json.dumps(notifications._sanitize_services(services))
        for secret in (SECRET_TOKEN, SECRET_PASSWORD, SECRET_API_KEY):
            self.assertNotIn(secret, blob)

    def test_handles_empty_and_malformed_input(self):
        self.assertEqual(notifications._sanitize_services([]), [])
        self.assertEqual(notifications._sanitize_services(None), [])
        self.assertEqual(notifications._sanitize_services({'a': 1}), [])
        # 列表里的非 dict 条目也不能炸（原样递归，标量不变）
        self.assertEqual(notifications._sanitize_services(['x', 3]), ['x', 3])


class TestMirrorIsSanitized(unittest.TestCase):
    """``save_config`` 镜像到 Properties 的值不含 secret；磁盘文件保持原样。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'notifications.json')
        self._saved = (notifications._CONFIG_FILE,
                       notifications._mirror_to_properties,
                       notifications._cache,
                       notifications._props_mirror)
        notifications._CONFIG_FILE = self.path
        notifications._mirror_to_properties = True
        notifications._cache = None
        notifications._props_mirror = None
        self.props = _FakeProps()
        patcher = mock.patch.object(notifications, '_properties_client',
                                    return_value=self.props)
        patcher.start()
        self.addCleanup(patcher.stop)

        def _restore():
            (notifications._CONFIG_FILE,
             notifications._mirror_to_properties,
             notifications._cache,
             notifications._props_mirror) = self._saved

        self.addCleanup(_restore)

    def _cfg(self):
        """构造一份含真 token/password 的通知配置。

        Returns:
            dict: 两个服务（mattermost + webhook），都带凭据。
        """
        return {
            'push_enabled': True,
            'default_service': 'Mattermost',
            'services': [
                {'name': 'Mattermost', 'type': 'mattermost', 'enabled': True,
                 'config': {'url': 'chat.example.com', 'port': 443,
                            'scheme': 'https', 'token': SECRET_TOKEN,
                            'team_name': 'T', 'channel_name': 'C'}},
                {'name': 'Hook', 'type': 'webhook', 'enabled': False,
                 'config': {'url': 'https://hooks.example.com/'
                                   + SECRET_TOKEN, 'timeout': 5}},
                {'name': 'Gotify', 'type': 'gotify', 'enabled': True,
                 'config': {'url': 'https://gotify.example.com',
                            'token': SECRET_TOKEN}},
            ],
        }

    def test_mirror_contains_no_secret(self):
        notifications.save_config(self._cfg())

        mirrored = self.props.values['push_services']
        blob = json.dumps(mirrored)
        self.assertNotIn(SECRET_TOKEN, blob)
        # 镜像确实发生了、且结构可读（服务名/类型/启用状态/非敏感字段）
        self.assertEqual([svc['name'] for svc in mirrored],
                         ['Mattermost', 'Hook', 'Gotify'])
        self.assertTrue(mirrored[0]['enabled'])
        self.assertEqual(mirrored[0]['config']['url'], 'chat.example.com')
        self.assertEqual(mirrored[0]['config']['port'], 443)
        self.assertTrue(self.props.values['push_enabled'])
        self.assertEqual(self.props.values['default_service'], 'Mattermost')

    def test_mirror_flags_and_default_service_still_mirrored(self):
        notifications.save_config(self._cfg())
        self.assertIn('push_enabled', self.props.values)
        self.assertIn('default_service', self.props.values)
        self.assertIn('push_services', self.props.values)

    def test_config_file_keeps_real_secret(self):
        """镜像脱敏不影响落盘：notifications.json 必须保留真 token（投递要用）。"""
        cfg = self._cfg()
        notifications.save_config(cfg)
        with open(self.path, encoding='utf-8') as handle:
            saved = json.load(handle)
        self.assertEqual(saved, cfg)
        self.assertEqual(saved['services'][0]['config']['token'], SECRET_TOKEN)
        self.assertIn(SECRET_TOKEN, saved['services'][1]['config']['url'])

    def test_cached_config_keeps_real_secret(self):
        notifications.save_config(self._cfg())
        self.assertEqual(notifications.get_config()['services'][0]
                         ['config']['token'], SECRET_TOKEN)


class TestTrackedPropertiesFile(unittest.TestCase):
    """被 git 跟踪的 properties.json 里不得出现明文敏感键值。"""

    def test_no_plaintext_secrets_in_tracked_properties(self):
        path = os.path.join(ROOT, 'configuration', 'properties',
                            'properties.json')
        if not os.path.isfile(path):
            self.skipTest('仓库里没有 configuration/properties/properties.json')
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)

        leaks = []

        def walk(node, trail=''):
            if isinstance(node, dict):
                for key, value in node.items():
                    path_key = '%s.%s' % (trail, key) if trail else str(key)
                    if (notifications._is_sensitive_key(key)
                            and isinstance(value, str)
                            and value.strip() not in ('', notifications._MASK)):
                        leaks.append('%s = %r' % (path_key, value[:40]))
                    walk(value, path_key)
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, '%s[%d]' % (trail, index))

        walk(data)
        self.assertEqual(leaks, [],
                         'properties.json 里出现明文凭据：%s' % leaks)


if __name__ == '__main__':
    unittest.main()
