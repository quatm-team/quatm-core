"""Camera Hub service and camera control helpers."""

import time, sys

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


import numpy as np
import copy
from termcolor import colored
from quatm.servers import send_info, send_error

from quatm.servers import send_info, send_error, send_warning

from PySide6 import QtCore, QtGui
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QGridLayout
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QMainWindow, QDialog, QLabel

import quatm
from quatm.servers import Properties, PropertyAttribute, DataClient, ImageClient, CommandClient
from quatm.servers import iconpath
from quatm.servers.configreader import warn_hub_if_exposed
from quatm.utils.i18n import tr


try:
    import rotpy
    from rotpy.system import SpinSystem
    from rotpy.camera import CameraList
    sim = False
except:
    class SpinSystem:
        '''驱动未安装时的替身类'''
        def __init__(self):
            send_error(tr('[CameraHub] blackfly driver not installed'))
    class CameraList:
        def __init__(self):
            pass

    sim = False


class CameraHub:
    """相机管理器：负责相机的枚举、启停与配置。

    持有全局 SpinSystem 与各相机的 CamServer，通过属性树与 GUI 交互。
    Future work: automatic reconfiguration, configuration reload without
    interrupting acquisition, and camera-crash detection.

    Args:
        props (Properties): 本模块的属性树实例。
        parent: 通常为持有本对象的 GUI（用于访问 camWidgets 等）。

    Attributes:
        camservers (dict[str, CamServer]): 已启动的相机名 → CamServer。
        camlist: 最近一次枚举得到的相机列表。
        serials (list[str]): 最近一次枚举得到的序列号列表。
    """

    def __init__(self, props, parent=None):
        """初始化相机管理器：创建 SpinSystem 并枚举已连接的相机。

        Args:
            props (Properties): 属性树实例。
            parent: 宿主 GUI 对象。
        """
        self.props = props
        self.parent = parent
        self.system = SpinSystem()
        self.camservers = {}
        # 枚举缓存（时间戳, serials）：10 秒内的启动直接复用上次结果，
        # 避免对每台相机反复 init/deinit（Spinnaker 每次枚举都重新读取
        # 设备，相机启动困难时频繁枚举会加重 USB 抖动）。
        self._serials_cache = (0.0, [])
        self.camlist, self.serials = self.get_camlist()

    def start_cam(self, name, serial):
        """启动一台相机（若同名相机已在运行则先停止）。

        启动前重新枚举；若相机不在线（或已被占用）则把对应 GUI 标签标红。

        Args:
            name (str): 相机名（用于命名图像流与属性）。
            serial (int): 相机序列号。
        """
        serial_str = str(serial)
        if name in self.camservers:
            self.stop_cam(name)
        self.camlist, self.serials = self.get_camlist()
        if serial_str not in self.serials:
            send_error(f'[CameraHub] {name} camera not connected or in use by another instance.')
            camWidget = self.parent.camWidgets[name]
            camWidget.label.setStyleSheet("background-color: red")
        else:
            cam = CamServer(name, serial, self.system)
            self.camservers[name] = cam
            cam.run()
            send_info(f'[CameraHub] Camera {name} started')

    def stop_cam(self, name):
        """停止一台相机并释放其 CamServer。

        Args:
            name (str): 要停止的相机名。
        """
        if name in self.camservers:
            cam = self.camservers[name]
            cam.cam.stop()
            cam.stop()
            del cam
            send_info(f'[CameraHub] Camera {name} stopped')
        else:
            send_info(f'[CameraHub] Camera {name} not running')

    def config_cam(self, name):
        """对运行中的相机重新应用属性配置。

        Args:
            name (str): 要配置的相机名。
        """
        if name in self.camservers:
            cam = self.camservers[name]
            cam.configure()
        else:
            send_info(f'[CameraHub] Camera {name} not running')

    def get_camlist(self, force=False):
        """枚举当前连接的相机（默认带 10 秒缓存）。

        Args:
            force (bool): True 时跳过缓存强制重新枚举；默认 10 秒内的
                调用复用上次结果（避免频繁 init/deinit 相机）。

        Returns:
            (camlist, serials): 相机列表与序列号列表；sim 模式下返回 ([], [])。
        """
        if not force:
            age = time.time() - self._serials_cache[0]
            if age < 10 and self._serials_cache[1]:
                return self.camlist, self.serials
        if not sim:
            #print("Getting camlist")
            camlist = CameraList.create_from_system(self.system, update_cams=True, update_interfaces=True)
            serials = []
            for i in range(camlist.get_size()):
                try:
                    c = camlist.create_camera_by_index(i)
                    c.init_cam()
                    serials.append(c.camera_nodes.DeviceSerialNumber.get_node_value())
                    c.deinit_cam()
                    c.release()

                except Exception as e:
                    # TODO: automatic IP configuration
                    # c.force_ip()
                    send_error(f'[CameraHub]  try autoforce IP: {e}')

                finally:
                    if c is not None:
                        c.deinit_cam()
                        c.release()
            #self.serials = serials
            #print(serials)
            self._serials_cache = (time.time(), serials)
            return camlist, serials
        else:
            return [], []


class CamServer():
    """控制一台相机并处理与 Image/Data 总线的通信。

    每个 CamServer 对应一台相机：负责图像流发布（ImageClient）、命令订阅
    （CommandClient）、实验索引进度的订阅（DataClient）以及相机属性在属性
    树中的同步。

    Args:
        name (str): 相机名，用于标识属性并创建命名图像流。
        serial (int): 相机序列号。
        system (SpinSystem): 访问所有相机的系统接口。

    Attributes:
        imstream (ImageClient): 图像发布流。
        cmdstream (CommandClient): 命令订阅流。
        name (str): 属性命名空间（'Cameras/<name>'）。
        experiment_timings (list): 实验开始时间与索引记录。
    """
    _resolution = PropertyAttribute('resolution [m per pixel]',(0.00001,0.00001))
    _conf_name  = PropertyAttribute('Configuration_name','DefaultConfig')

    _autoreconfigure = PropertyAttribute('Autroreconfigure',True)

    def __init__(self,name,serial,system):
        """初始化驱动、建立图像/命令/索引流并把属性应用到相机。

        若命名配置尚不存在，则用相机当前值创建新配置。

        Args:
            name (str): 相机名（自动截取 '/' 后的末段）。
            serial (int): 相机序列号。
            system (SpinSystem): 系统接口。
        """
        name = name.split('/')[-1]
        # set up quatm environment
        self.imstream = ImageClient(name)
        self.cmdstream = CommandClient(name)
        self.cmdstream.subscribe(name)
        self.name = 'Cameras/'+name
        self.indexq = DataClient(self.name)
        self.indexq.subscribe(['Experiment.start'])
        self.experiment_timings = [[0,{'_run':0,'_repetition':0,'_task':0}]]
        self._props = Properties(self.name)
        props = self._props
        self.confname = copy.copy(self._conf_name)
        self.system = system

        self.lastinfo = (0,0,0)
        self.imgindex = 0

        self.cam = Blackfly(serial, self.system)
        self.configure()
        self.connected = True


    def configure(self):
        """把命名配置写入相机；若无配置则从相机当前设置创建。

        在 with self.cam 上下文（自动初始化/收尾）内执行：配置名为
        _conf_name；不存在时读取相机当前属性并保存为命名配置，否则
        应用配置并把（可能被相机修正过的）结果写回属性树。
        """
        # write the properties of the configuration to the cam
        cam_props = self._props.get(self._conf_name, 'Empty')
        with self.cam:
            # if the configuration doesn't exist, we create it from current settings
            if cam_props == 'Empty':
                cam_props = self.cam.getProps()
                self._props.set(self._conf_name, cam_props)

            else:
                cam_props = self.cam.setProps(cam_props)
                self._props.set(self._conf_name, cam_props)
        self._cam_props = cam_props

    def update_experiment_indices(self):
        """从索引流接收实验开始消息，维护 experiment_timings 列表。

        拷贝自 bfly.py。保留最近 20 条 (时间戳, {_run,_repetition,_task})。
        """

        while self.indexq.has_new_data():
            #print('datamgr.py data incomming!')
            recvmsg=self.indexq.recv()
            A=None
            if len(recvmsg)==2:
                msg,msg_dic=recvmsg
            elif len(recvmsg)==3:
                msg,msg_dic,A=recvmsg
            else:
                send_error(tr('[CamServer] datamgr message error'))
                return
            #print(msg)
            if msg=='Experiment.start':
                indices=dict((k,msg_dic[k]) for k in ('_run','_repetition','_task') if k in msg_dic)
                timestamp=msg_dic['_starttime']
                self.experiment_timings.append([timestamp,indices])
                while len(self.experiment_timings)>20:
                    del self.experiment_timings[0]

    def run(self):
        """启动图像采集：初始化相机并以回调方式持续发送图像。

        以 _image_callback 为回调启动相机。自动重配置（autoreconfigure）
        Not implemented yet; a timed callback would be preferable to the
        current polling loop.
        """

        # Start the cam with a callback function for image acquisition
        self.cam.initialize_cam()
        self.cam.start(self._image_callback)

        # Future work: use a timed callback for automatic reconfiguration
        # instead of a polling loop.

    def stop(self):
        """停止图像采集并释放相机（调用 cam.finalize_cam 收尾）。"""
        self.cam.finalize_cam()

    def _image_callback(self, handler, camera, image):
        """相机图像回调：深拷贝图像并发送到图像流。

        出错时记录 send_error 而不中断采集。

        Args:
            handler (rotpy.ImageEventHandler): 事件处理器。
            camera: 相机对象。
            image: 图像对象（不是 numpy 数组）。
        """
        try:
            imagec = image.deep_copy_image(image)
            image.release()
            self.sendimage(imagec)
        except Exception as e:
            send_error(f'[CamServer] _image_callback()  {self.name}: {e}')

    def _image_to_array(self, im):
        """把 Blackfly 图像对象转换成 numpy 数组。

        Args:
            im: Blackfly 驱动给出的图像对象。

        Returns:
            np.ndarray: 图像数组（按位深取 uint8/uint16），已翻转并旋转。
        """
        h = im.get_height()
        w = im.get_width()
        bit_depth = im.get_bits_per_pixel()
        if bit_depth == 8:
            dt = np.uint8
        elif bit_depth == 16:
            # for some reason the buffer order is changed to big endian in MONO16 pixel format
            dt = np.dtype(np.uint16)
            dt = dt.newbyteorder('>')
        else:
            # MONO12/MONO14 are stored in 16 bits; anything else falls back to uint8
            dt = np.uint16 if bit_depth > 8 else np.uint8
        ima = np.frombuffer(im.get_image_data(), dtype=dt)
        ima = ima.reshape(h, w)
        ima = np.flipud(ima)
        # 90° 整数倍旋转用切片即可：旧实现 scipy.ndimage.rotate(ima, -90) 的默认
        # 参数是 order=3 + prefilter，实测 2048x2048 要 **~470 ms/帧**（10 fps
        # 相机根本跑不动），而这里 6.5 ms 且**逐像素完全相同**（4 种分辨率验证）。
        # k 必须是 -1：np.rot90(ima)（默认 k=1）方向不同——历史上就是因为这个被
        # 反馈“离子列从横排变竖排”而回退的，不要改成默认值。
        ima = np.ascontiguousarray(np.rot90(ima, -1))

        return ima

    def generate_image_index(self, _task=0, _repetition=0, _run=0):
        """按当前测量计数图像：测量序号变化时把计数器清零。

        直接拷贝自 bfly.py。

        Args:
            _task (int): 当前任务序号。
            _repetition (int): 当前重复序号。
            _run (int): 当前 run 序号。
        """
        measIndex = (_task, _repetition, _run)
        if self.lastinfo == measIndex:
            self.imgindex += 1
        else:
            self.imgindex = 0
            self.lastinfo = measIndex

    def sendimage(self, im):
        """把一张相机图像发送到图像流（ImageClient）。

        图像先转为 numpy 数组，再附上 binning、时间戳、实验索引等元数据
        字典一起发送。

        Args:
            im: Blackfly 图像对象。
        """
        im_arr = self._image_to_array(im)
        image_timestamp = self.cam.cpu_time + im.get_frame_timestamp()/self.cam.tick_tock # timestamp of cam fpga clock
        run_task = self.find_experiment_indices(image_timestamp)
        self.generate_image_index(run_task)
        if time.time() - image_timestamp > 5:
            send_warning(f'[CamServer] Image readout delayed of {self.name} by {time.time()-image_timestamp:.0f}s')

        # collect image metadata and store it in info dict
        gain = self.cam._get_camera_node_value_by_name("Gain")
        offset = [im.get_offset_x(),
                  im.get_offset_y()]
        binning = [self.cam._get_camera_node_value_by_name("BinningHorizontal"),
                   self.cam._get_camera_node_value_by_name("BinningVertical")]
        img_resolution = [res*bin for res,bin in zip(self._resolution, binning)]
        info = {}
        info['timestamp'] = image_timestamp
        info['_imgresolution'] = img_resolution
        info['_offset'] = offset
        info['_imgindex'] = self.imgindex
        info['camgain'] = gain
        info['bits_per_pixel'] = im.get_bits_per_pixel()
        info['pixel_format'] = im.get_pix_fmt()
        info.update(run_task)

        self.imstream.send(im_arr, info)


    def find_experiment_indices(self,timestamp):
        """根据图像时间戳找到对应的 run/task/repetition 索引。

        拷贝自 bfly.py。

        Args:
            timestamp (float): 图像时间戳（相机时钟，秒）。

        Returns:
            dict: 形如 {'_run':..,'_repetition':..,'_task':..} 的索引字典。
        """
        self.update_experiment_indices()
        tstamps=[i[0] for  i in self.experiment_timings]
        tstamps = np.array(tstamps)

        index = 0
        if timestamp > tstamps[0]:
            index = np.argwhere(tstamps-timestamp < 0)[-1][0]
        return self.experiment_timings[int(index)][1]


class Blackfly():
    """Blackfly 相机相机驱动的封装：支持用单个字典批量设置相机属性。

    Args:
        serial (int): 相机序列号。
        system (SpinSystem): 访问所有相机的系统接口。
    """
    def __init__(self, serial, system):
        """按序列号初始化一台相机，创建与其交互所需的系统接口。

        Args:
            serial (int|str): 相机序列号。
            system (SpinSystem): 系统接口。
        """

        # Exclude non-writable params from printed warnings:
        self.non_writables = np.array(['AutoGainLowerLimit', 'AutoGainUpperLimit', 'DeviceVendorName',
                                       'DeviceModelName', 'DeviceVersion', 'DeviceSerialNumber',
                                       'DeviceID', 'pgrSensorDescription', 'DeviceSVNVersion', 'DeviceFirmwareVersion',
                                       'DeviceScanType', 'DeviceTemperature', 'pgrDeviceUptime',
                                       'pgrPowerSupplyVoltage', 'pgrPowerSupplyCurrent', 'DeviceMaxThroughput',
                                       'AutoExposureTimeLowerLimit', 'DecimationHorizontal',
                                       'AutoExposureTimeUpperLimit', 'pgrAutoExposureCompensationLowerLimit',
                                       'pgrAutoExposureCompensationUpperLimit', 'LineMode',
                                       'AcquisitionFrameRate', 'AcquisitionStatus', 'PixelCoding', 'SensorWidth',
                                       'SensorHeight', 'WidthMax', 'HeightMax', 'BinningHorizontal', 'PixelSize',
                                       'PixelColorFilter', 'PixelDynamicRangeMin', 'PixelDynamicRangeMax',
                                       'pgrDefectPixelCorrectionType', 'UserSetCurrent', 'LineStatus', 'LineStatusAll',
                                       'DataFlashPageSize', 'DataFlashPageCount', 'GevCurrentIPAddress',
                                       'GevCurrentSubnetMask', 'GevCurrentDefaultGateway', 'GevGVCPPendingTimeout',
                                       'GevTimestampTickFrequency', 'GevTimestampValue', 'GevStreamChannelCount',
                                       'GevSupportedOption', 'GevVersionMajor', 'GevVersionMinor',
                                       'GevDeviceModeIsBigEndian', 'GevDeviceModeCharacterSet', 'GevInterfaceSelector',
                                       'GevMACAddress', 'GevFirstURL', 'GevSecondURL', 'GevNumberOfInterfaces',
                                       'GevLinkSpeed', 'GevMessageChannelCount', 'GevPrimaryApplicationSocket',
                                       'GevPrimaryApplicationIPAddress', 'GevSCPDirection', 'GevSCSP', 'PayloadSize',
                                       'TransmitFailureCount'])

        self.serial = serial
        self.system = system
        cameras = CameraList.create_from_system(self.system, update_cams=True, update_interfaces=True)
        self.camera = cameras.create_camera_by_serial(str(self.serial))
        self.running = False

        self._image_handler = None

    def _synchronize_timestamp(self):
        """记录当前 CPU 时间并复位相机内部时钟（时间戳同步用）。

        复位 + 锁存 + 读取实测约 100 µs。设置 self.cpu_time 与 self.tick_tock，
        供后续把相机帧时间戳换算成实验控制软时间戳。
        """
        reset = self.camera.get_node_map().get_node_by_name("GevTimestampControlReset")

        self.cpu_time = time.time()
        self.tick_tock = self._get_camera_node_value_by_name("GevTimestampTickFrequency")
        # tested: resetting + latching + reading takes 100us real time
        reset.execute_node()

    def _get_camera_node_value_by_name(self, name):
        """按名字快速读取一个相机节点（属性）的值。

        Args:
            name (str): 节点名（如 'Gain'）。

        Returns:
            节点当前值。
        """
        value = self.camera.get_node_map().get_node_by_name(name).get_node_value()
        return value

    def setProps(self, props):
        """把属性字典应用到相机，并返回实际生效的属性。

        若相机正在采集会先停止，设置完成后再恢复。返回的字典可能与输入
        不同（相机可能拒绝不兼容的值）。

        Args:
            props (dict): 属性字典；键对应相机节点名，值为要设置的值。

        Returns:
            dict: 实际写入后读回的属性。
        """
        # stop the cam if it's running, to set properties
        cam_status = self.running
        self.stop()
        nodemap = self.camera.get_node_map()
        self._iter_property_dict(props, nodemap)
        props = self.getProps()

        # restart the cam if it has been running before
        if cam_status:
            self.start()
        return props

    def _iter_property_dict(self, dic, nodemap):
        """递归遍历属性字典并写入相机节点。

        Args:
            dic (dict): 待写入的属性字典。
            nodemap: 相机的节点映射（node map）。
        """
        for key, val in dic.items():
            send_info("[Blackfly] Setting {} to {}".format(key, val))
            # I. we're at the lowest level, set the value in the node
            if not isinstance(val, dict):
                node = nodemap.get_node_by_name(key)
                if node is None: continue
                if node.is_writable():
                    try:
                        node.set_node_value(val)
                    except rotpy.system.SpinnakerAPIException as e:
                        # 旧代码写的是 send_error("[Blackfly] e")：异常对象被丢掉、
                        # 消息里只剩一个字母 e，完全没法排查。
                        send_error(tr('[Blackfly] setting %r failed: %s') % (key, e))
                else:
                    send_warning(f'[Blackfly Driver] bfly2.py {self.serial}: Node {key} not writable.')

            # II. It's a SpinEnumNode, set the 'value' from the dict
            elif "options" in val:
                node = nodemap.get_node_by_name(key)
                if node.is_writable():
                    enum_item = node.get_entry_by_name(val['value'])
                    node.set_node_value(enum_item)
                else:
                    send_warning(f'[Blackfly Driver] bfly2.py {self.serial}: Node {key} not writable.')

            # III. it's a SpinTreeNode, we iterate on
            else:
                self._iter_property_dict(val, nodemap)

    def _get_camera_properties(self):
        """返回相机全部可用节点的层次化属性字典（从 Root 节点递归收集）。"""
        nodemap = self.camera.get_node_map()
        root_node = nodemap.get_node_by_name("Root")
        props = self._iterate_nodes(root_node)
        return props["Root"]

    def _get_system_properties(self):
        """返回 self.system 全部可用节点的层次化属性字典（从 Root 节点递归收集）。"""
        sysnm = self.system.get_tl_node_map()
        sys_root = sysnm.get_node_by_name("Root")
        props = self._iterate_nodes(sys_root)
        return props["Root"]

    def getProps(self):
        """读取相机当前全部可配置属性。

        Returns:
            dict: 相机当前属性字典。
        """
        props = self._get_camera_properties()
        return props

    def get_image(self):
        """从相机读取一帧图像。

        Returns:
            Blackfly 图像对象（不是 numpy 数组）；出错时返回 None。
        """
        try:
            im = self.camera.get_next_image()
            imc = im.deep_copy_image(im)
            im.release()
            return imc

        except Exception as e:
            send_warning(f'[Blackfly Driver] Cam {self.serial}: Error get image: {e}')
            return None

    def initialize_cam(self):
        """初始化相机并同步时间戳（init_cam + _synchronize_timestamp）。"""
        self.camera.init_cam()
        self._synchronize_timestamp()

    def __enter__(self):
        """安全初始化相机运行时环境（with 块进入时调用）。"""

        self.initialize_cam()
        return self

    def start(self, callback=None):
        """挂接图像回调并开始采集（若尚未在运行）。

        Args:
            callback: 每帧图像到达时回调（handler, camera, image）。
        """
        if not self.running:
            send_info(tr("[CameraHub] starting camera acquisition"))
            if callback is not None:
                send_info(f"[CameraHub] {callback}")
                self._image_handler = self.camera.attach_image_event_handler(callback)
            self.camera.begin_acquisition()
            self.running = True

    def stop(self):
        """停止图像采集并卸下已挂接的图像回调（若相机正在运行，同时
        复位运行标志）。"""
        if self.running:
            self.camera.end_acquisition()
            if self._image_handler is not None:
                self.camera.detach_image_event_handler(self._image_handler)
                self._image_handler = None
            self.running = False

    def finalize_cam(self):
        """停止采集并释放相机句柄（stop + deinit + release）。"""
        self.stop()
        self.camera.deinit_cam()
        self.camera.release()

    def __exit__(self,type,value,traceback):
        """退出运行时上下文：确保即使发生异常相机也会被断开。"""
        self.finalize_cam()

    def _iterate_nodes(self, node, lvl=0, dic=None):
        """递归遍历相机的节点（属性）并存入字典。

        Args:
            node: 当前节点（SpinTreeNode 表示分类，其余可写节点为属性）。
            lvl (int): 递归层级（仅供调试缩进）。
            dic (dict|None): 累积结果的字典（跨递归共享）。

        Returns:
            dict: 收集到的节点值字典。
        """
        # never share a mutable default across calls (a shared dict would
        # accumulate stale entries between getProps() calls)
        if dic is None:
            dic = {}
        # SpinTreeNodes are not properties themselves but represent categories.
        if type(node) == rotpy.node.SpinTreeNode:
            dic[node.get_name()] = {}
            for n in node.get_children():
                if n.is_available():
                    # print(lvl*"    ", n.get_name())
                    self._iterate_nodes(n, lvl=lvl+1, dic=dic[node.get_name()])

        # all available nodes that represent properties get stored with their values
        elif node.is_available():
            if type(node) is rotpy.node.SpinEnumNode:
                dic[node.get_name()] = {"value":node.get_node_value().get_enum_name(), "options": node.get_entries_names()}

            elif type(node) is not rotpy.node.SpinCommandNode:
                dic[node.get_name()] = node.get_node_value()

        return dic

class CameraGUI(QWidget):
    """相机控制 GUI：为每台相机提供一个控制面板。

    Attributes:
        hub (CameraHub): 相机管理器。
        camWidgets (dict[str, CamWidget]): 相机名 → 控制面板。
    """
    _cam_serials = {"Axial_Ba": 15422718, "Align_Ba": 14466865, "Radial_Rb": 15358916}

    def __init__(self):
        """构建界面：创建属性、CameraHub 与各相机的控制面板。"""
        super().__init__()
        self.props = Properties('Servers/CameraHub')
        self._props = self.props
        self._name = 'CameraHub'
        self.hub = CameraHub(self.props, parent=self)
        self.camWidgets = {}
        self.setStyleSheet(
            "CameraGUI {"
                "background-color: rgb(195,205,230);"
                "color:blue; margin:0px; "
                "border:5px solid rgb(0, 0, 80);"
            "} "
            "QPushButton {"
                "background-color: white;"
                "color:#000000; "
                "margin:1px; "
                "border:0px solid rgb(20, 240, 255);"
            "} ")
        layout = QGridLayout()

        cnum = 1
        for cam in self._cam_serials.keys():
            camWidget = CamWidget(cam, parent=self)
            self.camWidgets[cam] = camWidget
            layout.addWidget(camWidget, 1, cnum)
            cnum += 1
        self.setLayout(layout)

    def closeEvent(self,event):
        """关闭窗口时先停止所有相机再接受关闭事件。

        Args:
            event (QCloseEvent): 关闭事件。
        """
        for cam in self._cam_serials:
            send_info(f"[Camera Hub] Stopping {cam}")
            self.hub.stop_cam(cam)
        event.accept()

class CamWidget(QWidget):
    """单台相机的控制面板：Start/Stop/Config 按钮与状态标签。

    Attributes:
        hub (CameraHub): 相机管理器。
        label (QLabel): 状态/相机名标签。
        startButton/stopButton (QPushButton): 启停按钮。
    """
    def __init__(self, name, parent=None):
        """构建某台相机的控制面板。

        Args:
            name (str): 相机名（须在 parent._cam_serials 中）。
            parent (CameraGUI|None): 宿主 GUI。
        """
        super().__init__(parent)

        self.setStyleSheet(
            "CamWidget {"
                "background-color: rgb(210,230,240);"
                "color:blue; "
                "margin:7px; "
                "border:7px solid rgb(220, 240, 255); "
            "} "
            "QPushButton {"
                "background-color: rgb(210,230,240);"
                "color:#000000; "
                "margin:1px; "
                "border:0px solid rgb(20, 240, 255);"
            "} ")
        self.setStyleSheet(
            "CamWidget {"
                "background-color: rgb(210,230,240);"
                "color:blue; "
                "margin:1px; "
                "border:2px solid rgb(220, 240, 255); "
            "} ")

        self.hub = parent.hub
        self.name = name
        self.serial = parent._cam_serials[name]
        layout = QVBoxLayout()
        self.label = QLabel(name)
        layout.addWidget(self.label)

        self.startButton = QPushButton(tr('Start'))
        self.startButton.pressed.connect(self.start_cam)
        layout.addWidget(self.startButton)

        self.stopButton = QPushButton(tr('Stop'))
        self.stopButton.clicked.connect(self.stop_cam)
        layout.addWidget(self.stopButton)

        configButton = QPushButton(tr('Config'))
        configButton.pressed.connect(self.config_cam)

        self.setLayout(layout)

    def start_cam(self):
        """按钮回调：启动本面板对应的相机，并把标签/按钮更新为运行态样式。"""
        send_info(f"[CameraHub] Starting camera {self.name} with serial {self.serial}")
        self.label.setStyleSheet("background-color: green")
        self.startButton.setStyleSheet("color: black")
        self.hub.start_cam(self.name, self.serial)

    def stop_cam(self):
        """按钮回调：停止本面板对应的相机，并把标签/按钮复位为停止态样式。"""
        send_info(f"[CameraHub] Stopping camera {self.name} with serial {self.serial}")
        self.hub.stop_cam(self.name)
        self.stopButton.setStyleSheet("color: black")
        self.label.setStyleSheet("background-color: transparent")

    def config_cam(self):
        """按钮回调：对本面板相机应用配置；相机未运行时发送错误提示。
        配置后重新启动采集。"""
        send_info(f"[CameraHub] Configuring camera {self.name} with serial {self.serial}")
        cs = self.hub.camservers.get(self.name)
        if cs is None:
            send_error(f"[CameraHub] Camera {self.name} not running, cannot configure.")
            return
        self.hub.config_cam(self.name)
        cs.run()


def main():
    """启动 Camera Hub 的 Qt 应用（独立运行入口）。"""
    # 安全：ZMQ 无认证/无加密；若 Image Hub 端点被配置到局域网，启动时警告。
    warn_hub_if_exposed('Image Hub', 'Camera Hub')
    app = QApplication(sys.argv)
    Win = CameraGUI()
    # 图标集在 quatm/GUI 下：用到时才导入，别让 servers 模块在 import 期就
    # 反向依赖 GUI 包（服务层要保持只依赖 Qt，不拉整个 GUI 栈）
    try:
        from quatm.GUI.infra.icons import get_icon
        Win.setWindowIcon(get_icon('/cam.png'))
    except Exception:  # noqa: BLE001 - GUI/icon assets are optional in core-only installs
        pass
    Win.setWindowTitle(tr('Camera Hub'))
    Win.show()
    sys.exit(app.exec())

## Start Qt event loop unless running in interactive mode or using pyside.
if __name__ == '__main__':
    if sys.flags.interactive != 1:
        main()