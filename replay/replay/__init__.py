# -*- coding: utf-8 -*-
"""replay 项目：B站直播录播回放站（v2.2 起兼任采集与存储管线载体）。

与 bili-monitor 的关系（架构前提，改动前必读；v2.2 架构改版定稿）：
- 数据属主翻转：共享 SQLite（live.db，WAL 多进程——本侧 capture 一写、
  两个 web 读写）与 recordings/ 录像目录归本项目 data/（服务器
  /opt/replay/data/）；表结构由本侧 replay/db.py 维护（init_db 幂等），
  监控类表 bili-monitor 侧持锁步 DDL 副本；
- 本项目承载：capture 采集子包（replay.capture.main，blivedm 弹幕/礼物
  采集 + 房间状态轮询 + 整场录制/直播截取/切片执行）、转码+上云+归档+
  释放管线（replay/web/pipeline.py，replay-web lifespan 内 daemon 线程）、
  回放动线（三角色登录/回放页/管理页/回放 API/OSS 签名直链）；
- bili-monitor 仅保留监控数据页（只读分析 + 房间/账户管理），经共享库
  读取本侧采集的数据；高能时刻功能已于 v2.2 整体移除；
- 管理员账户（admin_users）共用；监控码（rooms.replay_code）、自动全程
  录制开关（rooms.auto_full）在共享 rooms 表上，本侧读写；
- SSO 桥接：读本侧 [bili] 配置指向的 bili-monitor web_secret.key 验签
  其 Cookie（管理员跳转自动续登）。
"""
