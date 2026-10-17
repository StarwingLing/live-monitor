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

# ---------------------------------------------------------------------------
# DDL(属主在此;bili_monitor/db.py 锁步副本)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS qq_notify (
    room_id      INTEGER PRIMARY KEY,       -- 对应 rooms.room_id(监控中房间)
    enabled      INTEGER NOT NULL DEFAULT 0,     -- 0=关 1=开(总览页开关列)
    group_ids    TEXT    NOT NULL DEFAULT '[]',  -- 目标群号 JSON 数组,如 '[1,2]'
                                                   -- (两通道统一填 QQ 群号;official
                                                   -- 发送前经 qq_group_map 换 openid)
    template_on  TEXT    NOT NULL DEFAULT '',    -- 房间专属开播文案(空=用全局)
    template_off TEXT    NOT NULL DEFAULT '',    -- 房间专属下播文案(空=用全局)
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
    notify_on        INTEGER NOT NULL DEFAULT 1,   -- 全局开播通知(默认开)
    notify_off       INTEGER NOT NULL DEFAULT 1,   -- 全局下播通知(默认开)
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
CREATE TABLE IF NOT EXISTS qq_group_map (
    group_id     INTEGER PRIMARY KEY,      -- QQ 群号(绑定会话锚定的用户输入)
    group_openid TEXT NOT NULL,            -- 官方通道群标识(32 位十六进制串)
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
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
]

# 全局配置读出后的字段白名单(顺序即日志/文档展示口径)。
# notify_cover 已停用(2026-10-14:封面/时长均为纯模板变量,不再读该开关)
_GLOBAL_FIELDS = ("qq_account", "onebot_url", "onebot_token", "webui_url",
                  "webui_token", "channel", "official_app_id",
                  "official_app_secret", "command_replies", "notify_on",
                  "notify_off", "template_on", "template_off",
                  "debounce_offline")


def connect(path: str) -> sqlite3.Connection:
    """连共享库(只读写通知两表;WAL + busy_timeout 与既有共库进程一致)。"""
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    """建通知两表(幂等)+ 旧表补列。本进程首启调用;bili 侧锁步副本亦幂等。"""
    conn.executescript(SCHEMA)
    for ddl in _MIGRATIONS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()


def load_global(conn: sqlite3.Connection) -> dict:
    """读全局配置单行;库为空表时返回代码级默认(与 DDL 默认一致)。"""
    row = conn.execute("SELECT * FROM qq_notify_global WHERE id = 1").fetchone()
    if row is None:
        return {"qq_account": "", "onebot_url": "http://127.0.0.1:3000",
                "onebot_token": "", "webui_url": "http://127.0.0.1:6099",
                "webui_token": "", "channel": "napcat",
                "official_app_id": "", "official_app_secret": "",
                "command_replies": "{}",
                "notify_on": 1, "notify_off": 1,
                "template_on": "📺 {uname} 开播了:{title}\n{url}\n{cover}",
                "template_off": "🌙 {uname} 下播了\n⏱ 本次直播:{duration}",
                "debounce_offline": 300}
    d = {k: row[k] for k in _GLOBAL_FIELDS}
    d["notify_on"] = bool(d["notify_on"])
    d["notify_off"] = bool(d["notify_off"])
    return d


def load_room_configs(conn: sqlite3.Connection) -> dict[int, dict]:
    """读启用房间的通知配置(轮询范围 = 开且在监控列表内的房间)。

    返回 {room_id: {groups: [群号], template_on, template_off}}:
    - JOIN rooms 过滤 enabled=1:房间被暂停/移出监控即停止通知(单一事实来源);
    - group_ids 非法 JSON 的行记 WARNING 后跳过(不让一条脏配置拖垮整轮);
      两通道统一填 QQ 群号——official 发送前经 load_group_map 换 openid。
    """
    out: dict[int, dict] = {}
    rows = conn.execute("""
        SELECT n.room_id, n.group_ids, n.template_on, n.template_off,
               r.anchor_name
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
        out[row["room_id"]] = {"groups": groups,
                               "template_on": row["template_on"] or "",
                               "template_off": row["template_off"] or "",
                               # 通知文案的主播名优先用库值(get_info 的
                               # uname 在播时为空,见 bilibili.parse_info)
                               "anchor_name": row["anchor_name"] or ""}
    return out


def load_group_map(conn: sqlite3.Connection) -> dict[int, str]:
    """读群号→openid 映射表(official 通道发送前换算用)。

    空表返回 {};绑定由 bili 管理页「群映射获取」写入。
    """
    try:
        rows = conn.execute(
            "SELECT group_id, group_openid FROM qq_group_map").fetchall()
    except sqlite3.OperationalError:
        return {}   # 表未建(极旧库未跑 ensure_tables 的防御分支)
    return {int(r["group_id"]): str(r["group_openid"]) for r in rows}
