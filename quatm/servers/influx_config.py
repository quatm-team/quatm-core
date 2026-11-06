"""Shared InfluxDB connection configuration for Quatm services.

Configuration is resolved from the ``Servers/InfluxDB`` section of
``configuration/configfile/configfile.json``, then from ``INFLUX_*``
environment variables. Missing credentials raise :class:`InfluxDBConfigError`;
secrets are never included in error messages.
"""

import os
import sys

from quatm.servers import send_error
from quatm.servers.configreader import ConfigReader
from quatm.utils.i18n import tr

#: 连接参数 → 环境变量名（与 configfile ``Servers/InfluxDB`` 段的键一一对应）。
_ENV_KEYS = {
    'url': 'INFLUX_URL',
    'token': 'INFLUX_TOKEN',
    'org': 'INFLUX_ORG',
    'host': 'INFLUX_HOST',
    'port': 'INFLUX_PORT',
}

#: 客户端（写点/查询）需要的参数。
CLIENT_KEYS = ('url', 'token', 'org')

#: RPC 服务端需要的参数（还要监听 host/port）。
SERVER_KEYS = ('url', 'token', 'org', 'host', 'port')


class InfluxDBConfigError(RuntimeError):
    """缺少 InfluxDB 连接配置（错误信息只列缺失项，绝不包含 token 值）。"""


def _config_error_text(missing):
    """构造缺配置的可读错误文本。

    只出现环境变量名与配置路径，**不含任何凭据值**。

    Args:
        missing (list[str]): 缺失的键名（url/token/org/host/port 的子集）。

    Returns:
        str: 多行错误说明（已过 :func:`quatm.utils.i18n.tr`）。
    """
    env_names = ', '.join(_ENV_KEYS[key] for key in missing)
    keys = ', '.join(missing)
    return '\n'.join((
        tr('[InfluxDB] Missing connection settings (configfile '
           'Servers/InfluxDB or environment): {keys}', keys=env_names),
        tr('[InfluxDB] No credentials are bundled with quatm. Put '
           '{fields} in configuration/configfile/configfile.json under '
           'Servers -> InfluxDB or provide the matching INFLUX_* environment '
           'variables.', fields=keys),
        tr('[InfluxDB] The token is never logged. Fill in the settings and '
           'retry.'),
    ))


def resolve_influx_config(required=CLIENT_KEYS):
    """解析 InfluxDB 连接配置：configfile → 环境变量 → 缺失即报错。

    参数按**逐项**优先级合并：配置文件里有值的键优先，缺的键用同名环境变量
    补。**没有任何内置默认值**。

    Args:
        required (tuple[str, ...]): 必填的键；默认客户端三项，服务端用
            :data:`SERVER_KEYS`。

    Returns:
        dict: 解析出的连接配置（至少含 ``required`` 里的键）。

    Raises:
        InfluxDBConfigError: 缺少必填参数（错误信息只列缺失的键名，不含 token 值）。
    """
    file_conf = {}
    try:
        file_conf = (ConfigReader.getConfiguration()
                     .get('Servers', {})
                     .get('InfluxDB', {}))
    except Exception:  # noqa: BLE001 - 没有 configfile 时按“未配置”处理
        file_conf = {}
    if not isinstance(file_conf, dict):
        file_conf = {}

    conf = {}
    for key in tuple(required):
        value = file_conf.get(key)
        value = str(value).strip() if value is not None else ''
        if not value:
            value = os.environ.get(_ENV_KEYS[key], '').strip()
        conf[key] = value

    missing = [key for key in required if not conf[key]]
    if missing:
        raise InfluxDBConfigError(_config_error_text(missing))
    return conf


def report_influx_config_error(error):
    """把缺配置错误打印到 stderr 并发一条 ``send_error``（消息不含 token）。

    Args:
        error (InfluxDBConfigError | str): 缺配置错误（或其可读文本）。
    """
    message = str(error)
    print(message, file=sys.stderr)
    try:
        send_error(message)
    except Exception:  # noqa: BLE001 - 消息中枢不可用时保留本地报错即可
        pass
