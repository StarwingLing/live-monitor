# bili-monitor 项目专属约定（子代理说明）

> 本文件只放 bili-monitor 项目专属的规则与状态；跨项目通用规则（环境、部署同步、
> 版本管理、跨机同步、预更新文档、技术调研等）见工作区根目录 [AGENTS.md](../AGENTS.md)。
> 凡涉及本项目的对话：先读根文件，再读本文件；项目功能与结构说明以 `tutorial/` 为准。
>
> **v2.2 架构改版（2026-10-12）**：本项目收敛为**纯监控数据 web**（React 前端 +
> FastAPI 查询/分析 + 房间与账户管理）；capture 采集、录制/切片执行、转码+上云+
> 释放管线、回放动线全部归 replay 项目（数据属主同样翻转，见 replay/AGENTS.md）。
> 高能时刻功能已整体移除（详情页原板块位置改为收益/人气双轴曲线）。

## 前端适配约定

- **详情页曲线板块**（v2.2 建，v2.5.1 换标准）：原高能时刻板块位置为「收益 /
  人气曲线」双 y 轴 ECharts 折线（左轴元/分、右轴**房间观众数·高能榜口径**
  [WS ONLINE_RANK_COUNT→popularity_snapshots.viewers，与总览同接峰值同源，
  VLivePulse 站人气曲线同标准]；v2.2~v2.5.0 曾用 anchor_snapshots.online 加权
  热度，因 WS popularity 恒推 1 借道 HTTP，v2.5.1 弃用）；按 session_id 直查
  （在播场次只取本场次快照，不穿场）；仅场次模式渲染。
- **详情页场次封面板块**（v2.5.1）：场次模式下主播卡粉丝双图位置改显本场
  直播间封面（live_sessions.cover_url，采集端建场写入）；无封面（历史场次）
  显示占位文案；今日/在播模式仍为粉丝量+每日增长双图。
- （历史）replay 按钮页移动端适配约定已随 v1.3.0 剥离失效；本项目旧 replay 页
  仅余 302 桩。

## 数据库与运行依赖（v2.2 起）

- **共享库路径**：live.db 归 replay 项目 data/，本站经 `[server].db_path` 指向
  （服务器 /opt/replay/data/live.db；本地联调 ../replay/data/live.db，默认仍为
  本项目 data/live.db 供独立测试）。
- **DDL 锁步**：本侧 `db.py` 只维护监控类表（danmaku/gifts/场次/快照/画像/
  rooms/admin_users 等）；录制四表与高能两表已移除（replay 侧建表）。**改共享
  表（含 rooms 列）须同版同步 replay/db.py 与两项目测试建表子集**。
  v2.5.1 起 `live_sessions.cover_url`（开播封面，采集端建场写入、本侧
  dashboard 透出）为最新一处锁步列；replay tests/helpers.py 建表子集同步。
- **配置**：仅 [server]（含 db_path）/[capture].admin_password（登录门与
  X-Admin-Token 旧通道）/[web].replay_base/[tier] 四段；[oss]/[recording]/
  [retention] 已迁 replay config。

## 服务器重启的数据保全规程（v2.2 起整体迁 replay）

计划内重启且重启窗口内有直播采集在跑时，按 **replay/AGENTS.md** 的同名规程
执行（托底采集命令为 replay 侧 `uv run python -m replay.capture.main`；快照/
回灌工具 replay/deploy/backfill_local_data.py）。本项目无采集进程，无需单独
保全步骤。

## 从服务器同步数据的口径（2026-10-03 新增；v2.2 路径更新）

本地调试/排查需要服务器数据时，**只同步数据库，不同步视频**：

- 拉取数据一律用 `uv run python ../replay/deploy/backfill_local_data.py snapshot`
  （SQLite 在线备份快照，只拉 live.db 单个数据库文件，天然不含视频；v2.2 起
  工具随采集进程迁 replay 项目）。**禁止**用 scp/rsync/tar 把服务器
  `/opt/replay/data/recordings/`（整场录播、切片、ffmpeg 分段）整体拷回
  本地——体积可达 GB 级，且属 replay 项目运行时产物，本侧无消费场景。
- 快照库中 `recordings` 等录制表的行会随 DB 一并到本地，其 `file_path`
  指向的 mp4 在本地不存在属**预期行为**；需要看片走公网 `/replay/` 对应页面。
- 排查录制/切片问题需确认服务器上成片文件是否真实存在时，SSH 到服务器
  `ls -lh /opt/replay/data/recordings/` 查看即可，不把文件拉回本地
  （确需单个视频回本地，须先经用户确认）。

