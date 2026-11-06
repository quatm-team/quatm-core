"""Compatibility import path for :mod:`quatm.i18n`.

The implementation lives in :mod:`quatm.i18n`. This module only re-exports its
public API so existing imports continue to work and both paths share one
language state.
"""

from quatm.i18n import *  # noqa: F401,F403
from quatm.i18n import (  # noqa: F401
    LANGUAGES,
    SOURCE_LANGUAGE,
    available_languages,
    catalog,
    catalog_paths,
    init_i18n,
    install_qt_translations,
    language,
    load_catalog,
    locale_dir,
    localized_label,
    missing_keys,
    normalize_language,
    project_locale_dir,
    project_settings_path,
    set_language,
    system_language,
    tr,
)


def __getattr__(name):
    """把实现模块里未显式列出的名字也转发过来。

    Args:
        name (str): 要取的名字。

    Returns:
        object: 实现模块里的同名对象。

    Raises:
        AttributeError: 实现模块里也没有这个名字。
    """
    import quatm.i18n as _impl
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            'module %r has no attribute %r' % (__name__, name)) from None
