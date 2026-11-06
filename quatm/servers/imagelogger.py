"""Image logger service for camera streams."""

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

from quatm.servers import Properties,ImageClient
from quatm.servers import configreader  as cr
import time
import argparse
import json




class ImageStreamLogger():
    """监视图像流活动并把活跃图像流写入属性树。

    Args:
        name (str): Properties/ImageClient 实例名。
    """
    def __init__(self, name):
        """初始化并订阅全部图像流。

        Args:
            name (str): Properties/ImageClient 实例名。
        """
        self.prop = Properties(name, enable_monitors=False)
        self.stream = ImageClient(name)
        self.stream.subscribe('')
        self._dirty = False

    def run(self):
        """主循环：监听图像流并更新属性树里的活跃流表（常驻）。

        收到图像帧即刷新对应流的时间戳；每秒检查一次，把仍待写的变化写回
        ``active`` 属性，并清理超过 maxage 未活动的流（避免 active 表无限
        增长污染下游订阅）。
        """
        lastupdate = time.time()
        active = self.prop.get('active', {})
        maxage = float(self.prop.get('maxStreamInactive[s]', 1000))
        while True:
            try:
                # 只取主题：旧实现用 recv() 会把每一帧图像（数 MB）整块拷进
                # 本进程再丢掉，而这里只用到通道名与帧数。1 s 超时同时保证
                # “没数据时也会醒来看一眼”，让下面的超时清理真的每秒都跑
                # （旧实现无数据就永远阻塞在 recv 里，清理永不执行）。
                topic, parts = self.stream.recv_topic_only(timeout_ms=1000)
            except Exception as e:  # noqa: BLE001
                # 畸形帧/瞬时错误不退出常驻进程
                print('ImageStreamLogger: recv error, continuing: %r' % (e,))
                time.sleep(0.05)
                continue
            if topic is not None:
                active[topic] = {'timestamp': time.time(), 'parts': parts}
                self._dirty = True
            if time.time() - lastupdate > 1:
                # 清理超时未活动的流
                active = {k: v for k, v in active.items()
                          if time.time() - float(v.get('timestamp', 0)) < maxage}
                if self._dirty:
                    self.prop.set('active', active)
                    self._dirty = False
                    lastupdate = time.time()


def run_ImageLogger(name):
    """以独立进程方式运行图像流监视器（由 ProcessManager 拉起）。

    Args:
        name (str): 实例名（Properties/ImageClient）。
    """
    # 安全：ZMQ 无认证/无加密；若 Image Hub 端点被配置到局域网，启动时警告。
    cr.warn_hub_if_exposed('Image Hub', 'Image Logger')
    d=ImageStreamLogger(name)
    d.run()



if __name__ == "__main__":
#    parser = argparse.ArgumentParser()
#    parser.add_argument('name', nargs=1, help='name of this program instance')
#    args = parser.parse_args()
#    name=args.name[0]
    name='Servers/ImageStream'
    run_ImageLogger(name)
