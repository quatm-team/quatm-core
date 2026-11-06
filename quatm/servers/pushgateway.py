"""常驻推送网关：Message Hub → 策略过滤 → outbox → 逐 sink 投递。

为什么需要一个常驻进程
----------------------
``send_warning()`` 之类是在**实验线程**里调用的，任何同步网络请求都可能把
时序打乱（旧实现一个 5 s 超时就能毁掉一次实验）。所以推送拆成两半：

* ``quatm.servers.notifications.push_message()`` 只写 outbox（毫秒级）；
* 本模块订阅 Message Hub，按策略过滤后把事件补进 outbox，并负责真正的
  HTTP 投递、退避重试、令牌桶限流与保留期清理。

它由 ProcessManager 按 configfile 的 ``Servers/Push Gateway`` 条目拉起，
与界面无关：Browser 没开、人不在实验室，消息照样进 outbox 并发出去。

策略（``notifications.json`` 的 ``gateway`` 段）
----------------------------------------------
``min_level``          只推不低于该级别的消息（默认 ``warning``）
``source_allow/deny``  按来源文件名（支持 ``fnmatch`` 通配）过滤
``quiet_hours``        静默时段内只放过高于 ``max_level`` 的消息
``dedupe_window_s``    同 ``level|source|正文`` 在窗口内只推一条，正文补 ``(×N)``
``rate_limit_per_s``   每 sink 令牌桶；超限只推迟 ``next_try``，不算失败
``max_pending_per_sink`` 积压超过该值的新消息直接丢弃并告警（防止手机被刷爆）
``retention_days`` / ``max_events``  定期清理已终结的数据

防回环：网关自己也会 ``send_info/send_warning``（启动、投递失败提示），这些
消息同样会经过 Message Hub。默认 ``source_deny`` 含 ``pushgateway.py``，
因此不会把网关日志再推给自己。

启动方式::

    python quatm/servers/pushgateway.py 'Servers/Push Gateway'
    python -m quatm.servers.pushgateway
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


import fnmatch
import signal
import threading
import time
from datetime import datetime

from quatm.servers import notifications, pushstore
from quatm.servers import send_error, send_info, send_warning
from quatm.servers.messageclient import MessageClient
from quatm.servers.messagestore import MessageRow
from quatm.servers.pushstore import level_rank
from quatm.utils.i18n import tr

#: 网关投递循环空闲时的轮询间隔 [s]。
IDLE_SLEEP_S = 0.5
#: 每轮最多取多少条待投递行。
BATCH_SIZE = 20
#: 心跳刷新间隔 [s]（要小于 ``HEARTBEAT_STALE_S``）。
HEARTBEAT_INTERVAL_S = 2.0
#: 同类告警的最小间隔 [s]（避免失败风暴把 Message Hub 也刷爆）。
WARN_INTERVAL_S = 300.0


class _TokenBucket:
    """简单的令牌桶（每个 sink 一个实例，投递线程内使用）。

    Args:
        rate (float): 每秒补充的令牌数；``<=0`` 表示不限流。
        burst (float): 桶容量（允许的突发条数）。
    """

    def __init__(self, rate, burst):
        """初始化满桶。

        Args:
            rate (float): 每秒令牌数。
            burst (float): 桶容量，最小 1。
        """
        self.rate = max(0.0, float(rate or 0.0))
        self.burst = max(1.0, float(burst or max(1.0, self.rate)))
        self.tokens = self.burst
        self.stamp = time.time()

    def take(self):
        """尝试取一个令牌。

        Returns:
            bool: 取到为 True；需要等待为 False。
        """
        if self.rate <= 0:
            return True
        now = time.time()
        self.tokens = min(self.burst,
                          self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def wait_time(self):
        """返回“再等到下一个令牌”需要的秒数。

        Returns:
            float: 等待秒数（不限流时为 0）。
        """
        if self.rate <= 0:
            return 0.0
        return max(0.05, (1.0 - self.tokens) / self.rate)


class PushGateway:
    """把 Message Hub 上的消息按策略转成推送投递（可独立运行或嵌入测试）。

    Args:
        gateway (dict|None): 网关策略配置；None 时取 ``notifications.json``。
        store (PushStore|None): outbox；None 时用项目默认 ``push.db``。
        source (Iterable[str]|None): 消息来源；None 时订阅 Message Hub。
            传入可迭代的原始消息字符串时用于测试（耗尽即停止）。
        service_getter (callable|None): 返回服务列表的函数，默认读配置。

    Attributes:
        store (PushStore): 使用的 outbox。
        config (dict): 生效的网关策略配置。
    """

    def __init__(self, gateway=None, store=None, source=None,
                 service_getter=None):
        """保存依赖，准备好停止事件与限流桶（不启动任何线程）。

        Args:
            gateway (dict|None): 网关策略配置。
            store (PushStore|None): outbox 实例。
            source (Iterable[str]|None): 测试用消息来源。
            service_getter (callable|None): 服务列表提供者。
        """
        self.config = gateway if isinstance(gateway, dict) \
            else notifications.get_gateway_config()
        #: 配置是外部注入的（测试）：不要热加载磁盘配置、也不看全局开关。
        self._explicit_config = isinstance(gateway, dict)
        self._config_mtime = self._config_mtime_now()
        self.store = store if store is not None else notifications.get_store()
        self._source = source
        self._service_getter = service_getter or notifications.get_services
        self._stop = threading.Event()
        self._buckets = {}
        self._warn_stamps = {}
        self._next_prune = 0.0
        self.delivered_total = 0

    # ------------------------------------------------------------------ 生命周期
    @property
    def heartbeat_dir(self):
        """心跳文件所在目录（与 ``push.db`` 同级）。

        Returns:
            str: 目录绝对路径。
        """
        return os.path.dirname(getattr(self.store, 'path', '') or '.')

    def start(self):
        """在后台线程里启动网关（不阻塞调用方）。

        Returns:
            threading.Thread: 运行 :meth:`run` 的守护线程。
        """
        thread = threading.Thread(target=self.run, name='push-gateway',
                                  daemon=True)
        thread.start()
        return thread

    def stop(self):
        """请求网关停止；工作线程会在一个轮询周期内退出，可以重复调用。"""
        self._stop.set()

    def run(self):
        """阻塞运行网关：心跳线程 + 投递线程 + 消息泵，直到 :meth:`stop`。"""
        self._stop.clear()
        hb_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat_loop,
                                     args=(hb_stop,), name='push-heartbeat',
                                     daemon=True)
        worker = threading.Thread(target=self._delivery_loop,
                                  name='push-delivery', daemon=True)
        heartbeat.start()
        worker.start()
        try:
            if self._source is None:
                self._pump_zmq()
            else:
                self._pump_iterable(self._source)
        finally:
            self._stop.set()
            hb_stop.set()
            worker.join(timeout=5.0)
            heartbeat.join(timeout=3.0)
            pushstore.clear_heartbeat(self.heartbeat_dir)

    def _heartbeat_loop(self, hb_stop):
        """定时刷新心跳文件（界面据此判断网关是否在运行）。

        Args:
            hb_stop (threading.Event): 停止信号。
        """
        while not hb_stop.is_set():
            pushstore.write_heartbeat(self.heartbeat_dir, name='pushgateway',
                                      extra={'delivered': self.delivered_total})
            hb_stop.wait(HEARTBEAT_INTERVAL_S)

    # ------------------------------------------------------------------ 消息泵
    def _pump_zmq(self):
        """订阅 Message Hub 并逐条处理，直到收到停止信号。"""
        client = MessageClient('pushgateway')
        # SUB 过滤器为空时 ZMQ 收不到任何消息（默认一个都不收），必须显式
        # 订阅空串 = 全部通道；与 messagelogger 的做法一致。
        client.subscribe('')
        while not self._stop.is_set():
            if not client.has_new_data():
                self._stop.wait(0.2)
                continue
            message = client.recv()
            if message:
                self.handle_raw(message[0])

    def _pump_iterable(self, source):
        """逐条处理给定的原始消息（测试用），迭代器耗尽即停止。

        Args:
            source (Iterable[str]): 原始线格式消息。
        """
        for raw in source:
            if self._stop.is_set():
                break
            self.handle_raw(raw)
        self._stop.set()

    # ------------------------------------------------------------------ 策略
    def accept(self, row):
        """判断一条消息是否应当推送（级别/来源/静默时段）。

        Args:
            row (MessageRow): 解析后的消息。

        Returns:
            bool: 通过全部策略为 True。
        """
        cfg = self.config
        if not cfg.get('enabled', True):
            return False
        if not self._explicit_config and not notifications.is_push_enabled():
            return False
        if level_rank(row.level) < level_rank(cfg.get('min_level', 'warning')):
            return False
        source = str(row.source or '').lower()
        deny = [str(p).lower() for p in (cfg.get('source_deny') or ())]
        if any(fnmatch.fnmatch(source, pattern) for pattern in deny):
            return False
        allow = [str(p).lower() for p in (cfg.get('source_allow') or ())]
        if allow and not any(fnmatch.fnmatch(source, pattern)
                             for pattern in allow):
            return False
        return not self._in_quiet_hours(row.level)

    def _in_quiet_hours(self, level):
        """判断当前时刻是否处于静默时段（且该级别不够高、应当压掉）。

        Args:
            level (str): 消息级别。

        Returns:
            bool: 应当静默时为 True。
        """
        quiet = self.config.get('quiet_hours') or {}
        if not isinstance(quiet, dict):
            return False
        start = str(quiet.get('start') or '').strip()
        end = str(quiet.get('end') or '').strip()
        if not start or not end:
            return False
        if level_rank(level) > level_rank(quiet.get('max_level', 'info')):
            return False
        try:
            start_t = datetime.strptime(start, '%H:%M').time()
            end_t = datetime.strptime(end, '%H:%M').time()
        except ValueError:
            return False
        now = datetime.now().time()
        if start_t <= end_t:
            return start_t <= now < end_t
        return now >= start_t or now < end_t

    def _sinks_for(self, row):
        """列出这条消息要投递的 sink（过服务级 ``min_level`` 与积压上限）。

        Args:
            row (MessageRow): 消息。

        Returns:
            list[str]: sink 名列表（可能为空）。
        """
        rank = level_rank(row.level)
        limit = int(self.config.get('max_pending_per_sink') or 0)
        backlog = self.store.pending_counts() if limit > 0 else {}
        sinks = []
        for svc in self._service_getter() or ():
            name = str(svc.get('name') or '')
            if not name or not svc.get('enabled', False):
                continue
            if rank < level_rank(svc.get('min_level') or 'debug'):
                continue
            if limit > 0 and backlog.get(name, 0) >= limit:
                self._report('backlog', 'push sink %r backlog %d >= %d, '
                             'dropping new messages' % (name, backlog.get(name),
                                                        limit))
                continue
            sinks.append(name)
        return sinks

    def _title_for(self, row):
        """按模板生成推送标题（默认 ``WARNING · browser.py``）。

        Args:
            row (MessageRow): 消息。

        Returns:
            str: 标题文本。
        """
        template = str(self.config.get('title_template')
                       or '{level} · {source}')
        try:
            return template.format(level=str(row.level).upper(),
                                   source=row.source,
                                   content=row.content)
        except (KeyError, IndexError, ValueError):
            return '%s · %s' % (str(row.level).upper(), row.source)

    def _dedupe_window(self, level):
        """返回该级别适用的去重窗口（秒），不适用的级别返回 0。

        Args:
            level (str): 消息级别。

        Returns:
            float: 窗口秒数。
        """
        levels = [str(v).lower() for v in
                  (self.config.get('dedupe_levels') or ())]
        if levels and str(level).lower() not in levels:
            return 0.0
        try:
            return max(0.0, float(self.config.get('dedupe_window_s') or 0))
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ 入口
    def handle_raw(self, raw):
        """处理一条线格式消息：解析 → 过滤 → 写 outbox。

        Args:
            raw (str): ``"<Level>: <ts>$<source>$<content>$<extra>"``。

        Returns:
            int|None: 新建/命中的事件 id；被过滤或解析失败时为 None。
        """
        row = MessageRow.parse(raw)
        if row is None or not self.accept(row):
            return None
        sinks = self._sinks_for(row)
        if not sinks:
            return None
        key = '%s|%s|%s' % (row.level, row.source, row.content)
        event_id, _count = self.store.add_event(
            level=row.level, source=row.source, title=self._title_for(row),
            body=row.content, extras=row.extra, dedupe_key=key,
            dedupe_window=self._dedupe_window(row.level), sinks=sinks)
        return event_id

    # ------------------------------------------------------------------ 投递
    def _delivery_loop(self):
        """持续把 outbox 里到期的投递发出去，直到收到停止信号。"""
        while not self._stop.is_set():
            self._maybe_reload_config()
            delivered = 0
            try:
                delivered = self.deliver_once()
            except Exception as exc:  # noqa: BLE001 - 投递线程绝不能死
                self._report('loop', 'push delivery loop error: %s' % exc)
            try:
                self._maybe_prune()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响投递
                self._report('prune', 'push prune failed: %s' % exc)
            if delivered == 0:
                self._stop.wait(IDLE_SLEEP_S)

    def deliver_once(self):
        """投递一轮到期消息（按 sink 分组、逐条发送）。

        本轮认领到的行会在 :meth:`PushStore.claim` 里变成 ``inflight``（带租约）。
        投递循环因为停止信号/异常提前退出时，**没处理完的行会在 finally 里
        放回 pending**，不必等租约过期；这样网关重启后立刻能继续投递。

        Returns:
            int: 本轮成功送达的条数。
        """
        rows = self.store.claim(limit=BATCH_SIZE)
        if not rows:
            return 0
        services = {}
        for svc in self._service_getter() or ():
            name = str(svc.get('name') or '')
            if name:
                services[name] = svc
        grouped = {}
        for row in rows:
            grouped.setdefault(row['sink'], []).append(row)
        # 本轮已认领但还没终结的 (event_id, sink)；finally 里把剩下的放回 pending
        unfinished = {(int(row['event_id']), str(row['sink'])) for row in rows}

        delivered = 0
        max_attempts = int(self.config.get('max_attempts') or 8)
        try:
            for name, items in grouped.items():
                if self._stop.is_set():
                    break
                svc = services.get(name)
                if not svc or not svc.get('enabled', False):
                    for item in items:
                        self.store.mark_skipped(item['event_id'], name,
                                                'service missing or disabled')
                        unfinished.discard((int(item['event_id']), name))
                    continue
                bucket = self._bucket(name)
                for index, item in enumerate(items):
                    if self._stop.is_set():
                        break
                    if not bucket.take():
                        wait = bucket.wait_time()
                        for rest in items[index:]:
                            self.store.defer(rest['event_id'], name, wait)
                            unfinished.discard((int(rest['event_id']), name))
                        break
                    body = item['body']
                    if int(item.get('count') or 1) > 1:
                        body = '%s  (×%d)' % (body, int(item['count']))
                    options = item.get('options') or {}
                    try:
                        notifications._dispatch(
                            svc, body, title=item['title'],
                            level=item['level'],
                            file_path=options.get('file_path'),
                            attachments=options.get('attachments'),
                            channel=options.get('channel'),
                            overrides=options.get('overrides'),
                            extras=item.get('extras'))
                    except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它
                        retrying = self.store.mark_failed(
                            item['event_id'], name, str(exc),
                            max_attempts=max_attempts)
                        unfinished.discard((int(item['event_id']), name))
                        self._report('%s:%s' % (name, type(exc).__name__),
                                     'push via %r failed: %s%s'
                                     % (name, exc,
                                        '' if retrying else ' (given up)'))
                    else:
                        self.store.mark_sent(item['event_id'], name)
                        unfinished.discard((int(item['event_id']), name))
                        delivered += 1
                        self.delivered_total += 1
            return delivered
        finally:
            for event_id, sink in unfinished:
                try:
                    self.store.release(event_id, sink)
                except Exception:  # noqa: BLE001 - 收尾失败靠租约过期兜底
                    pass

    @staticmethod
    def _config_mtime_now():
        """返回 notifications.json 的修改时间（读不到时为 0）。

        Returns:
            float: 文件 mtime 秒数。
        """
        try:
            return os.path.getmtime(notifications._CONFIG_FILE)
        except OSError:
            return 0.0

    def _maybe_reload_config(self):
        """配置文件被界面改动后热加载网关策略（无需重启常驻服务）。"""
        if self._explicit_config:
            return
        stamp = self._config_mtime_now()
        if stamp == self._config_mtime:
            return
        self._config_mtime = stamp
        try:
            notifications._reload()
            self.config = notifications.get_gateway_config()
        except Exception as exc:  # noqa: BLE001 - 热加载失败保留旧配置
            self._report('config', 'push config reload failed: %s' % exc)

    def _bucket(self, name):
        """取某个 sink 的令牌桶（按需创建）。

        Args:
            name (str): sink 名。

        Returns:
            _TokenBucket: 该 sink 的限流桶。
        """
        bucket = self._buckets.get(name)
        if bucket is None:
            try:
                rate = float(self.config.get('rate_limit_per_s') or 0.0)
            except (TypeError, ValueError):
                rate = 0.0
            try:
                burst = float(self.config.get('rate_burst') or rate or 1.0)
            except (TypeError, ValueError):
                burst = max(1.0, rate)
            bucket = _TokenBucket(rate, burst)
            self._buckets[name] = bucket
        return bucket

    def _maybe_prune(self):
        """按 ``prune_interval_s`` 定期清理过期/超量数据。"""
        now = time.time()
        if now < self._next_prune:
            return
        try:
            interval = max(30.0, float(self.config.get('prune_interval_s') or 300))
        except (TypeError, ValueError):
            interval = 300.0
        self._next_prune = now + interval
        self.store.prune(max_events=int(self.config.get('max_events') or 20000),
                         retention_days=float(
                             self.config.get('retention_days') or 30))

    def _report(self, key, message):
        """向 Message Hub 报告一个问题（同类告警按时间节流）。

        网关自身消息由 ``source_deny`` 排除，不会形成推送回环。

        Args:
            key (str): 告警分类（用于节流）。
            message (str): 告警文本。
        """
        now = time.time()
        last = self._warn_stamps.get(key, 0.0)
        if now - last < WARN_INTERVAL_S:
            return
        self._warn_stamps[key] = now
        try:
            send_warning('[Push Gateway] ' + str(message))
        except Exception:  # noqa: BLE001 - 告警失败只能放弃
            pass


def _install_signal_handlers(gateway):
    """把 SIGINT/SIGTERM 转成优雅停止（只允许在主线程调用）。

    Args:
        gateway (PushGateway): 要停止的网关实例。
    """

    def _handler(_signum, _frame):
        gateway.stop()

    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def main(argv=None):
    """进程入口：启动推送网关并阻塞到收到停止信号。

    Args:
        argv (list[str]|None): 命令行参数（首个参数是 ProcessManager 传入的
            条目名，本服务不需要，直接忽略）。

    Returns:
        int: 退出码（0=正常停止，1=启动失败）。
    """
    gateway = PushGateway()
    _install_signal_handlers(gateway)
    # 启动前检查是否已有别的网关在跑：双网关虽然靠“认领 + 租约”不会再重复
    # 投递同一条消息，但会互相抢行、让投递顺序和日志都变乱；直接让新进程
    # 退出更可预期（心跳过期 = 上一个网关已被强杀，照常启动并接管）。
    other = pushstore.read_heartbeat(gateway.heartbeat_dir)
    if other and int(other.get('pid') or 0) != os.getpid():
        print(tr('[Push Gateway] another gateway is already running '
                 '(pid {pid}, host {host}); this process exits.',
                 pid=int(other.get('pid') or 0),
                 host=other.get('host') or ''), file=sys.stderr)
        return 0
    try:
        send_info('[Push Gateway] watching Message Hub (min level %s)'
                  % gateway.config.get('min_level', 'warning'))
    except Exception:  # noqa: BLE001 - 消息通道不可用时仍然继续跑
        pass
    try:
        gateway.run()
    except KeyboardInterrupt:
        gateway.stop()
    except Exception as exc:  # noqa: BLE001 - 崩溃要留下可读原因
        try:
            send_error('[Push Gateway] stopped unexpectedly: %s' % exc)
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        try:
            send_info('[Push Gateway] stopped')
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
