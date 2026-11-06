"""quatm 的 Mattermost 集成。

``post_to_notification`` 为向后兼容保留：现在统一走 ``quatm.servers.notifications``
里“可选”的推送层（由 ``configuration/notifications.json`` 配置），因此推送
关闭或未配置 mattermost 服务时为空操作，且从不抛异常。

``MattermostReader`` 行为不变（监听 mattermost websocket 命令）；其连接参数
默认取已配置的 mattermost 服务。
"""

import json

from quatm.servers.notifications import post_to_notification, _mattermost_service

__all__ = ['post_to_notification', 'MattermostReader']


class MattermostReader:
    """监听 Mattermost websocket 命令并转发给 parent 的辅助类。

    连接参数默认取已配置的 mattermost 服务（``notifications.json``）；
    收到消息 ``'terminate loop'`` 时调用 ``parent.terminate``，收到
    ``'run loop'`` 时调用 ``parent.run_loop``。

    Args:
        parent: 拥有 run_loop/terminate 方法的宿主对象（如 Looper）。
        team_name (str|None): 团队名（默认取配置）。
        channel_name (str|None): 频道名（默认取配置）。
        token (str|None): 访问令牌（默认取配置）。
    """

    def __init__(self, parent, team_name=None, channel_name=None, token=None):
        """建立 Mattermost 连接并挂起消息处理。

        Args:
            parent: 宿主对象。
            team_name (str|None): 团队名。
            channel_name (str|None): 频道名。
            token (str|None): 令牌。
        """
        self.parent = parent
        cfg = {}
        svc = _mattermost_service()
        if svc is not None:
            cfg = dict(svc.get('config') or {})
        team_name = team_name or cfg.get('team_name', 'νTiamo')
        channel_name = channel_name or cfg.get('channel_name', 'TiamoC')
        token = token or cfg.get('token', '')

        from mattermostdriver import Driver
        options = {
            "url": cfg.get('url', 'qsim-mattermost.uni-freiburg.de'),
            "port": int(cfg.get('port', 443)),
            "scheme": cfg.get('scheme', 'https'),
            "token": token,
        }
        self.driver = Driver(options=options)
        self.driver.login()
        channel_id = self.driver.channels.get_channel_by_name_and_team_name(
            team_name, channel_name)["id"]

        async def handle_message(post, **kwargs):
            message = json.loads(post)
            print(message)
            if message == 'terminate loop':
                self.parent.terminate(reason='mattermost input')
            if message == 'run loop':
                self.parent.run_loop()

        self.driver.init_websocket(handle_message)
