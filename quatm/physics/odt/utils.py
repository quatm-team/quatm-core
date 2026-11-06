"""Shared numerical helpers for optical-trap models."""


import scipy
import numpy as np
from math import floor
from numpy.linalg import norm
import warnings


def AngleBetweenVectors(v,w):
    """返回两向量之间的夹角（度）。

    Args:
        v: 向量一。
        w: 向量二。

    Returns:
        float: 夹角（度）。
    """
    return np.degrees(np.arccos(np.dot(v,w)/(norm(v)*norm(w))))

def Dz(phi):
    """绕 z 轴旋转 phi 度（带外积约定）的旋转矩阵。

    Args:
        phi (float): 旋转角（度）。

    Returns:
        np.matrix: 3x3 旋转矩阵。
    """
    phi = np.radians(phi)
    return np.matrix([[np.cos(phi),-np.sin(phi),0.0],[np.sin(phi),np.cos(phi),0.0],[0.0,0.0,1.0]])

def Dy(phi):
    """绕 y 轴旋转 phi 度的旋转矩阵。

    Args:
        phi (float): 旋转角（度）。

    Returns:
        np.matrix: 3x3 旋转矩阵。
    """
    phi = np.radians(phi)
    return np.matrix([[np.cos(phi),0.0,np.sin(phi)],[0.0,1.0,0.0],[-np.sin(phi),0.0,np.cos(phi)]])

def Dx(phi):
    """绕 x 轴旋转 phi 度的旋转矩阵。

    Args:
        phi (float): 旋转角（度）。

    Returns:
        np.matrix: 3x3 旋转矩阵。
    """
    phi = np.radians(phi)
    return np.matrix([[1.0,0.0,0.0],[0.0,np.cos(phi),-np.sin(phi)],[0.0,np.sin(phi),np.cos(phi)]])

def Rot3Dz(v,phi):
    """把若干三维行向量绕 z 轴旋转 phi 度。

    Args:
        v: 形状为 (N,3) 的行向量数组。
        phi (float): 旋转角（度）。

    Returns:
        np.ndarray: 旋转后的向量。
    """
    if(v.shape[1] != 3):
        print("Please insert a 3D row vector!")
        return np.array([[0.,0.,0.]])
    else:
        ret_v = np.array([np.zeros(v.shape[1] * v.shape[0])]).reshape(v.shape[0],v.shape[1])
        D = Dz(phi)
        for i in range(v.shape[0]):
            ret_v[i] = D.dot(v[i].T)
        return ret_v
    
def Rot3Dx(v,phi):
    """把若干三维行向量绕 x 轴旋转 phi 度。

    Args:
        v: 形状为 (N,3) 的行向量数组。
        phi (float): 旋转角（度）。

    Returns:
        np.ndarray: 旋转后的向量。
    """
    if(v.shape[1] != 3):
        print("Please insert a 3D row vector!")
        return np.array([[0.,0.,0.]])
    else:
        ret_v = np.array([np.zeros(v.shape[1] * v.shape[0])]).reshape(v.shape[0],v.shape[1])
        D = Dx(phi)
        for i in range(v.shape[0]):
            ret_v[i] = D.dot(v[i].T)
        return ret_v
    
def Rot3Dy(v,phi):
    """把若干三维行向量绕 y 轴旋转 phi 度。

    Args:
        v: 形状为 (N,3) 的行向量数组。
        phi (float): 旋转角（度）。

    Returns:
        np.ndarray: 旋转后的向量。
    """
    if(v.shape[1] != 3):
        print("Please insert a 3D row vector!")
        return np.array([[0.,0.,0.]])
    else:
        ret_v = np.array([np.zeros(v.shape[1] * v.shape[0])]).reshape(v.shape[0],v.shape[1])
        D = Dy(phi)
        for i in range(v.shape[0]):
            ret_v[i] = D.dot(v[i].T)
        return ret_v

def Rot3Dxyz(v,phi,theta,psi):
    """依次绕 x、y、z 轴旋转三维行向量（欧拉角式组合旋转）。

    Args:
        v: 形状为 (N,3) 的行向量数组。
        phi/theta/psi (float): 各轴旋转角（度）。

    Returns:
        np.ndarray: 旋转后的向量。
    """
    ret_v = np.array([np.zeros(v.shape[1] * v.shape[0])]).reshape(v.shape[0],v.shape[1])
    ret_v = Rot3Dx(v,phi)
    ret_v = Rot3Dy(ret_v,theta)
    ret_v = Rot3Dz(ret_v,psi)
    return ret_v

def Rot3Dz_m(hessian,phi):
    """绕 z 轴旋转一个 3x3 海森（矩阵）到新坐标系。

    Args:
        hessian: 3x3 矩阵。
        phi (float): 旋转角（度）。

    Returns:
        np.ndarray: 旋转后的矩阵。
    """
    phi = np.radians(phi)
    if(hessian.shape[1] != 3 or hessian.shape[0] != 3):
        print("Pleaser insert a full hessian matrix (3x3)!")
        return np.array([[0.,0.,0.],[0.0,0.0,0.0],[0.,0.,0.]])
    else:
        D = np.array([[np.cos(phi),-np.sin(phi),0.0],[np.sin(phi),np.cos(phi),0.0],[0.0,0.0,1.0]])
        return np.dot(D,np.dot(hessian,D.T))


def Rot3Dxyz_m(hessian,phi,theta,psi):
    """用组合旋转矩阵 R=Dz·Dy·Dx 对 3x3 海森做相似变换。

    Args:
        hessian: 3x3 矩阵。
        phi/theta/psi (float): 各轴旋转角（度）。

    Returns:
        np.matrix: 变换后的矩阵。
    """
    rotmat = Dz(psi)*Dy(theta)*Dx(phi)
    return rotmat*np.matrix(hessian)*rotmat.T





def Wigner3j(j1,j2,j3,m1,m2,m3):
    """计算 Wigner 3j 符号（Racah 公式）。

    Args:
        j1,j2,j3,m1,m2,m3: 角动量量子数与磁量子数（整数或半整数）。

    Returns:
        float: 3j 符号值；输入非法返回 -1 或 0。
    """
#======================================================================
# Wigner3j.m by David Terr, Raytheon, 6-17-04
#
# Compute the Wigner 3j symbol using the Racah formula [1]. 
#
# Usage: 
# from wigner import Wigner3j
# wigner = Wigner3j(j1,j2,j3,m1,m2,m3)
#
#  / j1 j2 j3 \
#  |          |  
#  \ m1 m2 m3 /
#
# Reference: Wigner 3j-Symbol entry of Eric Weinstein's Mathworld: 
# http://mathworld.wolfram.com/Wigner3j-Symbol.html
#======================================================================

    # Error checking
    if ( ( 2*j1 != floor(2*j1) ) | ( 2*j2 != floor(2*j2) ) | ( 2*j3 != floor(2*j3) ) | ( 2*m1 != floor(2*m1) ) | ( 2*m2 != floor(2*m2) ) | ( 2*m3 != floor(2*m3) ) ):
        print('All arguments must be integers or half-integers.')
        return -1

    # Additional check if the sum of the second row equals zero
    if ( m1+m2+m3 != 0 ):
        #print '3j-Symbol unphysical'
        return 0

    if ( j1 - m1 != floor ( j1 - m1 ) ):
        print('2*j1 and 2*m1 must have the same parity')
        return 0
    
    if ( j2 - m2 != floor ( j2 - m2 ) ):
        print('2*j2 and 2*m2 must have the same parity')
        return; 0

    if ( j3 - m3 != floor ( j3 - m3 ) ):
        print('2*j3 and 2*m3 must have the same parity')
        return 0
    
    if ( j3 > j1 + j2)  | ( j3 < abs(j1 - j2) ):
        print('j3 is out of bounds.')
        return 0

    if abs(m1) > j1:
        print('m1 is out of bounds.')
        return 0

    if abs(m2) > j2:
        print('m2 is out of bounds.')
        return 0 

    if abs(m3) > j3:
        print('m3 is out of bounds.')
        return 0

    t1 = j2 - m1 - j3
    t2 = j1 + m2 - j3
    t3 = j1 + j2 - j3
    t4 = j1 - m1
    t5 = j2 + m2

    tmin = max( 0, max( t1, t2 ) )
    tmax = min( t3, min( t4, t5 ) )
    tvec = np.arange(tmin, tmax+1, 1)

    wigner = 0

    for t in tvec:
        wigner += (-1)**t / ( scipy.misc.factorial(t) * scipy.misc.factorial(t-t1) * scipy.misc.factorial(t-t2) * scipy.misc.factorial(t3-t) * scipy.misc.factorial(t4-t) * scipy.misc.factorial(t5-t) )

    return wigner * (-1)**(j1-j2-m3) * np.sqrt(  scipy.misc.factorial(j1+j2-j3) * scipy.misc.factorial(j1-j2+j3) * scipy.misc.factorial(-j1+j2+j3) / scipy.misc.factorial(j1+j2+j3+1) * scipy.misc.factorial(j1+m1) * scipy.misc.factorial(j1-m1) * scipy.misc.factorial(j2+m2) * scipy.misc.factorial(j2-m2) * scipy.misc.factorial(j3+m3) * scipy.misc.factorial(j3-m3) )


def Wigner6j(j1,j2,j3,J1,J2,J3):
    """计算 Wigner 6j 符号（Racah 公式）。

    Args:
        j1,j2,j3,J1,J2,J3: 六个角动量量子数（整数或半整数）。

    Returns:
        float: 6j 符号值；输入非法返回 -1 或 0。
    """
#======================================================================
# Calculating the Wigner6j-Symbols using the Racah-Formula                
# Author: Ulrich Krohn                                            
# Date: 13th November 2009
#                                                                         
# Based upon Wigner3j.m from David Terr, Raytheon                         
# Reference: http://mathworld.wolfram.com/Wigner6j-Symbol.html            
#
# Usage: 
# from wigner import Wigner6j
# WignerReturn = Wigner6j(j1,j2,j3,J1,J2,J3)
#
#  / j1 j2 j3 \
# <            >  
#  \ J1 J2 J3 /
#
#======================================================================

    # Check that the js and Js are only integer or half integer
    if ( ( 2*j1 != round(2*j1) ) | ( 2*j2 != round(2*j2) ) | ( 2*j2 != round(2*j2) ) | ( 2*J1 != round(2*J1) ) | ( 2*J2 != round(2*J2) ) | ( 2*J3 != round(2*J3) ) ):
        print('All arguments must be integers or half-integers.')
        return -1
    
# Check if the 4 triads ( (j1 j2 j3), (j1 J2 J3), (J1 j2 J3), (J1 J2 j3) ) satisfy the triangular inequalities
    if ( ( abs(j1-j2) > j3 ) | ( j1+j2 < j3 ) | ( abs(j1-J2) > J3 ) | ( j1+J2 < J3 ) | ( abs(J1-j2) > J3 ) | ( J1+j2 < J3 ) | ( abs(J1-J2) > j3 ) | ( J1+J2 < j3 ) ):
        print('6j-Symbol is not triangular!')
        return 0
    
    # Check if the sum of the elements of each traid is an integer
    if ( ( 2*(j1+j2+j3) != round(2*(j1+j2+j3)) ) | ( 2*(j1+J2+J3) != round(2*(j1+J2+J3)) ) | ( 2*(J1+j2+J3) != round(2*(J1+j2+J3)) ) | ( 2*(J1+J2+j3) != round(2*(J1+J2+j3)) ) ):
        print('6j-Symbol is not triangular!')
        return 0
    
    # Arguments for the factorials
    t1 = j1+j2+j3
    t2 = j1+J2+J3
    t3 = J1+j2+J3
    t4 = J1+J2+j3
    t5 = j1+j2+J1+J2
    t6 = j2+j3+J2+J3
    t7 = j1+j3+J1+J3

    # Finding summation borders
    tmin = max(0, max(t1, max(t2, max(t3,t4))))
    tmax = min(t5, min(t6,t7))
    tvec = np.arange(tmin,tmax+1,1)
        
    # Calculation the sum part of the 6j-Symbol
    WignerReturn = 0
    for t in tvec:
        WignerReturn += (-1)**t*scipy.misc.factorial(t+1)/( scipy.misc.factorial(t-t1)*scipy.misc.factorial(t-t2)*scipy.misc.factorial(t-t3)*scipy.misc.factorial(t-t4)*scipy.misc.factorial(t5-t)*scipy.misc.factorial(t6-t)*scipy.misc.factorial(t7-t) )

    # Calculation of the 6j-Symbol
    return WignerReturn*np.sqrt( TriaCoeff(j1,j2,j3)*TriaCoeff(j1,J2,J3)*TriaCoeff(J1,j2,J3)*TriaCoeff(J1,J2,j3) )


def TriaCoeff(a,b,c):
    """计算角动量耦合的三角形系数（用于 6j 符号）。

    Args:
        a,b,c: 角动量量子数。

    Returns:
        float: 三角形系数。
    """
    # Calculating the triangle coefficient
    return scipy.misc.factorial(a+b-c)*scipy.misc.factorial(a-b+c)*scipy.misc.factorial(-a+b+c)/(scipy.misc.factorial(a+b+c+1))

    
def tuple_without(original_tuple, element_to_remove):
    """返回去掉指定元素后的新元组（保留原元组不变）。

    Args:
        original_tuple (tuple): 原元组。
        element_to_remove: 要去掉的元素。

    Returns:
        tuple: 过滤后的元组。
    """
    new_tuple = []
    for s in list(original_tuple):
        if not s == element_to_remove:
            new_tuple.append(s)
    return tuple(new_tuple)

def rot_2d_by_angle(vector, angle):
    """Rotates a 2D-vector by an angle given in degrees
    Args:
        vecto 2d array: vector to be rotated
        angle float: rotation angle

    Returns:
        2d-array:rotated angle
    """    
    theta = np.deg2rad(angle)
    rot_matrix = rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    return np.dot(rot_matrix,vector)

# wilson score interval，见 https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
# z = 1 因为取 1-sigma 区间
def wilsonC(n,k,z=1):
    """Wilson 区间中心值（已废弃：请改用 quatm.physics.odt.statistics）。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间中心。
    """
    warnings.warn('moved to statistic', DeprecationWarning)
    center = (k/n + z**2/(2*n))/(1+z**2/n)
    return center


def wilsonU(n,k,z=1):
    """Wilson 区间上限（已废弃：请改用 quatm.physics.odt.statistics）。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间上限。
    """
    upper = np.minimum(1, (1/(1.+z**2/n))*(k/n+z**2/(2*n)+z*np.sqrt((k/n**2)*(1.-k/n)+(z**2/(4.*n**2)))))
    warnings.warn('moved to statistic', DeprecationWarning)
    return upper


def wilsonL(n,k,z=1):
    """Wilson 区间下限（已废弃：请改用 quatm.physics.odt.statistics）。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        Wilson 区间下限。
    """
    lower = np.maximum(0, (1./(1.+z**2/n))*(k/n+z**2./(2.*n)-z*np.sqrt((k/n**2)*(1.-k/n)+(z**2/(4.*n**2)))))
    warnings.warn('moved to statistic', DeprecationWarning)
    return lower

    
def wilson_score(n,k,z=1):
    """Wilson 区间三要素（已废弃：请改用 quatm.physics.odt.statistics）。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (float): z-sigma 区间（默认 1）。

    Returns:
        (lower, center, upper): Wilson 区间三元组。
    """
    warnings.warn('moved to statistic', DeprecationWarning)
    lower = wilsonL(n,k,z)
    center = wilsonC(n,k,z)
    upper = wilsonU(n,k,z)
    return lower, center, upper


def wilson_err(n, k, z=1):
    """Wilson 区间概率与误差棒（已废弃：请改用 quatm.physics.odt.statistics）。

    Args:
        n (int|np.ndarray): 总尝试次数。
        k (int|np.ndarray): 成功次数。
        z (int): z-sigma 区间（默认 1）。

    Returns:
        (prob, l_err, u_err): 概率、下误差、上误差。
    """
    warnings.warn('moved to statistic', DeprecationWarning)
    prob = k/n
    l_err = prob - wilsonL(n,k,z)
    u_err = wilsonU(n,k,z) - prob    
    return prob, l_err, u_err