# live-monitor · B站直播监控 / 录制 / 回放 / 通知 套件（公共仓）

一个自托管的 Bilibili 直播间运营工具集：三个共享一份数据库、可独立部署的
服务端子项目，加一个纯本地运行的辅助工具：

| 子项目 | 角色 | 监听 | 说明文档 |
|--------|------|------|----------|
| [`replay/`](replay/) | **数据属主 + 执行层**：blivedm 弹幕/礼物采集、房间轮询、人气快照、录制/直播截取/打点切片、转码+上云+归档+释放管线、切片一键投稿 B 站、三角色回放站 | `127.0.0.1:8003`（web）+ capture 进程（无端口） | [replay/README.md](replay/README.md)、[replay/deploy/README.md](replay/deploy/README.md) |
| [`bili-monitor/`](bili-monitor/) | **监控数据 web**：React 前端 + FastAPI 查询/分析（弹幕/礼物/营收/观众画像/收益人气曲线/弹幕词云）+ 房间与账户管理 + 多机器人 QQ 通知与 AI 智能回复配置 UI | `127.0.0.1:8001` | [bili-monitor/tutorial/README.md](bili-monitor/tutorial/README.md) |
| [`live-qq-notify/`](live-qq-notify/) | **开播 QQ 群通知器**（可选）：轮询开播状态，按机器人经 QQ 官方机器人 OpenAPI 或 NapCat(OneBot) 分组发群通知，附下播报告图 | 无 HTTP 端口（NapCat 3000/6099 仅回环、不经 Nginx） | [live-qq-notify/tutorial/README.md](live-qq-notify/tutorial/README.md) |
| [`song-detector/`](song-detector/) | **本地辅助工具**（可选）：YAMNet 离线识别录播中的唱歌/音乐段落（连续 ≥2 分钟才算唱歌段），输出时间清单 + 本地网页校对页，供切片前定位歌曲位置；全部本机离线完成 | 无（校对页 `127.0.0.1:8765`，仅本机） | [song-detector/使用说明.md](song-detector/使用说明.md) |

数据流：`replay` 采集/录制 → 共享 SQLite `replay/data/live.db`（WAL 多进程）
→ `bili-monitor` 只读分析展示；`live-qq-notify` 读同一库的房间开播状态与
通知配置。三个服务端项目同级部署（`/opt/replay`、`/opt/bili-monitor`、
`/opt/live-qq-notify`），相对路径 `../bili-monitor/data` 是 SSO 桥接的
默认定位方式，**目录名不要改**。`song-detector` 不连数据库、不部署服务器：
按「录播日期 + 录播名」的文件命名约定与 replay 下载到本机的录播对号
（replay 切片抽屉提供「下载 low 以进行识别 / 加载本地识别文件」联动）。

> 本仓库为脱敏后的公共发布版：不含任何真实服务器地址、账号凭据、云存储
> 桶名与已采集数据；内部版本历史文档（《版本更新笔记.md》《预更新计划.md》）
> 未随仓发布。各子项目教程中 `<服务器IP>` `<SSH端口>` `<SSH私钥路径>`
> `<本地工作区目录>` 等占位符请按你自己的环境替换。

## 部署所需的外部配置一览（集中清单）

除第 1 行的基础环境外，**其余全部可选**——不配置任何账号与云服务也能完整
跑监控 / 录制 / 回放 / 数据分析，只是相应功能降级或不可用。含密钥的配置只
落在服务器端 `config.toml`（不入库）或共享数据库（页面管理），键含义以各
`config.example.toml` 注释为准。

| # | 外部依赖 / 配置 | 支撑的功能 | 不配置时的降级 | 配置位置 |
|---|----------------|-----------|---------------|----------|
| 1 | **服务器基础环境**：Linux + systemd + Nginx + uv + ffmpeg；**最低配置 2 核 / 2GB 内存**可跑全部服务端（原始部署 2C/1.7G 实测稳定）；磁盘按留存策略估（OSS 关闭时录播全量落本地，原画参考每小时 GB 级）；构建前端需任一机器有 Node.js ≥ 18 | 全部服务端 | —（必须） | 根 [AGENTS.md](AGENTS.md) 部署工作流、各 `deploy/README.md` |
| 2 | **B 站登录 Cookie**（任一账号，浏览器导出整串） | 完整弹幕昵称 / 真实 uid（匿名采集会被平台打码）；原画档录制（匿名 / 登录失效被限 720p） | 匿名采集可用，昵称打码、录制限档 | `replay/config.toml` `[capture].cookie`；失效换串重启 replay-capture |
| 3 | **投稿 B 站账号** + biliup-rs 二进制（v0.2.4，一次安装） | 切片一键投稿 B 站（新视频 / 分P追加 / 合集） | 投稿功能禁用，录制/切片不受影响 | `replay/config.toml` `[bili].biliup_bin` + 管理页「账号管理」扫码登录（凭据落 `data/biliup-cookies.json`） |
| 4 | **阿里云 OSS**（主桶 + 可选切片桶 + AccessKey/SK） | 录播/切片云端留存、回放页签名直链播放、磁盘释放后回看、下播报告图公网中转 | `enabled=false` 纯本地留存（磁盘自担） | `replay/config.toml` `[oss]` 段；`live-qq-notify` 报告图中转共用同份 AK（其 `[oss]` 段） |
| 5 | **QQ 官方机器人**（[q.qq.com](https://q.qq.com) 注册，AppID + AppSecret） | 开播/下播 QQ 群通知（官方通道；被动回复 + 有限主动推送额度） | 该通道不可用 | bili-monitor 管理页「机器人管理」（多机器人档案，配置存共享库） |
| 6 | **NapCatQQ**（服务器另部署，版本按 live-qq-notify tutorial/03 锁定）+ 用于发通知的 QQ 号 | QQ 群通知（NapCat 通道：主动推送无额度限制，小服务器推荐主力通道） | 该通道不可用（与官方通道可并存按机器人选） | NapCat 部署运维见 `live-qq-notify/tutorial/03`；地址/token/扫码登录在管理页配置 |
| 7 | **AI 大模型 API Key**（OpenAI 兼容协议；智谱 / DeepSeek / 通义 / Kimi / 硅基流动等均可） | 群内 @机器人 未命中指令时的 LLM 兜底智能回复（频控可调） | 不开启智能回复 | 管理页「机器人管理 → AI 智能回复」（服务商下拉自动回填地址） |
| 8 | **song-detector 模型**（TFHub YAMNet，约 4MB） | 唱歌/音乐段落本地识别 | 该工具不可用 | 无需配置：首次运行自动下载；断网环境手动下载见 `song-detector/使用说明.md` |

## 前置要求

- **本地**：Python 3.12 + [uv](https://docs.astral.sh/uv/)（各子项目独立
  uv 项目，`uv sync` 一键建环境）；Node.js ≥ 18（构建两个 React 前端；
  song-detector 为纯 Python 工具，无需 Node）；
- **服务器**：Linux + systemd；Nginx；`uv`；`ffmpeg`（录制/转码需要，
  不录制可不装并把 `[recording].enabled` 置 false）；
- **外部账号与云服务**：全部可选，清单见上节「部署所需的外部配置一览」。

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

# 3) 可选：开播 QQ 群通知器（先按 tutorial/03 部署 NapCat，再启动本进程）
cd ../live-qq-notify && uv sync
cp config.example.toml config.toml   # [db].path = "../replay/data/live.db"
uv run python -m live_qq_notify.main

# 4) 可选：本地唱歌片段识别（独立工具，不依赖上面的服务）
cd ../song-detector && uv sync
uv run python -m songdetector analyze <录播文件或目录>
uv run python -m songdetector serve    # 浏览器开 http://127.0.0.1:8765 校对
```

本地未配置口令时为**调试模式**（匿名访问即管理员）。首次启动自动生成
初始凭据与签名密钥（`data/web_credentials.txt`、`data/web_secret.key`）。
前端开发调试：两个 Web 子项目 `frontend/` 下 `npm install && npm run dev`
（生产部署才需要 `npm run build` 出 `dist/`）。

## 服务器部署（总体顺序）

> 详细步骤见各子项目 `deploy/README.md` / `tutorial/05`；AI 辅助部署的
> 操作指引见根 [AGENTS.md](AGENTS.md)。**顺序：先 replay（建库）→
> 后 bili-monitor（db_path 指向）→ 可选 live-qq-notify；song-detector
> 是纯本地工具，不部署服务器。**

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
| bili-monitor web | `bili-monitor-web.service` | 127.0.0.1:8001 | `/bili-monitor/` | 400M |
| replay web（含管线） | `replay-web.service` | 127.0.0.1:8003 | `/replay/` | 800M |
| replay 采集/录制 | `replay-capture.service` | —（出站连接） | — | 800M |
| live-qq-notify（可选） | `live-qq-notify.service` | —（出站轮询） | — | 400M |

（内存上限为 2 核 / 2GB 级小服务器的实测口径，可按机器调整；原始部署在
更低规格上稳定运行。）

## 配置与凭据红线（必读）

- `config.toml`（含 Cookie/OSS AccessKey/口令）与 `data/`（数据库、录像、
  凭据文件）**永不入库、永不跨环境同步**——.gitignore 已排除，部署时
  服务器上从模板另写；
- 所有服务只监听 `127.0.0.1`，公网一律经 Nginx 反代 + 应用内登录页；
- 初始凭据在首次启动自动生成（`data/web_credentials.txt`），登录后尽快
  在管理页修改/重置；
- 改**共享表结构**必须同版同步：监控类表同步
  `replay/replay/db.py` 与 `bili-monitor/bili_monitor/db.py`（DDL 锁步
  约定，详见各项目 AGENTS.md）；通知类表（qq_notify / qq_group_map /
  qq_bots 等）DDL 属主为 live-qq-notify（其 store.py），bili-monitor
  db.py 持锁步副本，改结构须同版同步两侧；
- 服务器重启前若有直播采集在跑，按 `replay/AGENTS.md` 的
  「服务器重启的数据保全规程」执行托底采集与回灌。

## 用途与合规

本套件面向**自有或获得授权管理的 Bilibili 直播间**的数据记录与运营分析
（弹幕/礼物统计、录播归档、回放切片、开播通知）。使用时应遵守目标平台
的服务条款与当地法律法规；请勿用于未授权的第三方数据采集或其他侵权用途。
前端视觉设计基于 ThreeUI（MIT License，见
`frontend/src/assets/FONT-LICENSES.md` 的字体许可说明）。
