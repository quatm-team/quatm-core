"""quatm 的推送通知层：配置 + 入队 API（真正的投递由常驻网关完成）。

推送默认关闭，全部由 ``configuration/notifications.json`` 驱动（同时镜像到
``Properties('Notifications')``，便于在属性树里查看）：

    {
      "push_enabled": true,
      "default_service": "Gotify",
      "gateway": {"min_level": "warning", "dedupe_window_s": 60},
      "services": [
        {"name": "Gotify", "type": "gotify",
         "config": {"url": "https://gotify.example.com", "token": "A..."}},
        {"name": "手机", "type": "timelynotify",
         "config": {"base_url": "https://timelynotify.oicode.cn/<device_key>"}}
      ]
    }

后端（service ``type``）：

* ``mattermost``    - mattermostdriver API（url/port/scheme/token/team/channel）
* ``webhook``       - 通用 JSON ``{text: msg}`` POST（微信/Slack/钉钉机器人）
* ``gotify``        - Gotify 服务端 ``POST /message``（``X-Gotify-Key`` app token）
* ``timelynotify``  - 及时通知 / Bark 的 GET 路径推送（base_url 已含 device key）
* ``bark``          - 与 ``timelynotify`` 同一实现，便于沿用 Bark 的配置习惯

**调用方不做网络请求**：``push_message()`` 只把事件写进推送 outbox
（``configuration/push/push.db``）就返回，真正的 HTTP 投递、重试、退避、
限流与去重由 :mod:`quatm.servers.pushgateway` 常驻进程完成。这样网络抖动
永远不会拖住实验线程；进程被停也不丢消息（下次启动接着发）。

``test_service()`` 是唯一同步发请求的入口：用户在编辑器里点“测试”时
必须立刻看到成功/失败，所以它直接调用 :func:`_dispatch`。

配置可用 GUI 入口 ``quatm/GUI/entries/notifications_editor.py``（由
ProcessManager 启动）管理，也可直接编辑 JSON 文件。
"""

import copy
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from urllib.parse import quote, urlsplit

import requests

from quatm.servers import pushstore
from quatm.servers.configreader import CONFIG_DIR
from quatm.servers import jsonutil
from quatm.servers.pushstore import PushStore, level_rank

_log = logging.getLogger('quatm.notifications')

# Can be overridden in tests / when the config should live elsewhere.
_CONFIG_FILE = os.path.join(str(CONFIG_DIR), 'notifications.json')

_DEFAULT_CONFIG = {
    'push_enabled': False,
    'default_service': 'Gotify',
    'gateway': {
        'enabled': True,
        'min_level': 'warning',
        'source_allow': [],
        # 网关自己也会发消息（启动/投递失败提示），不排除会形成回环
        'source_deny': ['pushgateway.py'],
        'dedupe_window_s': 60,
        'dedupe_levels': ['debug', 'info', 'warning'],
        'title_template': '{level} · {source}',
        'max_attempts': 8,
        'max_pending_per_sink': 200,
        'rate_limit_per_s': 1.0,
        'rate_burst': 5,
        'quiet_hours': {'start': '', 'end': '', 'max_level': 'info'},
        'retention_days': 30,
        'max_events': 20000,
        'prune_interval_s': 300,
    },
    'services': [
        {'name': 'Gotify', 'type': 'gotify', 'enabled': False,
         'config': {'url': '', 'token': '', 'timeout': 5}},
        {'name': 'TimelyNotify', 'type': 'timelynotify', 'enabled': False,
         'config': {'base_url': '', 'timeout': 5}},
        {'name': 'Mattermost', 'type': 'mattermost', 'enabled': False,
         'config': {
             'url': 'qsim-mattermost.uni-freiburg.de',
             'port': 443,
             'scheme': 'https',
             'token': '',
             'team_name': 'νTiamo',
             'channel_name': 'TiamoC',
         }},
    ],
}

_lock = threading.Lock()
_cache = None

# Set to False in tests / when the property hub must not be touched.
_mirror_to_properties = True

#: 属性镜像复用的 ``Properties('Notifications')`` 客户端（None = 还没建）。
#: 每次 ``save_config`` 都新建一个会留下一个后台线程 + 两三个 ZMQ socket；
#: 复用同一个实例即可避免这种泄漏（解释器退出时 properties 模块的 atexit
#: 处理器会统一安全关闭它）。
_props_mirror = None
_props_mirror_lock = threading.Lock()


def _deepcopy(obj):
    """深拷贝（供配置缓存返回副本，避免外部改到内部状态）。

    Args:
        obj: 任意对象。

    Returns:
        obj 的深拷贝。
    """
    return copy.deepcopy(obj)


def _merge_defaults(cfg):
    """把旧配置文件补齐到当前默认结构（新增字段不必用户手改 JSON）。

    Args:
        cfg (dict): 从磁盘读到的配置（可能缺少 ``gateway`` 等新段）。

    Returns:
        dict: 补齐后的配置副本。
    """
    merged = _deepcopy(cfg) if isinstance(cfg, dict) else {}
    defaults = _deepcopy(_DEFAULT_CONFIG)
    for key, value in defaults.items():
        if key == 'gateway':
            gateway = merged.get('gateway')
            base = value
            if isinstance(gateway, dict):
                base.update({k: v for k, v in gateway.items() if v is not None})
            merged['gateway'] = base
        elif key not in merged or merged[key] in (None, ''):
            merged[key] = value
    return merged


def get_config():
    """返回当前通知配置（缓存副本，改动不影响内部状态）。

    Returns:
        dict: 通知配置；读取失败时回退到默认配置。
    """
    global _cache
    with _lock:
        if _cache is None:
            cfg = None
            try:
                # read_json：Windows 上 os.replace 期间读方 open 可能短暂
                # PermissionError（文件共享冲突），这里短暂重试；损坏 JSON
                # 仍按原逻辑回退默认配置。
                cfg = jsonutil.read_json(_CONFIG_FILE, default=None)
            except Exception as exc:
                _log.warning('failed to read %s: %s', _CONFIG_FILE, exc)
            if not isinstance(cfg, dict):
                cfg = _deepcopy(_DEFAULT_CONFIG)
            _cache = _merge_defaults(cfg)
        return _deepcopy(_cache)


def _reload():
    """丢弃缓存配置，使下一次 get_config() 重新读文件。"""
    global _cache
    with _lock:
        _cache = None


def _atomic_write_json(path, payload):
    """原子地写一个 JSON 文件（同目录临时文件 + fsync + ``os.replace``）。

    实现与重试策略委托给 :func:`quatm.servers.jsonutil.atomic_write_json`：
    POSIX 上 ``os.replace`` 原子，读者要么看到旧文件要么看到完整新文件；
    Windows 上目标文件被读取方短暂打开时 ``os.replace`` 会报
    ``PermissionError``，共享实现会退避重试，之后才抛（调用方记日志、保留旧
    配置）。临时文件放在目标**同目录**，跨目录/跨盘 replace 不保证原子。

    Args:
        path (str): 目标文件路径。
        payload (dict): 要写入的内容（须可 JSON 序列化）。

    Raises:
        OSError|TypeError|ValueError: 最终仍失败时原样抛出；目标文件保持原样。
    """
    jsonutil.atomic_write_json(path, payload)


def _properties_client():
    """返回用于镜像的 ``Properties('Notifications')`` 实例（进程内复用）。

    复用的理由：``Properties`` 是有后台线程和 ZMQ socket 的客户端，每次
    save 都 new 一个（旧实现）会泄漏线程/描述符；同名实例虽然会被
    properties 模块自动去重关闭，但新的实例仍会立刻重建一整套连接，代价高。
    实例创建失败时异常向上抛，由 :func:`save_config` 当作“镜像失败”忽略。

    Returns:
        Properties: 可用于 ``set()`` 的客户端。
    """
    global _props_mirror
    with _props_mirror_lock:
        props = _props_mirror
        closed = getattr(props, '_closed', None) if props is not None else None
        if props is None or (closed is not None and closed.is_set()):
            from quatm.servers.properties import Properties
            props = Properties('Notifications', enable_monitors=False)
            _props_mirror = props
        return props


#: 敏感键的识别片段（对键名做「小写 + 只留字母数字」归一化后再匹配）。
#: 例：``token`` / ``API_KEY`` / ``client_secret`` / ``app-token`` 都会命中。
_SENSITIVE_KEY_PARTS = (
    'token', 'password', 'passwd', 'secret', 'apikey', 'credential',
    'privatekey', 'authorization', 'bearer',
)

#: 脱敏后的占位值。刻意用可读的非空串：属性树里看得出“这里有凭据但被隐藏”，
#: 同时又不会把任何片段带进被 git 跟踪的 properties.json。
_MASK = '***'

#: 某些后端的 **URL 本身就是凭据**（webhook 机器人 URL、Bark/及时通知的
#: server+device key）。按服务类型把对应字段也掩掉。
_CREDENTIAL_URL_FIELDS = {
    'webhook': ('url',),
    'timelynotify': ('base_url',),
    'bark': ('base_url',),
}


def _is_sensitive_key(key):
    """判断一个配置键名是否属于“凭据类”（大小写/分隔符不敏感）。

    Args:
        key: 键名（任意类型，内部按字符串处理）。

    Returns:
        bool: 键名归一化后包含任一敏感片段则为 True。
    """
    normalized = ''.join(ch for ch in str(key).lower() if ch.isalnum())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _mask_sensitive(obj):
    """递归复制 ``obj``，并把敏感键的值替换成 :data:`_MASK`。

    Args:
        obj: 任意 JSON 兼容对象（dict/list/标量）。

    Returns:
        dict|list|object: 脱敏后的新对象；``obj`` 本身绝不被修改。
    """
    if isinstance(obj, dict):
        return {key: (_MASK if _is_sensitive_key(key)
                      else _mask_sensitive(value))
                for key, value in obj.items()}
    if isinstance(obj, list):
        return [_mask_sensitive(item) for item in obj]
    return obj


def _sanitize_services(services):
    """把推送服务列表脱敏，供镜像进属性树使用（纯函数，不修改入参）。

    为什么要脱敏：``save_config`` 的镜像会经 Propertylogger 落盘到**被 git
    跟踪的** ``configuration/properties/properties.json``。若原样镜像，用户
    填的 token/password 会经这条路径重新进入版本库。因此镜像前统一脱敏：

    * 递归把敏感键（``token`` / ``password`` / ``apikey`` / ``api_key`` /
      ``secret`` / ``credential`` …，大小写不敏感）的值替换成 ``'***'``；
    * 某些后端的 URL 本身就是凭据（``webhook`` 的 ``url``、``timelynotify``/
      ``bark`` 的 ``base_url``），按 ``type`` 一并掩掉；
    * 其它字段与整体结构保持不变（属性树里仍能看到服务名/类型/启用状态等）。

    磁盘上的 ``notifications.json`` **不脱敏**（投递需要真 token），本函数只
    用于属性镜像。

    Args:
        services (list): 配置里的 ``services`` 列表。

    Returns:
        list[dict]: 脱敏后的新列表；入参不是 list 时返回空列表。
    """
    if not isinstance(services, list):
        return []
    sanitized = []
    for service in services:
        if not isinstance(service, dict):
            sanitized.append(_mask_sensitive(service))
            continue
        clean = _mask_sensitive(service)
        service_type = str(clean.get('type', '')).lower()
        config = clean.get('config')
        if isinstance(config, dict):
            for field in _CREDENTIAL_URL_FIELDS.get(service_type, ()):
                if config.get(field):
                    config[field] = _MASK
        sanitized.append(clean)
    return sanitized


def save_config(cfg):
    """把 ``cfg`` 原子地持久化到 notifications.json 并镜像进属性树。

    写文件走 :func:`_atomic_write_json`：磁盘上永远只有“旧配置”或“完整新
    配置”两种状态，热加载方不会读到半截 JSON。写失败时旧文件保持不变，
    缓存也不更新（下次 ``get_config()`` 读到的仍是旧配置，与磁盘一致）。

    Args:
        cfg (dict): 完整通知配置（结构见 :data:`_DEFAULT_CONFIG`）。

    Returns:
        None。
    """
    global _cache
    try:
        _atomic_write_json(_CONFIG_FILE, cfg)
    except Exception as exc:  # noqa: BLE001 - 写配置失败只记日志
        # 只记录文件/异常本身，绝不把 cfg 的内容（含 token）写进日志。
        _log.warning('failed to write %s: %s', _CONFIG_FILE, exc)
        return
    with _lock:
        _cache = _deepcopy(cfg)
    # mirror into the unified property system (best effort; the property hub
    # may be unavailable in standalone scripts -> short timeout + fallback)
    if not _mirror_to_properties:
        return
    try:
        props = _properties_client()
        props.set('push_enabled', bool(cfg.get('push_enabled', False)))
        props.set('default_service', cfg.get('default_service', ''))
        # 脱敏后再镜像：属性树会经 Propertylogger 落盘到被 git 跟踪的
        # configuration/properties/properties.json，绝不能把 token 带进去。
        props.set('push_services', _sanitize_services(cfg.get('services', [])))
    except Exception as exc:  # noqa: BLE001 - 属性镜像只是诊断副本，失败不影响落盘
        # 同样不打印配置内容（服务条目里有 token）；只记异常类型/文本。
        _log.debug('failed to mirror notifications into properties: %s', exc)


def is_push_enabled():
    """全局总开关（为 False 时所有推送为空操作）。

    Returns:
        bool: 是否启用推送。
    """
    return bool(get_config().get('push_enabled', False))


def get_services():
    """返回配置里的推送服务列表。

    Returns:
        list[dict]: 服务条目（name/type/enabled/config 等）。
    """
    return get_config().get('services', [])


def get_service(name):
    """按名字查一个推送服务（返回其副本）。

    Args:
        name (str): 服务名。

    Returns:
        dict|None: 服务配置副本；找不到返回 None。
    """
    for svc in get_services():
        if svc.get('name') == name:
            return _deepcopy(svc)
    return None


def _mattermost_service():
    """返回配置中（优先已启用）的 mattermost 服务；没有则返回 None。"""
    services = get_services()
    for svc in services:
        if (svc.get('type') == 'mattermost' and svc.get('enabled', False)):
            return svc
    for svc in services:                     # fall back to any mattermost entry
        if svc.get('type') == 'mattermost':
            return svc
    return None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _post_mattermost(cfg, msg, file_path=None, attachments=None):
    """经 Mattermost 服务把消息发出（后端实现，内部用）。

    Args:
        cfg (dict): 服务配置（url/port/scheme/token/team/channel）。
        msg (str): 消息文本。
        file_path (str|None): 可选附件文件路径。
        attachments (dict|None): 可选 Mattermost 附件结构。
    """
    # 懒导入：mattermostdriver 不是必装依赖，未配置 Mattermost 的机器上
    # 不应因为 import 失败而拖垮整个推送模块（旧实现漏了这行 import，
    # 一旦真的调用就是 NameError）。
    from mattermostdriver import Driver

    message = datetime.now().strftime('%a, %d.%m., %H:%M - ') + msg
    options = {
        'url': cfg.get('url', 'qsim-mattermost.uni-freiburg.de'),
        'port': int(cfg.get('port', 443)),
        'scheme': cfg.get('scheme', 'https'),
        'token': cfg.get('token', ''),
    }
    driver = Driver(options=options)
    driver.login()
    team = cfg.get('team_name', 'νTiamo')
    channel = cfg.get('channel_name', 'TiamoC')
    channel_id = driver.channels.get_channel_by_name_and_team_name(team, channel)['id']
    post_options = {'channel_id': channel_id, 'message': message}
    if file_path is not None:
        form_data = {
            'channel_id': ('', channel_id),
            'client_ids': ('', 'id_for_the_file'),
            'files': (file_path, open(file_path, 'rb')),
        }
        pp = driver.files.upload_file(channel_id, form_data)
        post_options['file_ids'] = [pp['file_infos'][0]['id']]
    if attachments is not None:
        post_options['props'] = {'attachments': attachments}
    driver.posts.create_post(post_options)
    driver.logout()


def _timeout(cfg):
    """取服务配置里的 HTTP 超时（秒），缺失或非法时退回 5 秒。

    Args:
        cfg (dict): 服务配置。

    Returns:
        float: 超时秒数，最小 1 秒（避免把网关投递线程卡死太久）。
    """
    try:
        return max(1.0, float(cfg.get('timeout', 5)))
    except (TypeError, ValueError):
        return 5.0


def _base_url(cfg):
    """把 ``url``（可带 scheme/port）组合成干净的基地址。

    Args:
        cfg (dict): 服务配置，支持 ``url`` / ``base_url``、``scheme``、``port``。

    Returns:
        str: 去掉末尾斜杠的基地址。

    Raises:
        ValueError: 没有配置任何地址时抛出。
    """
    url = str(cfg.get('url') or cfg.get('base_url') or '').strip()
    if not url:
        raise ValueError('push service has no url')
    if '://' not in url:
        scheme = str(cfg.get('scheme') or 'https').strip() or 'https'
        url = '%s://%s' % (scheme, url)
    port = cfg.get('port')
    if port:
        parts = urlsplit(url)
        if parts.port is None:
            netloc = parts.hostname or ''
            if parts.username:
                netloc = '%s@%s' % (parts.username, netloc)
            url = '%s://%s:%s%s' % (parts.scheme, netloc, int(port), parts.path)
    return url.rstrip('/')


def _check_response(resp, what):
    """检查 HTTP 响应，非 2xx 时抛出带响应片段的可读异常。

    Args:
        resp (requests.Response): 响应对象。
        what (str): 服务类型名（用于错误信息，如 ``'gotify'``）。

    Raises:
        RuntimeError: 状态码 >= 400 时抛出。
    """
    if resp.status_code >= 400:
        raise RuntimeError('%s returned HTTP %s: %s'
                           % (what, resp.status_code,
                              (resp.text or '')[:200]))


def _post_webhook(cfg, msg, title=None):
    """通用 JSON webhook 后端（覆盖微信/Slack/钉钉类机器人）。

    Args:
        cfg (dict): 服务配置（url/timeout/extra/with_title）。
        msg (str): 消息文本。
        title (str|None): 可选标题；``with_title`` 为真时作为独立字段发送。
    """
    url = cfg.get('url', '')
    if not url:
        raise ValueError('webhook service has no url')
    payload = {'text': ('%s\n%s' % (title, msg)) if title else msg}
    if title and cfg.get('with_title'):
        payload['title'] = title
    extra = cfg.get('extra')
    if isinstance(extra, dict):
        payload.update(extra)
    resp = requests.post(url, json=payload, timeout=_timeout(cfg))
    _check_response(resp, 'webhook')


def _post_gotify(cfg, title, body, level='info', extras=None):
    """向 Gotify 服务端推送一条消息（``POST /message`` + app token）。

    Gotify 的 ``priority`` 决定客户端是否响铃，因此没显式配置时按级别映射：
    debug=0 / info=2 / warning=5 / error=8。

    Args:
        cfg (dict): 服务配置（url/token/priority/extras/timeout）。
        title (str|None): 消息标题。
        body (str): 消息正文（Gotify 支持 Markdown）。
        level (str): quatm 级别，用于推导默认 priority。
        extras (dict|None): Gotify extras（键名形如 ``quatm::meta``）。
    """
    base = _base_url(cfg)
    url = base if base.endswith('/message') else base + '/message'
    token = str(cfg.get('token') or '').strip()
    if not token:
        raise ValueError('gotify service has no app token')
    payload = {'title': title or '', 'message': body or ''}
    priority = cfg.get('priority')
    if priority in (None, ''):
        priority = {'debug': 0, 'info': 2, 'warning': 5, 'error': 8}.get(
            str(level).lower(), 2)
    try:
        payload['priority'] = int(priority)
    except (TypeError, ValueError):
        payload['priority'] = 2
    merged = dict(extras) if isinstance(extras, dict) else {}
    if isinstance(cfg.get('extras'), dict):
        merged.update(cfg['extras'])
    if merged:
        payload['extras'] = merged
    resp = requests.post(url, json=payload, timeout=_timeout(cfg),
                         headers={'X-Gotify-Key': token})
    _check_response(resp, 'gotify')


def _post_timelynotify(cfg, title, body, level='info', extras=None):
    """按 Bark/及时通知的 V1 路径格式推送（``/<title>/<body>?params``）。

    ``base_url`` 必须已经包含 device key（例如
    ``https://timelynotify.oicode.cn/vdHxG7K8VxsajJaKuXEkcj``）。标题与正文
    逐段 URL 编码，其余展示参数（icon/group/sound/ttl/level/copy/url）走
    query，与官方文档、短信转发器、Bark 客户端保持同一套约定。

    Args:
        cfg (dict): 服务配置（base_url/icon/group/sound/ttl/level/timeout）。
        title (str|None): 通知标题。
        body (str): 通知正文。
        level (str): quatm 级别；``level_from_severity`` 为真时映射为
            active/passive。
        extras (dict|None): 保留参数（V1 路径没有等价字段，忽略）。
    """
    base = _base_url(cfg)
    title = title or ''
    body = body or ''
    if not title and not body:
        raise ValueError('timelynotify push needs a title or a body')
    path = ''
    if title and body:
        path = '/%s/%s' % (quote(title, safe=''), quote(body, safe=''))
    elif body:
        path = '/%s' % quote(body, safe='')
    else:
        path = '/%s' % quote(title, safe='')
    params = {}
    for key in ('icon', 'group', 'sound', 'level', 'url', 'copy', 'id'):
        value = cfg.get(key)
        if value not in (None, ''):
            params[key] = value
    ttl = cfg.get('ttl')
    if ttl not in (None, ''):
        params['ttl'] = ttl
    if cfg.get('level_from_severity') and 'level' not in params:
        params['level'] = 'passive' if str(level).lower() in ('debug', 'info') \
            else 'active'
    resp = requests.get(base + path, params=params or None, timeout=_timeout(cfg))
    _check_response(resp, 'timelynotify')
    try:
        data = resp.json()
    except ValueError:
        return
    if isinstance(data, dict) and isinstance(data.get('code'), int) \
            and data['code'] >= 400:
        raise RuntimeError('timelynotify code %s: %s'
                           % (data.get('code'), data.get('message', '')))


#: 有实现的后端类型（未知类型不参与入队，避免永远失败的死信）。
KNOWN_TYPES = ('mattermost', 'webhook', 'gotify', 'timelynotify', 'bark')


def _dispatch(svc, msg, title=None, level='info', file_path=None,
              attachments=None, channel=None, overrides=None, extras=None):
    """经单个服务同步发送一条消息（失败抛异常，供测试与网关投递线程使用）。

    Args:
        svc (dict): 服务条目（type/config 等）。
        msg (str): 消息正文。
        title (str|None): 消息标题（Mattermost 会拼在正文前一行）。
        level (str): quatm 级别，用于推导 Gotify priority 等字段。
        file_path (str|None): 可选附件（仅 mattermost）。
        attachments (dict|None): 可选 Mattermost 附件。
        channel (str|None): 可选频道覆盖（mattermost）。
        overrides (dict|None): 可选配置覆盖。
        extras (dict|None): 透传给支持 extras 的后端（gotify）。

    Raises:
        ValueError: 服务类型未知或缺少必需配置。
    """
    stype = (svc.get('type') or '').lower()
    cfg = dict(svc.get('config') or {})
    if isinstance(overrides, dict):
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    if channel:
        cfg['channel_name'] = channel
    if stype == 'mattermost':
        text = ('%s\n%s' % (title, msg)) if title else msg
        _post_mattermost(cfg, text, file_path=file_path, attachments=attachments)
    elif stype == 'webhook':
        _post_webhook(cfg, msg, title=title)
    elif stype == 'gotify':
        _post_gotify(cfg, title, msg, level=level, extras=extras)
    elif stype in ('timelynotify', 'bark'):
        _post_timelynotify(cfg, title, msg, level=level, extras=extras)
    else:
        raise ValueError('unknown push service type %r (%s)'
                         % (stype, svc.get('name')))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _matching_sinks(level='info', service=None, service_type=None):
    """选出这一条消息应该投给的 sink（已启用 + 类型已知 + 过服务质量阈值）。

    Args:
        level (str): 消息级别，用于比较服务条目上的 ``min_level``。
        service (str|None): 只要这个名字的服务（None=全部）。
        service_type (str|None): 只要这个类型的服务（None=全部）。

    Returns:
        list[dict]: 服务配置副本（名称唯一，保持配置文件顺序）。
    """
    rank = level_rank(level)
    matched = []
    seen = set()
    for svc in get_services():
        name = str(svc.get('name') or '')
        stype = str(svc.get('type') or '').lower()
        if not name or name in seen:
            continue
        if not svc.get('enabled', False) or stype not in KNOWN_TYPES:
            continue
        if service is not None and name != service:
            continue
        if service_type is not None and stype != service_type:
            continue
        if rank < level_rank(svc.get('min_level') or 'debug'):
            continue
        seen.add(name)
        matched.append(_deepcopy(svc))
    return matched


def push_message(msg, file_path=None, attachments=None, channel=None,
                 service=None, service_type=None, config_overrides=None,
                 title=None, level='info', extras=None, source='api'):
    """把一条通知放进推送 outbox，**立即返回且不做任何网络请求**。

    事件先写 ``configuration/push/push.db``，由常驻
    :mod:`quatm.servers.pushgateway` 负责投递、重试与限流。因此网络不可用、
    网关未运行、甚至推送库暂时被锁时，本函数都不会抛异常——实验线程绝不会
    被推送拖住（旧实现是同步 ``requests``，一个超时就能毁掉时序）。

    Args:
        msg (str): 要推送的正文。
        file_path (str|None): 可选附件路径（仅 mattermost）。
        attachments (dict|None): 可选 Mattermost 附件结构。
        channel (str|None): 覆盖频道/目标（mattermost）。
        service (str|None): 只走该名字的服务（None=全部已启用）。
        service_type (str|None): 只走该类型的服务，如 ``'gotify'``。
        config_overrides (dict|None): 后端专用配置覆盖。
        title (str|None): 推送标题；None 时按级别自动生成。
        level (str): 级别（debug/info/warning/error），决定默认 priority。
        extras (dict|None): 随消息发出的结构化字段（gotify extras）。
        source (str): 事件来源标签（一般是调用方脚本名）。

    Returns:
        list[str]: 实际排队的 sink 名；推送关闭或没有匹配 sink 时为空列表。
    """
    if not is_push_enabled():
        return []
    sinks = _matching_sinks(level=level, service=service,
                            service_type=service_type)
    if not sinks:
        return []
    options = {}
    if file_path is not None:
        options['file_path'] = file_path
    if attachments is not None:
        options['attachments'] = attachments
    if channel is not None:
        options['channel'] = channel
    if config_overrides is not None:
        options['overrides'] = config_overrides
    if title is None:
        title = '%s - %s' % (str(level).upper(), source or 'quatm')
    try:
        get_store().add_event(level=level, source=source or 'api', title=title,
                              body=msg, extras=extras,
                              options=options or None,
                              sinks=[svc['name'] for svc in sinks])
    except Exception as exc:  # noqa: BLE001 - 入队失败也不能打断实验
        _log.warning('failed to enqueue push message: %s', exc)
        return []
    return [svc['name'] for svc in sinks]


def post_to_notification(msg, file_path=None, team_name=None, channel_name=None,
                       token=None, attachments=None, level='info', title=None):
    """向后兼容的 Mattermost 推送入口（现在同样只入队，不阻塞调用方）。

    保留旧签名以便既有调用方继续工作；连接参数默认取 notifications.json 里
    （已启用）的 mattermost 服务，显式传入的 ``team_name`` / ``channel_name``
    / ``token`` 会覆盖配置。推送全局关闭或未配置 mattermost 服务时为空操作；
    从不抛异常。

    Args:
        msg (str): 消息文本。
        file_path (str|None): 可选附件。
        team_name (str|None): 覆盖团队名。
        channel_name (str|None): 覆盖频道名（同时作为 channel 传给后端）。
        token (str|None): 覆盖令牌。
        attachments (dict|None): 可选 Mattermost 附件。
        level (str): 级别（决定默认 priority / 标题）。
        title (str|None): 可选标题。

    Returns:
        list[str]: 实际排队的 sink 名。
    """
    overrides = {}
    if team_name is not None:
        overrides['team_name'] = team_name
    if token is not None:
        overrides['token'] = token
    return push_message(msg, file_path=file_path, attachments=attachments,
                        channel=channel_name, service_type='mattermost',
                        config_overrides=overrides, level=level, title=title,
                        source='mattermost.py')


def test_service(name, level='info'):
    """向某个推送服务**同步**发一条测试消息（唯一的阻塞入口）。

    用户在编辑器里点“测试”需要立刻看到结果，因此这里直接走
    :func:`_dispatch`，不经过 outbox。

    Args:
        name (str): 服务名。
        level (str): 测试消息的级别。

    Returns:
        tuple[bool, str]: ``(是否成功, 空串或错误信息)``。
    """
    if not is_push_enabled():
        return False, 'push is disabled globally'
    svc = get_service(name)
    if svc is None:
        return False, 'no push service named %r' % name
    try:
        _dispatch(svc, 'Test push from the notifications editor',
                  title='quatm push test', level=level)
        return True, ''
    except Exception as exc:  # noqa: BLE001 - 测试入口只回报错误文本
        return False, str(exc)


# ---------------------------------------------------------------------------
# Outbox / gateway helpers
# ---------------------------------------------------------------------------

_stores = {}
_stores_lock = threading.Lock()


def store_path():
    """返回当前项目的推送库路径（跟随 notifications.json 所在的目录）。

    Returns:
        str: ``<configuration>/push/push.db`` 的绝对路径。
    """
    return os.path.join(os.path.dirname(_CONFIG_FILE), 'push', 'push.db')


def get_store():
    """返回当前项目的 outbox 单例（按路径缓存，线程安全）。

    Returns:
        PushStore: 已打开的推送库。
    """
    path = os.path.abspath(store_path())
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = PushStore(path)
            _stores[path] = store
        return store


def get_gateway_config():
    """返回网关策略配置（已补齐默认值，可直接用于过滤/去重/限流）。

    Returns:
        dict: ``gateway`` 段配置副本。
    """
    cfg = get_config().get('gateway')
    if not isinstance(cfg, dict):
        return _deepcopy(_DEFAULT_CONFIG['gateway'])
    return _deepcopy(cfg)


def gateway_alive():
    """判断常驻推送网关是否在运行（依据 push 目录里的心跳文件）。

    Returns:
        dict|None: 心跳内容（pid/host/age），未运行时为 None。
    """
    return pushstore.read_heartbeat(os.path.dirname(store_path()))


def push_status():
    """返回 outbox 计数与网关运行状态（供 GUI/Web 的状态栏显示）。

    Returns:
        dict: ``pending/sent/failed/skipped/events`` + ``gateway`` 布尔值。
    """
    stats = get_store().stats()
    heartbeat = gateway_alive()
    stats['gateway'] = bool(heartbeat)
    stats['gateway_pid'] = (heartbeat or {}).get('pid')
    return stats


def retry_failed():
    """把 ``failed`` 的投递重新排队（界面上的“重投失败”按钮）。

    Returns:
        int: 被重置的投递行数。
    """
    return get_store().retry_failed()


def recent_pushes(limit=50):
    """返回最近的推送事件及各 sink 的投递状态（界面“推送历史”用）。

    Args:
        limit (int): 最多返回多少条事件。

    Returns:
        list[dict]: 事件列表（含 ``deliveries`` 子列表）。
    """
    return get_store().recent(limit)
