"""Experiment status and progress reporting."""

import time
from datetime import datetime
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

from quatm.servers import DataClient
from quatm.servers.configreader import warn_hub_if_exposed
from quatm.servers.mattermost import post_to_notification

# 安全：ZMQ 无认证/无加密；若 Data Hub 端点被配置到局域网，启动时警告。
warn_hub_if_exposed('Data Hub', 'Experiment Status')

name = "experiment_status_reporter"
stream = DataClient(name)
stream.subscribe("Experiment.start")

icons = {"00_Interaction_optical_dipole_trap": ":boom:",
         "triggercam": ":eyes:",
         "00_loading_scheme": ":sparkler:",
         "01_optical_trapping": ":mouse_trap:",
         "02_compensation_helper": ":mag:",
         "03_pumpi_the_pump": ":fuelpump:",
         "odt_helper": ":dart:",
         "SetPiezoMirrorVoltage": ":zap:",
         "compensation_helper": ":battery:",
         "rf_trap_defaults": ":black_circle:",
         "tickle_ion": ":joy:"}

experiment_counts = {}
n_tot = 0
msgmm = r"**Summary of the last 100 experiments** "
msgmm += "\n \n"

while True:
    while stream.has_new_data():
        msg = stream.recv()
        if msg != None:
            n_tot += 1
            metadata = msg[1]

            name = metadata.get("_expName", "unknown")
            if type(name) is list:
                name = name[0]
            if not isinstance(name, str):
                name = str(name)

            icon = "▒"
            if name in icons.keys():
                icon = icons[name]

            msgmm += icon

            if name in experiment_counts.keys():
                experiment_counts[name] += 1
            else:
                experiment_counts[name] = 1

            ## POSTING FOR EVERY EXPERIMENT MIGHT BE TOO VERBOSE
            #msgmm = "{} Currently running `{}`".format(icon, metadata["_expName"])
            #attachmentmm = {}
            #text = ""
            #for key, value in sorted(metadata.items()):
            #    text += "    {}: {} \n".format(key, value)
            #attachmentmm["text"] = text
            #attachmentmm["title"] = "Experiment configuration"
            #attachmentmm["color"] = "ADD8E6"

            if n_tot == 100:
                sorted_experiment_counts = sorted(experiment_counts.items(), key=lambda x:x[1])
                attachmentmm = {}
                text = ""
                for exp, counts in sorted_experiment_counts[::-1]:
                    text += "    {}: {} \n".format(exp, counts)
                attachmentmm["text"] = text
                attachmentmm["title"] = "Summarized experiment counts"
                attachmentmm["color"] = "ADD8E6"
                post_to_notification(msg=msgmm, channel_name="quatm-experiment-status", attachments=[attachmentmm])

                # reset things
                msgmm = r"**Summary of the last 100 experiments** "
                msgmm += "\n \n"

                n_tot = 0
                experiment_counts = {}

    # 空闲时也要睡：原来的 sleep 在"有数据"分支内，空闲时外层循环
    # 无任何等待，会 100% 占满一个 CPU 核心
    time.sleep(0.1)