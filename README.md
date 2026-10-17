# live-monitor · B站直播监控 / 录制 / 回放 / 通知 套件（公共仓）

一个自托管的 Bilibili 直播间运营工具集，由三个可独立部署、共享一份数据库的
子项目组成：

| 子项目 | 角色 | 监听 | 说明文档 |
|--------|------|------|----------|
| [`replay/`](replay/) | **数据属主 + 执行层**：blivedm 弹幕/礼物采集、房间轮询、人气快照、录制/直播截取/打点切片、转码+上云+归档+释放管线、三角色回放站 | `127.0.0.1:8003`（web）+ capture 进程（无端口） | [replay/README.md](replay/README.md)、[replay/deploy/README.md](replay/deploy/README.md) |
| [`bili-monitor/`](bili-monitor/) | **监控数据 web**：React 前端 + FastAPI 查询/分析（弹幕/礼物/营收/观众画像/收益人气曲线）+ 房间与账户管理 + QQ 通知配置 UI | `127.0.0.1:8001` | [bili-monitor/tutorial/README.md](bili-monitor/tutorial/README.md) |
| [`live-qq-notify/`](live-qq-notify/) | **开播 QQ 群通知器**（可选）：轮询开播状态，经 QQ 官方机器人 OpenAPI 或 NapCat(OneBot) 发群通知 | 无 HTTP 端口（NapCat 3000/6099 仅回环、不经 Nginx） | [live-qq-notify/tutorial/README.md](live-qq-notify/tutorial/README.md) |

数据流：`replay` 采集/录制 → 共享 SQLite `replay/data/live.db`（WAL 多进程）
→ `bili-monitor` 只读分析展示；`live-qq-notify` 读同一库的房间开播状态与
通知配置。三项目同级部署（`/opt/replay`、`/opt/bili-monitor`、
`/opt/live-qq-notify`），相对路径 `../bili-monitor/data` 是 SSO 桥接的
默认定位方式，**目录名不要改**。

> 本仓库为脱敏后的公共发布版：不含任何真实服务器地址、账号凭据、云存储
> 桶名与已采集数据；内部版本历史文档（《版本更新笔记.md》《预更新计划.md》）
> 未随仓发布。各子项目教程中 `<服务器IP>` `<SSH端口>` `<SSH私钥路径>`
> `<本地工作区目录>` 等占位符请按你自己的环境替换。

## 前置要求

- **本地**：Python 3.12 + [uv](https://docs.astral.sh/uv/)（各子项目独立
  uv 项目，`uv sync` 一键建环境）；Node.js ≥ 18（构建两个 React 前端）；
- **服务器**：Linux + systemd；Nginx；`uv`；`ffmpeg`（录制/转码需要，
  不录制可不装并把 `[recording].enabled` 置 false）；
- **可选**：阿里云 OSS（录播云端留存，不用则 `[oss].enabled = false`
  纯本地）；QQ 机器人（AppID/AppSecret 或 NapCat，仅通知功能需要）；
  B站登录 Cookie（不配则匿名采集，弹幕昵称/uid 会被平台打码）。

## 快速开始（本地体验）

```bash
git clone <本仓库> live-monitor && cd live-monitor

# 1) 回放站（数据属主，先起它——建库建表）
cd replay && uv sync
uv run uvicorn replay.web.app:app --host 127.0.0.1 --port 8003   # web
# 另开终端（可选，采集数据源）：
uv run python -m replay.capture.main

# 2) 监控数据 web（读 replay 的库）
cd ../bili-monitor && uv sync
# 首次运行先复制配置：cp config.example.toml config.toml
#   并设 [server].db_path = "../replay/data/live.db"
uv run uvicorn bili_monitor.web.app:app --host 127.0.0.1 --port 8001
```

本地未配置口令时为**调试模式**（匿名访问即管理员）。首次启动自动生成
初始凭据与签名密钥（`data/web_credentials.txt`、`data/web_secret.key`）。
前端开发调试：各子项目 `frontend/` 下 `npm install && npm run dev`
（生产部署才需要 `npm run build` 出 `dist/`）。

## 服务器部署（总体顺序）

> 详细步骤见各子项目 `deploy/README.md` / `tutorial/05`；AI 辅助部署的
> 操作指引见根 [AGENTS.md](AGENTS.md)。**顺序：先 replay（建库）→
> 后 bili-monitor（db_path 指向）→ 可选 live-qq-notify。**

1. **前端构建**（本地）：两个子项目 `frontend/` 下
   `npm install && npm run build`（产物 `dist/` 不入库，构建时按
   `/bili-monitor`、`/replay` 前缀绑定）；
2. **同步代码**（tar over ssh，排除 `.venv/__pycache__/node_modules/data/
   config.toml`）到 `/opt/<项目名>/`；
3. **配置**：每项目 `cp config.example.toml config.toml` 后修改——
   replay 七段（端口/root_path/cookie/录制/留存/OSS/tier）、
   bili-monitor 四段（端口/root_path/**db_path 指向
   `/opt/replay/data/live.db`**/replay_base）、live-qq-notify
   `[db].path` 指向同一库；
4. **环境与 systemd**：各项目 `uv sync`，安装
   `deploy/*.service` 到 `/etc/systemd/system/` 后
   `systemctl enable --now`（共 3+1 个单元，见下表）；
5. **Nginx**：在**默认 server 块**内加入两段 location
   （`deploy/nginx-bili-monitor.conf`、`deploy/nginx-replay.conf`），
   `proxy_pass` **不带末尾斜杠**（保留前缀，配套 FastAPI
   `root_path`），`nginx -t && systemctl reload nginx`；
6. **验收**：每服务四项——进程存活 / 回环监听（`ss -tlnp`）/
   nginx 配置生效 / 公网 `http://<服务器IP>/<前缀>/` 可达。

### 端口与服务清单

| 服务 | systemd 单元 | 监听 | Nginx 前缀 | MemoryMax |
|------|--------------|------|-----------|-----------|
| bili-monitor web | `bili-monitor-web.service` | 127.0.0.1:8001 | `/bili-monitor/` | 250M |
| replay web（含管线） | `replay-web.service` | 127.0.0.1:8003 | `/replay/` | 800M |
| replay 采集/录制 | `replay-capture.service` | —（出站连接） | — | 800M |
| live-qq-notify（可选） | `live-qq-notify.service` | —（出站轮询） | — | 80M |

（内存上限为 2 核 / 2GB 级小服务器的实测口径，可按机器调整；原始部署在
更低规格上稳定运行。）

## 配置与凭据红线（必读）

- `config.toml`（含 Cookie/OSS AccessKey/口令）与 `data/`（数据库、录像、
  凭据文件）**永不入库、永不跨环境同步**——.gitignore 已排除，部署时
  服务器上从模板另写；
- 所有服务只监听 `127.0.0.1`，公网一律经 Nginx 反代 + 应用内登录页；
- 初始凭据在首次启动自动生成（`data/web_credentials.txt`），登录后尽快
  在管理页修改/重置；
- 改**共享表结构**必须同版同步 `replay/replay/db.py` 与
  `bili-monitor/bili_monitor/db.py`（DDL 锁步约定，详见各项目 AGENTS.md）；
- 服务器重启前若有直播采集在跑，按 `replay/AGENTS.md` 的
  「服务器重启的数据保全规程」执行托底采集与回灌。

## 用途与合规

本套件面向**自有或获得授权管理的 Bilibili 直播间**的数据记录与运营分析
（弹幕/礼物统计、录播归档、回放切片、开播通知）。使用时应遵守目标平台
的服务条款与当地法律法规；请勿用于未授权的第三方数据采集或其他侵权用途。
前端视觉设计基于 ThreeUI（MIT License，见
`frontend/src/assets/FONT-LICENSES.md` 的字体许可说明）。
