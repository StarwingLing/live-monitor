# -*- coding: utf-8 -*-
"""live-qq-notify:B 站直播间开播状态 → QQ 群通知服务。

架构:
- 本进程为**同步轮询器**:每轮从共享 live.db(bili-monitor / replay 共库)读
  房间级通知配置(qq_notify 表,总览页开关写入)+ 全局配置(qq_notify_global
  表,管理页「开播账号管理」写入),对启用房间请求 B 站公开接口比对
  live_status,翻转时经 NapCat(OneBot 11 HTTP)发群消息;
- 房间名单单一事实来源 = bili-monitor 监控列表(rooms 表,本进程只读);
- 除轮询节奏外的全部业务配置(开关/群映射/文案/账号)都在共享库,
  页面改完最迟一个轮询周期生效,无需重启本进程。
"""
