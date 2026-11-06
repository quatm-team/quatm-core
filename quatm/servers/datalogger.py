"""Data logger service for HDF5 acquisition."""

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


from quatm.servers import Properties,DataClient,PropertyAttribute
from quatm.servers.configreader import warn_hub_if_exposed
import time


class DataStreamLogger():
    """监视“数据流活动”并把活跃流写入属性树。

    每个发数据的流都会被记录到 ``active`` 字典（带最后活动时间戳）；超过
    ``maxage`` 秒无活动的流会被移除，供其它面板判断数据流是否存活。

    Args:
        name (str): Properties/DataClient 实例名。

    Attributes:
        active (dict): ``{流名: {'timestamp': ..., 'parts': n}}``。
        maxage (float): 流最大无活动秒数（读属性 ``maxStreamInactive[s]``）。
    """
    active=PropertyAttribute('active',{})
    maxage=PropertyAttribute('maxStreamInactive[s]',1000)

    def __init__(self,name):
        """初始化：订阅全部流。

        Args:
            name (str): Properties/DataClient 实例名。
        """
        self._props = Properties(name, enable_monitors=False)
        # 1 s 接收超时：不仅为了及时退出，更因为下面的“清除超时未活动的流”
        # 写在收到消息之后 —— 没有超时的话，流全空闲时 recv 永远阻塞，
        # 那句注释说的“每秒检查一次”其实从不执行。
        self.stream = DataClient(name, recvtimeout=1000)
        self.stream.subscribe('')       #listen to all streams
        self.updated = False

    def run(self):
        """主循环：监听各流数据并维护 ``active`` 字典（常驻）。

        收到消息即更新对应流的时间戳；每秒检查一次并清掉超时未活动的流，
        变化时写回属性树。
        """
        lastupdate = time.time()
        active = self.active
        while True:
            try:
                msg = self.stream.recv()
            except Exception as e:  # noqa: BLE001
                # 畸形帧/瞬时错误不退出常驻进程
                print('DataStreamLogger: recv error, continuing: %r' % (e,))
                time.sleep(0.05)
                continue
            if msg is not None:
                active[msg[0]] = {'timestamp': time.time(), 'parts': len(msg)}
                self.updated = True
            if time.time() - lastupdate > 1:   # update dictionary every second
                active = self.removeInactiveStreams(active)
                if self.updated:
                    self.active = active
                    self.updated = False
                    lastupdate = time.time()

    def removeInactiveStreams(self,streamlist):
        """滤掉超过 ``maxage`` 秒无活动的流。

        Args:
            streamlist (dict): ``{流名: {'timestamp': ...}}``。

        Returns:
            dict: 仍活跃的流；有变化时置 ``self.updated = True``。
        """
        new= {k:v for k,v in streamlist.items() if time.time()-v['timestamp']<self.maxage}
        if new!=streamlist:
            self.updated=True
        return new

def run_DataLogger(name):
    """以独立进程方式运行数据流监视器（由 ProcessManager 拉起）。

    Args:
        name (str): 实例名（Properties/DataClient）。
    """
    # 安全：ZMQ 无认证/无加密；若 Data Hub 端点被配置到局域网，启动时警告。
    warn_hub_if_exposed('Data Hub', 'Data Logger')
    d=DataStreamLogger(name)
    d.run()


if __name__ == "__main__":
    name='Servers/DataStream'
    run_DataLogger(name)

