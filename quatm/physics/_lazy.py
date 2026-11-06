"""极小惰性导入助手：把重量级依赖推迟到首次真正使用。

``quatm.physics`` 里有一部分模块直接来自分析 notebook，原先把
``matplotlib`` / ``tqdm`` / ``influxdb_client`` 写在模块顶层。后果是任何
``import quatm.physics.odt.evaluations`` 都会在**导入期**拉起整套绘图栈
（无显示器/离屏环境、只需要数值计算的脚本、CI 都会被拖慢甚至报错）。

这里提供两个零依赖的小代理：

* :func:`lazy_module` —— 代理一个模块，``plt = lazy_module('matplotlib.pyplot')``
  之后照常 ``plt.subplots(...)``，首次属性访问时才 import；
* :func:`lazy_attr` —— 代理一个可调用对象（类/函数），
  ``Parameters = lazy_attr('lmfit', 'Parameters')`` 之后 ``Parameters()`` 等价。

限制：代理对象不是真正的类，**不能**用于 ``isinstance``、基类继承或类型标注。
确实需要类型本身的模块（例如 ``models.py`` 用 ``lmfit.Model`` 作基类）应保留
顶层导入，并在文档里写明。
"""

import importlib


class _LazyModule:
    """按需 import 的模块代理（只转发属性访问）。"""

    __slots__ = ('_name', '_module')

    def __init__(self, name):
        """记录模块名，暂不导入。

        Args:
            name (str): 完整模块名，如 ``'matplotlib.pyplot'``。
        """
        self._name = name
        self._module = None

    def _load(self):
        """首次使用时导入并缓存真实模块。

        Returns:
            module: 真实模块对象。
        """
        if self._module is None:
            self._module = importlib.import_module(self._name)
        return self._module

    def __getattr__(self, attr):
        """转发到真实模块的属性。"""
        if attr.startswith('__') and attr.endswith('__'):
            # 让 copy/pickle/调试器看到的是代理本身，而不是触发一次 import
            raise AttributeError(attr)
        return getattr(self._load(), attr)

    def __repr__(self):
        """调试输出：尚未加载时明确标记为惰性。"""
        state = 'loaded' if self._module is not None else 'lazy'
        return '<lazy module %r (%s)>' % (self._name, state)


class _LazyCallable:
    """按需 import 的可调用对象代理（用于类/函数）。"""

    __slots__ = ('_module_name', '_attr', '_target')

    def __init__(self, module_name, attr):
        """记录模块名与属性名。

        Args:
            module_name (str): 完整模块名。
            attr (str): 模块内的属性名（通常是类名）。
        """
        self._module_name = module_name
        self._attr = attr
        self._target = None

    def _load(self):
        """首次调用时解析目标对象。"""
        if self._target is None:
            module = importlib.import_module(self._module_name)
            self._target = getattr(module, self._attr)
        return self._target

    def __call__(self, *args, **kwargs):
        """等价于调用真实对象。"""
        return self._load()(*args, **kwargs)

    def __getattr__(self, attr):
        """转发类属性/静态方法访问（不触发实例化）。"""
        if attr.startswith('__') and attr.endswith('__'):
            raise AttributeError(attr)
        return getattr(self._load(), attr)

    def __repr__(self):
        """调试输出。"""
        state = 'loaded' if self._target is not None else 'lazy'
        return '<lazy callable %s.%s (%s)>' % (self._module_name, self._attr, state)


def lazy_module(name):
    """返回模块代理。

    Args:
        name (str): 完整模块名。

    Returns:
        _LazyModule: 首次属性访问时才 import 的代理。
    """
    return _LazyModule(name)


def lazy_attr(module_name, attr):
    """返回可调用对象代理。

    Args:
        module_name (str): 完整模块名。
        attr (str): 模块内属性名。

    Returns:
        _LazyCallable: 首次调用时才 import 的代理。
    """
    return _LazyCallable(module_name, attr)
