"""Quatm Core: a headless runtime for quantum-experiment control.

The package is independent of the repository layout. Install the distribution
``quatm-core`` and import ``quatm`` from any user project. A user project
supplies ``configuration/``, ``experiments/`` and ``Data/``; its root is found
through ``QUATM_PROJECT_ROOT`` or the current working directory.

The optional desktop platform can layer additional subpackages onto the same
``quatm`` package. Core-only installations do not require them.
"""

# 版本单一事实源：改这里必须同步 pyproject.toml 的 [project] version。
__version__ = "1.0.1"

import importlib.util
import os
import pkgutil
import sys
import threading

# The public core may be combined with the private platform at build time.
# Extending the package path keeps ``quatm`` importable when both source trees
# are placed on PYTHONPATH without shipping proprietary code in this repository.
__path__ = pkgutil.extend_path(__path__, __name__)


def _maybe_run_first_run_setup():
    """Run the optional GUI project setup when a GUI platform is present.

    The core has no GUI dependency. When the optional platform provides
    ``quatm.project_setup`` and this process is a GUI entry point, it may create
    the minimal user-project skeleton before the GUI starts.

    Returns:
        bool: True when startup should continue.
    """
    if os.environ.get('QUATM_SKIP_FIRST_RUN'):
        return True
    if threading.current_thread() is not threading.main_thread():
        return True
    entry = os.path.basename(sys.argv[0] if sys.argv else '')
    if entry in ('first_run.py', 'project_setup.py'):
        return True
    try:
        from quatm.project_setup import ensure_project_ready
    except ImportError:
        return True
    return ensure_project_ready(entry=entry)

def _gui_available():
    """Return whether the optional GUI package is installed alongside the core."""
    try:
        return importlib.util.find_spec('quatm.GUI') is not None
    except Exception:  # noqa: BLE001 - a broken optional package is not fatal
        return False


if _gui_available() and not _maybe_run_first_run_setup():
    raise SystemExit(1)


