# replay 服务器部署步骤（全新部署向导）

> v2.2 架构：本项目是整个直播监控套件的**数据属主**——blivedm 采集、房间轮询、
> 人气快照、录制/直播截取/打点切片执行、转码+上云+归档+释放管线与回放站
> 全部在本项目，**双 systemd 服务**（replay-capture / replay-web）。
> 共享数据（live.db、recordings/、cookies.json）在 `/opt/replay/data/`，
> bili-monitor 经其 `[server].db_path` 指向同一数据库只读消费。
>
> 多项目总体部署顺序与端口约定见**仓库根 [README.md](../../README.md)**；
> 本文只讲 replay 自身的部署细节。

## 0. 前置条件

- 服务器：Linux + systemd，已安装 uv、ffmpeg（PATH 可执行）、Nginx；
- 本地（或服务器）：Node.js ≥ 18——前端 `frontend/` 需 `npm run build`
  产出 `dist/`（dist 不入库；构建产物按 `/replay` 前缀绑定，见
  `frontend/vite.config.ts` 注释）；
- 同机同级目录 `/opt/bili-monitor/` 已部署（SSO 桥接读其
  `data/web_secret.key` 验签会话；replay 配置默认按相对路径
  `../bili-monitor/data` 定位，无需显式配置）。

## 1. 前端构建（本地执行）

```bash
cd frontend
npm install
npm run build          # 产出 frontend/dist/（base=/replay/）
```

## 2. 同步代码到服务器

```bash
# 在本仓库根目录执行（排除各运行时产物；rsync 可用时亦可）
tar czf - --exclude .venv --exclude __pycache__ --exclude node_modules \
    --exclude data --exclude config.toml replay/. \
  | ssh -p <SSH端口> -i "<SSH私钥路径>" root@<服务器IP> \
      "mkdir -p /opt/replay && tar xzf - -C /opt/replay"
```

（`data/` 是运行数据目录，**绝不能被本地覆盖**；`config.toml` 在服务器
单独写，见第 3 步。）

## 3. 服务器配置 `/opt/replay/config.toml`

由 `config.example.toml` 复制后修改（七段完整键表与注释以模板为准）：

```toml
[server]
listen_host = "127.0.0.1"     # 只监听回环，公网经 Nginx 反代
listen_port = 8003
root_path = "/replay"         # Nginx 前缀反代配套

[bili]
# data_dir / bili_secret_path 留空 = 默认取同级 ../bili-monitor/data
# （仅定位其 web_secret.key 做 SSO 验签，通常无需配置）

[capture]
cookie = ""                   # B站登录 Cookie 串（空=匿名采集，昵称/uid 打码）

# [recording] / [oss] / [retention] / [tier]：
# 按模板注释填写；不用阿里云 OSS 则保持 [oss].enabled = false（纯本地留存）。
```

## 4. 装环境 + systemd 双服务

```bash
ssh -p <SSH端口> -i "<SSH私钥路径>" root@<服务器IP>
cd /opt/replay && uv sync
cp deploy/replay-web.service deploy/replay-capture.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now replay-capture replay-web
```

首次启动自动生成：`data/live.db`（建表）、`data/web_credentials.txt`
（切片员共用密码明文，管理页可查看/重置）。

## 5. Nginx 反代

在服务器 Nginx 的默认 server 块（IP 直达场景 Host 不固定，独立 server 块
不会被 default_server 选中——实测踩坑）加入 `deploy/nginx-replay.conf`
中的 location 块；**`proxy_pass` 不带末尾斜杠**（保留 `/replay` 前缀转发，
与 `root_path="/replay"` 配套，否则 `/static` 挂载 404）：

```bash
nginx -t && systemctl reload nginx
```

## 6. 部署验收（四项全过才算完成）

1. `systemctl status replay-capture replay-web` 均存活；
2. `ss -tlnp | grep 8003` 应为 `127.0.0.1:8003`；
3. `nginx -t` 通过且已 reload；
4. 本地浏览器/curl 访问 `http://<服务器IP>/replay/login` 可达。

## 7. 功能验收清单

- [ ] 登录页三角色：管理员（bili-monitor 同款账号密码，无需监控码）→ 登录后
      进入管理页；切片员（共用密码+昵称+监控码）→ 可起停录制/切片/注释；
      游客（仅监控码）→ 只读（无大按钮、写接口 403）；
- [ ] SSO：bili-monitor 已登录管理员跳转 `/replay/...` 自动续登管理员；
- [ ] 管理页 `/replay/manage`：切片员密码显示/复制、监控码复制/重置、
      自动全程录制开关、房间名单自管（B站两级拉取）；
- [ ] 采集：`journalctl -u replay-capture -f` 可见房间连接与轮询；
      开播后人气快照/弹幕持续写入（bili-monitor 详情页曲线动起来）；
- [ ] 管线：`journalctl -u replay-web | grep pipeline` 无异常；
      一场录制收尾后 low 转码 / 上云（启用 OSS 时）正常运转；
- [ ] 回放页冒烟：播放/弹幕/注释/切片、录播命名与删除（管理员）、
      OSS 签名播放（本机已释放时走云端直链）；
- [ ] 移动端：375px 宽登录页/回放页/管理页布局正常、筛选可折叠。

## 8. 运维工具（deploy/ 内）

- `backfill_local_data.py`——服务器重启/维护窗口的**数据保全闭环**工具
  （snapshot 取在线快照 → 本地托底采集 → export/apply 回灌去重合并），
  配合仓库根 AGENTS.md「服务器重启的数据保全规程」使用；公共仓副本不内置
  服务器默认值，`--host` / `--key` 必须显式传；
- `restore_recording_from_oss.py`——从 OSS 回灌单场录播原片到本地
  （启用云存储后，切片/原画质播放的按需回拉）。
