# replay · B站直播采集 / 录制 / 回放站

v2.2 架构定位：本项目是整个直播监控套件的**数据属主与执行层**——
blivedm 弹幕/礼物采集、房间轮询与人气快照（`replay.capture`）、
录制/直播截取/打点切片执行、转码+上云+归档+释放管线
（`replay.web.pipeline`，web 服务内 daemon 线程）与三角色回放站都在这里。
共享数据库 `data/live.db` 与录像目录 `data/recordings/` 归本项目；
[bili-monitor](../bili-monitor/) 经其 `[server].db_path` 指向同一数据库，
做只读的监控数据分析展示。

## 快速开始（本地开发）

```bash
# 0. 前置：同级目录存在 bili-monitor 项目（SSO 桥接读其 data/web_secret.key；
#    本地未配置口令时为调试模式——匿名访问自动以管理员身份进入）
cd replay
uv sync

# 1. 启动采集（弹幕/礼物/录制数据源；不启动则无新数据）
uv run python -m replay.capture.main

# 2. 启动 web（回放站 + 转码上云管线）
uv run uvicorn replay.web.app:app --host 127.0.0.1 --port 8003

# 3. 浏览器访问
#    登录页   http://127.0.0.1:8003/login
#    管理页   http://127.0.0.1:8003/manage
#    回放页   http://127.0.0.1:8003/<房间号>/replay

# 4. 测试（常规套件无需 ffmpeg；e2e 录制套在无 ffmpeg 环境自动跳过）
uv run python tests/test_replay_auth.py
uv run python tests/test_replay_api.py
```

## 三角色与监控码

| 角色 | 登录方式 | 能力 |
|------|----------|------|
| 管理员 | bili-monitor 同款账号密码（无需监控码） | 全局读写所有房间 + 管理页（监控码、切片员密码、自动全程录制开关），登录后进入管理页 |
| 切片员 | 切片员共用密码（全员一个）+ 昵称 + 监控码 | 该房间起停录制、直播截取、打点切片、注释 |
| 游客 | 仅监控码 | 该房间只读：浏览归档、播放、查切片、看注释 |

监控码一房一码（`r<房间号>-<10位随机>`），管理页生成/重置；切片员/游客
登录必填，按码进入对应主播的回放页。切片员共用密码首次启动自动播种到
`data/web_credentials.txt`，由管理员在管理页查看/重置后分发（仅作身份
验证，昵称才是署名身份）。

## SSO（与 bili-monitor 互通）

bili-monitor 已登录的管理员在总览页点「录制」→ 跳转 `/replay/<房>/replay`
后自动保持管理员身份（本项目用其 `web_secret.key` 验签其 Cookie 并补发
本站会话）；旧监控码会话自动映射为对应房间切片员。本地开发时
bili-monitor 总览页跳转依赖其 `config.toml [web].replay_base`（服务器
部署为默认 `/replay` 无需配置）。

## 文档索引

- [AGENTS.md](AGENTS.md)——项目专属约定（共享架构与 DDL 锁步、角色口径、
  服务器重启数据保全规程、前端适配断点）
- [deploy/README.md](deploy/README.md)——服务器部署步骤与验收清单
- `config.example.toml`——配置模板（本项目自有七段：
  `[server]/[bili]/[capture]/[recording]/[oss]/[retention]/[tier]`，
  OSS AccessKey 只此一处维护）
- 仓库根 [README.md](../README.md) / [AGENTS.md](../AGENTS.md)——多项目
  总览与通用部署约定
