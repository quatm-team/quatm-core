"""消息日志的结构化记录与持久化存储（SQLite 索引 + 可选 JSONL 兼容镜像）。

设计目标（回应“消息的存储和可视化有没有更优雅的方式”）：

1. **结构化记录**：消息在网络上是 ``"<Level>: <时间戳>$<来源>$<内容>"`` 这一
   历史线格式（见 :mod:`quatm.servers.messageclient`）；本模块提供
   :func:`parse_raw` 把它解析成 :class:`MessageRow`（NamedTuple，仍是元组，
   因此老的 ``row[0] / row[2]`` 下标代码照样能用），另外支持**可选的第四段**
   JSON 附加字段（不破坏旧格式：没有第四段就是 ``extra`` 为空）。
2. **持久化**：主打 SQLite（``configuration/messagelist/messages.db``），
   带 ``ts``/``date``/``level`` 索引，支持“级别 + 来源 + 全文 + 日期范围 +
   分页”查询。

   旧版按天的 ``<date>.json`` 行文件（JSONL）**默认不再写也不再导入**：

   * 镜像默认关（``QUATM_MESSAGE_JSONL=1`` 可重新打开）：SQLite 已经是唯一
     权威存储，再写一份只是重复占盘（实验室里积到了 GB 级）；
   * 导入默认关（``QUATM_IMPORT_JSONL=1`` 或 ``migrate_jsonl=True`` 可打开）：
     把几 GB 历史灌进库要几十分钟，而且会把启动/界面拖死，所以改成
     “要用的人自己显式开”。

   也就是说：**新消息只进 SQLite**，界面启动永远不再做历史导入。
3. **线程安全**：写盘线程写、GUI 线程读。SQLite 连接以
   ``check_same_thread=False`` 创建，所有操作在一把锁内完成，并开启 WAL
   （读写互不阻塞）。
4. **保留策略**：消息库不会无限增长。常驻记录器周期性地调 :meth:`prune`
   按天数（默认 30 天）与总行数（默认 20 万）淘汰旧消息；阈值可经
   ``Properties('MessageStore')`` 或环境变量调整（见模块常量）。

用法::

    store = MessageStore()                 # 默认 <workpath>/configuration/messagelist
    store.append([MessageRow.parse('Info: 2026-09-10 12:00:01$x.py$hi')])
    rows = store.query(levels=['error'], text='dds', limit=100, offset=0)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from collections import namedtuple
from datetime import datetime
from quatm.servers import sqlite_utils
from quatm.utils.i18n import tr

_log = logging.getLogger('quatm.messagestore')

# 消息级别（小写，与 messageclient 的 'Error'/'Warning'/'Info'/'Debug' 对应）
LEVELS = ('debug', 'info', 'warning', 'error')

# 时间戳格式：ISO 到毫秒（与历史 JSONL 记录一致，前 10 字符即日期）
TS_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
_TS_LEN = 23


class MessageRow(namedtuple('MessageRow', 'time source level content extra')):
    """一条消息记录：``(时间, 来源, 级别, 内容, 附加字段)``。

    仍然是普通元组（老的 ``row[0]../row[3]`` 下标访问继续可用），但多了
    字段名与 :meth:`parse` 解析。``extra`` 默认为 None（兼容只有 4 段的旧
    记录），有结构化附加字段时是一个 dict。

    Args:
        time (str): ``'YYYY-MM-DD HH:MM:SS.mmm'`` 时间戳。
        source (str): 消息来源（发出消息的代码文件名）。
        level (str): 级别（``debug/info/warning/error``，小写）。
        content (str): 消息正文。
        extra (dict|None): 可选的结构化附加字段。
    """

    __slots__ = ()

    def __new__(cls, time, source, level, content, extra=None):
        """构造记录（``extra`` 允许省略，保持 4 元组可用）。"""
        return super().__new__(cls, time, source, level, content, extra)

    @property
    def date(self):
        """返回该消息的日期字符串 ``YYYY-MM-DD``（取时间戳前 10 字符）。

        Returns:
            str: 日期；时间戳异常时退化为今天。
        """
        if isinstance(self.time, str) and len(self.time) >= 10:
            return self.time[:10]
        return datetime.now().date().isoformat()

    @classmethod
    def parse(cls, raw):
        """把线格式字符串解析为 :class:`MessageRow`。

        兼容三种输入：
          * 历史线格式 ``"Info: ts$source$content"``；
          * 带附加字段的 ``"Info: ts$source$content${\\"k\\":1}"``；
          * 内容里含 ``$``（第四段不是合法 JSON）时按内容处理，不误判。

        Args:
            raw: 原始消息（字符串，或 ``messageclient`` 收到的 ``(str, {})`` 元组）。

        Returns:
            MessageRow|None: 解析结果；格式不符返回 None。
        """
        if not raw:
            return None
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ''
        raw_str = str(raw)

        parts = raw_str.split('$', 3)
        if len(parts) < 3:
            return None
        header, source, content = parts[0], parts[1], parts[2]
        extra = None
        if len(parts) == 4:
            try:
                parsed = json.loads(parts[3])
            except Exception:  # noqa: BLE001 - 第四段不是 JSON：内容本身含 '$'
                content = content + '$' + parts[3]
            else:
                if isinstance(parsed, dict):
                    extra = parsed
                else:
                    content = content + '$' + parts[3]

        if ': ' not in header:
            return None
        level, timestamp_str = header.split(': ', 1)

        try:
            timestamp = datetime.strptime(timestamp_str, TS_FORMAT)
        except ValueError:
            timestamp = None
        if timestamp is None:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%H:%M:%S.%f', '%H:%M:%S'):
                try:
                    parsed_ts = datetime.strptime(timestamp_str, fmt)
                except ValueError:
                    continue
                if fmt.startswith('%H'):
                    parsed_ts = datetime.combine(datetime.now().date(),
                                                 parsed_ts.time())
                timestamp = parsed_ts
                break
        if timestamp is None:
            timestamp = datetime.now()

        return cls(timestamp.isoformat(sep=' ', timespec='milliseconds'),
                   source, str(level).lower(), content, extra)


def _default_db_path():
    """返回默认数据库路径（``<workpath>/configuration/messagelist/messages.db``）。

    Returns:
        str: 绝对路径。
    """
    from quatm.servers import workpath
    return os.path.join(workpath, 'configuration', 'messagelist', 'messages.db')


# 共享的“存储级别策略”文件名（与数据库同目录）。界面与常驻 message logger 都读它，
# 因此“只存 warning/error”这类策略对两边一致（见 quatm/servers/messagelogger.py）。
POLICY_FILENAME = 'store_policy.json'

# 消息的 ``extra`` 里带这个键（值为真）= “不管存储级别策略，都要写下来”。
# 用途：**会话边界消息**（例如 ProcessManager 关闭时的“窗口已关闭，正在停所有
# 常驻进程”）。用户把策略设成“只存 error”后，日志里会连“这套软件什么时候开过、
# 什么时候关的”都没有，排查问题时很难对齐时间线；这类消息一个会话只有一两条，
# 留着不占空间。
KEEP_FLAG = 'keep'


def always_keep(extra):
    """这条消息是否要求“不受存储级别策略影响、一定写库”。

    判定方式：``extra``（消息线格式第四段的结构化字段）里有真值的
    :data:`KEEP_FLAG`。界面上选中该条消息时能在“附加字段”里看到 ``keep``，
    一看就知道它为什么没被策略丢掉。

    Args:
        extra (dict|None): 消息的结构化附加字段。

    Returns:
        bool: 要求强制保留为 True。
    """
    return bool(isinstance(extra, dict) and extra.get(KEEP_FLAG))


def policy_path(db_path=None):
    """返回“存储级别策略”文件路径（与消息库同目录）。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        str: 策略文件绝对路径。
    """
    return os.path.join(os.path.dirname(db_path or _default_db_path()),
                        POLICY_FILENAME)


def load_level_policy(db_path=None, default=None):
    """读取共享的存储级别策略。

    文件不存在、内容损坏、级别名全不合法都返回 ``default``（默认 None =
    所有级别都保存），且绝不抛异常：策略文件坏了不该让记录服务起不来。

    Args:
        db_path (str|None): 消息库路径；None 用默认位置。
        default: 读不到时返回的值。

    Returns:
        list|None: 小写的级别名列表；没有有效策略时返回 ``default``。
    """
    try:
        with open(policy_path(db_path), 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return default
    if isinstance(raw, dict):
        raw = raw.get('levels')
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple)):
        return default
    levels = [str(item).strip().lower() for item in raw if str(item).strip()]
    return levels or default


def save_level_policy(levels, db_path=None):
    """原子地写入共享存储级别策略（``None``/空 表示保存所有级别）。

    Args:
        levels (Iterable|None): 级别名集合。
        db_path (str|None): 消息库路径；None 用默认位置。

    Returns:
        str|None: 写入的文件路径；写失败时返回 None（调用方无需处理）。
    """
    path = policy_path(db_path)
    payload = {'levels': [str(level).strip().lower()
                          for level in (levels or []) if str(level).strip()]}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        return None
    return path


# SQLite 等锁时间（秒）：另一个进程（常驻 Message Logger / 另一个 Stream
# Monitor 窗口）正在批量写或迁移历史时，要给它足够时间。Windows 上文件锁
# 更严，原来的 5~10 s 太短，会直接抛 database is locked。
# 注意这是**每个语句**的等锁上限，所以“一个写操作”的最坏耗时约为
# ``SQLITE_TIMEOUT_S * WRITE_RETRIES``（几十秒到一两分钟）；常见情况（锁只
# 持续几百毫秒）第一次就成功了，不会走到重试。
SQLITE_TIMEOUT_S = 30.0
# 等锁超时（说明锁被持了很久）后再重试的次数与退避（秒，指数退避封顶 2 s）
WRITE_RETRIES = 4
WRITE_RETRY_BACKOFF_S = 0.25
# JSONL 历史导入的分批大小：小批量 + 每批提交，避免长时间持写锁
MIGRATE_BATCH = 2000
# 环境变量：JSONL 兼容镜像（默认关）与旧 JSONL 历史导入（默认关）。
# 两个都默认关：实验室的历史 ``<date>.json`` 已到 GB 级，再写镜像只是重复
# 占盘，而把 GB 级历史导进库要几十分钟且会把界面拖死（用户机器上实测 15-20
# 分钟），所以“要用的人显式打开”。
JSONL_MIRROR_ENV = 'QUATM_MESSAGE_JSONL'
JSONL_IMPORT_ENV = 'QUATM_IMPORT_JSONL'

# 消息保留策略：默认保留 30 天 / 最多 20 万条。两项都可以从
# ``Properties('MessageStore')`` 的 ``retention_days`` / ``max_rows`` 读取，
# 并可用环境变量覆盖（环境变量优先，便于离线脚本/临时排查时不动属性树）。
# 取值 ``<= 0`` 表示该项不限制（例如只想按天数淘汰）。
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_ROWS = 200000
RETENTION_DAYS_ENV = 'QUATM_MESSAGE_RETENTION_DAYS'
MAX_ROWS_ENV = 'QUATM_MESSAGE_MAX_ROWS'
# Properties 读取缓存时间 [s]：prune 是周期性动作，不必每轮都连一次属性系统
# （在没起 Property Logger 的进程里，创建客户端最多要等 0.5 s 的握手超时）。
PROPERTY_SETTINGS_TTL_S = 300.0
# VACUUM 会拿独占写锁并重写整个库（GB 级库要几十秒到几分钟），绝不能每次
# prune 都做：只有本次删除量够大、且距上次 VACUUM 足够久时才做一次。
VACUUM_MIN_INTERVAL_S = 6 * 3600.0
VACUUM_MIN_DELETED_ROWS = 5000


def _env_flag(name, default=False):
    """读一个布尔环境变量。

    Args:
        name (str): 环境变量名。
        default (bool): 环境变量为空/未设置时的取值。

    Returns:
        bool: ``1/true/yes/on`` 为 True，``0/false/no/off`` 为 False。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in ('0', 'false', 'no', 'off')


def _parse_number(value, cast):
    """把配置值安全地转成数字（非法/空值返回 None，调用方继续用下一优先级）。

    Args:
        value: 原始配置值（字符串/数字/None）。
        cast (callable): ``float`` 或 ``int``。

    Returns:
        float|int|None: 转换结果；无法转换时为 None。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return cast(str(value).strip())
    except (TypeError, ValueError):
        return None


#: ``Properties('MessageStore')`` 读取结果缓存（值 + 读取时刻）。
_PROPERTY_SETTINGS = {'values': {}, 'ts': float('-inf')}
_PROPERTY_SETTINGS_LOCK = threading.Lock()


def _property_prune_settings():
    """从 ``Properties('MessageStore')`` 读取保留策略（进程内缓存，失败不抛）。

    读的是**整棵属性树的副本**再按名字取键，而不是 ``props.get('retention_days')``：
    后者在键不存在时会创建并广播 ``None``，等于“读配置”顺手污染了属性树。

    Returns:
        dict: 形如 ``{'retention_days': 30, 'max_rows': 200000}`` 的**原始**值
        （可能缺键）；属性系统不可用时为空 dict。
    """
    now = time.time()
    with _PROPERTY_SETTINGS_LOCK:
        if now - _PROPERTY_SETTINGS['ts'] < PROPERTY_SETTINGS_TTL_S:
            return dict(_PROPERTY_SETTINGS['values'])
    values = {}
    try:
        from quatm.servers.properties import Properties
        props = Properties('MessageStore', enable_monitors=False)
    except Exception as exc:  # noqa: BLE001 - 属性系统不可用就只用默认值
        _log.debug('MessageStore properties unavailable: %s', exc)
    else:
        try:
            tree = props.get('/')
            section = tree.get('MessageStore') if isinstance(tree, dict) else None
            if isinstance(section, dict):
                values = {key: section.get(key)
                          for key in ('retention_days', 'max_rows')}
        except Exception as exc:  # noqa: BLE001 - 读失败按“没配置”处理
            _log.debug('reading MessageStore properties failed: %s', exc)
        finally:
            try:
                props.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响读取结果
                pass
    with _PROPERTY_SETTINGS_LOCK:
        _PROPERTY_SETTINGS['values'] = dict(values)
        _PROPERTY_SETTINGS['ts'] = now
    return values


def _prune_limits(retention_days=None, max_rows=None):
    """解析 prune 的保留期与容量上限。

    优先级从高到低：显式参数 → 环境变量（:data:`RETENTION_DAYS_ENV` /
    :data:`MAX_ROWS_ENV`）→ ``Properties('MessageStore')`` → 默认值
    （:data:`DEFAULT_RETENTION_DAYS` / :data:`DEFAULT_MAX_ROWS`）。

    Args:
        retention_days: 显式保留天数（``<=0`` 不按时间淘汰）。
        max_rows: 显式总行数上限（``<=0`` 不按容量淘汰）。

    Returns:
        tuple[float, int]: ``(保留天数, 行数上限)``。
    """
    props = _property_prune_settings()
    retention = _parse_number(retention_days, float)
    if retention is None:
        retention = _parse_number(os.environ.get(RETENTION_DAYS_ENV), float)
    if retention is None:
        retention = _parse_number(props.get('retention_days'), float)
    if retention is None:
        retention = float(DEFAULT_RETENTION_DAYS)
    rows = _parse_number(max_rows, int)
    if rows is None:
        rows = _parse_number(os.environ.get(MAX_ROWS_ENV), int)
    if rows is None:
        rows = _parse_number(props.get('max_rows'), int)
    if rows is None:
        rows = int(DEFAULT_MAX_ROWS)
    return max(0.0, retention), max(0, rows)


class MessageStore:
    """消息 SQLite 存储：批量写入 + 带过滤的分页查询。

    线程安全（内部一把锁 + WAL），可在写盘线程写、GUI 线程读。

    Args:
        path (str|None): 数据库路径；None 用默认位置；``':memory:'`` 用于测试。
        jsonl_mirror (bool|None): 是否同时追加旧的 ``<date>.json`` 行文件
            （None = 读环境变量 ``QUATM_MESSAGE_JSONL``，**默认关闭**）。
        migrate_jsonl (bool|None): 是否把同目录已有的 JSONL 历史导入数据库
            （None = 读环境变量 ``QUATM_IMPORT_JSONL``，**默认关闭**——GB 级
            历史导入要几十分钟，不能在启动路径上做）。
    """

    SCHEMA_VERSION = 2

    def __init__(self, path=None, jsonl_mirror=None, migrate_jsonl=None):
        """打开/创建数据库（默认不做任何历史导入）。

        Args:
            path (str|None): 数据库路径。
            jsonl_mirror (bool|None): 是否维护 JSONL 兼容镜像（默认关）。
            migrate_jsonl (bool|None): 是否导入已有 JSONL 历史（默认关）。
        """
        self.path = path or _default_db_path()
        if jsonl_mirror is None:
            jsonl_mirror = _env_flag(JSONL_MIRROR_ENV, False)
        self.jsonl_mirror = bool(jsonl_mirror)
        if migrate_jsonl is None:
            migrate_jsonl = _env_flag(JSONL_IMPORT_ENV, False)
        self.migrate_jsonl = bool(migrate_jsonl)
        self._lock = threading.Lock()
        self._fts = False
        self._json1 = False
        # 存储策略：只保存这些级别（None = 全部保存），用于避免落盘无用消息
        self._levels = None
        self._skipped = 0
        # 上次 VACUUM 的时间与本次 prune 的统计（供诊断/测试）
        self._last_vacuum = float('-inf')
        self.last_prune_deleted = 0
        self.last_pruned_at = None
        if self.path != ':memory:':
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # timeout：SQLite 自带的“等锁”时间。默认 5 s 对“另一个进程正在批量
        # 写/迁移”太短——Windows 上文件锁更严，会直接报 database is locked
        # （用户机器上 Message Logger 启动时踩过）；下面还有一层重试。
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     timeout=SQLITE_TIMEOUT_S)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        if self.migrate_jsonl:
            self._migrate_jsonl()

    # ------------------------------------------------------------------ setup
    def _init_schema(self):
        """建表与建索引（幂等），遇锁重试；同时探测是否支持 FTS5 全文检索。

        注意：``PRAGMA journal_mode=WAL`` 与建表**都需要写锁**——常驻
        Message Logger 正在批量写时，构造 Store（Stream Monitor 启动路径）
        就可能拿到 ``database is locked``，所以这里也要重试（同迁移）。
        """
        with self._lock:
            sqlite_utils.run_with_retry(
                self._conn, self._lock, self._schema_statements,
                retries=WRITE_RETRIES, backoff=WRITE_RETRY_BACKOFF_S)

    def _schema_statements(self):
        """建表/建索引/能力探测（一个事务），供 :func:`sqlite_utils.run_with_retry` 调用。"""
        c = self._conn
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute("""CREATE TABLE IF NOT EXISTS meta
                     (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        date TEXT NOT NULL,
                        level TEXT NOT NULL,
                        source TEXT NOT NULL,
                        content TEXT NOT NULL,
                        extra TEXT)""")
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_date_level '
                  'ON messages(date, level)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_source '
                  'ON messages(source)')
        self._migrate_columns(c)
        try:
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                         USING fts5(content, source)""")
            self._fts = True
        except sqlite3.OperationalError:
            # 老 sqlite 没编 FTS5：退回 LIKE 搜索（功能等价，只是慢些）
            self._fts = False
        try:
            c.execute("SELECT json_extract('{\"a\":1}', '$.a')")
            self._json1 = True
        except sqlite3.OperationalError:
            # 没编 JSON1 时无法按结构化字段过滤，退回字符串 LIKE
            self._json1 = False
        c.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                  ('schema_version', str(self.SCHEMA_VERSION)))
        c.commit()

    @staticmethod
    def _table_columns(conn, table):
        """返回表的现有列名集合（用于幂等的 schema 迁移）。

        Args:
            conn (sqlite3.Connection): 数据库连接。
            table (str): 表名。

        Returns:
            set[str]: 列名集合；表不存在时为空集合。
        """
        return {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}

    @classmethod
    def _ensure_columns(cls, conn, table, columns):
        """给旧库补上后来新增的列（``ALTER TABLE ADD COLUMN``，幂等）。

        老版本建的 ``messages`` 表没有 ``extra`` 列，直接插入会报
        ``table messages has no column named extra``，Message Logger 一启动
        就挂。这里在打开库时补齐缺失列（只加列，不动已有数据）。

        Args:
            conn (sqlite3.Connection): 数据库连接。
            table (str): 表名。
            columns (Sequence[tuple[str, str]]): ``(列名, 类型/默认值声明)``。

        Returns:
            set[str]: 本次真正新增的列名集合（调用方据此决定要不要回填数据）。
        """
        existing = cls._table_columns(conn, table)
        added = set()
        for name, declaration in columns:
            if name in existing:
                continue
            conn.execute('ALTER TABLE %s ADD COLUMN %s %s'
                         % (table, name, declaration))
            existing.add(name)
            added.add(name)
        return added

    def _migrate_columns(self, conn):
        """把旧库的 ``messages`` 表补齐到当前 schema（见 :meth:`_ensure_columns`）。

        ``date`` 是历史记录里可能缺失的派生列：补上后从 ``ts`` 前 10 个字符
        回填，避免旧行在按日期查询/淘汰时被漏掉。``extra`` 缺失也能补上，
        这样带结构化附加字段的新消息不会因为旧库而写入失败。只在**本次真的
        新增了列**时才对全表回填（GB 级库上无条件的 UPDATE 会拖慢每次启动）。

        Args:
            conn (sqlite3.Connection): 数据库连接。
        """
        added = self._ensure_columns(conn, 'messages',
                                     [('extra', 'TEXT'), ('date', 'TEXT')])
        if 'date' in added:
            conn.execute("UPDATE messages SET date = substr(ts, 1, 10)")

    def _migrate_jsonl(self):
        """把 ``messagelist/*.json`` 里尚未入库的历史导入数据库（**默认不调用**）。

        只在显式要求时才会跑（``migrate_jsonl=True`` / ``QUATM_IMPORT_JSONL=1``）：
        GB 级历史要几十分钟，放在启动路径上会把界面拖死（用户机器上实测
        15-20 分钟且窗口无响应）。要导的话请自己另开线程或离线跑。

        **容错要点**（用户机器上 Message Logger 启动时踩过 ``database is
        locked``）：

        * 分批导入（每 :data:`MIGRATE_BATCH` 行提交一次），不长时间持写锁；
        * 每批走 :func:`quatm.servers.sqlite_utils.run_with_retry`，另一个进程在写时靠重试熬过去；
        * **进度按文件记录**（``meta['jsonl_imported:<文件名>']`` 记已导入行数）：
          中途失败下次启动接着导，不会因为重跑而把同一批消息重复插一遍
          （``append`` 故意不去重，见它的 docstring）；
        * 只有**所有文件都导完**才写 ``jsonl_migrated``；中途被锁/读不动时
          只记录各文件的进度，绝不写完成标记（否则剩下的历史就永远不会
          再导了）；
        * 单个文件出错不影响其它文件；
        * 整个过程**绝不抛异常**：迁移只是“把旧文件搬进库”，失败不该让
          Message Logger / Stream Monitor 起不来。
        """
        if self.path == ':memory:':
            return 0
        folder = os.path.dirname(self.path)
        if not os.path.isdir(folder):
            return 0
        try:
            with self._lock:
                done = self._conn.execute(
                    'SELECT value FROM meta WHERE key = ?',
                    ('jsonl_migrated',)).fetchone()
            if done is not None:
                return 0
            imported = 0
            complete = True
            for name in sorted(os.listdir(folder)):
                if not (name.endswith('.json') and len(name) == 15):
                    continue
                try:
                    added, finished = self._import_jsonl_file(folder, name)
                except Exception as exc:  # noqa: BLE001 - 一个文件坏不影响别的
                    self._warn('导入历史文件 %s 失败（下次启动会继续）：%s'
                               % (name, exc))
                    complete = False
                    continue
                imported += added
                complete = complete and finished
            if complete:
                with self._lock:
                    def _mark():
                        self._mark_jsonl_migrated()
                    sqlite_utils.run_with_retry(
                        self._conn, self._lock, _mark,
                        retries=WRITE_RETRIES,
                        backoff=WRITE_RETRY_BACKOFF_S)
            else:
                self._warn('历史 JSONL 还没导完（已导入 %d 条，下次启动继续）'
                           % imported)
            return imported
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响正常记录
            self._warn('历史 JSONL 导入未完成（下次启动会继续）：%s' % exc)
            return 0

    def _mark_jsonl_migrated(self):
        """写 ``meta['jsonl_migrated']``（并提交），表示历史 JSONL 已全部导入。

        只在**所有**历史文件都导完时调用；被锁住而没导完时绝不能写这个标记，
        否则剩下的历史在下次启动时就不会再导入了。
        """
        self._conn.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                           ('jsonl_migrated', datetime.now().isoformat()))
        self._conn.commit()

    def _import_jsonl_file(self, folder, name):
        """导入单个历史 JSONL 文件（从上次中断处继续），返回本次导入条数。

        Args:
            folder (str): 消息目录。
            name (str): 文件名（``YYYY-MM-DD.json``）。

        Returns:
            tuple: ``(本次真正写入库的行数, 是否已整文件导完)``。第二个值为
                False 表示只导了一部分（写库一直锁着/文件读不动），调用方
                **不能**标记“历史已导入”，下次启动要接着导。
        """
        path = os.path.join(folder, name)
        key = 'jsonl_imported:' + name
        with self._lock:
            row = self._conn.execute('SELECT value FROM meta WHERE key = ?',
                                     (key,)).fetchone()
        already = int(row[0]) if row is not None else 0
        rows = []
        seen = 0
        added = 0
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    seen += 1
                    if seen <= already:
                        continue        # 上次已经导过，跳过
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001 - 跳过坏行
                        continue
                    if isinstance(rec, (list, tuple)) and len(rec) >= 4:
                        rows.append(MessageRow(
                            str(rec[0]), str(rec[1]), str(rec[2]).lower(),
                            str(rec[3]),
                            rec[4] if len(rec) > 4 and isinstance(rec[4], dict)
                            else None))
                    if len(rows) >= MIGRATE_BATCH:
                        added += self._append_migrated(rows, key, seen)
                        rows = []
        except OSError as exc:
            self._warn('读历史文件 %s 失败：%s' % (name, exc))
            return added, False
        if rows:
            added += self._append_migrated(rows, key, seen)
        return added, True

    def _append_migrated(self, rows, key, seen):
        """写一批迁移数据并记录进度（已提交行号），返回写入条数。

        Args:
            rows (list): 待写入的记录。
            key (str): 该文件的进度 meta 键。
            seen (int): 已读到的行号（下次从这个行号之后续传）。

        Returns:
            int: 写入条数。
        """
        written = self.append(rows, mirror=False)
        with self._lock:
            def _save_progress():
                self._conn.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                                   (key, str(seen)))
                self._conn.commit()
            sqlite_utils.run_with_retry(
                self._conn, self._lock, _save_progress,
                retries=WRITE_RETRIES, backoff=WRITE_RETRY_BACKOFF_S)
        return written

    @staticmethod
    def _warn(message):
        """打一条告警日志（延迟 import，避免 servers 包与存储层循环依赖）。

        Args:
            message (str): 告警文本。
        """
        try:
            from quatm.servers import send_warning
            send_warning(tr('[MessageStore] ') + str(message))
        except Exception:  # noqa: BLE001 - 记日志失败也不能影响主流程
            pass

    # ------------------------------------------------------------------ write
    def set_level_filter(self, levels):
        """设置“只保存这些级别”的存储策略（None/空 表示全部保存）。

        过滤在 :meth:`append` 里生效，因此 SQLite 与 JSONL 镜像一致受影响；
        已有的历史数据不会被删。

        Args:
            levels (Iterable|None): 级别名集合（大小写不敏感）。
        """
        if not levels:
            self._levels = None
            return
        wanted = {str(level).strip().lower() for level in levels if str(level).strip()}
        self._levels = wanted or None

    def level_filter(self):
        """返回当前的存储级别过滤（None 表示全部保存）。

        Returns:
            set|None: 级别集合。
        """
        return set(self._levels) if self._levels else None

    def skipped_count(self):
        """返回因存储策略而未写入的消息条数（供界面提示）。

        Returns:
            int: 跳过条数。
        """
        return self._skipped

    def duplicate_count(self):
        """返回因去重而未重复写入的消息条数。

        兼容接口：现在不再用唯一索引去重（同一条消息在一秒内合法地重复很多次
        是常见情况，见历史库里的消息风暴），因此这里恒为 0。

        Returns:
            int: 固定 0。
        """
        return 0

    def _write_payload(self, payload):
        """一个事务写入消息与 FTS，必要时重试（调用方已持锁）。

        Args:
            payload (list[tuple]): 已规整好的行数据。
        """
        def _do():
            cur = self._conn.executemany(
                'INSERT INTO messages (ts, date, level, source, content, extra) '
                'VALUES (?, ?, ?, ?, ?, ?)', payload)
            if self._fts:
                # 最后一行的 id（executemany 后 cursor 就有 lastrowid）→
                # 不必再跑一次 SELECT MAX(id)
                last_id = cur.lastrowid
                if last_id is None:
                    last_id = self._conn.execute(
                        'SELECT MAX(id) FROM messages').fetchone()[0] or 0
                first_id = last_id - len(payload) + 1
                self._conn.executemany(
                    'INSERT INTO messages_fts (rowid, content, source) '
                    'VALUES (?, ?, ?)',
                    [(first_id + i, p[4], p[3])
                     for i, p in enumerate(payload)])
            self._conn.commit()
        sqlite_utils.run_with_retry(
            self._conn, self._lock, _do,
            retries=WRITE_RETRIES, backoff=WRITE_RETRY_BACKOFF_S)

    def append(self, rows, mirror=None):
        """批量写入消息（一个事务；同时按天追加 JSONL 镜像）。

        不做内容去重：同一条文本在同一秒内合法地重复很多次是常见情况（消息
        风暴），因此记录端的唯一性是靠“同一时刻只有一个写者”保证的（见
        :mod:`quatm.servers.messagelogger` 的记录权心跳文件）。

        写入遇 ``database is locked``（另一个进程正在写/迁移）时自动重试，
        仍失败才抛出——调用方（Message Logger / Stream Monitor）会自己兜底。

        Args:
            rows (Iterable): :class:`MessageRow` 或等价的 4/5 元组。
            mirror (bool|None): 覆盖实例的 JSONL 镜像设置。

        Returns:
            int: 实际写入条数（被存储策略跳过的条数不计入，见
            :meth:`skipped_count`）。带 :data:`KEEP_FLAG` 的消息（会话边界）
            不受策略影响。
        """
        rows = [r for r in rows if r]
        if not rows:
            return 0
        if self._levels is not None:
            kept = []
            for r in rows:
                level = str(r[2] if not isinstance(r, MessageRow) else r.level)
                extra = (r.extra if isinstance(r, MessageRow) else
                         (r[4] if len(r) > 4 and isinstance(r[4], dict) else None))
                if level.strip().lower() in self._levels or always_keep(extra):
                    kept.append(r)
            self._skipped += len(rows) - len(kept)
            rows = kept
            if not rows:
                return 0
        do_mirror = self.jsonl_mirror if mirror is None else bool(mirror)
        normalized = []
        payload = []
        for r in rows:
            if not isinstance(r, MessageRow):
                r = MessageRow(str(r[0]), str(r[1]), str(r[2]).lower(),
                               str(r[3]),
                               r[4] if len(r) > 4 and isinstance(r[4], dict)
                               else None)
            normalized.append(r)
            payload.append((r.time, r.date, r.level, r.source, r.content,
                            json.dumps(r.extra, ensure_ascii=False)
                            if r.extra else None))
        with self._lock:
            self._write_payload(payload)
        if do_mirror:
            # 注意传规范化后的行：调用方可能直接给 4 元组（tuple 没有 .date）
            self._mirror_jsonl(normalized)
        return len(rows)

    def _mirror_jsonl(self, rows):
        """把消息按日期追加到旧的 ``<date>.json`` 行文件（兼容镜像）。

        Args:
            rows (list): 已写入数据库的记录。
        """
        groups = {}
        for r in rows:
            groups.setdefault(r.date, []).append(list(r[:4]))
        folder = os.path.dirname(self.path)
        for date_str, items in groups.items():
            try:
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, f'{date_str}.json'), 'a',
                          buffering=8192, encoding='utf-8') as f:
                    f.write('\n'.join(json.dumps(it) for it in items) + '\n')
            except OSError:
                # JSONL 只是兼容镜像：失败不该影响主库写入
                continue

    # -------------------------------------------------------------- retention
    def prune(self, retention_days=None, max_rows=None, vacuum=None, now=None):
        """删除过期/超量的历史消息（消息库不会无限增长）。

        默认保留 :data:`DEFAULT_RETENTION_DAYS` 天、最多
        :data:`DEFAULT_MAX_ROWS` 行；实际取值按“显式参数 → 环境变量 →
        ``Properties('MessageStore')`` → 默认值”解析（见 :func:`_prune_limits`），
        所以实验室可以在属性树里调 ``retention_days`` / ``max_rows``。

        **绝不删除正在写入的数据**：整个删除在 :attr:`_lock` 内完成（与
        :meth:`append` 互斥），SQLite 也只能看到已提交的行；本方法只按
        ``ts``（早于保留期）或最老的 id（超出容量）删除，刚写入/尚未提交
        的消息不会被命中。

        VACUUM 不是每次都做（见 :data:`VACUUM_MIN_INTERVAL_S` /
        :data:`VACUUM_MIN_DELETED_ROWS`）：它要独占写锁且重写整个文件，
        对 GB 级库代价太大。``vacuum=False`` 可显式跳过。

        Args:
            retention_days (float|None): 保留天数；``None`` 按配置解析，
                ``<=0`` 不按时间淘汰。
            max_rows (int|None): 总行数上限；``None`` 按配置解析，
                ``<=0`` 不按容量淘汰。
            vacuum (bool|None): 是否允许 VACUUM；``None`` 按阈值判断。
            now (float|None): 参考时间（测试用）；``None`` 用当前时间。

        Returns:
            int: 本次实际删除的消息条数。
        """
        retention, rows = _prune_limits(retention_days, max_rows)
        now = time.time() if now is None else float(now)

        def _do():
            return self._prune_locked(retention, rows, now)

        with self._lock:
            deleted = sqlite_utils.run_with_retry(
                self._conn, self._lock, _do,
                retries=WRITE_RETRIES, backoff=WRITE_RETRY_BACKOFF_S)
        if deleted:
            self.last_prune_deleted = deleted
            self.last_pruned_at = now
        if vacuum is not False:
            self._maybe_vacuum(deleted, now)
        return deleted

    def _prune_locked(self, retention_days, max_rows, now):
        """执行一次删除事务（调用方已持锁；FTS 行先删，再删主表）。

        Args:
            retention_days (float): 保留天数（``<=0`` 跳过）。
            max_rows (int): 行数上限（``<=0`` 跳过）。
            now (float): 参考时间。

        Returns:
            int: 删除条数。
        """
        c = self._conn
        deleted = 0
        if retention_days > 0:
            cutoff = datetime.fromtimestamp(
                now - retention_days * 86400.0).isoformat(
                    sep=' ', timespec='milliseconds')
            # 先删 FTS 镜像行：messages 行还在时才能按 ts 子查询定位 rowid。
            # 否则删除主表后这些 rowid 会变成永远清不掉的“幽灵行”（FTS
            # 表自己不会随主表级联删除）。
            if self._fts:
                c.execute('DELETE FROM messages_fts WHERE rowid IN '
                          '(SELECT id FROM messages WHERE ts < ?)', (cutoff,))
            cur = c.execute('DELETE FROM messages WHERE ts < ?', (cutoff,))
            deleted += max(0, int(cur.rowcount or 0))
        if max_rows > 0:
            total = int(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
            extra = total - max_rows
            if extra > 0:
                if self._fts:
                    c.execute('DELETE FROM messages_fts WHERE rowid IN '
                              '(SELECT id FROM messages ORDER BY id LIMIT ?)',
                              (int(extra),))
                cur = c.execute('DELETE FROM messages WHERE id IN '
                                '(SELECT id FROM messages ORDER BY id LIMIT ?)',
                                (int(extra),))
                deleted += max(0, int(cur.rowcount or 0))
        c.commit()
        return deleted

    def _maybe_vacuum(self, deleted, now):
        """按阈值决定是否 VACUUM（不是每次 prune 都做）。

        Args:
            deleted (int): 本次 prune 删除的行数。
            now (float): 参考时间。

        Returns:
            bool: 真的执行了 VACUUM 为 True。
        """
        if deleted < VACUUM_MIN_DELETED_ROWS:
            return False
        if now - self._last_vacuum < VACUUM_MIN_INTERVAL_S:
            return False
        try:
            with self._lock:
                self._conn.execute('VACUUM')
        except sqlite3.Error as exc:
            # 另一个进程正在读/写时 VACUUM 会拿不到独占锁；下次再试即可，
            # 不重试（VACUUM 可能耗时很久，重试只会把记录器卡更久）。
            _log.warning('messages.db VACUUM skipped: %s', exc)
            return False
        self._last_vacuum = now
        return True

    # ------------------------------------------------------------------- read
    def _where(self, levels=None, sources=None, text=None, date_from=None,
               date_to=None, device=None):
        """拼出查询条件（供 :meth:`query`/:meth:`count` 共用）。

        Args:
            levels (Sequence[str]|None): 级别白名单。
            sources (Sequence[str]|None): 来源白名单。
            text (str|None): 全文关键词（FTS5 或 LIKE）。
            date_from (str|None): 起始日期（含）。
            date_to (str|None): 结束日期（含）。
            device (str|None): 按结构化附加字段 ``extra.device`` 过滤（JSON1；
                没编 JSON1 时退回 LIKE，见 :attr:`_json1`）。

        Returns:
            tuple[str, list]: ``(WHERE 子句, 参数列表)``。
        """
        clauses = []
        params = []
        if levels:
            clauses.append('m.level IN (%s)' % ','.join('?' * len(levels)))
            params.extend([str(l).lower() for l in levels])
        if sources:
            clauses.append('m.source IN (%s)' % ','.join('?' * len(sources)))
            params.extend([str(s) for s in sources])
        if device:
            if self._json1:
                clauses.append("json_extract(m.extra, '$.device') = ?")
                params.append(str(device))
            else:
                clauses.append('m.extra LIKE ?')
                params.append('%%"device":"%s"%%' % device)
        if date_from:
            clauses.append('m.date >= ?')
            params.append(str(date_from))
        if date_to:
            clauses.append('m.date <= ?')
            params.append(str(date_to))
        if text:
            if self._fts:
                clauses.append('m.id IN (SELECT rowid FROM messages_fts '
                               'WHERE messages_fts MATCH ?)')
                params.append(self._fts_query(text))
            else:
                clauses.append('(m.content LIKE ? OR m.source LIKE ?)')
                like = f'%{text}%'
                params.extend([like, like])
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return where, params

    @staticmethod
    def _fts_query(text):
        """把用户输入转成安全的 FTS5 查询串（引号包裹 + 前缀匹配）。

        Args:
            text (str): 用户输入关键词。

        Returns:
            str: FTS5 MATCH 表达式。
        """
        tokens = [t for t in str(text).replace('"', ' ').split() if t]
        return ' '.join('"%s"*' % t for t in tokens) or '""'

    def query(self, levels=None, sources=None, text=None, date_from=None,
              date_to=None, limit=200, offset=0, newest_first=True,
              device=None):
        """按条件分页查询消息。

        Args:
            levels (Sequence[str]|None): 级别白名单（如 ``['error']``）。
            sources (Sequence[str]|None): 来源白名单。
            text (str|None): 全文关键词。
            date_from (str|None): 起始日期 ``YYYY-MM-DD``（含）。
            date_to (str|None): 结束日期（含）。
            limit (int): 每页条数。
            offset (int): 偏移（分页用）。
            newest_first (bool): True 时按时间倒序（最新在前）。
            device (str|None): 按 ``extra.device`` 过滤。

        Returns:
            list[MessageRow]: 命中的消息行。
        """
        where, params = self._where(levels, sources, text, date_from, date_to,
                                    device)
        order = 'DESC' if newest_first else 'ASC'
        sql = (f'SELECT m.ts, m.level, m.source, m.content, m.extra '
               f'FROM messages m {where} ORDER BY m.ts {order}, m.id {order} '
               f'LIMIT ? OFFSET ?')
        with self._lock:
            cur = self._conn.execute(sql, params + [int(limit), int(offset)])
            out = []
            for row in cur.fetchall():
                extra = None
                if row['extra']:
                    try:
                        extra = json.loads(row['extra'])
                    except Exception:  # noqa: BLE001 - 坏 JSON 当成无附加字段
                        extra = None
                out.append(MessageRow(row['ts'], row['source'], row['level'],
                                      row['content'], extra))
            return out

    def count(self, levels=None, sources=None, text=None, date_from=None,
              date_to=None, device=None):
        """返回满足条件的消息条数（分页总数用）。

        Args:
            levels (Sequence[str]|None): 级别白名单。
            sources (Sequence[str]|None): 来源白名单。
            text (str|None): 全文关键词。
            date_from (str|None): 起始日期。
            date_to (str|None): 结束日期。
            device (str|None): 按 ``extra.device`` 过滤。

        Returns:
            int: 条数。
        """
        where, params = self._where(levels, sources, text, date_from, date_to,
                                    device)
        with self._lock:
            cur = self._conn.execute(
                f'SELECT COUNT(*) FROM messages m {where}', params)
            return int(cur.fetchone()[0])

    def level_counts(self, date_from=None, date_to=None):
        """按级别统计条数（用于界面上的级别计数徽标）。

        Args:
            date_from (str|None): 起始日期。
            date_to (str|None): 结束日期。

        Returns:
            dict: ``{级别: 条数}``。
        """
        where, params = self._where(date_from=date_from, date_to=date_to)
        with self._lock:
            cur = self._conn.execute(
                f'SELECT m.level, COUNT(*) AS n FROM messages m {where} '
                f'GROUP BY m.level', params)
            return {row['level']: int(row['n']) for row in cur.fetchall()}

    def sources(self, limit=200, date_from=None, date_to=None):
        """返回出现过的来源列表（按条数降序），供过滤器下拉框使用。

        Args:
            limit (int): 最多返回多少个来源。
            date_from (str|None): 只统计这个日期（含）之后的来源；大库上传参
                能直接限定索引范围，避免全表聚合。
            date_to (str|None): 只统计这个日期（含）之前的来源。

        Returns:
            list[str]: 来源名（消息来源脚本文件名）列表。
        """
        where, params = self._where(date_from=date_from, date_to=date_to)
        with self._lock:
            cur = self._conn.execute(
                'SELECT m.source, COUNT(*) AS n FROM messages m %s '
                'GROUP BY m.source ORDER BY n DESC LIMIT ?' % where,
                params + [int(limit)])
            return [row['source'] for row in cur.fetchall()]

    def devices(self, limit=200, date_from=None, date_to=None):
        """返回结构化附加字段里出现过的设备名（按条数降序）。

        用于历史页的“设备”下拉框：只有调用 ``send_*(..., extra={'device': ...})``
        的消息才有设备名，其余消息不会被列出来。

        Args:
            limit (int): 最多返回多少个设备名。
            date_from (str|None): 只统计这个日期（含）之后的消息；**强烈建议传**
                ——设备名在 ``extra`` JSON 里，没有索引，不限日期时每行都要
                解一次 JSON（大库上会耗时几十秒到几分钟，界面会像卡死）。
            date_to (str|None): 只统计这个日期（含）之前的消息。

        Returns:
            list[str]: 设备名列表；数据库没编 JSON1 时返回空列表。
        """
        if not self._json1:
            return []
        where, params = self._where(date_from=date_from, date_to=date_to)
        with self._lock:
            cur = self._conn.execute(
                "SELECT d, COUNT(*) AS n FROM (SELECT json_extract(extra, "
                "'$.device') AS d FROM messages m %s) WHERE d IS NOT NULL "
                'GROUP BY d ORDER BY n DESC LIMIT ?' % where,
                params + [int(limit)])
            return [row['d'] for row in cur.fetchall()]

    def close(self):
        """关闭数据库连接（幂等：重复调用或在从未打开的情况下都能安全返回）。"""
        with self._lock:
            try:
                self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            # 顺手把 WAL 并回主库：被别的进程挡着 checkpoint 时 ``-wal`` 会一直长
            # （GB 级），下次打开光恢复就要很久。别人还在读时 TRUNCATE 会失败，
            # 那就下次再说，不影响退出。
            try:
                self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
