"""Bokeh 图像网络服务（示例/调试用）。

按属性 ``/Servers/ImageStream/active`` 中启用的图像流，为每个流建一个
Bokeh figure 并订阅 ImageClient；``callback`` 定期把最新帧刷新到页面。
本模块在 import 时即执行初始化并操作 bokeh ``curdoc``（供 bokeh server 用）。
"""

from random import random
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

from quatm.servers import Properties, ImageClient

from bokeh.layouts import column, row, grid, gridplot
from bokeh.models import Button, Title, ColumnDataSource
from bokeh.models.glyphs import Text
from bokeh.palettes import RdYlBu3
from bokeh.plotting import figure, curdoc

import numpy as np
import time
import datetime

props = Properties('bokeh_image_server')

active_streams = props.get('/Servers/ImageStream/active')
active_stream_keys = active_streams.keys()
#active_stream_keys = ['Axial']
print(active_stream_keys)

stream_dict = {}
figures = []
# create some random 2D data to plot
fx, fy = np.random.randint(1,10,2)
N = 500
x = np.linspace(0, 10, N)
y = np.linspace(0, 10, N)
xx, yy = np.meshgrid(x, y)
d = np.sin(fx*xx)*np.cos(fy*yy)
d = np.zeros_like(d)

for ask in active_stream_keys:
    subdict = {}
    # create a figure
    p = figure(tooltips=[("x", "$x"), ("y", "$y"), ("value", "@image")],plot_width=300, plot_height=300,
            title=ask)
    p.sizing_mode = 'scale_both'
    p.x_range.range_padding = p.y_range.range_padding = 0

        # must give a vector of image data for image parameter
    pi = p.image(image=[d], x=0, y=0, dw=1, dh=1, palette="Inferno256")
    pids = pi.data_source

    # add text label that shows timestamp
    ptext = Text(x='x', y='y', text="text", text_color="#96deb3")
    textsource = ColumnDataSource({'x':[0], 'y':[0], 'text':['timestamp']})
    p.add_glyph(textsource, ptext)

    subdict['figure'] = p
    subdict['image'] = pi
    subdict['datasource'] = pids
    subdict['datasource_text'] = textsource

    figures.append(p)

    # create image client
    imcli = ImageClient('imageserver_'+ask)
    imcli.subscribe(ask)
    subdict['image_client'] = imcli

    stream_dict[ask] = subdict

# create a callback that will add a number in a random location
def callback():
    """定时回调：把每个图像流的最新一帧与时间戳刷到对应 figure。

    逐流读取 ImageClient 缓冲（非阻塞）；有新帧则更新数据源与时间戳文本。
    """
    for ask in active_stream_keys:
        imcli = stream_dict[ask]['image_client']
        imgdata = None
        while imcli.has_new_data():
            msg,head,imgdata = imcli.recv()
        if imgdata is not None:
            stream_dict[ask]['datasource'].data = {'image':[imgdata]}
            timestamp = datetime.datetime.fromtimestamp(head['timestamp']).strftime('%d-%m-%Y, %H:%M:%S')
            stream_dict[ask]['datasource_text'].data = {'x':[0], 'y':[0], 'text': [timestamp]}

# put the button and plot in a layout and add to the document
cur = curdoc()
print(figures)
cur.add_root(grid(figures, sizing_mode='scale_both', ncols=3))
#cur.add_root(column(figures))
cur.add_periodic_callback(callback, 300)

