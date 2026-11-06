"""变密度采样（variable density sampling）辅助函数。"""

import numpy as np
import matplotlib.pyplot as plt
import random

def gauss_prime_norm(x, mu, sigma):
    """高斯函数导数的归一化形式（峰值幅度归一为 1）。

    Args:
        x: 自变量（数值或数组）。
        mu (float): 高斯中心。
        sigma (float): 高斯宽度。

    Returns:
        归一化的导数（最大值为 1）。
    """
    xred = x - mu
    pref = np.exp(.5) * 1/sigma
    exp = np.exp(-(xred/sigma)**2/2)
    return xred * pref * exp


def gauss_prime_norm_abs(x, mu, sigma):
    """高斯函数导数绝对值的归一化形式（峰值归一为 1）。

    Args:
        x: 自变量（数值或数组）。
        mu (float): 高斯中心。
        sigma (float): 高斯宽度。

    Returns:
        归一化导数的绝对值。
    """
    return np.abs(gauss_prime_norm(x, mu, sigma))
    
 
def create_support_points_stochastic(start, stop, steps, interest_fn, max_stepsize=.5, enhancement=.5, **kwargs):
    """生成一组支撑点，随机分布并在感兴趣区域加密。

    先构造均匀过采样网格，再按兴趣函数给出的权重随机“梳除”点，使采样
    在感兴趣区域更密集。

    Args:
        start (float): 扫描起点（总在最终支撑点中）。
        stop (float): 扫描终点（总在最终支撑点中）。
        steps (int): 最终支撑点数量。
        interest_fn (callable): 输入支撑点元素与 kwargs，返回 0~1 的兴趣度
            （0 低、1 高）。
        max_stepsize (float): 支撑点允许的最大间距。
        enhancement (float): 0~1；1 表示在感兴趣区域高度加密，0 表示无加密
            （纯随机）。
        **kwargs: 传给 interest_fn 的附加参数。

    Returns:
        np.ndarray: 从 start 到 stop 按兴趣加权的随机支撑点。

    注意：stepsize 过小且 enhancement 过大时可能采样失败。
    """
    # initially create an equally spaced oversampled support
    x_init = np.linspace(start, stop, steps*50).tolist()
    
    # now go through the list and stochastically comb out undesired points
    n = 0 
    while len(x_init) > steps and n<20:
        removable_indices = []
        for i, xi in enumerate(x_init):
            if i == 0 or i == len(x_init)-1:
                continue
            # check whether max step size is sattisfied if we would remove the point
            if x_init[i+1]-x_init[i-1] < max_stepsize:
                # stochastically remove that point depending on its weight
                weight = enhancement*interest_fn(xi, **kwargs)
                
                # choose random between 0 and 1 to decide whether point will be kept
                P = random.random()
                if weight < P:
                    removable_indices.append(i)
                    
        # check whether the removal of points would decrease the sample size below the desired number
        new_length = len(x_init) - len(removable_indices)
        if new_length < steps:
            diff = steps - new_length
            # remove excess indices randomly from list
            for i in range(diff):
                index_to_remove = random.choice(removable_indices)
                removable_indices.remove(index_to_remove)
        
        # remove the chosen points, start from the end to avoid indexing errors      
        for i in removable_indices[::-1]:
            # here we need to check again whether, maybe now, the max step size might be exceeded
            if x_init[i+1]-x_init[i-1] < max_stepsize:
                del x_init[i]
        n+=1
    
    if n == 20 and len(x_init) != steps:
        print("Unsuccessfull attempt. Maximum number of iterations reached!")
    return np.array(x_init)
    

