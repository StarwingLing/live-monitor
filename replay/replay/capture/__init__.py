# -*- coding: utf-8 -*-
"""replay 采集子包（v2.2 自 bili-monitor 迁入）。

blivedm WS 采集 + 房间状态轮询 + 人气采样 + 整场录制/直播截取/手动切片
执行 + 每日维护。高能时刻功能已于 v2.2 移除（buffer 缓冲流随之退役）。
入口：``python -m replay.capture.main``。
"""
