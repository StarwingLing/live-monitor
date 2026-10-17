# B站直播间数据监控系统 — 技术方案文档（v0.2 实施修订）

> 状态：**历史设计文档（存档）**。M1-M6 已于 2026-09-12 全部实施并部署上线，项目现已迭代至 v0.5.1。
> v0.2 变更：补充实施过程中确认的技术细节（见 2.1 实施勘误与补充）。
> **阅读提示（2026-09-17）**：正文保留 v0.1/v0.2 规划期口径，部分决策在后续版本已变更（文中以「后变更」标注）；现行表结构、接口、配置与部署方法以 `tutorial/`（02/04/05/06 章）为准（内部版本历史未随本公共仓发布）。后续主要变更：v0.2.1 短间隔场次合并、v0.3 上舰 ×N 展示修正、v0.4 移动端适配、v0.5 登录双角色与运营功能。

## 2.1 实施勘误与补充（2026-09-12，均已实测确认）

1. **B站 HTTP API 风控**：匿名 aiohttp 默认 UA 会被 412 拦截；所有 HTTP 请求必须带浏览器 UA，并在 config 配置登录 cookie 进一步降低风控概率（`build_bili_session()` 统一构造）。
2. **POPULARITY_CHANGE 消息**：B站已把心跳人气值改为该消息推送（blivedm 1.1.7 未解析）。`capture/handler.py` 通过向 `BaseHandler._CMD_CALLBACK_DICT` 注册补丁回调支持它；blivedm 上游支持后可移除。
3. **主播昵称接口**：`live_user/v1/Master/get_info` 已要求登录（code 1000003），改用 `xlive/web-room/v1/index/getInfoByRoom`（匿名可用）。
4. **消息时间戳精度不一**：DANMU_MSG 为毫秒、礼物/SC 为秒，`_ts()` 按数量级自适应。
5. **舰长价格**：`UserToastV2Message.price` 直接是人民币元（含连续开通总额），无需固定档位价表；`source==2`（赠送）不计收入。
6. **大航海/粉丝团总数接口不可用**（需 App 级登录），页面"大航海"展示监控期内购买过舰长的去重用户数，已注明口径。
7. **新增 anchor_snapshots 表**：轮询房间 API 时顺带记录粉丝数（attention）/在线数快照，支撑详情页粉丝增长曲线与总览页粉丝增长列。
8. **异常退出恢复**：服务启动时把 end_time 为 NULL 的遗留场次补关闭（取最后一条人气快照时间），直播中被 kill 不会留下永不停的场次。
9. **前端**：Jinja2 + 原生 JS + 本地 echarts.min.js；总览页/详情页/管理页布局按 `web-example/` 参考图实现并通过截图视觉验收。
10. **同接人数无法获取（口径修正，2026-09-12 下午）**：B站已不对外提供真实同接（仅主播后台可见）。原"同接峰值"全部改口径：
    - **看过人数**（getInfoByRoom `watched_show.num`）：本场累计真实观看数，作为最接近的真实指标（首页列、今日指标卡、曲线主轴）；
    - **人气值**（get_info `online` / 心跳）：B站加权热度，曲线右轴虚线展示并注明；
    - 页面固定附口径说明。
11. **大航海真实数可用**：getInfoByRoom `guard_info.count` 为真实大航海人数，优先展示（无快照时回退"监控期购买人数"并标注）。
12. **弹幕用户隐私打码**：B站对开启隐私的用户返回打码昵称（超***）且 uid=0。给 BLiveClient 传登录 uid 会导致 WS 鉴权失败无限重连（已实测，不可用）；改用消息内 `uid_crc32`（十六进制哈希）转负整数作伪 uid，保证隐私用户在用户分层中不被并成同一人（uid=0 占比实测降为 0）。付费消息（礼物/SC/舰长）不受打码影响。
13. **新增场次详情页** `/session/{room_id}/{session_id}`：历史场次行点击进入，含六项指标、观看双轴曲线、弹幕流、礼物明细。
14. **用户分层板块加高**（190→240px）修复图例遮挡。
15. **真实同接可获取（推翻第 10 条结论，2026-09-12 午后）**：直播间页面右上角"房间观众"来自弹幕 WS 的 `ONLINE_RANK_COUNT` 消息（data.count），blivedm 将其放在"未知命令日志去重表"（logged_unknown_cmds）里静默忽略——向 `_CMD_CALLBACK_DICT` 注册同名回调即可接管（字典分发优先于忽略表，已实测）。当前所有"同接"展示均为该真实口径（实测与页面 1834 一致）。同时确认 B站新协议下心跳人气值恒为 1，曲线"人气值"改为就近取 2 分钟轮询快照的 `online`（加权值）。第 10 条的"看过人数"保留在数据库中作辅助口径。
16. **第二轮 UI 修正（2026-09-12 晚）**：① 同接曲线仅保留"房间观众"单曲线（应用户要求移除人气值线）；② `user_stats` 增加 `uname` 列（实时 UPSERT 跟随更新 + 每日重算回填），用户榜显示昵称；③ 主播昵称缺失时由采集轮询自动补全（房间直接写库加入的场景）；④ "用户分层"环形图改为"观众构成"柱状图：全场观众（窗口内看过人数峰值）/活跃用户（发弹幕人数）/核心用户（付费人数）；⑤ 弹幕流卡片 flex 布局纵向撑满 + 拉取量 50→200；⑥ 分层统计排除 uid=0 历史脏数据。弹幕用户名打码（超***）为 B站隐私策略，无法还原，仅付费消息有完整昵称。
17. **舰长价格单位修正（2026-09-12 晚，用户实测发现）**：`USER_TOAST_V2.price` 单位是**金瓜子**而非元（实测舰长首月 138000 瓜子 = ¥138），入库须 ÷1000。已修正 handler 并对历史 guard 记录做了 `value_yuan/1000` 修复 + 场次/用户画像重算。用户"贡献138000却不在观众中"的疑点即源于此：该用户真实贡献 ¥138，已正确计入核心用户与用户榜。
18. **弹幕板块折叠与搜索（2026-09-12 晚）**：最新弹幕板块固定高度约 20 条（640px，超出内部滚动）；板块顶部新增搜索栏，支持按用户名/弹幕内容前端过滤最近 200 条，实时显示命中计数。
19. **观众构成口径改为本场（2026-09-12 晚，应用户要求）**：柱状图三指标改为按"本场直播"统计——直播中取当前场次，未开播取最近一场（标题显示"本场"或"上一场#N"）。全场观众=本场看过人数峰值；活跃/核心=本场 session_id 明细去重。原近 30 天窗口口径不再使用。
20. **打码弹幕全名还原（2026-09-12 晚）**：B站对开启隐私的弹幕用户只给打码昵称+uid=0，但消息携带 `uid_crc32`（已实测 == `zlib.crc32(str(uid))` 十六进制）。利用两个未打码来源——高能榜（真实 uid+完整昵称）与礼物/SC/舰长/互动消息——建立 **`uname_map` 全局总表**（B站 uid/昵称全站唯一，跨房间共享，无需按主播维护）；writer 在写入打码弹幕时凭 `crc32(uid)` 反查还原为真实 uid+全名（实时），每日维护任务再回填历史打码行（`_unmask_history`）。高能榜两种编码：V2 为 JSON（data.list）；**V3 为 protobuf（data.pb base64，无公开 schema）**，已实现通用 wire-format 解析（单层条目 + 昵称启发式，防 URL/纯数字误判）。覆盖率取决于高能榜滚动名单：活跃观众与付费用户可还原，未上榜偶发观众保持打码（B站隐私硬边界）。实测 <房间号>：修复后新弹幕约 21% 还原为全名，映射表随榜单推送持续增长。
21. **打码根因=连接身份，已根治（2026-09-12 晚确认）**：打码与否取决于**弹幕连接的鉴权身份**——官方端以登录身份连接返回完整昵称+真实 uid；匿名连接打码（uid=0+昵称打码）。换用有效 cookie（另一账号，DedeUserID=<B站uid>，nav isLogin=true）后，blivedm 自动以登录身份初始化（以登录 uid 连接）、连接稳定无重连，**弹幕 100% 完整昵称+真实 uid**（此前恒打码的用户"<某观众昵称>"等已验证全名）。此前失败的原因链：旧 SESSDATA 失效（nav -101）→ 匿名 token 与显式传的 uid 不匹配 → WS 鉴权失败无限重连。榜单 crc 映射（第 20 条）保留作无 cookie 场景的兜底；历史打码弹幕由每日回填逐步修复（现在每个弹幕发送者都会进 uname_map，回填覆盖率将接近全量）。
22. **场次判定加固（2026-09-12 晚）**：① **重启/崩溃不拆场**——取消"启动时统一关闭遗留场次"，改为各房间首次轮询按实际情况处理：仍在直播→收养沿用原场次（日志"沿用遗留场次 #N"），确已下播→补关闭（结束时间取最后一条快照）；② **下播宽限改为宽限期内反复复核**（原为一次性等待后单次复核），宽限默认 300s→600s，主播临时调网、B站状态抖动、本机短暂波动都不会误判下播；宽限期间用 `_ending` 标记防止轮询循环重复触发。
23. **cookie 保活机制（2026-09-12 晚）**：采集服务新增保活循环（默认每 6 小时，启动时立即执行一次）：① 访问 B站首页触发 Set-Cookie 刷新短时效凭据（bili_ticket 等约 2 天有效期，aiohttp cookie jar 自动接收）；② 调 nav 检查登录态，失效时打告警日志（采集不中断，退回匿名打码态）；③ 刷新出的 cookie 持久化到 `data/cookies.json`，启动时与 config 静态 cookie 合并（文件值优先），跨重启延续登录态。SESSDATA 本身月级有效，主要失效场景是账号异地登录/风控下线，运维上以日志告警 + 更换 config cookie 重启为处置手段。
24. **主播真实头像（2026-09-12 晚）**：来源 `getInfoByRoom` 的 `anchor_info.base_info.face`（与昵称同接口一次取全）。rooms 表加 `avatar_url` 列；采集轮询自动补全缺失项；添加房间接口同步保存。前端头像以真实图片圆形裁剪展示，加载失败回退首字圆形渐变兜底。两个配套修复：① base.html 声明 `<meta name="referrer" content="no-referrer">`——hdslb.com CDN 有防盗链，带本站 Referer 会 403（实测），空 Referer 放行；② API 响应统一加 `Cache-Control: no-store`——FastAPI 默认无缓存头，浏览器启发式缓存会拿到补全前的旧数据。
25. **详情页交互增强（2026-09-12 晚）**：① 最新弹幕倒序展示（最新在最上）；② **自适应轮询**——按上次拉取间隔内新增弹幕数动态调整下次间隔（0 条→20s，约 10 条→10s，30 条→5s，≥100 条→1-2s，公式 `20/(1+new/10)` 夹在 1~20s）；③ 用户榜改为全量列出（上限 500，实测 220 人），容器固定高度折叠滚动；取消"天数"列，"礼物/弹幕"表头点击降序排序（前端本地排序）。
26. **详情页布局对齐与 SC/礼物板块（2026-09-12 晚）**：① 行内等高对齐——主播卡底部对齐同接曲线卡底部、弹幕卡底部对齐用户榜底部（grid 拉伸 + `flex: 1 1 0` 防内容撑高行）；② 用户榜改单表 + sticky 表头（原双表导致表头与内容错列）；③ 新增底部扩展行两板块（上下端对齐）：**SC 列表**（本场，行=时间+用户名+¥价格+内容，档位背景色对齐 B站：≥1000 红/≥500 橙/≥100 金/≥50 紫/≥30 蓝/其余 绿，底部 SC 营收总计）与**礼物列表**（本场非 SC 礼物按名称聚合，两列：名称×数量/价值，sticky 表头，底部礼物营收总计）。场次口径与观众构成一致（直播中=本场，未开播=最近一场）。
27. **历史场次页复用详情页界面（2026-09-12 晚）**：`/session/{room}/{sid}` 改为渲染 room.html（原独立 session.html 已删除）。dashboard API 增加 `?session_id=N` 场次模式：指标卡改"本场数据"（营收/弹幕/礼物笔数/同接峰值，无环比）、曲线为该场次同接曲线、弹幕流/观众构成/SC/礼物全部按该场次过滤、主播卡显示起止时间、副标题"场次 #N · 数据只读"、右上角"← 返回直播间"。页面标题为 **"X月X日X时直播场次详情"**（取场次开播时间）。
28. **礼物列表缩略图（2026-09-12 晚）**：gifts 表加 `gift_img` 列（来源 SEND_GIFT 的 `gift_info.img_basic`，部分礼物消息本身不带图则留空）；聚合时 `max(gift_img)` 取该名称任一非空图；前端每行礼物名前展示 42px 缩略图，无图置空占位，行高加倍（~33px→68px）。
29. **粉丝牌胶囊（2026-09-12 晚）**：最新弹幕与用户榜的用户名后展示 B站风格等高胶囊粉丝牌 `[牌名|等级]`。数据链路：DANMU_MSG/SEND_GIFT 的 medal_name/medal_level 入库（danmaku 加两列，user_stats 加两列并随实时 UPSERT 跟随最新佩戴）；前端胶囊 17px 等高、牌名渐变底+等级深色块，配色由牌名哈希固定（同牌同色）；未佩戴不显示。历史数据（加列前）无粉丝牌，新数据起生效。
30. **粉丝牌官方配色 + 舰队标识 + 曲线叠加（2026-09-12 晚）**：① 粉丝牌颜色优先用官方消息自带 `mcolor`（24-bit RGB，danmaku/user_stats 各加 medal_color 列），无值时按牌名哈希兜底。② 舰队标识：弹幕 `privilege_type`（0/1总督/2提督/3舰长）与礼物 `guard_level` 入库，用户名后显示同款胶囊（总督 #ff4d4f/提督 #c46bff/舰长 #4d8bff），user_stats 取历史最高舰队等级。**官方徽章图片 URL 不在消息中返回 → 下版本 TODO：接入徽章图片资源**。③ 同接曲线叠加"活跃用户"柱状系列：每分钟发弹幕的去重用户数，直播中按场次、未开播按近 3 小时窗口，与观众曲线按 HH:MM 对齐同轴。④ 主播卡改 flex 列布局，粉丝增长曲线（高度 200→300px，+50%）推到卡片底部。
31. **等级块段位色 + 彻底移除修复（2026-09-12 晚）**：① 粉丝牌胶囊**牌名块与等级块统一用等级段位色**（20-29 浅蓝 #4a9eff、30-39 深蓝 #1e5fd6、40-49 紫 #8b5cf6、50+ 金 #f5a623；20 以下无段位，统一用官方牌名色，无官方色按牌名哈希）；② 彻底移除房间 500 修复：`live_sessions` 等表外键引用 `rooms`，删除名单行需对该连接 `PRAGMA foreign_keys=OFF`（明细按约定保留）。

## 1. 项目定位

在阿里云 ECS（<服务器IP>，2 核 / 1.7GB 内存）上持续监控约 10 个 B 站直播间，记录：

- 开播 / 下播时间（每次开播生成一条"直播场次"记录）
- 礼物收入（含 SC 醒目留言、舰长开通，折算金额）
- 弹幕记录（明细）
- 弹幕互动与互动人员分布（活跃 / 核心用户分层）

数据通过自建 Web 页面查看，页面布局由用户后续给出指示。

## 2. 已确认的技术决策

### 2.0 context7 核对记录（2026-09-12）

已用 context7 MCP（Context7 v4.1.0）核对本项目核心技术：

- **blivedm**（`/xfgryujk/blivedm`，41 个代码示例，来源信誉 High）：BLiveClient 用法、`BaseHandler` 全部回调名与字段（`_on_danmaku` / `_on_gift` / `_on_super_chat` / `_on_user_toast_v2` / `_on_interact_word_v2` / `_on_heartbeat`）均与本文档一致。**重要修正：Web 接口无开播/下播回调，需自轮询房间 API**（详见 6.1）；开放平台有该事件但需主播身份码授权，不适用。
- **FastAPI**（`/websites/fastapi_tiangolo`）：`root_path` 用法确认，支持应用级 `FastAPI(root_path=...)` 与 uvicorn `--root-path` 两种方式。
- SQLite / ECharts 属稳定技术，无版本风险点。

| 决策项 | 结论 |
|---|---|
| 可视化 | **放弃 Grafana**，自建前端（FastAPI + Jinja2 + ECharts） |
| 房间名单 | **固定名单，但可在监控页面灵活增删**（名单存数据库 `rooms` 表，页面提供管理入口，增删后采集服务热加载，无需重启） |
| 采集库 | blivedm（xfgryujk）引入、不 fork 不修改（后变更：初定 git 依赖，服务器连不上 GitHub，改为本地打包 wheel 随 `vendor/` 分发） |
| 数据库 | SQLite，WAL 模式 |
| 服务形态 | 采集与 Web 拆分为两个 systemd 服务，共用同一个 SQLite 文件 |
| 部署 | `/opt/bili-monitor/`，端口 127.0.0.1:8001，Nginx 前缀 `/bili-monitor/`（后变更：v0.5 起 Basic Auth 移除，访问控制由应用内登录页承担） |

## 3. 技术栈与依赖

- Python 3.12（uv 管理，本工作区约定）
- 采集：`blivedm`（现为本地 wheel `vendor/blivedm-1.1.7-py3-none-any.whl`；初版曾用 git 依赖，因服务器拉取 GitHub 失败改为 wheel 分发）、`aiohttp`（blivedm 自带依赖）
- Web：`fastapi`、`uvicorn`、`jinja2`
- 前端：ECharts（本地静态文件引入，不走 CDN，避免服务器外网波动影响页面）
- 配置：`config.toml`（Python 3.11+ 内置 `tomllib` 读取，无需额外依赖）

## 4. 架构

```
┌─────────────────────────────────────────────┐
│  bili-monitor-capture.service (asyncio)     │
│  ├─ RoomManager：管理 10 个房间客户端生命周期 │
│  ├─ 每房间一个 blivedm.BLiveClient           │
│  ├─ 未开播房间：2 分钟轮询房间 API           │
│  └─ 事件回调 → 批量写 SQLite (WAL)           │
└──────────────────┬──────────────────────────┘
                   │ data/live.db (SQLite, WAL)
┌──────────────────┴──────────────────────────┐
│  bili-monitor-web.service (FastAPI/uvicorn) │
│  127.0.0.1:8001, root_path=/bili-monitor    │
│  ├─ /api/...      查询接口                   │
│  ├─ /admin/rooms  房间名单增删（写 rooms 表）│
│  └─ /             前端页面                    │
└──────────────────┬──────────────────────────┘
                   │
        Nginx :80 → /bili-monitor/ → 8001（应用内登录，v0.5 起）
```

### 4.1 为什么拆两个服务

- 采集进程崩溃重启时，Web 仍可查询历史数据；Web 重启/改版不影响采集连续性。
- SQLite WAL 模式支持"一写多读"：capture 是唯一写者（页面增删房间时 Web 也写 `rooms` 表——见 6.3 的写冲突处理）。

### 4.2 资源预算（1.7GB 内存）

| 组件 | 预估内存 |
|---|---|
| capture 进程（10 房间 WS 连接） | 100-200MB |
| web 进程（uvicorn 单 worker） | 60-100MB |
| SQLite 页缓存 | <50MB |
| 系统 + Nginx | ~300MB |
| **合计** | **约 600-700MB**，留有余量 |

写入量估算：10 房间 × 同接 200，弹幕峰值合计约 30-80 条/秒；批量 insert（每 1 秒或每 200 条 flush 一次），WAL 模式下无压力。

## 5. 数据模型（DDL 细节）

库文件：`/opt/bili-monitor/data/live.db`（本地开发为项目内 `data/live.db`）

> 下述为 v0.1 初版 DDL（历史口径）。v0.2~v0.5 陆续增列/新表：danmaku 表情列与粉丝牌列、gifts 盲盒/缩略图列、rooms 头像/描述列、新表 emotes / blind_gift_prices / uname_map / room_tags / session_titles / admin_users / app_secrets 等——现行完整表结构见 `tutorial/02-目录结构与文件作用地图.md` 与 `bili_monitor/db.py` 的 SCHEMA。

```sql
-- 监控房间名单（页面可增删）
CREATE TABLE rooms (
    room_id     INTEGER PRIMARY KEY,        -- 真实房间号（长号）
    short_id    INTEGER NOT NULL DEFAULT 0, -- 短号（无则为 0）
    anchor_name TEXT NOT NULL DEFAULT '',   -- 主播昵称（入库时查询一次填充）
    enabled     INTEGER NOT NULL DEFAULT 1, -- 0=暂停监控（保留历史）
    created_at  TEXT NOT NULL               -- ISO8601 本地时间
);

-- 直播场次（开播→下播 为一条）
CREATE TABLE live_sessions (
    session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL REFERENCES rooms(room_id),
    start_time  TEXT NOT NULL,
    end_time    TEXT,                       -- NULL=正在直播
    peak_popularity INTEGER NOT NULL DEFAULT 0,  -- 场内最高同接/人气
    danmaku_count    INTEGER NOT NULL DEFAULT 0, -- 冗余计数，加速列表页
    gift_value_yuan  REAL NOT NULL DEFAULT 0.0   -- 冗余合计（含SC/舰长）
);
CREATE INDEX idx_sessions_room ON live_sessions(room_id, start_time DESC);

-- 弹幕明细
CREATE TABLE danmaku (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(session_id),  -- NULL=非直播时段
    room_id     INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    uname       TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          TEXT NOT NULL               -- 弹幕发送时间（B站服务器时间）
);
CREATE INDEX idx_danmaku_session ON danmaku(session_id, ts);
CREATE INDEX idx_danmaku_uid ON danmaku(uid);

-- 礼物明细（普通礼物 + SC + 舰长统一记录，type 区分）
CREATE TABLE gifts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(session_id),  -- NULL=场外收入（未开播时收到）
    room_id     INTEGER NOT NULL,
    is_offline  INTEGER NOT NULL DEFAULT 0,  -- 1=非直播时段发生（场外礼物/续费）
    uid         INTEGER NOT NULL,
    uname       TEXT NOT NULL,
    type        TEXT NOT NULL,              -- gift / sc / guard
    name        TEXT NOT NULL,              -- 礼物名 / SC内容(另存text字段) / 舰长等级
    text        TEXT NOT NULL DEFAULT '',   -- SC 留言内容
    count       INTEGER NOT NULL DEFAULT 1, -- 礼物件数
    value_yuan  REAL NOT NULL,              -- 折算人民币（见 7.2）
    ts          TEXT NOT NULL
);
CREATE INDEX idx_gifts_session ON gifts(session_id);
CREATE INDEX idx_gifts_uid ON gifts(uid);

-- 其他互动（进场带粉丝牌、关注、点赞等，type 区分）
CREATE TABLE interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES live_sessions(session_id),
    room_id     INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    uname       TEXT NOT NULL,
    type        TEXT NOT NULL,              -- enter_medal / follow / like / guard_buy ...
    detail      TEXT NOT NULL DEFAULT '',   -- JSON 附加信息（粉丝牌名/等级等）
    ts          TEXT NOT NULL
);
CREATE INDEX idx_inter_session ON interactions(session_id);
CREATE INDEX idx_inter_uid ON interactions(uid);

-- 同接/人气快照（每房间每 60s 采样一次）
CREATE TABLE popularity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    session_id  INTEGER,                    -- 关联场次（未开播为 NULL）
    popularity  INTEGER NOT NULL,           -- blivedm 心跳包人气值
    ts          TEXT NOT NULL
);
CREATE INDEX idx_snap_room_ts ON popularity_snapshots(room_id, ts);

-- 用户画像（聚合表，每日任务 + 实时增量更新）
CREATE TABLE user_stats (
    uid             INTEGER NOT NULL,
    room_id         INTEGER NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    danmaku_count   INTEGER NOT NULL DEFAULT 0,
    gift_value_yuan REAL NOT NULL DEFAULT 0.0,
    interact_days   INTEGER NOT NULL DEFAULT 0,  -- 近30天有互动的自然日数
    tier            TEXT NOT NULL DEFAULT '普通' -- 普通/活跃/核心，见 7.3
);
CREATE INDEX idx_user_tier ON user_stats(room_id, tier);
```

## 6. 采集服务设计（capture）

### 6.0 信息采集范围汇总（Web 接口，无需登录）

**WS 实时推送：**

| 类别 | 消息 | 关键字段 | 入库 |
|---|---|---|---|
| 弹幕 | DANMU_MSG | uid/昵称/内容/类型(文本·表情·语音)/粉丝牌/舰队等级/时间戳 | danmaku |
| 礼物 | SEND_GIFT | 礼物名/数量/瓜子类型(silver免费·gold付费)/总瓜子数/送礼人 | gifts |
| 醒目留言 | SUPER_CHAT_MESSAGE(+删除事件) | 价格(元)/内容/时长 | gifts(sc) |
| 上舰续费 | USER_TOAST_V2(旧版GUARD_BUY) | 舰队等级/连续月份/来源(购买·赠送)/金额 | gifts(guard) |
| 互动 | INTERACT_WORD_V2 | 进入/关注/分享/特别关注/互粉/点赞 | interactions |
| 人气 | 心跳 | 人气值(加权热度) | popularity_snapshots |

**轮询房间 API 补充：** 开播/下播状态与开播时间、标题/分区、累计观看人数、粉丝数（快照，可画增长曲线）。

**拿不到（固有限制）：** 营收总额榜、完整大航海名单。舰长**自动续费不产生 WS 消息**，无法实时捕获（见 7.2 口径说明）。（初版误列于此的"真实同接人数"已被勘误 15 推翻——`ONLINE_RANK_COUNT` 可获取；"未带 cookie 字段缺失"的根因也由勘误 21 修正为连接身份问题。）

### 6.1 blivedm 事件映射

> **已通过 context7（/xfgryujk/blivedm）核对回调名与字段**。注意：**Web 接口（BLiveClient）没有开播/下播事件回调**，开播/下播只能靠轮询房间 API 检测（开放平台 OpenLiveClient 有 start/end 事件，但需要开发者密钥 + 每个主播的身份码授权，不适合监控任意 10 个房间，故不采用）。

| blivedm 回调（web 接口） | 处理 |
|---|---|
| 轮询房间 API（自实现） | 检测开播/下播，维护 `live_sessions` 生命周期 |
| `_on_danmaku` | 写 `danmaku`（字段 uid/uname/msg 均确认），更新 `user_stats` 增量 |
| `_on_gift` | 写 `gifts`(type=gift)，按 `total_coin/1000` 折算（gold 瓜子）；`coin_type=silver`（免费礼物）记 0 元 |
| `_on_super_chat` | 写 `gifts`(type=sc)，`price` 字段即人民币元（已确认） |
| `_on_user_toast_v2` | 写 `gifts`(type=guard)；`source==2` 为赠送，过滤；`guard_level` 1总督/2提督/3舰长 |
| `_on_interact_word_v2` | `msg_type` 1进入/2关注/3分享/4特别关注/5互粉/6点赞，按需写 `interactions` |
| `_on_heartbeat` | 心跳人气值，60s 采样写 `popularity_snapshots`，更新场次 peak |

- BLiveClient 用法已确认：`BLiveClient(room_id, session=aiohttp_session)` + `client.set_handler(handler)` + `start()/join()/stop_and_close()`。
- **可选 SESSDATA cookie**（config 配置项）：不带 cookie 时部分用户信息字段可能缺失，带上可获取完整用户信息；cookie 过期不影响连接，只影响字段完整度。（勘误 21 修正：打码根因是弹幕连接的鉴权身份——有效 cookie 以登录身份连接即全量完整昵称+真实 uid，失效则退回匿名打码态，并配套 6 小时保活自检。）

### 6.2 房间状态机（WS 常驻 + 轮询判状态）

> 设计要点：**WS 连接 7×24 常驻**（B站在未开播时弹幕服务器仍会推送消息，可捕获场外礼物/弹幕）；**轮询仅用于判定开播状态**，生成/关闭场次。礼物表 `session_id` 可空，未开播时收到的记为"场外收入"。

```
监控中(enabled=1)
  ├─ WS 连接：常驻，断线自动重连（指数退避，上限 60s），开播与否都保持
  └─ 状态轮询：每 120s 调房间 API 判定开播状态
       ├─ 未开播→开播：新建 live_sessions
       └─ 开播→未开播：进入宽限期反复复核（默认 600s，勘误 22；防下播马上复播拆场）
           → 仍下播则关闭 session（场外消息继续记录，session_id=NULL）
```

- 服务启动时对每个 enabled 房间先查一次直播状态再决定连接/轮询。
- **热加载**：capture 每 30s 重读 `rooms` 表，发现新增 enabled 房间即启动客户端，被删除/禁用的房间断开连接（进行中的 session 正常关闭）。

### 6.3 写入策略与写冲突

- capture 内置内存队列，事件回调只入队；单独的 writer 协程批量 `executemany` 写库，避免高频事务。
- Web 端"房间增删"也要写库（`rooms` 表 + 少量 `live_sessions` 状态查询）。SQLite WAL 下多进程偶发 `SQLITE_BUSY`，统一策略：**所有连接 `busy_timeout=5000`**；Web 端写操作频率极低（人为操作），冲突概率可忽略。

### 6.4 数据保留

- 弹幕/互动明细保留 **90 天**（可配）；`gifts`、`live_sessions`、`popularity_snapshots`（采样数据）永久保留。
- capture 内每日 04:00 清理任务（`DELETE WHERE ts < cutoff` + 定期 `PRAGMA incremental_vacuum`，库文件启用 auto_vacuum）。

## 7. 统计口径（关键细节）

### 7.1 时间

- 所有 `ts` 使用 B 站消息自带时间（毫秒时间戳转 ISO8601，北京时间）。
- 直播时长 = `end_time - start_time`；跨天直播归入开播日。

### 7.2 收入折算

- 普通礼物：`total_price/1000` 元（blivedm 的 price 单位是金瓜子；注意区分折扣前 `price` 与实付 `discount_price`，**采用实付**）。
- SC：`price` 字段即元。
- 舰长：固定档位价（开通月费记入当次；续费同样触发 USER_TOAST，会重复计入——初版接受此口径并在页面注明"含续费"）。**舰长自动续费（下播后自动扣款）不产生 WS 消息，无法实时记录**；如后续需要精确对账，可增加带登录 cookie 定期查大航海名单的比对任务（初版不做，页面口径注明）。
- 场外收入（未开播时段收到的礼物/SC，`is_offline=1`）：计入房间总收入，不计入任何直播场次；页面单独列示。

### 7.3 用户分层规则（2026-09-12 定稿，简单三级）

> 后变更（v0.5 定稿口径）：核心 = 舰长/提督/总督，或场均送礼 ≥ `core_gift_per_session_yuan`（10 元）；活跃 = 任一单场弹幕数 ≥ `active_min_danmaku`（10 条）；普通 = 其余。详见 `tutorial/06-v0.5功能：登录双角色与运营功能.md` 第 4 节。

以配置窗口（默认 30 天）内明细聚合（v0.1/v0.2 口径，已被上表取代）：

| 层级 | 条件 |
|---|
| 核心 | 有礼物贡献（付费礼物/SC/舰长，折算金额 > 0） |
| 活跃 | 无礼物贡献但发过弹幕 |
| 普通 | 其余（仅关注/分享/免费礼物等互动） |

每日 04:00 聚合任务重算全量分层；直播中实时增量只更新计数，层级次日刷新。
注意：普通礼物消息同样受隐私打码（uid=0，SEND_GIFT 与 V2 pb 均无可识别字段），
打码送礼者不进个人画像（避免并成假用户），其金额计入营收/核心用户数统计
（核心人数对打码送礼做 +1 近似）。

### 7.4 同接/人气说明

blivedm 心跳的人气值是 B 站加权后的"热度"，并非精确同接。若需真实同接，可在页面标注为"人气值"；后续如需精确在线人数需另接 B 站其他接口（初版不做）。

## 8. Web 服务设计

- FastAPI，`root_path="/bili-monitor"`（用法已通过 context7 核对：`FastAPI(root_path="/bili-monitor")` 或 uvicorn `--root-path /bili-monitor`），uvicorn 单 worker，`127.0.0.1:8001`。
- 静态资源（ECharts、CSS、JS）由 FastAPI `StaticFiles` 提供。

### 8.1 API 列表（初版）

> 初版规划清单（历史口径）。现行接口已大幅扩充——登录/账户（/api/auth、/api/admins）、描述/标签、数据分析、盲盒映射等，且全站由登录中间件保护（未登录 401/302、游客只读）；完整清单见 `tutorial/04-Web界面与API.md`。

| 路由 | 说明 |
|---|---|
| `GET /api/rooms` | 房间列表 + 各房间当前状态（直播中/轮询中）、当前场次摘要 |
| `POST /api/rooms` | 添加房间 `{room_id}`（自动查主播名，去重校验） |
| `DELETE /api/rooms/{room_id}` | 移除监控（历史数据保留，房间标记 enabled=0 或删除行，见 8.2） |
| `GET /api/rooms/{room_id}/sessions` | 场次列表（分页，含时长/弹幕数/收入） |
| `GET /api/rooms/{room_id}/sessions/{sid}` | 场次详情：同接曲线、收入明细、弹幕列表 |
| `GET /api/rooms/{room_id}/users` | 用户分布：分层统计 + 用户榜（弹幕/金额/天数排序） |
| `GET /api/overview` | 总览：近 7/30 天各房间开播时长、收入、弹幕量对比 |

写操作（增删房间）建议单独加一层简单口令（页面输入，存 config），Basic Auth 只做外层门禁。（后变更：v0.1 即以 `X-Admin-Token` 口令实现；v0.5 起升级为管理员/游客双角色登录页 + 签名 Cookie，Basic Auth 移除，口令通道保留兼容。）

### 8.2 移除房间的语义

- 默认"暂停监控"：`enabled=0`，历史数据完整可查，可随时恢复。
- 提供"彻底移除"二级确认操作：删 `rooms` 行（明细数据保留，仅从页面隐藏；不物理删明细，避免误删）。

### 8.3 前端

- 等待用户给出**布局指示**后实现；技术形式为 Jinja2 模板 + 原生 JS + ECharts。
- 页面骨架预留：总览页 / 房间详情页（场次列表）/ 场次详情页 / 用户分布页 / 房间管理页。（实际实现：用户分布并入详情页、无独立页面；后续版本另增盲盒价值页（v0.2 增、v0.5 删）、登录页与数据分析页（v0.5）。）

## 9. 配置文件 config.toml

> v0.2 时期示例（历史口径）。现行键值见 `config.example.toml`：`end_grace_sec` 默认 600，[capture] 新增 `session_merge_gap_sec` / `cookie_keepalive_hours` / `emoticon_refresh_hours`，[tier] 改为 `window_days` / `core_gift_per_session_yuan` / `active_min_danmaku`。

```toml
[server]
listen_host = "127.0.0.1"
listen_port = 8001

[capture]
poll_interval_sec = 120        # 未开播轮询间隔
snapshot_interval_sec = 60     # 人气采样间隔
reconnect_max_wait = 60        # 断线重连退避上限
end_grace_sec = 300            # 下播宽限
flush_interval_sec = 1         # 批量写库间隔
admin_password = ""            # 房间增删口令

[retention]
danmaku_days = 90
interaction_days = 90

[tier]                         # 用户分层阈值
core_gift_yuan = 50
core_days = 10
active_danmaku = 10
active_days = 3
window_days = 30
```

## 10. 部署清单

1. 同步：`tar czf - --exclude .venv --exclude __pycache__ --exclude data . | ssh -i <密钥> root@<服务器IP> "mkdir -p /opt/bili-monitor && tar xzf - -C /opt/bili-monitor"`（后变更：v0.2 起同步固定排除 `data/` 与 `config.toml`）
2. 服务器 `uv sync`（后变更：blivedm 已改为本地 wheel 随 `vendor/` 分发，无需访问 GitHub）
3. `deploy/bili-monitor-capture.service`、`deploy/bili-monitor-web.service` → `/etc/systemd/system/`，`systemctl enable --now`
4. Nginx：location 并入 `/etc/nginx/conf.d/00-default.conf` 的 default_server（IP 访问 Host 不匹配独立 server 块，实测踩坑），`nginx -t && systemctl reload nginx`（后变更：v0.5 起 Basic Auth 移除）
5. 在你自己的运维清单登记端口 8001（本仓端口约定见根 README）
6. 验收四项：systemctl status × 2、`ss -tlnp | grep 8001`、`nginx -t`、本地访问 `http://<服务器IP>/bili-monitor/`（已于 2026-09-12 执行通过；现行验收口径含登录检查，见 tutorial/05 第 6 节）

## 11. 实施步骤与里程碑

| # | 内容 | 验证 |
|---|---|---|
| M1 | 项目初始化 + 单房间采集入库（弹幕/礼物/开播下播） | 本地连真实房间跑通，sqlite 查询有数据 |
| M2 | 多房间管理、状态机、热加载、快照采样 | 同时监控 2+ 房间过夜稳定 |
| M3 | 聚合任务 + 用户分层 + 保留策略 | 跑聚合脚本，分层结果合理 |
| M4 | FastAPI 全部接口 | curl 逐个验证 |
| M5 | 前端页面（需用户布局指示） | 本地浏览器验收 |
| M6 | 服务器部署 + Basic Auth + tutorial 文档 | 已完成（2026-09-12 部署验收通过） |

## 12. 风险与备注

- **blivedm 依赖 B 站私有协议**，接口变动可能导致采集失效——以库形式引入、不魔改，便于跟进上游升级。
- **未登录身份**采集：部分消息（如 SC 删除、部分礼物折扣信息）可能拿不到或字段不同，初版接受。
- **服务器访问 GitHub**：拉 git 依赖可能受限，M1 时实测，不通则改 vendor 方案。（已实测不通，现状即 vendor wheel 方案。）
- 弹幕量大时 `danmaku` 表 90 天约 10-40GB？——按 80 条/秒峰值、平均远低于此估算，90 天明细约 1-3GB，磁盘充足（确认服务器盘容量后如有压力再下调保留期）。
- 本项目所有时间均为北京时间；SQLite 存 ISO8601 文本，前端直接展示。
