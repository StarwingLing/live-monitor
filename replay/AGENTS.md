# replay 项目专属约定（子代理说明）

> 本文件只放 replay 项目专属的规则与状态；跨项目通用规则（环境、部署同步、
> 版本管理、跨机同步、预更新文档、技术调研等）见工作区根目录
> [AGENTS.md](../AGENTS.md)。凡涉及本项目的对话：先读根文件，再读本文件。

## 与 bili-monitor 的共享关系（v2.2 架构改版，改动前必读）

- **数据属主在本侧**：共享 SQLite（本项目 `data/live.db`，服务器
  /opt/replay/data/，WAL 多进程——本侧 capture 一写、两 web 读写）与
  `data/recordings/` 录像目录均归本项目；表结构与迁移由本项目 `replay/db.py`
  的 init_db 幂等维护（capture 与 web 启动各跑一次）；
- **DDL 锁步**：监控类表（danmaku/gifts/场次/快照/画像/rooms/admin_users 等）
  bili-monitor 侧持有一份 DDL 副本（其登录/分析所需）——**任何一侧改共享表
  结构，必须同版同步另一侧 db.py 与两项目测试的建表子集**；录制四表
  （recordings/recording_notes/manual_clips/manual_clip_notes）为本项目专属；
  高能两表已随 v2.2 移除（存量由 deploy/cleanup_he_v2_2.py 清除）；
- **职责边界（v2.2 翻转）**：本项目承载采集（replay.capture.main：blivedm
  弹幕/礼物 + 房间轮询 + 人气快照）、录制/直播截取/切片执行、转码+上云+归档+
  释放管线（replay/web/pipeline.py，replay-web lifespan 内 daemon 线程）、
  回放动线（三角色登录/回放页/管理页/回放 API/OSS 签名直链）；bili-monitor
  仅剩监控数据页（只读分析 + 房间/账户管理），经共享库读本侧采集的数据；
- **配置边界**：本项目 config.toml 七段自有（[server]/[bili]/[capture]/
  [recording]/[oss]/[retention]/[tier]，AK 只此一处）；[bili] 段仅存 SSO
  桥接项（默认取 ../bili-monitor/data/web_secret.key 验签其 Cookie）；
- **管理员账户一体**：登录校验查共享库 `admin_users`（与 bili-monitor 总览页
  同一账号）；监控码在共享库 `rooms.replay_code`（本侧管理页生成/重置），
  `rooms.auto_full` 由本侧管理页写、capture 对账消费；rooms 表两侧各写各的
  字段（bili 写名单/标签/描述，本侧写 replay_code/auto_full，capture 补主播
  信息）。

## 服务器重启的数据保全规程（v2.2 自 bili-monitor 迁入）

适用条件：**计划内**重启服务器（系统更新、维护等），且重启窗口内有直播采集
在跑。不得直接重启，按五步闭环（命令在本地 replay 项目文件夹内、Git Bash 执行）：

1. **版本对齐**：`git fetch origin && git status`，落后则 `git pull --ff-only`；
2. **取快照 + 记边界**（SQLite 在线备份，不中断服务器采集）：
   `uv run python deploy/backfill_local_data.py snapshot`；
3. **本地托底采集**：核对本地 config.toml 房间清单/cookie 与服务器一致后，
   `uv run python -m replay.capture.main`（本地端顶住重启窗口继续采集）；
4. **重启服务器（须经用户确认）**：`ssh -p <SSH端口> root@<服务器IP> "reboot"`；
   服务恢复并完成部署验收后立刻停本地采集（越早停重叠窗口越小）；
5. **数据回灌**：`uv run python deploy/backfill_local_data.py export` →
   `apply --dry-run` → `apply`；完成后向用户汇报闭环结果。

工具要点（重叠窗口去重、场次不拆、幂等事务）见其文件头注释。

## 角色与登录动线（v0.1 定稿；v0.1.1 增补切片员密码；v0.1.2 管理员免监控码）

- 三角色统一登录页 `/login`：**管理员**（账号密码即可，全局权限可管理
  所有房间，登录后进入管理页）、**切片员**（切片员共用密码 + 昵称 +
  监控码，能力集合 = 原 bili 监控码会话：起停录制/直播截取/打点切片/
  注释）、**游客**（仅监控码，只读浏览）；
- **切片员共用密码**（v0.1.1；v0.1.2 改明文存储）：全员一个、仅作身份
  验证；**明文入库**（app_secrets key='clipper_password'，与监控码同口径：
  管理页需随时查看/复制分发，哈希不可逆无法回显；泄露影响面有限——
  切片员登录还需对应房间的监控码），恒时比对校验；首次启动播种 + 旧版
  哈希格式启动时自动迁移重置（明文写 bili 侧凭据文件），管理页明文显示
  + 重置（旧密码立即失效，已登录会话不受影响）；
- **监控码是切片员/游客的必填项**，按码进入对应主播的回放页
  （`/<房间号>/replay`）——码是「入场凭证 + 房间导航」，实际能力由角色定；
  **管理员无需监控码**（v0.1.2：全局权限不绑定房间，登录页监控码框在
  管理员 tab 下弱化免填）；
- **SSO 桥接**（middleware）：无本站会话但持有效 bili 会话 Cookie 时自动
  续登——bili admin → 本站 admin；bili 旧监控码会话（role=replay）→ 对应
  房间 clipper；bili guest 不映射。退出登录只清本站 Cookie，bili 会话仍在
  时下次访问会自动续登（文档口径，不是 bug）。

## 前端适配约定

- 三页（login / admin / replay）均做了**移动端适配**：≤1020px 归档组卡
  纵向堆叠（播放器在上、选集列表在下）；≤1000px 筛选板块回落顶部横条且
  默认折叠（页角「管理/退出」fixed 右上，筛选标题行预留其宽度避免遮挡）；
  ≤560px 大按钮缩小、切片抽屉全屏、重命名气泡限宽。改动后须在 375px 宽
  与桌面宽度各过一遍。

## 数据口径备忘

- 本地开发：bili config 未设 admin_password = 调试模式，与生产**共用同一
  权限门**（v0.1.2 起）：完全匿名时 middleware 注入匿名管理员（用户名
  `(local)`，保持"打开就能用"）；**有任何会话则按会话角色完整校验**——
  本地登录切片员/游客即可测试对应视图与权限（写接口 403、管理页弹回等
  均与生产行为一致）。本地库来自快照，录播文件不在本地属预期（播放走
  服务器公网页面）；
- **身份切换动线**（v0.1.2 定稿）：**到达登录页即退出**——middleware 对
  `/login` 页面请求清除本站会话 Cookie 并抑制 SSO 桥接续登，三角色一致，
  页面恒为"已退出所有角色、选择身份登录"的干净表单（无身份提示元素）；
  回放页/管理页页角「退出」按钮仅是快捷方式（跳登录页本身就会清会话）。
  SSO 语义提醒：bili-monitor 会话仍在时，从回放页（非登录页）进入仍会
  自动续登管理员——要换身份就去登录页选新身份登录（登录会覆盖会话
  Cookie）；
- 端口：8003（8001 bili-monitor、8002 bus-manager 已占）；服务器
  root_path=/replay，Nginx 前缀反代，安全组不开新端口；
- OSS：签名用公网 endpoint 客户端（懒加载），删除联动用临时内网客户端；
  `oss.enabled=false`（本地默认，读 bili config）时 /oss-url 404，页面走
  纯本地行为。
