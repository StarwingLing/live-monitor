# live-qq-notify 导览

> B 站直播间开播状态 → QQ 群通知服务(v0.1.0)
> 由 NapCatQQ(OneBot 11)承担 QQ 协议端,bili-monitor 页面承担全部配置管理。

## 项目一句话

独立轮询器:每 15 秒查 B 站公开接口 `get_info`(小写!驼峰已下线),对开启通知的房间检测
开播/下播翻转,经 NapCat 的 OneBot HTTP 接口发群消息;全部业务配置(开关/群映射/文案/
账号)存共享 live.db,由 bili-monitor 总览页「通知」列与管理页「开播账号管理」管理,
60 秒内热生效,无需重启任何服务。

## 学习路线(按序阅读)

1. [01-构建思路.md](01-构建思路.md) —— 需求从哪来、为什么选 NapCat、架构与关键决策
2. [02-目录结构与文件作用地图.md](02-目录结构与文件作用地图.md) —— 每个文件干什么、表结构
3. [03-部署与运维.md](03-部署与运维.md) —— 服务器部署全流程(NapCat 安装/踩坑)、保活体系、排障速查

## 快速上手(本地联调)

```bash
uv run python tests/make_test_db.py     # 生成本地测试库 data/test_live.db
uv run python tests/mock_onebot.py      # 双监听 mock(OneBot 3000 + WebUI 6099)
uv run python -m live_qq_notify.main    # 通知器(config.toml 指向测试库)
uv run python tests/test_state_machine.py && uv run python tests/test_store.py  # 单测
```

## 关联项目

- `bili-monitor`(v2.3.0):配置 UI(总览「通知」列/管理「开播账号管理」+ 扫码登录嵌入)
  与本项目共享 live.db 的 `qq_notify`/`qq_notify_global` 两表(DDL 属主在本项目,锁步维护);
- `replay`:共享库与采集属主(本项目只读 `rooms` 表,不涉其表)。
