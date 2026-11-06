"""受限求值：用于生成扫描值的数值表达式安全求值。

扫描系统（quatm/GUI/browser.py）历史上对生成器字符串（如 ``np.linspace(0, 10,
100)``，存于共享属性树/配置文件）直接用内建 ``eval()``。eval 会执行*任意*
Python 代码——机器上任何可写文件（如属性文件）都可能向实验控制进程注入代码。

本模块提供等价的受限实现：只允许白名单内的 numpy 函数、少量只读内置
（``enumerate``/``len``/``range``/``zip``，供扫描生成器把网格展开成一维）、
推导式/lambda 这些纯计算语法与数值字面量，其余一律抛 ``ValueError``。

安全边界（2026-11 收紧）：
* **递归校验所有子节点**——包括推导式的 ``elt``/``ifs``/``iter``、lambda 体，
  以及数组方法调用链的底层表达式（``np.array([...]).flatten()`` 的
  ``np.array(...)`` 参数以前没被遍历，推导式因此绕过白名单直接执行）；
* 名字只允许 ``np``、调用方显式传入的 ``allowed_names`` 与推导式/lambda 的
  局部绑定名；函数只允许 numpy 白名单函数与 ``_SAFE_BUILTINS`` 里的只读内置；
* 属性只允许 ``np.<白名单>`` 与白名单数组方法，任何 ``_`` 开头的 dunder
  一律拒绝（``np.__dict__``、``np.ctypeslib`` 等）。
"""
import ast
import functools

import numpy as np

# numpy attribute / function names that may be used inside a scan generator.
# Anything else (including any dunder attribute) is rejected.
_ALLOWED_NUMPY_NAMES = {
    # array creation
    'linspace', 'arange', 'logspace', 'geomspace', 'zeros', 'ones', 'full',
    'empty', 'array', 'asarray', 'fromiter', 'repeat', 'tile', 'meshgrid',
    'reshape', 'flatten', 'ravel', 'concatenate', 'vstack', 'hstack', 'stack',
    'append', 'transpose', 'swapaxes', 'copy', 'flip', 'fliplr', 'flipud',
    # elementwise maths
    'sin', 'cos', 'tan', 'arcsin', 'arccos', 'arctan', 'exp', 'log', 'log10',
    'log2', 'sqrt', 'cbrt', 'abs', 'fabs', 'sign', 'floor', 'ceil', 'rint',
    'power', 'square', 'mod', 'maximum', 'minimum', 'clip', 'round',
    # constants
    'pi', 'e', 'inf', 'nan', 'newaxis',
    # random (values only, e.g. np.random.rand(...))
    'random', 'rand', 'randn', 'randint', 'uniform', 'normal', 'choice',
    'permutation',
    # misc useful for scans
    'zeros_like', 'ones_like', 'full_like', 'empty_like', 'where', 'indices',
    'cumsum', 'cumprod', 'diff', 'sort', 'argsort',
}

# methods that may be *called on the result* of an allowed expression, e.g.
# ``np.linspace(0, 1, 10).reshape(10, 1).flatten()``. Dunder access is always
# rejected.
_ALLOWED_ARRAY_METHODS = {
    'reshape', 'flatten', 'ravel', 'transpose', 'T', 'astype', 'copy',
    'conj', 'conjugate', 'real', 'imag', 'sum', 'mean', 'min', 'max', 'std',
    'cumsum', 'cumprod', 'dot', 'swapaxes', 'squeeze', 'repeat', 'take',
    'argmax', 'argmin', 'nonzero', 'clip', 'round',
}

#: 放行的“只读内置函数”：名字 → 实现。都只做纯计算、无 I/O、不接受代码对象。
#: 扫描生成器表达式（``listgenerators``）用它们把多维网格展开成一维序列，例如
#: tracked 配置 ``configuration/properties/properties.json`` 里的 2D 扫描：
#: ``np.array([np.flip(rep) if enum % 2 == 0 else rep
#:             for enum, rep in enumerate(np.repeat(...))]).flatten()``。
#: ``enumerate`` 是真实配置用到的；``len``/``range``/``zip`` 属同级别只读内置
#: （无 I/O、只处理数值/序列），一并放行，避免以后写扫描再改这里。
_SAFE_BUILTINS = {
    'enumerate': enumerate,
    'len': len,
    'range': range,
    'zip': zip,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.FloorDiv, ast.Mod,
    ast.MatMult,
    ast.USub, ast.UAdd, ast.Invert, ast.Not,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,   # & | ^ << >>
    ast.Constant,          # numbers, strings, None
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Name,              # only whitelisted names (e.g. "np", "a")
    ast.Attribute,         # only np.<whitelisted-name> chains
    ast.Call,
    ast.keyword,
    ast.Slice,
    ast.Load, ast.Store, ast.Del,
    ast.And, ast.Or,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.IfExp,
    ast.Subscript,
    ast.Starred,
    ast.BoolOp,
    ast.UnaryOp,
)


class _Visitor(ast.NodeVisitor):
    """AST 访问器：校验表达式只使用白名单语法与名字。

    遍历期间把首个违规原因记录到 self.error；调用方据此抛 ValueError。

    Args:
        allowed_names (set[str]): 允许引用的裸名字集合（不含 'np'）。
    """
    def __init__(self, allowed_names):
        """构造访问器。

        Args:
            allowed_names (set[str]): 允许引用的裸名字集合（不含 'np'）。
        """
        self.allowed_names = set(allowed_names)
        self.error = None

    def generic_visit(self, node):
        """默认访问：不在白名单中的语法节点一律拒绝。

        Args:
            node (ast.AST): 当前节点。
        """
        if not isinstance(node, _ALLOWED_NODES):
            self.error = 'disallowed syntax: {}'.format(type(node).__name__)
            return
        super().generic_visit(node)

    def _fail(self, message):
        """记录首个违规原因（只记第一条，避免被后续错误覆盖）。"""
        if self.error is None:
            self.error = message

    def _bind_target(self, target):
        """把推导式 ``for`` 的 target 绑定为局部名字（只允许纯名字/元组）。

        不允许 ``for a[0] in ...`` / ``for obj.attr in ...`` 这类目标：那会在
        运行时写调用方传入的对象（副作用），不是纯数值计算。
        """
        for node in ast.walk(target):
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                self._fail('unsupported comprehension target '
                           '(only plain names allowed)')
                return False
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                self.allowed_names.add(node.id)
        return True

    def _visit_comprehension(self, generators, visit_body):
        """校验推导式：按顺序校验各 ``for`` 的 iter/ifs 并绑定 target，最后 elt。

        这是安全修复的一部分：以前推导式不在 ``_ALLOWED_NODES`` 里，但因为
        ``visit_Attribute`` 的数组方法分支不遍历底层表达式，含推导式的表达式
        可以**完全绕过校验**直接 eval。现在递归校验，且 target 名字只在该
        推导式作用域内有效（退出后恢复外部名字集合）。
        """
        saved_names = set(self.allowed_names)
        try:
            for generator in generators:
                if self.error is not None:
                    return
                self.visit(generator.iter)
                if not self._bind_target(generator.target):
                    return
                for condition in generator.ifs:
                    self.visit(condition)
            if self.error is None:
                visit_body()
        finally:
            self.allowed_names = saved_names

    def visit_ListComp(self, node):
        """列表推导式：``[elt for ...]``。"""
        self._visit_comprehension(
            node.generators, lambda: self.visit(node.elt))

    def visit_SetComp(self, node):
        """集合推导式：``{elt for ...}``。"""
        self._visit_comprehension(
            node.generators, lambda: self.visit(node.elt))

    def visit_GeneratorExp(self, node):
        """生成器表达式：``(elt for ...)``。"""
        self._visit_comprehension(
            node.generators, lambda: self.visit(node.elt))

    def visit_DictComp(self, node):
        """字典推导式：``{key: value for ...}``。"""
        def visit_body():
            self.visit(node.key)
            self.visit(node.value)

        self._visit_comprehension(node.generators, visit_body)

    def visit_Lambda(self, node):
        """lambda：参数名按局部绑定，默认值与函数体都要递归校验。"""
        saved_names = set(self.allowed_names)
        try:
            arguments = node.args
            for argument in (list(getattr(arguments, 'posonlyargs', []))
                             + list(arguments.args)):
                self.allowed_names.add(argument.arg)
            if arguments.vararg is not None:
                self.allowed_names.add(arguments.vararg.arg)
            for argument in arguments.kwonlyargs:
                self.allowed_names.add(argument.arg)
            if arguments.kwarg is not None:
                self.allowed_names.add(arguments.kwarg.arg)
            for default in arguments.defaults:
                self.visit(default)
            for default in arguments.kw_defaults:
                if default is not None:
                    self.visit(default)
            self.visit(node.body)
        finally:
            self.allowed_names = saved_names

    def visit_Name(self, node):
        """校验裸名字：只允许白名单内的名字（np / 调用方名字 / 局部绑定）。

        Args:
            node (ast.Name): 名字节点。
        """
        # 推导式/lambda 的 target 是“绑定”不是“引用”，不需要在外部白名单里
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            return
        # only whitelisted bare names may be referenced (np, or e.g. 'a')
        if node.id not in self.allowed_names:
            self._fail('disallowed name: {!r}'.format(node.id))

    def visit_Attribute(self, node):
        """校验属性访问：只允许 np.<白名单名>、链式 numpy 属性与数组方法。

        Args:
            node (ast.Attribute): 属性节点。
        """
        if node.attr.startswith('_'):
            self._fail('dunder attribute access is not allowed')
            return
        # np.<name>  (and chains like np.random.rand)
        if isinstance(node.value, ast.Name) and node.value.id == 'np':
            if node.attr not in _ALLOWED_NUMPY_NAMES:
                self._fail('disallowed numpy attribute: {!r}'.format(node.attr))
            return
        # np.random.rand -> attribute on np.random
        if isinstance(node.value, ast.Attribute):
            self.visit(node.value)
            if node.attr not in _ALLOWED_NUMPY_NAMES:
                self._fail('disallowed numpy attribute: {!r}'.format(node.attr))
            return
        # method call / attribute on the result of an allowed expression, e.g.
        # np.linspace(0, 1, 10).reshape(...) or some_array.T
        if isinstance(node.value, (ast.Call, ast.List, ast.Tuple, ast.Constant,
                                   ast.Subscript)):
            # 安全修复：递归校验底层表达式。以前这里直接 return，导致
            # ``np.array([...推导式...]).flatten()`` 里的推导式逃过校验。
            self.visit(node.value)
            if node.attr not in _ALLOWED_ARRAY_METHODS:
                self._fail('disallowed array attribute: {!r}'.format(node.attr))
            return
        self._fail('attribute access on unsupported object')

    def visit_Call(self, node):
        """校验函数调用：只允许 numpy 函数（属性形式）与放行的只读内置函数。

        Args:
            node (ast.Call): 调用节点。
        """
        if isinstance(node.func, ast.Attribute):
            self.visit(node.func)
        elif isinstance(node.func, ast.Name) and node.func.id in _SAFE_BUILTINS:
            # 只读内置（enumerate/len/range/zip）：名字已按白名单判定
            pass
        else:
            self._fail('only numpy functions / whitelisted read-only builtins '
                       'may be called')
            return
        for arg in node.args:
            self.visit(arg)
        for keyword in node.keywords:
            self.visit(keyword)


@functools.lru_cache(maxsize=256)
def _compile_expression(expression, allowed_names):
    """校验并编译一个表达式（结果缓存）。

    为什么缓存：本模块的入口在 h5storage 的过滤器里是**每个数据点**都被调用的
    （Y 轴逐点 ``getFilter``），实测 ``ast.parse`` + ``compile`` 就要 22 µs/次
    （200 点 × 2 通道 ≈ 9 ms/刷新）。表达式与允许名集合都是稳定字符串，缓存
    完全安全：``__builtins__`` 只有 ``_SAFE_BUILTINS`` 里的只读内置，代码
    对象本身不携带任何调用状态。

    Args:
        expression (str): 表达式字符串。
        allowed_names (tuple): 允许引用的裸名字（**排序后的元组**，可哈希）。

    Returns:
        code: 可直接交给 :func:`eval` 的代码对象。

    Raises:
        ValueError: 表达式用到白名单之外的内容。
        SyntaxError: 表达式本身语法错误。
    """
    tree = ast.parse(expression, mode='eval')
    visitor = _Visitor(set(allowed_names))
    visitor.visit(tree)
    if visitor.error is not None:
        raise ValueError('unsafe expression {!r}: {}'.format(
            expression, visitor.error))
    return compile(tree, '<safe_eval_numpy>', 'eval')


def safe_eval_numpy(expression, context=None, allowed_names=None):
    """使用白名单内的 numpy 子集求值数值表达式（受限安全 eval）。

    Args:
        expression (str): 表达式字符串，如 'np.linspace(0, 10, 100)'。
        context (dict|None): 附加的名字 → 值绑定，如 {'a': arr}。
        allowed_names (iterable|None): 允许引用的附加裸名字（除 'np' 外），
            须出现在 context 中。

    Returns:
        求值结果（通常为 numpy 数组）。

    Raises:
        ValueError: 表达式用到白名单之外的内容。
        (SyntaxError, NameError, TypeError, ...): 输入本身有语法/运行错误时。
    """
    if not isinstance(expression, str):
        raise ValueError('scan generator must be a string, got {!r}'.format(
            type(expression).__name__))

    if allowed_names is None:
        allowed_names = set()
    tree_allowed = set(allowed_names) | {'np'}
    allowed = tree_allowed
    if context is None:
        context = {}
    # any extra name must have a value or evaluation would NameError anyway
    missing = allowed - {'np'} - set(context.keys())
    if missing:
        raise ValueError('missing values for names: {}'.format(sorted(missing)))

    # 编译（与安全校验）走缓存，理由见 _compile_expression
    code = _compile_expression(expression, tuple(sorted(allowed)))
    env = {'np': np, '__builtins__': {}}
    # 只读内置（enumerate/len/range/zip）通过普通名字暴露给表达式；不暴露
    # 任何其它 builtins（open/eval/__import__/getattr 等都不在 env 里）。
    env.update(_SAFE_BUILTINS)
    env.update(context)
    return eval(code, env)
