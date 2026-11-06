"""日期时间换算工具（自 ``quatm/physics/atom_ion/feshbach_plotter.py`` 拆出，
原属 ``pytiamo`` 包）。

quatm 项目只用到该模块中的这两个函数；放在这里可避免对物理辅助模块的
硬依赖。

* ``unix_to_dateTime`` - unix 秒 → Europe/Berlin 时区的 aware datetime
* ``dateTime_to_unix`` - datetime → unix 秒（UTC）
"""

import datetime

import numpy as np
import pytz
from dateutil.tz import tzlocal

__all__ = ['unix_to_dateTime', 'dateTime_to_unix']


def dateTime_to_unix(datetimes):
    """把 datetime（本地时区）换算为 unix 秒（UTC）。

    接受单个 datetime 或列表/元组/ndarray；单值会被包装成列表处理。

    Args:
        datetimes: 单个 datetime 或 datetime 序列。

    Returns:
        list[float]: 对应的 unix 时间戳（秒）。
    """
    # datetime.datetime 不是 numpy 标量：原先用 np.isscalar 判断会把单个
    # datetime 误当可迭代，导致 TypeError（'datetime.datetime' object is
    # not iterable）。显式识别单值并包装成列表；列表/元组/ndarray 输入
    # 行为不变（可安全传 [dt] 或 dt）。
    if isinstance(datetimes, datetime.datetime):
        datetimes = [datetimes]
    return [date.replace(tzinfo=tzlocal()).astimezone(tz=datetime.timezone.utc).timestamp()
            for date in datetimes]


def unix_to_dateTime(unix):
    """把 unix 秒换算为 Europe/Berlin 时区的 aware datetime。

    接受标量或序列；标量会被包装成列表。直接按 UTC 换算后转到 Berlin 时区
    （不做脆弱的 ISO 字符串往返）。

    Args:
        unix: unix 时间戳标量或序列（秒）。

    Returns:
        list[datetime.datetime]: Berlin 时区的 datetime 列表。
    """
    if np.isscalar(unix):
        unix = [unix]
    # Convert unix seconds directly to Berlin time (no fragile datetime -> ISO
    # string -> strptime round-trip: isoformat() omits '.%f' for whole-second
    # timestamps, which broke the old parser on e.g. matplotlib epoch ticks).
    return [datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).astimezone(
        pytz.timezone('Europe/Berlin')) for t in unix]
