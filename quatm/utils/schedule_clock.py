# -*- coding: utf-8 -*-
"""调度去重的纯函数（slot key / 触发判定）——给 Looper 等长跑调度用。

背景：Looper 的“自动终止 / 自动启动 / 定时 H5 保存”用 20 s tick + ``datetime.now()``
轮询，比一次长 QTimer 稳；但系统时钟被 NTP 回拨、用户改时间或夏令时（DST）切换时，
同一“日历时间点”可能被判定两次（重复执行），或提前执行。本模块提供**纯函数**
把“一次触发”抽象成 **slot key**：

* :func:`slot_key`：``kind`` + 目标时刻 → 稳定字符串，例如
  ``'terminate:2026-11-05T03:00'``；分钟精度（``isoformat(timespec='minutes')``），
  所以同一分钟内重复计算得到同一个 key（DST 回拨导致同一本地时间出现两次时也
  仍然相同 → 只触发一次）。
* :func:`should_fire`：``last_key`` 与当前 target 的 key 不同、且 ``now`` 已到
  target → True（“该 slot 尚未触发且已到点”）。触发后是否持久化由调用方负责。
* :func:`dedupe_key`：触发时调用方要写入的 key（``target`` 缺省用 ``now``）。
* :func:`parse_slot_key`：把 key 解析回 ``(kind, datetime)``；格式非法返回 None。
* :func:`slot_seconds`：``target - now`` 的秒数（可负），供调用方显示/判断。

约定：

* target 是本机 **naive datetime**（与 Looper 现有的 ``datetime.now()`` 一致）；
  **不依赖时区数据库**（不做 pytz/zoneinfo 转换，DST 由操作系统给出的 naive
  本地时间体现，重复 slot 由 key 相同而自然去重）。
* 本模块只做判定，不持久化、不触发动作、不 import Qt / Properties。
* 跨零点/每年重复的 target 由调用方生成 key：不同日期 → 不同 key（正常触发）；
  同一天同一时刻的重复出现（DST 回拨）→ 同 key（只触发一次）。
"""

from datetime import datetime

#: slot key 里时间部分的格式（分钟精度：同一分钟内的多次 tick 是同一个 slot）。
SLOT_TIME_FORMAT = '%Y-%m-%dT%H:%M'


def _coerce_datetime(value):
    """把 datetime/ISO 字符串转成 datetime；无法转换返回 None。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def slot_key(kind, target):
    """生成一个触发 slot 的稳定 key（``kind`` + 分钟精度时间）。

    Args:
        kind (str): 用途（``'terminate'`` / ``'start'`` / ``'h5save'`` ...）。
        target (datetime): 本次触发对应的目标时刻（naive 本地时间）。

    Returns:
        str: 形如 ``'terminate:2026-11-05T03:00'``；target/kind 非法时返回空串
            （调用方应视为“无法去重”而不是崩溃）。
    """
    if not kind:
        return ''
    when = _coerce_datetime(target)
    if when is None:
        return ''
    return '%s:%s' % (str(kind), when.strftime(SLOT_TIME_FORMAT))


def parse_slot_key(key):
    """把 :func:`slot_key` 生成的 key 解析回 ``(kind, datetime)``。

    Args:
        key (str): slot key。

    Returns:
        tuple[str, datetime] | None: 解析成功返回 ``(kind, target)``；
        格式非法/kind 为空/时间不可解析返回 None。
    """
    if not isinstance(key, str) or ':' not in key:
        return None
    kind, _, raw = key.partition(':')
    if not kind or not raw:
        return None
    when = _coerce_datetime(raw)
    if when is None:
        return None
    return str(kind), when


def slot_seconds(now, target):
    """``target - now`` 的秒数（正=还没到，负=已过）。

    Args:
        now (datetime): 当前时刻。
        target (datetime): 目标时刻。

    Returns:
        float: 相差秒数；参数非法时返回 ``float('nan')``（调用方可用
            ``math.isnan`` 判断，不要抛异常进 GUI 定时器）。
    """
    now_dt = _coerce_datetime(now)
    target_dt = _coerce_datetime(target)
    if now_dt is None or target_dt is None:
        return float('nan')
    return (target_dt - now_dt).total_seconds()


def dedupe_key(kind, target=None, now=None):
    """返回“本次触发应持久化”的 key（供触发方写入 ``last_fired``）。

    Args:
        kind (str): 用途。
        target (datetime|None): 目标时刻；None 用 ``now``。
        now (datetime|None): 当前时刻；target 与 now 都为空时用
            ``datetime.now()``。

    Returns:
        str: slot key（无法生成时空串）。
    """
    when = target if target is not None else (now or datetime.now())
    return slot_key(kind, when)


def should_fire(last_key, kind, target, now=None):
    """当前 ``target`` 对应的 slot 是否应触发（未触发过 + 已到点）。

    同名 slot 的 key 相同，因此：

    * 已触发过（``last_key == slot_key(kind, target)``）→ False（时钟回拨/重复
      tick/DST 回拨都不会再触发一次）；
    * ``now`` 早于 target → False（“不该触发的不能提前”）；
    * 未触发且已到点 → True（由调用方执行动作并持久化新 last_key）。

    Args:
        last_key (str|None): 上次已触发的 slot key（可为空/非法 → 视为未触发）。
        kind (str): 用途。
        target (datetime): 目标时刻。
        now (datetime|None): 当前时刻；None 表示“不检查是否到点”（调用方
            已自行判断 due）。

    Returns:
        bool: 是否应触发。
    """
    key = slot_key(kind, target)
    if not key:
        return False
    if last_key == key:
        return False
    if now is not None:
        now_dt = _coerce_datetime(now)
        target_dt = _coerce_datetime(target)
        if now_dt is None or target_dt is None or now_dt < target_dt:
            return False
    return True
