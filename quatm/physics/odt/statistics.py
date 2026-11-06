# -*- coding: utf-8 -*-
"""
Created on Thu Dec 10 17:23:44 2020

@author: Daniel
"""
import numpy as np


# wilson score interval，见 https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
# z = 1 因为取 1-sigma 区间
def wilsonC(n,k,z=1):
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


def wilsonU(n,k,z=1):
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


def wilsonL(n,k,z=1):
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

    
def wilson_score(n,k,z=1):
    """Wilson 区间三要素：下限、中心、上限。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        (lower, center, upper): Wilson 区间三元组。
    """
    lower = wilsonL(n,k,z)
    center = wilsonC(n,k,z)
    upper = wilsonU(n,k,z)
    return lower, center, upper


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
    l_err = prob - wilsonL(n,k,z)
    u_err = wilsonU(n,k,z) - prob    
    return prob, l_err, u_err


def getrandbincont(n,k,size=1, binsize = 0.005, samplesize = 10000):
    """由二项实验的 (n,k) 结果生成单次概率 p 的近似分布并抽样。

    通过反向二项分布生成 p 的近似后验分布，然后从中抽取指定数量的样本。

    Args:
        n (int): 总尝试次数。
        k (int): 成功次数。
        size (int): 抽样个数。
        binsize (float): p 的直方图分箱宽度。
        samplesize (int): 反演二项分布用的抽样次数。

    Returns:
        np.ndarray: 长度为 size 的 p 样本数组。
    """
    n_bins = int(1/binsize) + 1
    ps = np.linspace(0,1,n_bins)
    
    ###for each p in ps draw from bin(n,p) and see how often k was the result
    ###This gives a distribution for p depending on n and k
    binpdf = np.array([len(np.where(np.random.binomial(n,p,size=samplesize)==k)[0])/samplesize for p in ps])
    #normalize the distribution
    binpdf = binpdf/sum(binpdf)
    return np.random.choice(ps, p=binpdf,size=size)