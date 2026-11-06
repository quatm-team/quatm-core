"""Localization helpers for Quatm user-facing text.

Catalogs are JSON mappings from source English strings to translated strings.
The built-in catalog lives in ``quatm/locale/zh.json``; a user project can
override entries in ``<project>/configuration/locale/<lang>.json``. Missing
keys fall back to the source string, so a partial catalog never produces empty
labels.

Language selection (highest priority first):

1. ``QUATM_LANG=zh|en|auto``;
2. the project's ``configuration/locale/settings.json``;
3. the operating-system language (Chinese locales select ``zh``; others use
   the English source strings).

Public helpers include :func:`tr`, :func:`set_language`,
:func:`localized_label` and :func:`install_qt_translations`. The module is
standard-library only; Qt support is imported lazily by the optional GUI.
"""

import json
import os
import re
import sys

# 支持的语言包；第一个是“源码语言”（缺 key 时的回退）
SOURCE_LANGUAGE = 'en'
LANGUAGES = ('en', 'zh')

# 语言别名 → 规范名（QLocale 给的是 'zh_CN' / 'zh-Hans-CN' 这类）
_ALIASES = {
    'zh': 'zh', 'zh_cn': 'zh', 'zh_hans': 'zh', 'zh_hans_cn': 'zh',
    'zh_sg': 'zh', 'zh_tw': 'zh', 'zh_hk': 'zh', 'zh_mo': 'zh',
    'chinese': 'zh', 'cn': 'zh',
    'en': 'en', 'en_us': 'en', 'en_gb': 'en', 'english': 'en',
}

_state = {
    'language': None,      # 规范化后的当前语言；None 表示还没定
    'catalog': None,       # {原字符串: 译文}
    'sources': [],         # 本次实际读到的语言包路径（排查用）
    'missing': set(),      # 查不到的 key（供工具统计）
}

# Qt 自带翻译器的强引用（被 GC 掉翻译就失效，必须留一个）
_qt_translator = None


def locale_dir():
    """返回内置语言包目录（正常是 ``quatm/locale``）。

    历史：i18n 与语言包原先都住在 ``quatm/GUI/infra/`` 里（``GUI/infra/locale``），
    后来为了让**非 GUI** 的模块（servers / drivers / experiment / analysis）也能
    翻译消息、又不用导入 GUI 包，两者都搬了出来（``quatm/utils/i18n.py`` →
    现在统一在 ``quatm/i18n.py``，语言包在 ``quatm/locale/``）。

    注意**不能**按 ``__file__`` 的层级硬算目录：本模块历史上住过好几层，而按层级
    算错的后果是**整机界面静默变成英文**（找不到语言包时不报错、只回退原文），
    很难被发现。所以这里逐个候选目录探测，谁真的含语言包（``*.json``）就用谁；
    一个都没有时返回首选位置，让调用方明确“缺语言包”。

    Returns:
        str: 内置语言包目录的绝对路径。
    """
    import quatm                 # 只读 __file__；quatm/__init__.py 极轻
    pkg = os.path.dirname(os.path.abspath(quatm.__file__))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(pkg, 'locale'),                    # 现在的位置
        os.path.join(here, 'locale'),                   # 语言包与模块同目录
        os.path.normpath(os.path.join(here, '..', 'locale')),
        os.path.join(pkg, 'GUI', 'infra', 'locale'),    # 历史位置（旧部署）
    )
    for path in candidates:
        try:
            if any(name.endswith('.json') for name in os.listdir(path)):
                return path
        except OSError:
            continue
    return os.path.join(pkg, 'locale')


def project_locale_dir():
    """返回项目级语言包目录（可覆盖内置包，可不存在）。

    Returns:
        str: ``<项目根>/configuration/locale``；项目根取不到时返回空串。
    """
    try:
        from quatm.servers.configreader import PROJECT_ROOT
        return os.path.join(str(PROJECT_ROOT), 'configuration', 'locale')
    except Exception:  # noqa: BLE001 - 没有项目根（如纯库环境）就没有覆盖
        return ''


def project_settings_path():
    """返回项目级语言设置文件路径（``configuration/locale/settings.json``）。

    Returns:
        str: 路径；没有项目根时返回空串。
    """
    d = project_locale_dir()
    return os.path.join(d, 'settings.json') if d else ''


def normalize_language(name):
    """把各种写法（``zh_CN`` / ``zh-Hans-CN`` / ``Chinese``）规范成语言名。

    Args:
        name: 语言名（字符串）；其它类型/空值 → ``''``。

    Returns:
        str: ``'zh'`` / ``'en'``；不认识的语言返回 ``''``。
    """
    if not name:
        return ''
    key = str(name).strip().lower().replace('-', '_')
    if key in _ALIASES:
        return _ALIASES[key]
    for part in (key, key.split('_')[0]):
        if part in _ALIASES:
            return _ALIASES[part]
    return ''


def _env_language():
    """读 ``QUATM_LANG``（``auto``/空 = 未指定）。

    Returns:
        str: 规范化的语言名；未指定或不认识时返回 ``''``。
    """
    raw = os.environ.get('QUATM_LANG', '').strip().lower()
    if not raw or raw == 'auto':
        return ''
    return normalize_language(raw)


def _project_language():
    """读项目设置 ``configuration/locale/settings.json`` 的 ``language``。

    Returns:
        str: 规范化的语言名；没有文件/不合法时返回 ``''``。
    """
    path = project_settings_path()
    if not path or not os.path.isfile(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - 设置坏了就当没设
        return ''
    if isinstance(data, dict):
        raw = data.get('language')
    else:
        raw = data
    if str(raw).strip().lower() == 'auto':
        return ''
    return normalize_language(raw)


def _macos_preferred_languages():
    """读取 macOS 的「首选语言」列表（如 ``['zh-Hans-DE', 'en-DE']``）。

    为什么不用 ``QLocale.system()``：终端里常常有 ``LANG=C.UTF-8``（或
    ``en_US.UTF-8``），Qt 会跟着它报 ``en_US``，于是中文系统的用户看到英文。
    这里直接向 CoreFoundation 要"系统首选语言"（不受进程 locale 影响），
    取不到时退回 ``defaults read -g AppleLanguages``。

    Returns:
        list[str]: 语言标记列表（按优先级）；非 macOS / 取不到时为空列表。
    """
    if sys.platform != 'darwin':
        return []
    try:
        import ctypes
        import ctypes.util
        path = (ctypes.util.find_library('CoreFoundation')
                or '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cf = ctypes.cdll.LoadLibrary(path)
        cf.CFLocaleCopyPreferredLanguages.restype = ctypes.c_void_p
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        arr = cf.CFLocaleCopyPreferredLanguages()
        out = []
        for i in range(cf.CFArrayGetCount(arr)):
            buf = ctypes.create_string_buffer(256)
            item = cf.CFArrayGetValueAtIndex(arr, i)
            if cf.CFStringGetCString(item, buf, 256, 0x08000100):   # UTF-8
                out.append(buf.value.decode('utf-8', 'replace'))
        if out:
            return out
    except Exception:  # noqa: BLE001 - 没 ctypes/库不存在都走下面的退路
        pass
    try:
        import subprocess
        raw = subprocess.run(['defaults', 'read', '-g', 'AppleLanguages'],
                             capture_output=True, text=True, timeout=3).stdout
        return re.findall(r'"([^"]+)"', raw)
    except Exception:  # noqa: BLE001
        return []


def _system_language():
    """检测系统默认语言：中文 → ``'zh'``，其余 → ``'en'``。

    顺序：macOS「首选语言」（不受终端 LANG 影响）→ Qt ``QLocale.system()``
    的 ``uiLanguages()``/``name()`` → 环境变量 ``LC_ALL``/``LC_CTYPE``/``LANG``/
    ``LANGUAGE``。只看**列表里第一个**能识别的语言：是中文就用中文包，
    其余一律英文（符合“非中文 → 英文”的约定）。

    Returns:
        str: ``'zh'`` 或 ``'en'``。
    """
    candidates = list(_macos_preferred_languages())
    try:
        from PySide6.QtCore import QLocale
        loc = QLocale.system()
        candidates.extend(loc.uiLanguages() or [])
        candidates.append(loc.name() or '')
    except Exception:  # noqa: BLE001 - 没有 Qt 就只看环境变量
        pass
    for var in ('LC_ALL', 'LC_CTYPE', 'LANG', 'LANGUAGE'):
        value = os.environ.get(var, '').strip()
        if value and value.lower() not in ('c', 'posix'):
            candidates.append(value)
    for name in candidates:
        lang = normalize_language(name)
        if lang:
            return lang
    return SOURCE_LANGUAGE


def system_language():
    """返回“这台机器应该用哪个语言包”（含环境变量/项目设置覆盖）。

    Returns:
        str: ``'zh'`` 或 ``'en'``（也只会是 :data:`LANGUAGES` 里的值）。
    """
    return _env_language() or _project_language() or _system_language()


def _read_catalog(path):
    """读一个语言包文件（坏文件/不存在都当空表，绝不抛）。

    直接 try open，**不先** ``os.path.isfile``：启动路径上少一次文件系统访问
    （这个函数在每次进程启动装语言包时都会跑）。

    Args:
        path (str): 语言包路径。

    Returns:
        dict: ``{原字符串: 译文}``。
    """
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - 语言包坏了不能让界面起不来
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def catalog_paths(language=None):
    """返回某语言要读的语言包路径（内置在前、项目覆盖在后）。

    Args:
        language (str|None): 语言名；None 用当前语言。

    Returns:
        list[str]: 路径列表（可能不存在的路径也照列，便于排查）。
    """
    lang = normalize_language(language) or _state['language'] or SOURCE_LANGUAGE
    paths = [os.path.join(locale_dir(), '%s.json' % lang)]
    project = project_locale_dir()
    if project:
        paths.append(os.path.join(project, '%s.json' % lang))
    return paths


def load_catalog(language=None):
    """装载语言包（内置 + 项目覆盖，后者优先）。

    Args:
        language (str|None): 语言名；None 用当前语言。

    Returns:
        dict: ``{原字符串: 译文}``。
    """
    catalog = {}
    for path in catalog_paths(language):
        catalog.update(_read_catalog(path))
    return catalog


def language():
    """返回当前生效的语言（第一次调用时自动确定）。

    Returns:
        str: ``'zh'`` 或 ``'en'``。
    """
    if _state['language'] is None:
        set_language(system_language())
    return _state['language']


def set_language(language_name, app=None):
    """切换语言并重新装载语言包（之后新建的界面用新语言）。

    Args:
        language_name (str|None): ``'zh'`` / ``'en'`` / ``'auto'`` / None；
            不认识的值退回系统检测结果。
        app: QApplication；给了就顺手换掉 Qt 自带翻译器（标准按钮文字）。

    Returns:
        str: 实际生效的语言。
    """
    raw = str(language_name or '').strip().lower()
    if raw in ('', 'auto'):
        lang = normalize_language(system_language()) or SOURCE_LANGUAGE
    else:
        lang = (normalize_language(raw)
                or normalize_language(system_language())
                or SOURCE_LANGUAGE)
    _state['language'] = lang
    _state['sources'] = [p for p in catalog_paths(lang) if os.path.isfile(p)]
    _state['catalog'] = load_catalog(lang)
    _state['missing'].clear()
    install_qt_translations(app, lang)
    return lang


def available_languages():
    """列出可以切换到的语言。

    源码语言（``en``）总是可用（不需要语言包）；其余语言要有语言包才算。

    Returns:
        list[str]: 形如 ``['en', 'zh']``。
    """
    out = [SOURCE_LANGUAGE]
    for lang in LANGUAGES:
        if lang != SOURCE_LANGUAGE and any(os.path.isfile(p)
                                           for p in catalog_paths(lang)):
            out.append(lang)
    return out


def catalog():
    """返回当前语言包内容（拷贝，供检查/工具使用）。

    Returns:
        dict: ``{原字符串: 译文}``。
    """
    language()          # 确保已装载
    return dict(_state['catalog'])


def missing_keys():
    """返回本次运行中查过但语言包里没有的字符串（供翻译补全工具用）。

    Returns:
        list[str]: 按首次出现顺序排列的原字符串。
    """
    return list(_state['missing'])


def tr(text, **kwargs):
    """把代码里的原字符串翻译成当前语言（缺 key 就用原文）。

    Args:
        text: 原字符串（**一律英文**；源码里不该出现中文界面文字）。
        **kwargs: 占位符值；**只有给了参数**才会做 ``.format(**kwargs)``，
            避免把 JSON 示例里的大括号当占位符。

    Returns:
        str: 译文；语言包与原字符串都没有时返回 ``str(text)``。
    """
    if text is None:
        return ''
    key = text if isinstance(text, str) else str(text)
    lang = language()
    out = _state['catalog'].get(key)
    if out is not None:
        text = out
    elif lang != SOURCE_LANGUAGE or not key.isascii():
        # 需要翻译（或 key 是非 ASCII——源码里写了中文原文，属于该修的问题）：
        # 记下来给 tools/i18n/check.py 用。
        _state['missing'].add(key)
    if kwargs and isinstance(text, str):
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass            # 占位符对不上就原样返回，绝不因为翻译而崩界面
    return text


def localized_label(text, bilingual=True):
    """把名字做成**双语标签**：中文界面给 ``中文（English）``，其它情况原样返回。

    给**显示名**这类“专有名词”用：GUI 名、窗口名、平台身份（macOS 菜单栏/Dock、
    任务栏、``.desktop``/``InfoPlist.strings``）与帮助/关于正文里的窗口名。好处是
    中文用户看得懂，同时还能和英文界面、日志、脚本名对得上。

    与 :func:`tr` 的区别：``tr`` 是“全量翻译”；本函数只在**语言包里恰好有这个名
    字**时才动手，所以用户自定义的名字（没有译名）会原样透传，不会拼出半截括号。

    Args:
        text (str): 源码里的英文名（如 ``'Stream Monitor'``）。
        bilingual (bool): True 给 ``中文（English）``；False 只给 ``中文``。

    Returns:
        str: 展示用名字；没有译名或当前是英文界面时原样返回 ``text``。
    """
    translated = tr(text)
    if not translated or translated == text:
        return text
    return '%s（%s）' % (translated, text) if bilingual else translated


def init_i18n(app=None, language=None):
    """初始化 i18n（幂等）：确定语言、装载语言包、返回生效的语言。

    由 ``setup_app_theme`` 在 QApplication 建好后调用一次；``tr()`` 也会在
    首次使用时自动初始化，所以模块级常量即使更早求值也不会出错。

    Args:
        app: QApplication；给了就顺手装上 Qt 自带翻译（见
            :func:`install_qt_translations`）。
        language (str|None): 强制语言（``'zh'``/``'en'``/``'auto'``）。

    Returns:
        str: 生效的语言。
    """
    if language is None and _state['language'] is not None:
        return _state['language']
    return set_language(language if language is not None else system_language(),
                        app=app)


def install_qt_translations(app, language=None):
    """给 Qt 自己的控件装上官方翻译（标准按钮、文件对话框、日历…）。

    为什么需要：``QDialogButtonBox(Ok | Cancel)``、``QFileDialog``、
    ``QMessageBox`` 的按钮文字由 Qt 提供，不走我们的语言包；不装翻译器时
    中文界面里会出现 ``OK``/``Cancel``/``Save`` 这种英文按钮——这正是
    早期版本“大部分中文、个别按钮英文”的来源。

    翻译文件来自 PySide6 自带的 ``Qt/translations/qtbase_<lang>.qm``；
    ``en`` 是 Qt 的源语言，不需要（也会主动卸掉旧翻译器）。

    Args:
        app: QApplication；None 时直接返回 False。
        language (str|None): 语言名；None 用当前语言。

    Returns:
        bool: 是否成功装上翻译器。
    """
    global _qt_translator
    if app is None:
        return False
    lang = normalize_language(language or _state['language'] or system_language())
    try:
        from PySide6.QtCore import QLibraryInfo, QTranslator
    except Exception:  # noqa: BLE001 - 无 Qt 时静默（纯 CLI 路径）
        return False
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    names = {'zh': ('qtbase_zh_CN', 'qt_zh_CN')}.get(lang, ())
    if not names:
        return False
    try:
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    except Exception:  # noqa: BLE001
        return False
    for name in names:
        translator = QTranslator(app)
        if translator.load(name, path):
            app.installTranslator(translator)
            _qt_translator = translator   # 持引用：被 GC 掉翻译就失效
            return True
    return False
