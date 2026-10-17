# -*- coding: utf-8 -*-
"""bili-monitor：B站直播间数据监控系统。

包结构：
- config.py   配置加载（config.toml）
- db.py       SQLite 数据层（建表、连接、批量写入、聚合、清理）
- capture/    采集服务（blivedm 客户端管理、事件入库、状态轮询）
- web/        Web 服务（FastAPI 查询接口与管理页面）
"""
