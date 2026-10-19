# -*- coding: utf-8 -*-
"""共享库访问层:通知两表的 DDL 属主 + 每轮配置读取。

表结构(与 bili-monitor/db.py **锁步维护**——本模块是属主,bili 侧为让
管理页在通知器未启动时也能先建库配置而持有同款幂等 DDL;改结构须同版
同步两侧):
- qq_notify         房间级:开关 + 目标群 + 房间专属文案(总览页写)
- qq_notify_global  全局级:账号预留 / NapCat 连接 / 总开关与默认文案
                    (管理页「开播账号管理」写),固定单行(id=1)
"""
import json
import sqlite3
import time

# ---------------------------------------------------------------------------
# DDL(属主在此;bili_monitor/db.py 锁步副本)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_notify (
    room_id      INTEGER PRIMARY KEY,       -- 对应 rooms.room_id(监控中房间)
    enabled      INTEGER NOT NULL DEFAULT 0,     -- 0=关 1=开(总览页「机器人」开关列)
    group_ids    TEXT    NOT NULL DEFAULT '[]',  -- 目标群号 JSON 数组,如 '[1,2]'
                                                   -- (两通道统一填 QQ 群号;official
                                                   -- 发送前经 qq_group_map 换 openid)
    template_on  TEXT    NOT NULL DEFAULT '',    -- 房间专属开播文案(空=用全局)
    template_off TEXT    NOT NULL DEFAULT '',    -- 房间专属下播文案(空=用全局)
    -- v0.3.0 房间级个人配置(机器人管理页主播卡片):NULL=跟随全局,
    -- 0/1=本房间覆盖开关;防抖 NULL=跟随全局,数值=覆盖秒数
    notify_on        INTEGER,
    notify_off       INTEGER,
    report_offline   INTEGER,
    debounce_offline INTEGER,
    -- v0.4.0 多机器人 + 入群核查:房间归属机器人(NULL=默认机器人1);
    -- join_check=入群申请核查开关(bili 事件中枢消费,GROUP_JOIN_REQUEST
    -- 时查该房间粉丝牌/弹幕发通知;本进程不读,列归本表锁步)
    bot_id        INTEGER,
    join_check    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS qq_notify_global (
    id               INTEGER PRIMARY KEY CHECK (id = 1),  -- 固定单行
    qq_account       TEXT    NOT NULL DEFAULT '',  -- 预留:通知小号 QQ 号(用于
                                                   -- 校验 NapCat 实际登录号一致)
    onebot_url       TEXT    NOT NULL DEFAULT 'http://127.0.0.1:3000',
    onebot_token     TEXT    NOT NULL DEFAULT '',  -- NapCat HTTP 服务鉴权 token
    webui_url        TEXT    NOT NULL DEFAULT 'http://127.0.0.1:6099',  -- WebUI(扫码登录用)
    webui_token      TEXT    NOT NULL DEFAULT '',  -- WebUI 登录 token(webui.json 的 token)
    channel          TEXT    NOT NULL DEFAULT 'napcat',  -- 发送通道:'napcat'|'official'
                                                   -- (2026-10-17 v0.2.0;手动切换,不自动降级)
    official_app_id  TEXT    NOT NULL DEFAULT '',  -- 官方机器人 AppID(q.qq.com 管理端)
    official_app_secret TEXT NOT NULL DEFAULT '',  -- 官方机器人 AppSecret( AccessToken 换取)
    command_replies  TEXT    NOT NULL DEFAULT '{}', -- 群指令自动回复表 JSON dict
                                                   -- (v0.2.1;bili 管理页编辑,
                                                   -- 本进程不消费,列归本表锁步)
    -- v0.3.2 AI 智能回复(bili 事件中枢消费:指令未命中时 LLM 兜底;本进程
    -- 不读,列归本表锁步)。空串=内置默认:base=智谱标准 API、model=glm-4.5-flash
    ai_reply_enabled INTEGER NOT NULL DEFAULT 0,    -- 0=关 1=开
    ai_api_key       TEXT    NOT NULL DEFAULT '',   -- LLM API Key(Bearer 鉴权)
    ai_base_url      TEXT    NOT NULL DEFAULT '',   -- OpenAI 兼容基址(空=默认)
    ai_model         TEXT    NOT NULL DEFAULT '',   -- 模型名(空=默认)
    ai_system_prompt TEXT    NOT NULL DEFAULT '',   -- 人设提示词(空=内置默认)
    -- v2.5.0 AI 频控(页面可调;库值为准,空/脏时消费方回退代码默认 3 次/300s)
    ai_rate_limit     INTEGER NOT NULL DEFAULT 3,    -- 每群每窗口最多次数
    ai_rate_window    INTEGER NOT NULL DEFAULT 300,  -- 频控窗口(秒)
    notify_on        INTEGER NOT NULL DEFAULT 1,   -- 全局开播通知(默认开)
    notify_off       INTEGER NOT NULL DEFAULT 1,   -- 全局下播通知(默认开)
    report_offline   INTEGER NOT NULL DEFAULT 0,   -- 下播报告图(v0.3.0;下播文本
                                                   -- 后紧随发本场直播报告图,默认关)
    notify_cover     INTEGER NOT NULL DEFAULT 1,   -- 【停用 2026-10-14】封面改纯
                                                   -- 模板变量 {cover},列仅兼容保留
    template_on      TEXT    NOT NULL DEFAULT '📺 {uname} 开播了:{title}\n{url}\n{cover}',
    template_off     TEXT    NOT NULL DEFAULT '🌙 {uname} 下播了\n⏱ 本次直播:{duration}',
    debounce_offline INTEGER NOT NULL DEFAULT 300, -- 开播后 N 秒内的下播不通知
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO qq_notify_global (id) VALUES (1);

-- 群号→openid 映射(v0.2.0 官方通道):openid 是"机器人×群"维度标识,官方
-- 接口不提供群号反查,只能从机器人事件(GROUP_ADD_ROBOT/@消息)捕获——
-- bili 管理页「群映射获取」发起绑定会话写入本表;本进程发送前查表转换。
-- v0.4.0 复合主键 (bot_id, group_id):同一群在不同机器人下 openid 不同,
-- 允许并存;存量表无 bot_id 列,由 ensure_tables 里 _rebuild_group_map 迁移。
CREATE TABLE IF NOT EXISTS qq_group_map (
    bot_id       INTEGER NOT NULL DEFAULT 1,  -- 归属机器人(v0.4.0 多机器人)
    group_id     INTEGER NOT NULL,            -- QQ 群号(绑定会话锚定的用户输入)
    group_openid TEXT NOT NULL,               -- 官方通道群标识(32 位十六进制串)
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    remark       TEXT NOT NULL DEFAULT '',    -- 备注(v0.3.1 bili 管理页行内编辑;本进程不读,仅 DDL 锁步属主)
    PRIMARY KEY (bot_id, group_id)
);

-- 机器人档案(v0.4.0 多机器人):每行一个机器人,承载原 qq_notify_global 的
-- 全部配置字段 + 显示名;通知器按房间 qq_notify.bot_id 分组发送,bili 事件
-- 中枢为每个 official 机器人各持一条 WebSocket。迁移把 qq_notify_global
-- 现值种子为机器人 1;qq_notify_global 保留冻结(回滚保险,不再读写)。
CREATE TABLE IF NOT EXISTS qq_bots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL DEFAULT '',   -- 显示名(空=「机器人 N」)
    channel          TEXT    NOT NULL DEFAULT 'napcat',  -- 'napcat'|'official'
    qq_account       TEXT    NOT NULL DEFAULT '',   -- 预留:NapCat 登录号校验
    onebot_url       TEXT    NOT NULL DEFAULT 'http://127.0.0.1:3000',
    onebot_token     TEXT    NOT NULL DEFAULT '',
    webui_url        TEXT    NOT NULL DEFAULT 'http://127.0.0.1:6099',
    webui_token      TEXT    NOT NULL DEFAULT '',
    official_app_id  TEXT    NOT NULL DEFAULT '',
    official_app_secret TEXT NOT NULL DEFAULT '',
    command_replies  TEXT    NOT NULL DEFAULT '{}', -- 群指令表(bili 编辑,事件中枢消费)
    notify_on        INTEGER NOT NULL DEFAULT 1,
    notify_off       INTEGER NOT NULL DEFAULT 1,
    report_offline   INTEGER NOT NULL DEFAULT 0,
    template_on      TEXT    NOT NULL DEFAULT '📺 {uname} 开播了:{title}\n{url}\n{cover}',
    template_off     TEXT    NOT NULL DEFAULT '🌙 {uname} 下播了\n⏱ 本次直播:{duration}',
    debounce_offline INTEGER NOT NULL DEFAULT 300,
    -- AI 智能回复七列(同 v0.3.2 口径,页面可调)
    ai_reply_enabled INTEGER NOT NULL DEFAULT 0,
    ai_api_key       TEXT    NOT NULL DEFAULT '',
    ai_base_url      TEXT    NOT NULL DEFAULT '',
    ai_model         TEXT    NOT NULL DEFAULT '',
    ai_system_prompt TEXT    NOT NULL DEFAULT '',
    ai_rate_limit    INTEGER NOT NULL DEFAULT 3,
    ai_rate_window   INTEGER NOT NULL DEFAULT 300,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# 旧表补列迁移(2026-10-13 扫码登录嵌入:webui_url/webui_token;2026-10-14 封面:notify_cover;
# 2026-10-17 v0.2.0 官方通道:channel/official_app_id/official_app_secret)。
# CREATE TABLE IF NOT EXISTS 不会给已存在的表加列,须显式 ALTER;
# 列已存在时 OperationalError 忽略(与 bili db.py _MIGRATIONS 同款幂等风格)。
_MIGRATIONS = [
    "ALTER TABLE qq_notify_global ADD COLUMN webui_url TEXT NOT NULL "
    "DEFAULT 'http://127.0.0.1:6099'",
    "ALTER TABLE qq_notify_global ADD COLUMN webui_token TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN notify_cover INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE qq_notify_global ADD COLUMN channel TEXT NOT NULL DEFAULT 'napcat'",
    "ALTER TABLE qq_notify_global ADD COLUMN official_app_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN official_app_secret TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN command_replies TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE qq_notify_global ADD COLUMN report_offline INTEGER NOT NULL DEFAULT 0",
    # v0.3.0 房间级个人配置四列(NULL=跟随全局;无默认值,旧行自动为 NULL)
    "ALTER TABLE qq_notify ADD COLUMN notify_on INTEGER",
    "ALTER TABLE qq_notify ADD COLUMN notify_offline INTEGER",
    "ALTER TABLE qq_notify ADD COLUMN report_offline INTEGER",
    "ALTER TABLE qq_notify ADD COLUMN debounce_offline INTEGER",
    # v0.3.1 群备注(属主变更在此侧:本进程不读该列,锁步保证两侧同形;
    # bili 管理页群映射行内编辑/指令群下拉显示,重新绑定的 upsert 不触碰)
    "ALTER TABLE qq_group_map ADD COLUMN remark TEXT NOT NULL DEFAULT ''",
    # v0.3.2 AI 智能回复五列(bili 事件中枢消费:指令未命中 LLM 兜底;本进程
    # 不读——_GLOBAL_FIELDS 白名单不含,列在此仅为 DDL 属主锁步)
    "ALTER TABLE qq_notify_global ADD COLUMN ai_reply_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE qq_notify_global ADD COLUMN ai_api_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN ai_base_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN ai_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qq_notify_global ADD COLUMN ai_system_prompt TEXT NOT NULL DEFAULT ''",
    # v2.5.0 AI 频控两列(页面可调;2026-10-18 用户定稿从代码常量升级为配置项)
    "ALTER TABLE qq_notify_global ADD COLUMN ai_rate_limit INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE qq_notify_global ADD COLUMN ai_rate_window INTEGER NOT NULL DEFAULT 300",
    # v0.4.0 多机器人 + 入群核查(qq_group_map 的 bot_id 由表重建迁移,
    # 不在此 ALTER——旧表主键 group_id 须换复合键,见 _rebuild_group_map)
    "ALTER TABLE qq_notify ADD COLUMN bot_id INTEGER",
    "ALTER TABLE qq_notify ADD COLUMN join_check INTEGER NOT NULL DEFAULT 0",
]

# qq_bots 读出后的字段白名单(load_bots 返回形状,通知器按此取用)。
_BOT_FIELDS = ("id", "name", "channel", "qq_account", "onebot_url",
               "onebot_token", "webui_url", "webui_token", "official_app_id",
               "official_app_secret", "notify_on", "notify_off",
               "report_offline", "template_on", "template_off",
               "debounce_offline")


def _rebuild_group_map(conn: sqlite3.Connection) -> None:
    """旧版 qq_group_map(单列主键 group_id)→ v0.4.0 复合主键 (bot_id, group_id)。

    SQLite 不能 ALTER 主键,走 建新表→搬数据(存量行归机器人1)→换名;
    已是新结构的表(有 bot_id 列)直接跳过;表不存在时由 SCHEMA 负责创建。
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(qq_group_map)")]
    except sqlite3.OperationalError:
        # 中途失败自愈:qq_group_map 已 DROP 而 v04 新表还在 → 补完换名
        # (executescript 逐句提交,重试入口可能落在换名前的中间态)
        try:
            conn.execute("ALTER TABLE qq_group_map_v04 RENAME TO qq_group_map")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        return
    if not cols or "bot_id" in cols:
        return
    conn.executescript("""
        CREATE TABLE qq_group_map_v04 (
            bot_id       INTEGER NOT NULL DEFAULT 1,
            group_id     INTEGER NOT NULL,
            group_openid TEXT NOT NULL,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            remark       TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (bot_id, group_id));
        INSERT INTO qq_group_map_v04 (group_id, group_openid, updated_at, remark)
            SELECT group_id, group_openid, updated_at, remark FROM qq_group_map;
        DROP TABLE qq_group_map;
        ALTER TABLE qq_group_map_v04 RENAME TO qq_group_map;
    """)
    conn.commit()


def _seed_bots(conn: sqlite3.Connection) -> None:
    """qq_bots 空表时,把 qq_notify_global 现值种子为机器人 1(幂等)。

    v0.4.0 起 qq_notify_global 冻结不再读写(保留表作回滚保险);迁移后
    机器人 1 即原全局配置,行为无缝延续。
    """
    if conn.execute("SELECT 1 FROM qq_bots LIMIT 1").fetchone():
        return
    conn.execute("""
        INSERT INTO qq_bots (id, name, channel, qq_account, onebot_url,
            onebot_token, webui_url, webui_token, official_app_id,
            official_app_secret, command_replies, notify_on, notify_off,
            report_offline, template_on, template_off, debounce_offline,
            ai_reply_enabled, ai_api_key, ai_base_url, ai_model,
            ai_system_prompt, ai_rate_limit, ai_rate_window)
        SELECT 1, '默认机器人', COALESCE(channel,'napcat'), COALESCE(qq_account,''),
            COALESCE(onebot_url,'http://127.0.0.1:3000'), COALESCE(onebot_token,''),
            COALESCE(webui_url,'http://127.0.0.1:6099'), COALESCE(webui_token,''),
            COALESCE(official_app_id,''), COALESCE(official_app_secret,''),
            COALESCE(command_replies,'{}'), COALESCE(notify_on,1),
            COALESCE(notify_off,1), COALESCE(report_offline,0),
            COALESCE(template_on,''), COALESCE(template_off,''),
            COALESCE(debounce_offline,300),
            COALESCE(ai_reply_enabled,0), COALESCE(ai_api_key,''),
            COALESCE(ai_base_url,''), COALESCE(ai_model,''),
            COALESCE(ai_system_prompt,''), COALESCE(ai_rate_limit,3),
            COALESCE(ai_rate_window,300)
        FROM qq_notify_global WHERE id = 1
    """)
    conn.commit()

# 全局配置读出后的字段白名单(顺序即日志/文档展示口径)。
# notify_cover 已停用(2026-10-14:封面/时长均为纯模板变量,不再读该开关)
_GLOBAL_FIELDS = ("qq_account", "onebot_url", "onebot_token", "webui_url",
                  "webui_token", "channel", "official_app_id",
                  "official_app_secret", "command_replies", "notify_on",
                  "notify_off", "report_offline", "template_on",
                  "template_off", "debounce_offline")


def connect(path: str) -> sqlite3.Connection:
    """连共享库(只读写通知两表;WAL + busy_timeout 与既有共库进程一致)。"""
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _with_lock_retry(fn, conn: sqlite3.Connection,
                     tries: int = 10, gap: float = 3.0) -> None:
    """DDL/写迁移退避重试(2026-10-18 服务器部署实测)。

    共享库由 replay-capture 每秒高频写,SCHEMA executescript / 表重建 /
    种子 INSERT 偶发撞写锁(实测一次撞锁 8s+ 后恢复瞬时)——busy_timeout
    只挡得住短事务;长占用下退避重试比一次性失败退出(触发 systemd
    重启循环)稳。仅对 "locked" 重试,其他错误原样抛出。
    """
    last: sqlite3.OperationalError | None = None
    for i in range(tries):
        try:
            fn()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            last = e
            time.sleep(gap)
    raise last  # type: ignore[misc]


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建表(幂等)+ 旧表补列 + v0.4.0 迁移(群映射表重建/机器人种子)。

    本进程首启调用;bili 侧锁步副本亦幂等。DDL 与迁移整体包写锁重试
    (capture 高频写同库,偶发长写事务下退避重试,见 _with_lock_retry)。
    """
    _with_lock_retry(lambda: conn.executescript(SCHEMA), conn)
    for ddl in _MIGRATIONS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 列已存在
    _with_lock_retry(lambda: _rebuild_group_map(conn), conn)
    _with_lock_retry(lambda: _seed_bots(conn), conn)
    conn.commit()


def load_global(conn: sqlite3.Connection) -> dict:
    """【v0.4.0 起停用】读旧全局单行(冻结表,仅供迁移前测试/回滚核对)。

    生效配置一律走 load_bots(qq_bots 表,机器人 1 由迁移种子承接本表旧值)。
    """
    row = conn.execute("SELECT * FROM qq_notify_global WHERE id = 1").fetchone()
    if row is None:
        return {"qq_account": "", "onebot_url": "http://127.0.0.1:3000",
                "onebot_token": "", "webui_url": "http://127.0.0.1:6099",
                "webui_token": "", "channel": "napcat",
                "official_app_id": "", "official_app_secret": "",
                "command_replies": "{}",
                "notify_on": 1, "notify_off": 1, "report_offline": 0,
                "template_on": "📺 {uname} 开播了:{title}\n{url}\n{cover}",
                "template_off": "🌙 {uname} 下播了\n⏱ 本次直播:{duration}",
                "debounce_offline": 300}
    d = {k: row[k] for k in _GLOBAL_FIELDS}
    d["notify_on"] = bool(d["notify_on"])
    d["notify_off"] = bool(d["notify_off"])
    d["report_offline"] = bool(d["report_offline"])
    return d


def load_bots(conn: sqlite3.Connection) -> list[dict]:
    """读全部机器人档案(v0.4.0;按 id 升序,机器人 1 = 迁移种子的原全局配置)。

    返回 _BOT_FIELDS 白名单字段;notify 开关布尔化;凭据原样字符串。
    command_replies/AI 七列通知器不消费(bili 事件中枢读),不在此带出。
    """
    rows = conn.execute("SELECT * FROM qq_bots ORDER BY id").fetchall()
    out: list[dict] = []
    for row in rows:
        d = {k: row[k] for k in _BOT_FIELDS}
        d["notify_on"] = bool(d["notify_on"])
        d["notify_off"] = bool(d["notify_off"])
        d["report_offline"] = bool(d["report_offline"])
        out.append(d)
    return out


def load_room_configs(conn: sqlite3.Connection) -> dict[int, dict]:
    """读启用房间的通知配置(轮询范围 = 开且在监控列表内的房间)。

    返回 {room_id: {groups, template_on, template_off, 个人配置四键, bot_id}}:
    - JOIN rooms 过滤 enabled=1:房间被暂停/移出监控即停止通知(单一事实来源);
    - group_ids 非法 JSON 的行记 WARNING 后跳过(不让一条脏配置拖垮整轮);
      两通道统一填 QQ 群号——official 发送前经 load_group_map 换 openid;
    - 个人配置四键(notify_on/notify_off/report_offline/debounce_offline):
      None=跟随全局(库中 NULL),否则为本房间覆盖值;生效合并见
      decide.room_effective;
    - bot_id(v0.4.0):房间归属机器人,NULL=默认机器人 1(主循环按此分组,
      official 凭据/群映射随对应机器人)。
    """
    out: dict[int, dict] = {}
    rows = conn.execute("""
        SELECT n.room_id, n.group_ids, n.template_on, n.template_off,
               n.notify_on, n.notify_off, n.report_offline, n.debounce_offline,
               n.bot_id, r.anchor_name
        FROM qq_notify n JOIN rooms r ON r.room_id = n.room_id
        WHERE n.enabled = 1 AND r.enabled = 1
    """).fetchall()
    for row in rows:
        try:
            groups = [int(g) for g in json.loads(row["group_ids"])]
        except (ValueError, TypeError):
            print(f"[WARN] 房间 {row['room_id']} group_ids 非法(JSON 解析失败),本轮跳过:"
                  f" {row['group_ids']!r}")
            continue
        out[row["room_id"]] = {
            "groups": groups,
            "template_on": row["template_on"] or "",
            "template_off": row["template_off"] or "",
            "notify_on": _opt_bool(row["notify_on"]),
            "notify_off": _opt_bool(row["notify_off"]),
            "report_offline": _opt_bool(row["report_offline"]),
            "debounce_offline": (int(row["debounce_offline"])
                                 if row["debounce_offline"] is not None else None),
            # 房间归属机器人(NULL=默认机器人 1)
            "bot_id": int(row["bot_id"]) if row["bot_id"] is not None else 1,
            # 通知文案的主播名优先用库值(get_info 的
            # uname 在播时为空,见 bilibili.parse_info)
            "anchor_name": row["anchor_name"] or ""}
    return out


def _opt_bool(v) -> bool | None:
    """库中 INTEGER 列 → 三态 bool(NULL=None=跟随全局)。"""
    return None if v is None else bool(v)


def load_group_map(conn: sqlite3.Connection, bot_id: int = 1) -> dict[int, str]:
    """读某机器人的群号→openid 映射(official 通道发送前换算用)。

    v0.4.0 按 bot_id 过滤(openid 是机器人×群维度);空表/无该机器人行
    返回 {};绑定由 bili 管理页「群映射获取」写入。
    """
    try:
        rows = conn.execute(
            "SELECT group_id, group_openid FROM qq_group_map WHERE bot_id = ?",
            (int(bot_id),)).fetchall()
    except sqlite3.OperationalError:
        return {}   # 表未建(极旧库未跑 ensure_tables 的防御分支)
    return {int(r["group_id"]): str(r["group_openid"]) for r in rows}
