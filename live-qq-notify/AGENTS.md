# live-qq-notify 项目专属约定(子代理说明)

> 只放本项目专属规则与状态;跨项目通用规则见仓库根 [AGENTS.md](../AGENTS.md)。
> 凡涉及本项目的对话:先读仓库根文件,再读本文件。
> 功能与结构详解见 `tutorial/`。

## 项目形态(v0.2.0)

B 站开播状态 → QQ 群通知:独立轮询器(15s,`/opt/live-qq-notify`)+ **双发送通道**
+ bili-monitor v2.3.1 配置 UI。配置全在共享库(`qq_notify`/`qq_notify_global`
/**`qq_group_map` 群号→openid 映射表**,DDL 属主本项目,bili db.py 锁步——改表结构
须同版同步两侧):

- **official(当前生产通道)**:QQ 官方机器人 OpenAPI(AppID/Secret 换 AccessToken),
  无风控踢线;房间配置统一填 QQ 群号,发送前经映射表换 openid(未绑定群 WARN 跳过);
  封面图为文本后独立一条图片消息;凭据与通道开关在 qq_notify_global(channel 列)。
- **napcat(备用)**:协议端 `/root/Napcat` rootless 扫码,被腾讯风控反复踢线
  (10-15/16 事故,见版本笔记 v0.1.0),页面切回即用;群映射获取绑定会话/指令自动回复
  /事件中枢等 v0.2.0 新功能仅 official 可用。

## 关键红线

- **B 站接口必须小写 `room/v1/Room/get_info`**(驼峰已下线,HTTP 200 + code 1000003
  会被静默当未播——2026-10-14 整晚漏发事故);`code!=0` 必须按失败处理,不许静默;
- **错过的通知一律不补发**(宁漏发不重发,用户定稿);翻转瞬间掉线的缓解是
  当场等待重连 ≤60s(`_wait_bot`),不是补发队列;
- 通知器与 NapCat 的 WebUI/OneBot 端口(6099/3000)**只监听 127.0.0.1**,不经 Nginx;
- 通知运行状态用 `journalctl -u live-qq-notify`(WARN=接口失败,ERROR=掉线放弃,
  INFO=已发群)。

## 服务器运维要点(详见 tutorial/03)

- **版本钉死(2026-10-16 降级):NapCat 4.18.33 + QQ NT 3.2.32-52194,禁跑 `napcat update`**
  (官方安装器只装最新版,而最新版会被腾讯风控频繁踢线——issue #1728,10-15 曾一天被踢
  3 次、最短存活 30 分钟;降一档但别降太老,过老版本会被禁止登录)。回滚备份在服务器
  `/root/Napcat/opt/QQ.bak-4.18.38-53644/`,降级备份(qq deb/napcat zip/配置)在
  `/root/napcat-downgrade-backup/`;
- **napcat systemd unit 必须带 HOME/XDG_RUNTIME_DIR 等环境变量**,否则 QQ NT 反复
  Illegal instruction 崩溃(systemd-run --scope 对照实验定位过);
- 密码自动重登在 drop-in `/etc/systemd/system/napcat.service.d/quick-login.conf`
  (含明文密码,权限 600,不入任何 git);
- 服务器重启后检查单:tutorial/03 第五节(双服务存活 / 3000、6099 回环 / QQ 是否
  自动重登成功——本服务**无采集数据**,不涉 replay 的重启保全规程,但 QQ 掉线需关注);
- 通知器部署同步排除 `config.toml` 与 `data/`(state.json 是运行态,别覆盖)。

## 关联项目

- `bili-monitor`:通知 UI 与两表锁步(其 AGENTS 有对应提示);
- `replay`:共享库与 rooms 表属主(本项目只读,改 rooms 结构须过 replay)。
