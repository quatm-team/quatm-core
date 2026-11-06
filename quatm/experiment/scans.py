# -*- coding: utf-8 -*-
"""扫描（scan）构建的**唯一实现**：GUI Browser 与 Web 后端共用同一份语义。

历史上 GUI ``ExperimentWindow.setup_scan()`` 与 Web ``ExperimentService.
scan_build()`` 各写了一套“把扫描定义变成 (参数名, 参数值矩阵, 运行顺序)”，
细节逐渐漂移（随机化一边用 stdlib ``random.shuffle``、一边用
``np.random.shuffle``；``parameters`` 缺省值也一边 ``[]``、一边
``['--NONE--']``）。本模块把算法收拢到一处，两边只调用 :func:`build_scan`。

支持的两种 ``settings`` 形态（来自实验属性树 ``Experiments/<实验名>/scans/<名>``，
与 GUI SequenceEditor 存的是同一份数据）：

A. 多参数（新格式，``listgenerators``）::

       {'parameters': ['a', 'b'],
        'listgenerators': ['np.arange(0, 3)', 'np.linspace(10, 20, 3)'],
        'Randomize': False}

   每个参数一个生成器表达式，逐项用 :func:`quatm.utils.safe_eval.safe_eval_numpy`
   求值；参数名是 ``--NONE--`` 的槽位用“第一个生成器的值的 zeros_like”占位。

B. 单参数（旧格式，``listgenerator``）::

       {'parameters': ['freq'], 'listgenerator': 'np.linspace(1, 5, 5)'}

   求值结果 0 维 → 包成 1 个元素；1 维 → 转成一列；2 维 → 原样使用。

公共可选键 ``Randomize``（缺省 ``True``）：为真时用 stdlib ``random.shuffle``
原地打乱运行顺序（**以 GUI 为真源**；Web 以前用 numpy 随机，已统一）。

语义细节（均以 GUI ``setup_scan()`` 为唯一真源）：

* ``listgenerators`` 分支的 ``parameters`` 缺省 ``[]``；单 ``listgenerator`` 分支
  缺省 ``['--NONE--']``；
* ``listgenerators`` 为空列表时，用 ``[np.zeros(1)]`` 兜底再 ``vstack(...).T``，
  得到 1 行 1 列的 0；
* 空/空白的生成器表达式（历史配置：``--NONE--`` 槽位存成 ``''``）按
  ``np.zeros(1)`` 兜底——表示该槽位不扫描；全 NONE 的扫描因此得到 1 次运行、
  列数 = 槽位个数（见 :func:`_eval_generator`）；
* 非法生成器表达式让 ``safe_eval_numpy`` 的原异常**原样抛出**，不静默吞。
"""

import random

import numpy as np

from quatm.utils.safe_eval import safe_eval_numpy


def _eval_generator(expression):
    """求值一个扫描生成器槽位；空/空白表达式按“该槽不扫描”的 ``np.zeros(1)`` 兜底。

    真实配置里存在“参数槽是 ``--NONE--`` 且 ``listgenerators`` 是空串”的扫描
    （tracked ``interaction_optical_trapping.AD-demo`` 三个槽位全是空串、Tiamo
    ``compensation_helper.can`` 同理）。旧实现对 ``--NONE--`` 槽位用
    ``zeros_like(safe_eval_numpy(generators[0]))``，第一个生成器是空串时
    ``safe_eval_numpy('')`` 抛 ``SyntaxError``，整条扫描直接提交失败。

    ``--NONE--`` 槽位本来就不扫描（该列值恒为 0），空串等价于“没有取值表达式”：
    用 ``np.zeros(1)`` 作为形状来源。于是全 NONE 的扫描得到 1 次运行、列数 =
    参数/槽位个数；正常扫描（非空表达式）的求值、形状、顺序、随机化完全不变。

    Args:
        expression (str): ``listgenerator`` / ``listgenerators`` 里的一项。

    Returns:
        numpy.ndarray: 求值结果；空/空白表达式返回 ``np.zeros(1)``。
    """
    if not isinstance(expression, str) or not expression.strip():
        return np.zeros(1)
    return safe_eval_numpy(expression)


def build_scan(settings, randomize=None, shuffle=None):
    """把一份扫描定义构建成参数名/参数值矩阵/运行顺序。

    Args:
        settings (dict): 扫描定义（见模块文档的两种形态）；不会被修改。
        randomize (bool|None): 是否打乱运行顺序；None 表示取 ``settings`` 里的
            ``Randomize``（缺省 True）。显式传入时覆盖 settings。
        shuffle (callable|None): 打乱函数，接收一个 list 原地修改（默认
            :func:`random.shuffle`）。测试可注入确定性实现，避免依赖具体随机值。

    Returns:
        tuple[list, numpy.ndarray, list[int]]: ``(scan_pars, scan_values,
        scan_sequence)``：

        * ``scan_pars``：参数名列表（与 settings 里的 ``parameters`` 同序，
          长度可能少于生成器个数——GUI 对多出的生成器按 ``--NONE--`` 处理）；
        * ``scan_values``：``(n_runs, n_pars)`` 的二维 ``numpy.ndarray``；
        * ``scan_sequence``：``list(range(n_runs))`` 经随机化后的运行顺序。

        运行次数没有单独的返回字段：``n_runs = len(scan_sequence)``
        （也等于 ``scan_values.shape[0]``），调用方按需导出。

    Raises:
        ValueError/NameError/...: 生成器表达式非法时由 ``safe_eval_numpy``
            原样抛出的异常。
    """
    if 'listgenerators' in settings:
        generators = settings.get('listgenerators', [''])
        scan_pars = list(settings.get('parameters', []))
        scan_values = []
        for index, generator in enumerate(generators):
            par = scan_pars[index] if index < len(scan_pars) else '--NONE--'
            if par != '--NONE--':
                scan_values.append(_eval_generator(generator))
            elif generators:
                scan_values.append(
                    np.zeros_like(_eval_generator(generators[0])))
        if not scan_values:
            scan_values = [np.zeros(1)]
        scan_values = np.vstack(scan_values).T
    else:
        scan_pars = list(settings.get('parameters', ['--NONE--']))
        scan_values = _eval_generator(settings.get('listgenerator', ''))
        if np.ndim(scan_values) == 0:
            scan_values = np.array([scan_values])
        if np.ndim(scan_values) == 1:
            scan_values = scan_values[:, np.newaxis]

    n_runs = int(scan_values.shape[0])
    scan_sequence = list(range(n_runs))
    if randomize is None:
        randomize = bool(settings.get('Randomize', True))
    if randomize:
        (shuffle or random.shuffle)(scan_sequence)
    return scan_pars, scan_values, scan_sequence
