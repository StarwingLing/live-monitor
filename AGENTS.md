# live-monitor 公共仓 · AI 部署与维护指引（初始化说明）

> 本文件写给「拿到本仓库、要让 AI 代理把它部署到自己服务器」的用户与代理：
> 当用户说「帮我把这个项目部署到我的服务器」，按本文件的清单执行即可。
> 人类读者的总览见 [README.md](README.md)；各子项目的专属约定见其目录内
> `AGENTS.md`，功能与结构详解见各 `tutorial/`。

## 1. 仓库结构与阅读顺序

```
live-monitor/
├── README.md            # 人类导览：架构总览/前置要求/部署总序/端口表
├── AGENTS.md            # 本文件：AI 代理的操作指引
├── replay/              # 数据属主：采集+录制+管线+回放站（先部署）
│   ├── AGENTS.md        # 共享表 DDL 锁步/角色口径/重启数据保全规程
│   ├── README.md        # 本地快速开始
│   └── deploy/README.md # 服务器部署细节 + 验收清单
├── bili-monitor/        # 监控数据 web（后部署，db_path 指向 replay 库）
│   ├── AGENTS.md        # 数据库依赖/前端适配约定
│   └── tutorial/        # 01 构建思路 → 05 部署到服务器 → 06-08 功能章
└── live-qq-notify/      # 可选：开播 QQ 群通知器
    ├── AGENTS.md        # 双发送通道/关键红线
    └── tutorial/        # 01-03（含 NapCat 部署运维）
```

部署一个子项目前，先读它的 deploy 文档与 `config.example.toml` 注释——
**配置键的含义以模板注释为准，不要凭记忆猜**。

## 2. 核心不变量（任何操作都不得违反）

1. **同级目录名不可改**：三个子项目必须以 `replay` / `bili-monitor` /
   `live-qq-notify` 同级部署（默认相对路径 `../bili-monitor/data` 是
   SSO 桥接与共享库的定位方式）；
2. **只监听回环**：所有 Web 服务 `127.0.0.1:<端口>`，公网经 Nginx 反代；
   Nginx `proxy_pass` **不带末尾斜杠**（保留前缀，配套应用
   `root_path="/<前缀>"`，否则静态挂载 404）；
3. **`config.toml` 与 `data/` 永不提交、永不跨环境覆盖**：服务器配置从
   `config.example.toml` 另写；同步代码时排除二者（`data/` 是运行数据）；
4. **共享表 DDL 锁步**：改 `live.db` 共享表结构必须同版同步
   `replay/replay/db.py` 与 `bili-monitor/bili_monitor/db.py` 及两侧测试；
5. **端口约定**：8001 = bili-monitor、8003 = replay（8002 预留）；改动需
   同步改 systemd 单元、Nginx location、config 三处；
6. **小内存口径**：MemoryMax 见根 README 端口表；转码 ffmpeg 计入
   replay-web 的 cgroup，勿把上限压到 300M 以下（会 OOM 循环）。

## 3. 全新部署工作流（AI 代理照此执行）

### 阶段 0 · 环境确认（本地与服务器各一遍）

```bash
# 本地：uv / node / 可选 ffmpeg
uv --version && node --version
# 服务器：Linux + systemd + nginx + uv + ffmpeg
ssh -p <SSH端口> -i "<SSH私钥路径>" root@<服务器IP> \
    "nginx -v && uv --version && ffmpeg -version | head -1"
```

缺什么先补什么（uv 官方安装脚本；ffmpeg 用系统包管理器）。服务器规格
参考：2 核 / 2GB 内存可跑全部四个服务。

### 阶段 1 · 本地准备

```bash
cd <仓库根>
# 1) 两个前端构建（生产 base 已按 /bili-monitor、/replay 绑定）
(cd bili-monitor/frontend && npm install && npm run build)
(cd replay/frontend        && npm install && npm run build)
# 2) 两个主项目依赖锁定校验（可选，本地跑测试时才需要）
(cd replay && uv sync) && (cd bili-monitor && uv sync)
```

### 阶段 2 · 传输（本地 → 服务器，顺序：先 replay 后 bili-monitor）

```bash
tar czf - --exclude .venv --exclude __pycache__ --exclude node_modules \
    --exclude data --exclude config.toml replay/. \
  | ssh -p <SSH端口> -i "<SSH私钥路径>" root@<服务器IP> \
      "mkdir -p /opt/replay && tar xzf - -C /opt/replay"
# bili-monitor 同法，目标 /opt/bili-monitor；live-qq-notify 同法（可选）
```

### 阶段 3 · 服务器配置（每项目 cp 模板后改）

```bash
# /opt/replay/config.toml 七段：必改 root_path="/replay"；
#   [capture].cookie 粘贴用户 B站 Cookie（无则留空=匿名采集）；
#   [oss].enabled 按用户是否用阿里云 OSS（默认 false 纯本地留存）
# /opt/bili-monitor/config.toml 四段：必改
#   root_path="/bili-monitor"、db_path="/opt/replay/data/live.db"
# /opt/live-qq-notify/config.toml：[db].path="/opt/replay/data/live.db"
```

### 阶段 4 · 环境 + systemd + Nginx

```bash
cd /opt/replay && uv sync
cd /opt/bili-monitor && uv sync
cp /opt/replay/deploy/replay-{web,capture}.service \
   /opt/bili-monitor/deploy/bili-monitor-web.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now replay-capture replay-web bili-monitor-web
# Nginx：把两段 location（deploy/nginx-*.conf）并入默认 server 块
nginx -t && systemctl reload nginx
```

### 阶段 5 · 验收（每个 Web 服务四项全过才算完成）

```bash
systemctl status replay-capture replay-web bili-monitor-web   # ① 存活
ss -tlnp | grep -E '8001|8003'                                 # ② 127.0.0.1
nginx -t                                                        # ③ 配置
curl -I http://127.0.0.1/replay/login                           # ④ 经 Nginx 可达
```

### 阶段 6 · 初始化收尾（告诉用户三件事）

1. **初始凭据**：`/opt/bili-monitor/data/web_credentials.txt`
   （管理员/游客码）、`/opt/replay/data/web_credentials.txt`（切片员
   密码）——首次启动自动生成，登录后到管理页改掉；
2. **加房间**：bili-monitor 管理页（或 replay 管理页房间名单）添加
   直播间 ID，replay-capture 30s 内自动开始监控；
3. **QQ 通知（可选）**：bili-monitor 管理页「开播账号管理」配置通道
   （官方机器人 AppID/AppSecret 或 NapCat），live-qq-notify 需单独部署
   （见其 tutorial/03，含 NapCat 版本钉死等运维红线）。

## 4. 常见运维速查

| 场景 | 做法 |
|------|------|
| 看采集/管线日志 | `journalctl -u replay-capture -f` / `journalctl -u replay-web \| grep pipeline` |
| 换 B站 Cookie | 改 `/opt/replay/config.toml [capture].cookie` → `systemctl restart replay-capture` |
| 计划内重启服务器（有直播在跑） | 按 `replay/AGENTS.md`「服务器重启的数据保全规程」五步闭环（backfill 工具需显式 `--host/--key`） |
| 忘记管理员口令 | 删库中 `admin_users` 对应用户行 → 重启 bili-monitor-web（重新播种并写凭据文件） |
| 更新版本 | 本地改码 → 测试全绿 → 阶段 2 同步（**仍排除 data/ 与 config.toml**）→ 重启对应服务 → 重跑阶段 5 验收 |
| 磁盘紧张 | 确认 OSS 已启用且录播 `oss_key` 非空后按需释放本地（页面「删除服务器存档」只删本地不删云端） |

## 5. 明确不做的事

- 不替用户决定是否开启云存储/QQ 通知——问清需求再配；
- 不在公共仓提交任何真实凭据、服务器地址、采集数据（本仓已脱敏，保持）；
- 不跳过测试直接部署：本地改动后至少跑对应子项目 `tests/` 全量通过；
- 不删除或「清理」`data/` 下任何文件，除非用户明确要求并已备份。
