"""InfluxDB 服务端：把实验状态点写入 InfluxDB，并以 RPC 服务暴露客户端。

连接参数**全部来自运行时配置**，源码里不再内置任何地址或 token（旧版曾把
真实内网地址、明文 token 与 org 硬编码在本文件里，已作为凭据泄漏删除）。
解析链（逐项解析，配置文件优先）：

1. ``configuration/configfile.json`` 的 ``Servers/InfluxDB`` 段
   （``url`` / ``token`` / ``org`` / ``host`` / ``port``）；
2. 环境变量 ``INFLUX_URL`` / ``INFLUX_TOKEN`` / ``INFLUX_ORG`` /
   ``INFLUX_HOST`` / ``INFLUX_PORT``——补配置文件里缺的项；
3. 仍缺失时给出可读错误并以非 0 退出码结束，**绝不回退到内置凭据**。

日志与错误信息只包含 host/port/bucket 等非敏感字段，任何情况下都不打印 token。
"""

import os
import sys

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import logging

from quatm.servers import send_error
from quatm.servers.rpc import simple_server_loop

from datetime import datetime
import pytz

from quatm.servers.configreader import ConfigReader
from quatm.utils.i18n import tr

logger = logging.getLogger(__name__)

# 解析器统一放在 quatm.servers.influx_config（与 clients/physics/analysis/
# monitor 共用同一套规则，避免每个使用方各写一份）。
from quatm.servers.influx_config import (  # noqa: F401
    SERVER_KEYS, InfluxDBConfigError, report_influx_config_error,
    resolve_influx_config)


def _influx_config():
    """解析服务端连接配置（configfile → 环境变量 → 缺失即报错）。

    Returns:
        dict: 含 url/token/org/host/port 的连接配置。

    Raises:
        InfluxDBConfigError: 缺少必填参数（只列缺失键名，不含 token 值）。
    """
    return resolve_influx_config(SERVER_KEYS)


class influxdb:
    """InfluxDB 客户端封装：向实验数据库写入状态点或查询历史数据。

    连接参数由 :func:`_influx_config` 解析（configfile 的 ``Servers/InfluxDB``
    段 → ``INFLUX_*`` 环境变量 → 缺失即报错），默认写入 bucket="tiamo"。

    Attributes:
        name (str): 服务名（'InfluxDB'）。
        bucket (str): 写入/查询的 bucket 名。
    """
    def __init__(self):
        """建立 InfluxDB 连接并初始化写入/查询 API。

        连接参数缺失时抛出 :class:`InfluxDBConfigError`（可读错误，绝不
        回退到内置凭据）；连接成功后打印确认信息。
        """
        self.name = 'InfluxDB'
        self.bucket = "tiamo"
        self.client = None
        conf = _influx_config()
        self.client = InfluxDBClient(url=conf['url'],
                                     token=conf['token'],
                                     org=conf['org'])
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()
        self._conf = conf
        print('client initialised successfully!')

    def __del__(self):
        """析构时关闭底层 InfluxDB 客户端连接（连接未建立时是 no-op）。"""
        if getattr(self, 'client', None) is not None:
            self.client.close()

    def data2db(self, target, field, data):
        """把一个硬件状态点写入数据库。

        Args:
            target (str): 硬件名，作为 tag "Hardware" 的值。
            field (str): 字段名。
            data: 字段值（数值/布尔等）。
        """
        p = Point("lab_status").tag("Hardware", target).field(field, data)
        self.write_api.write(bucket=self.bucket, record=p)

    def time_convert(self, timestamp, local=True):
        """把 "dd/mm/YYYY HH:MM" 字符串时间解析为带时区的时间对象。

        Args:
            timestamp (str): 形如 "%d/%m/%Y %H:%M" 的时间字符串。
            local (bool): True 返回 Europe/Berlin 本地化时间；False 返回 naive 时间。

        Returns:
            datetime.datetime: 解析后的时间对象。
        """
        berlin_tz = pytz.timezone('Europe/Berlin')
        date_naive = datetime.strptime(timestamp, "%d/%m/%Y %H:%M")
        date_aware = berlin_tz.localize(date_naive)
        if local:
            return date_aware
        else:
            return date_naive

    def query_data(self, time_start, time_stop, field, local=True):
        """按时间范围查询某个字段的历史记录。

        Args:
            time_start (str): 起始时间（"dd/mm/YYYY HH:MM"）。
            time_stop (str): 结束时间（"dd/mm/YYYY HH:MM"）。
            field (str): 要查询的字段名。
            local (bool): 是否按 Europe/Berlin 时区解析时间。

        Returns:
            list[dict]: 每条记录为 {"time": "YYYY-mm-dd HH:MM:SS", field: 值}。
        """
        time_start = self.time_convert(time_start, local)
        time_stop = self.time_convert(time_stop, local)
        query = f'from(bucket:"tiamo") |> range(start: {time_start.isoformat()}, stop: {time_stop.isoformat()}) |> filter(fn:(r) => r._field == "{field}")'
        tables = self.query_api.query(query)
        df_list = []
        for table in tables:
            for record in table.records:
                df_list.append(
                    {"time": record.get_time().strftime("%Y-%m-%d %H:%M:%S"), f"{field}": record.get_value()})
        return df_list

    def close(self):
        """关闭 InfluxDB 客户端连接，释放底层资源并打印确认信息。

        连接从未建立（或已关闭）时是 no-op。
        """
        if getattr(self, 'client', None) is not None:
            self.client.close()
            print('client closed successfully!')


def main():
    """启动 InfluxDB RPC 服务：把客户端暴露为远程调用并阻塞监听。

    连接参数由 :func:`_influx_config` 解析；缺失时打印可读错误、发一条错误
    消息并以 :class:`SystemExit`（退出码 1）结束，绝不回退到内置凭据。

    通过 :func:`quatm.servers.rpc.simple_server_loop` 将 ``influxdb`` 实例
    暴露到配置的 host/port，退出时关闭客户端。

    Raises:
        SystemExit: 连接配置缺失时以退出码 1 结束。
    """
    try:
        dev = influxdb()
    except InfluxDBConfigError as exc:
        report_influx_config_error(exc)
        raise SystemExit(1) from None
    try:
        logger.info("Sipyco InfluxDB client open and listening...")
        simple_server_loop({"Sipyco InfluxDB client": dev},
                           host=dev._conf['host'], port=dev._conf['port'])
    finally:
        dev.close()


if __name__ == '__main__':
    try:
        print('Sipyco InfluxDB Client Starting!')
        main()
    except Exception as e:
        print(e)
