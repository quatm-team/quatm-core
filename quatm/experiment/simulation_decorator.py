"""Helpers for running experiment code in simulation mode."""

import sys
import os
# robust import bootstrap (independent of CWD / interpreter): add the repo
# root and <repo>/quatm to sys.path, derived from this file's own location
_here = os.path.dirname(os.path.abspath(__file__))
_repo = _here
while not os.path.isdir(os.path.join(_repo, 'quatm')):
    _parent = os.path.dirname(_repo)
    if _parent == _repo:
        break
    _repo = _parent
for _p in (_repo, os.path.join(_repo, 'quatm')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _here, _repo, _p


from quatm.servers import send_info, send_error, send_warning
from quatm.utils.i18n import tr

def void_func(*args, **kwargs):
    """空实现（模拟用）：接受任意参数并直接返回 None，绝不触碰硬件。"""
    pass


def simulation_decorator(f):
    """把单个函数包装成模拟版：调用时只走空实现，不执行真实函数体。

    Args:
        f (callable): 被包装的真实函数。

    Returns:
        callable: 模拟包装函数。
    """
    def new_func(*args, **kwargs):
        #if simulating: Currently only used when simulating
        if True:
            x=void_func(*args, **kwargs)
        else:
            x=f(*args,**kwargs)
        return x
    return new_func



def simulation_class_decorator(cls):
    """装饰一个驱动类，把其实例的所有方法变成“模拟”版本。

    生成的新类代理到被装饰类，但实例方法一律走空实现（不碰硬件）；
    同时用空 __init__/__del__ 覆盖，绝不调用真实驱动的构造/析构。

    Args:
        cls (type): 被装饰的真实驱动类。

    Returns:
        type: 模拟版类。
    """
    class _Simulated(cls):
        # 模拟实例绝不调用真实驱动的 __init__：那会连接/初始化真实硬件
        # （打开串口、VISA 会话、加载 ADwin 进程…）。实验代码在模拟模式下
        # 只读驱动的类属性/常量，不需要实例状态。
        def __init__(self, *args, **kwargs):
            pass

        # 模拟实例从不初始化硬件资源，却继承了驱动类的 __del__（例如
        # LDC4000.__del__ 里的 `del self.LDC4000`）。这里用空实现覆盖，
        # 避免垃圾回收时访问不存在的属性抛出被忽略的异常。
        def __del__(self):
            pass

    class NewCls(object):
        def __init__(self,*args,**kwargs):
            self.oInstance = _Simulated(*args,**kwargs)
        def __getattribute__(self,s):
            """属性代理：先查包装类自身，再取被包装实例的属性。

            取到的绑定方法会被包成模拟执行（不调用真实函数体）。
            """
            try:    
                x = super(NewCls,self).__getattribute__(s)
            except AttributeError:      
                pass
            else:
                return x
            x = self.oInstance.__getattribute__(s)
            # Python 3 里绑定方法类型是 method、函数类型是 function，
            # 旧的 `type(x) == type(self.__init__)` 恒为 False —— 模拟模式下
            # 驱动方法会被真实调用（模拟实例没有硬件状态，轻则 AttributeError，
            # 重则触达真实设备）。任何从 oInstance 取出的可调用对象一律
            # 包裹成 no-op，保证模拟运行绝不触硬件。
            if callable(x):
                return simulation_decorator(x)                 # this is equivalent of just decorating the method with time_this
            else:
                return x
    return NewCls


if __name__ == '__main__':
    class Foo:
        def __init__(self):
            pass
        def a(self):
            send_info(tr('[Experiment Simulation] demo method called'))


    @simulation_class_decorator
    class f2(Foo):
        def __init__(self,*args,**kwargs):
            if not simulating:
                super().__init__(*args,**kwargs)
        pass
    
