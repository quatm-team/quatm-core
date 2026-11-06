"""Wilson 二项比例置信区间（binomial proportion confidence interval）。

自 ``quatm/physics/odt/utils.py``（原 ``pytiamo`` 包）拆出——quatm 项目只用
其中的 ``wilson_err``（用于 quatm/GUI/widgets/eventfunctions.py）。

参见 https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
"""

import numpy as np

__all__ = ['wilsonC', 'wilsonU', 'wilsonL', 'wilson_score', 'wilson_err']


def _guarded(fn):
    """静默除零/无效值警告：n==0 时公式自然产生 NaN（调用方负责过滤
    空样本），不应该向终端刷 RuntimeWarning——Looper 高频跑实验时每任务
    刷一堆警告会严重拖慢 Windows 上的 GUI 进程。

    Args:
        fn (callable): 被包装的函数。

    Returns:
        callable: 抑制 numpy 除零/无效值警告的包装函数。
    """
    def wrapper(*args, **kwargs):
        with np.errstate(divide='ignore', invalid='ignore'):
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


@_guarded
def wilsonC(n, k, z=1):
    """Wilson 区间的中心值。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间中心。
    """
    center = (k/n + z**2/(2*n))/(1+z**2/n)
    return center


@_guarded
def wilsonU(n, k, z=1):
    """Wilson 区间的上限。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间上限（截断到 ≤1）。
    """
    upper = np.minimum(1, (1/(1.+z**2/n))*(k/n+z**2/(2*n)+z*np.sqrt((k/n**2)*(1.-k/n)+(z**2/(4.*n**2)))))
    return upper


@_guarded
def wilsonL(n, k, z=1):
    """Wilson 区间的下限。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间下限（截断到 ≥0）。
    """
    lower = np.maximum(0, (1./(1.+z**2/n))*(k/n+z**2./(2.*n)-z*np.sqrt((k/n**2)*(1.-k/n)+(z**2/(4.*n**2)))))
    return lower


@_guarded
def wilson_score(n, k, z=1):
    """Wilson 区间三要素：下限、中心、上限。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        (lower, center, upper): Wilson 区间三元组。
    """
    lower = wilsonL(n, k, z)
    center = wilsonC(n, k, z)
    upper = wilsonU(n, k, z)
    return lower, center, upper


@_guarded
def wilson_err(n, k, z=1):
    """Wilson 区间：估计概率及上下误差棒长度。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (int): z-sigma 区间（默认 1）。

    Returns:
        (prob, l_err, u_err): 概率估计、下误差、上误差。
    """
    prob = k/n
    l_err = prob - wilsonL(n, k, z)
    u_err = wilsonU(n, k, z) - prob
    return prob, l_err, u_err
