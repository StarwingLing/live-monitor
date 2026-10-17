# -*- coding: utf-8 -*-
"""SQLite 数据层。

设计要点（与 docs/TECH-SPEC.md 第 5 节对应）：
- WAL 模式：支持"采集进程一写 + Web 进程多读"；
- 所有连接统一 busy_timeout=5000，避免跨进程偶发 SQLITE_BUSY 直接报错；
- 表结构见 SCHEMA；明细表 session_id 可空（未开播时段收到的消息）；
- v2.2 架构改版：capture 与录制/切片/OSS 管线已迁 replay 项目，写入方在
  彼侧；本模块只维护**监控类表**（弹幕/礼物/场次/快照/画像/房间/账户）
  的 DDL，与 replay/db.py 锁步维护（改共享表须同版同步两侧）；录制四表
  与高能两表的 DDL 已移除（库里由 replay 侧 init_db 维护）。
"""
import sqlite3
import threading
import time
from pathlib import Path

from .config import DATA_DIR, DB_PATH

# ---------------------------------------------------------------------------
# 建表语句
# ---------------------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS rooms (
    room_id     INTEGER PRIMARY KEY,        -- 真实房间号（长号）
    short_id    INTEGER NOT NULL DEFAULT 0, -- 短号（无则为 0）
    anchor_name TEXT NOT NULL DEFAULT '',   -- 主播昵称
    avatar_url  TEXT NOT NULL DEFAULT '',   -- 主播头像 URL（getInfoByRoom）
    enabled     INTEGER NOT NULL DEFAULT 1, -- 0=暂停监控（保留历史）
    created_at  TEXT NOT NULL               -- ISO8601 本地时间
);

-- 直播场次（开播→下播 为一条）
CREATE TABLE IF NOT EXISTS live_sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id          INTEGER NOT NULL REFERENCES rooms(room_id),
    start_time       TEXT NOT NULL,
    end_time         TEXT,                  -- NULL=正在直播
    peak_popularity  INTEGER NOT NULL DEFAULT 0, -- 场内最高人气值（加权）
    peak_viewers     INTEGER NOT NULL DEFAULT 0, -- 场内最高房间观众数（真实同接，ONLINE_RANK_COUNT）
    danmaku_count    INTEGER NOT NULL DEFAULT 0, -- 冗余计数，加速列表页
    gift_value_yuan  REAL NOT NULL DEFAULT 0.0   -- 冗余合计（礼物+SC+舰长）
);
CREATE INDEX IF NOT EXISTS idx_sessions_room ON live_sessions(room_id, start_time DESC);

-- 弹幕明细（session_id 为 NULL 表示非直播时段收到）
CREATE TABLE IF NOT EXISTS danmaku (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER REFERENCES live_sessions(session_id),  -- NULL=非直播时段
    room_id      INTEGER NOT NULL,
    uid          INTEGER NOT NULL,
    uname        TEXT NOT NULL,
    medal_name   TEXT NOT NULL DEFAULT '',  -- 粉丝牌名称（未佩戴为空）
    medal_level  INTEGER NOT NULL DEFAULT 0,-- 粉丝牌等级
    medal_color  INTEGER NOT NULL DEFAULT 0,-- 粉丝牌颜色（24-bit RGB，官方消息字段）
    privilege_type INTEGER NOT NULL DEFAULT 0, -- 舰队等级 0无 1总督 2提督 3舰长
    text         TEXT NOT NULL,              -- 原始文本（含基础表情占位符，如 [大笑]）
    emots        TEXT NOT NULL DEFAULT '',   -- 表情包 JSON：{占位符: {url,width,height}}（v0.2）
    ts           TEXT NOT NULL               -- 消息时间（ISO8601 北京时间）
);
CREATE INDEX IF NOT EXISTS idx_danmaku_session ON danmaku(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_danmaku_uid ON danmaku(uid);
-- v0.8 性能优化：Web 端高频查询路径补索引（详情页弹幕流 / 今日指标按房间过滤）
-- idx_danmaku_room_id：弹幕流非场次模式 WHERE room_id ORDER BY id DESC（分页游标同用）
-- idx_danmaku_room_ts：今日/昨日弹幕数 WHERE room_id AND ts 范围
CREATE INDEX IF NOT EXISTS idx_danmaku_room_id ON danmaku(room_id, id);
CREATE INDEX IF NOT EXISTS idx_danmaku_room_ts ON danmaku(room_id, ts);

-- 礼物明细（普通礼物 + SC + 舰长统一记录，type 区分）
CREATE TABLE IF NOT EXISTS gifts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(session_id), -- NULL=场外收入
    room_id     INTEGER NOT NULL,
    is_offline  INTEGER NOT NULL DEFAULT 0,  -- 1=非直播时段发生
    uid         INTEGER NOT NULL,
    uname       TEXT NOT NULL,
    type        TEXT NOT NULL,              -- gift / sc / guard
    name        TEXT NOT NULL,              -- 礼物名 / 舰长等级文案
    text        TEXT NOT NULL DEFAULT '',   -- SC 留言内容
    count       INTEGER NOT NULL DEFAULT 1, -- 礼物件数 / 舰长月份数
    value_yuan  REAL NOT NULL,              -- 折算人民币
    gift_img    TEXT NOT NULL DEFAULT '',   -- 礼物缩略图 URL（SEND_GIFT.gift_img_basic）
    blind_box   TEXT NOT NULL DEFAULT '',   -- 盲盒名（空=非盲盒；v0.2：name 存开出的礼物名）
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gifts_session ON gifts(session_id);
CREATE INDEX IF NOT EXISTS idx_gifts_uid ON gifts(uid);
-- v0.8 性能优化：今日/昨日礼物指标按房间+时间窗过滤
CREATE INDEX IF NOT EXISTS idx_gifts_room_ts ON gifts(room_id, ts);

-- 其他互动（关注/分享/特别关注等，type 区分）
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(session_id),
    room_id     INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    uname       TEXT NOT NULL,
    type        TEXT NOT NULL,              -- follow / share / special_follow / mutual
    detail      TEXT NOT NULL DEFAULT '',   -- JSON 附加信息
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inter_session ON interactions(session_id);
CREATE INDEX IF NOT EXISTS idx_inter_uid ON interactions(uid);

-- 人气/观众快照（每房间定期采样；viewers=房间观众·真实同接，来自 WS 推送）
CREATE TABLE IF NOT EXISTS popularity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    session_id  INTEGER,                    -- 关联场次（未开播为 NULL）
    popularity  INTEGER NOT NULL,           -- 心跳人气值（加权热度）
    viewers     INTEGER NOT NULL DEFAULT 0, -- 房间观众数（真实同接，ONLINE_RANK_COUNT.count）
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_room_ts ON popularity_snapshots(room_id, ts);
-- v0.8 性能优化：详情页同接曲线按场次过滤（此前无索引全表扫描）
CREATE INDEX IF NOT EXISTS idx_snap_session ON popularity_snapshots(session_id);

-- 主播级指标快照（轮询房间 API 时顺带记录，供粉丝增长曲线等使用）
CREATE TABLE IF NOT EXISTS anchor_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    attention   INTEGER NOT NULL DEFAULT 0, -- 粉丝数（get_info）
    online      INTEGER NOT NULL DEFAULT 0, -- 人气值·加权（get_info/getInfoByRoom，非真实同接）
    watched     INTEGER NOT NULL DEFAULT 0, -- 本场累计看过人数（真实计数，getInfoByRoom）
    guards      INTEGER NOT NULL DEFAULT 0, -- 大航海人数（真实计数，getInfoByRoom.guard_info）
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anchor_room_ts ON anchor_snapshots(room_id, ts);

-- uid → 完整昵称映射（数据源：高能榜 ONLINE_RANK_V2、礼物/SC/舰长/互动等
-- 未打码消息）。弹幕隐私用户存的是 crc32 伪 uid，凭此表可反查还原全名。
CREATE TABLE IF NOT EXISTS uname_map (
    uid         INTEGER PRIMARY KEY,
    uname       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 盲盒礼物价值映射（v0.2：管理页可编辑）。
-- 用途：SEND_GIFT 盲盒消息的 price 字段缺失/为 0 时（查不到开出礼物的价值），
-- 采集端按本表兜底：key 既可以是"开出的礼物名"也可以是"盲盒名"。
CREATE TABLE IF NOT EXISTS blind_gift_prices (
    gift_name   TEXT PRIMARY KEY,        -- 礼物名 / 盲盒名
    price_yuan  REAL NOT NULL,           -- 对应价值（元/个）
    note        TEXT NOT NULL DEFAULT '',-- 备注（如"心动盲盒开出"）
    updated_at  TEXT NOT NULL
);

-- 表情缓存（v0.2）：B站直播间表情表（GetEmoticons API 定期拉取）。
-- 含基础表情（[dog]/[大笑] 等）与房间专属表情包（如 [涟洢Lianyi_打call]）。
-- emoji_key 统一"不带方括号"存储（API 的 emoji 字段去掉括号后的值），
-- 弹幕文本里的 [xxx] 占位符按 xxx 查本表换取图片 URL。
CREATE TABLE IF NOT EXISTS emotes (
    room_id     INTEGER NOT NULL,        -- 所属房间（表情表按房间拉取，各房间集合不同）
    emoji_key   TEXT NOT NULL,
    url         TEXT NOT NULL,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (room_id, emoji_key)
);


-- 用户画像（聚合表：实时增量更新计数 + 每日全量重算分层）
CREATE TABLE IF NOT EXISTS user_stats (
    uid             INTEGER NOT NULL,
    room_id         INTEGER NOT NULL,
    uname           TEXT NOT NULL DEFAULT '',   -- 最近一次出现的昵称（打码用户存打码名）
    medal_name      TEXT NOT NULL DEFAULT '',   -- 最近佩戴的粉丝牌
    medal_level     INTEGER NOT NULL DEFAULT 0,
    medal_color     INTEGER NOT NULL DEFAULT 0, -- 粉丝牌颜色（24-bit RGB）
    guard_level     INTEGER NOT NULL DEFAULT 0, -- 舰队等级 0无 1总督 2提督 3舰长（取历史最高）
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    danmaku_count   INTEGER NOT NULL DEFAULT 0,
    gift_value_yuan REAL NOT NULL DEFAULT 0.0,
    interact_days   INTEGER NOT NULL DEFAULT 0, -- 统计窗口内有互动的自然日数（日任务重算）
    tier            TEXT NOT NULL DEFAULT '普通', -- 普通/活跃/核心
    PRIMARY KEY (uid, room_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tier ON user_stats(room_id, tier);

-- 管理员账户（v0.5：不开放注册；行由种子逻辑与管理员管理接口产生）
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,             -- 限字母/数字/下划线/中划线（签名 Cookie 分隔符安全）
    password_hash TEXT NOT NULL,                -- pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>
    created_at    TEXT NOT NULL
);

-- 站点级键值存储（v0.5：当前仅游客码）
CREATE TABLE IF NOT EXISTS app_secrets (
    key   TEXT PRIMARY KEY,                     -- 'guest_code'
    value TEXT NOT NULL                         -- 与管理员口令同款 PBKDF2 哈希串
);

-- 主播关键词标签（v0.5：总览页"标签"列，仅管理员可增删）
CREATE TABLE IF NOT EXISTS room_tags (
    room_id    INTEGER NOT NULL REFERENCES rooms(room_id),
    tag        TEXT NOT NULL,                   -- 去首尾空格，单个 ≤ 12 字
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, tag)
);

-- 场次直播标题（v0.5：主播改标题追加一行；last_seen 随轮询刷新）
CREATE TABLE IF NOT EXISTS session_titles (
    session_id INTEGER NOT NULL REFERENCES live_sessions(session_id),
    title      TEXT NOT NULL,
    first_seen TEXT NOT NULL,                   -- 该标题首次出现时间
    last_seen  TEXT NOT NULL,                   -- 最近一次确认时间
    PRIMARY KEY (session_id, title)
);

-- ===== 开播 QQ 群通知（v2.3：live-qq-notify 项目联动）=====
-- DDL **属主为 live-qq-notify**（live_qq_notify/store.py），此处为锁步
-- 幂等副本（保证通知器未启动时管理页也能先建库配置）；改结构须同版
-- 同步两侧。房间级配置由总览页「通知」列开关/设置弹窗写入，通知器
-- 每轮热加载；replay 项目不涉及这两张表。
CREATE TABLE IF NOT EXISTS qq_notify (
    room_id      INTEGER PRIMARY KEY,       -- 对应 rooms.room_id
    enabled      INTEGER NOT NULL DEFAULT 0,     -- 0=关 1=开（总览页开关）
    group_ids    TEXT    NOT NULL DEFAULT '[]',  -- 目标群号 JSON 数组（两通道统一填
                                                  -- QQ 群号；official 发送前经
                                                  -- qq_group_map 换 openid）
    template_on  TEXT    NOT NULL DEFAULT '',    -- 房间专属开播文案（空=用全局）
    template_off TEXT    NOT NULL DEFAULT '',    -- 房间专属下播文案（空=用全局）
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 全局通知配置（单行表；管理页「开播账号管理」读写）
CREATE TABLE IF NOT EXISTS qq_notify_global (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    qq_account       TEXT    NOT NULL DEFAULT '',  -- 预留：通知小号 QQ 号（通知器
                                                   -- 用于校验 NapCat 实际登录号）
    onebot_url       TEXT    NOT NULL DEFAULT 'http://127.0.0.1:3000',
    onebot_token     TEXT    NOT NULL DEFAULT '',  -- NapCat HTTP 鉴权 token
    webui_url        TEXT    NOT NULL DEFAULT 'http://127.0.0.1:6099',  -- WebUI（扫码登录用）
    webui_token      TEXT    NOT NULL DEFAULT '',  -- WebUI 登录 token（webui.json 的 token）
    channel          TEXT    NOT NULL DEFAULT 'napcat',  -- 发送通道 'napcat'|'official'
                                                   -- （v2.3.1；手动切换不自动降级）
    official_app_id  TEXT    NOT NULL DEFAULT '',  -- 官方机器人 AppID（q.qq.com 管理端）
    official_app_secret TEXT NOT NULL DEFAULT '',  -- 官方机器人 AppSecret
    command_replies  TEXT    NOT NULL DEFAULT '{}', -- 群指令自动回复表 JSON dict
                                                    -- （v2.3.1；管理页键值对编辑，
                                                    -- 事件中枢消费；空={}回退代码默认）
    notify_on        INTEGER NOT NULL DEFAULT 1,   -- 全局开播通知（默认开）
    notify_off       INTEGER NOT NULL DEFAULT 1,   -- 全局下播通知（默认开）
    notify_cover     INTEGER NOT NULL DEFAULT 1,   -- 【停用 2026-10-14】封面改纯
                                                   -- 模板变量 {cover}，列仅兼容保留
    template_on      TEXT    NOT NULL DEFAULT '📺 {uname} 开播了:{title}\n{url}\n{cover}',
    template_off     TEXT    NOT NULL DEFAULT '🌙 {uname} 下播了\n⏱ 本次直播:{duration}',
    debounce_offline INTEGER NOT NULL DEFAULT 300, -- 开播后 N 秒内的下播不通知
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO qq_notify_global (id) VALUES (1);

-- 群号→openid 映射（v2.3.1 官方通道）：openid 是"机器人×群"维度标识，官方
-- 接口不提供群号反查，只能从机器人事件捕获——管理页「群映射获取」发起绑定
-- 会话（WebSocket 监听 GROUP_ADD_ROBOT/@消息）写入本表；通知器发送前查表转换。
CREATE TABLE IF NOT EXISTS qq_group_map (
    group_id     INTEGER PRIMARY KEY,      -- QQ 群号（绑定会话锚定的用户输入）
    group_openid TEXT NOT NULL,            -- 官方通道群标识（32 位十六进制串）
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

"""

# 舰长等级文案（guard_level: 1总督 2提督 3舰长）
GUARD_LEVEL_NAMES = {1: "总督", 2: "提督", 3: "舰长"}
# 互动消息 msg_type → interactions.type 的映射（1进入/6点赞 量大价值低，不记录）
INTERACT_TYPES = {2: "follow", 3: "share", 4: "special_follow", 5: "mutual"}


def now_iso() -> str:
    """当前本地时间（服务器时区）的 ISO8601 字符串，秒级精度。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """创建一个已配置好 PRAGMA 的数据库连接。

    capture 进程内由 writer 串行使用；web 进程每请求/每查询只读使用。
    check_same_thread=False：允许跨线程（线程池执行器）使用。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=5.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """建表（幂等）+ 轻量迁移。capture 与 web 启动时都会调用一次。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # 迁移：旧库补列（新列加入时间见注释；列已存在时报错忽略）
        _MIGRATIONS = [
            "ALTER TABLE anchor_snapshots ADD COLUMN watched INTEGER NOT NULL DEFAULT 0",  # 2026-09-12
            "ALTER TABLE anchor_snapshots ADD COLUMN guards INTEGER NOT NULL DEFAULT 0",   # 2026-09-12
            "ALTER TABLE popularity_snapshots ADD COLUMN viewers INTEGER NOT NULL DEFAULT 0",  # 2026-09-12
            "ALTER TABLE live_sessions ADD COLUMN peak_viewers INTEGER NOT NULL DEFAULT 0",    # 2026-09-12
            "ALTER TABLE user_stats ADD COLUMN uname TEXT NOT NULL DEFAULT ''",                # 2026-09-12
            "ALTER TABLE rooms ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''",                # 2026-09-12
            "ALTER TABLE gifts ADD COLUMN gift_img TEXT NOT NULL DEFAULT ''",                  # 2026-09-12
            "ALTER TABLE danmaku ADD COLUMN medal_name TEXT NOT NULL DEFAULT ''",             # 2026-09-12
            "ALTER TABLE danmaku ADD COLUMN medal_level INTEGER NOT NULL DEFAULT 0",          # 2026-09-12
            "ALTER TABLE user_stats ADD COLUMN medal_name TEXT NOT NULL DEFAULT ''",          # 2026-09-12
            "ALTER TABLE user_stats ADD COLUMN medal_level INTEGER NOT NULL DEFAULT 0",       # 2026-09-12
            "ALTER TABLE danmaku ADD COLUMN medal_color INTEGER NOT NULL DEFAULT 0",          # 2026-09-12
            "ALTER TABLE danmaku ADD COLUMN privilege_type INTEGER NOT NULL DEFAULT 0",       # 2026-09-12
            "ALTER TABLE user_stats ADD COLUMN medal_color INTEGER NOT NULL DEFAULT 0",       # 2026-09-12
            "ALTER TABLE user_stats ADD COLUMN guard_level INTEGER NOT NULL DEFAULT 0",       # 2026-09-12
            "ALTER TABLE gifts ADD COLUMN blind_box TEXT NOT NULL DEFAULT ''",               # 2026-09-13 v0.2
            "ALTER TABLE danmaku ADD COLUMN emots TEXT NOT NULL DEFAULT ''",                # 2026-09-13 v0.2
            "ALTER TABLE rooms ADD COLUMN description TEXT NOT NULL DEFAULT ''",            # 2026-09-16 v0.5 主播描述
            "ALTER TABLE rooms ADD COLUMN replay_code TEXT NOT NULL DEFAULT ''",            # 2026-10-01 v0.9 监控码（明文）
            "ALTER TABLE rooms ADD COLUMN auto_full INTEGER NOT NULL DEFAULT 0",           # 2026-10-05 v0.11 自动全程录制开关（共享列：replay 管理页写、本站 summary 读）
            "ALTER TABLE qq_notify_global ADD COLUMN webui_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:6099'",  # 2026-10-13 v2.3 扫码登录嵌入（属主 live-qq-notify，锁步）
            "ALTER TABLE qq_notify_global ADD COLUMN webui_token TEXT NOT NULL DEFAULT ''",                     # 2026-10-13 v2.3 WebUI 登录 token
            "ALTER TABLE qq_notify_global ADD COLUMN notify_cover INTEGER NOT NULL DEFAULT 1",                  # 2026-10-14 v2.3 封面开关（同日停用改纯模板变量，列兼容保留）
            "ALTER TABLE qq_notify_global ADD COLUMN channel TEXT NOT NULL DEFAULT 'napcat'",                   # 2026-10-17 v2.3.1 发送通道（官方 QQ 机器人 API，属主 live-qq-notify，锁步）
            "ALTER TABLE qq_notify_global ADD COLUMN official_app_id TEXT NOT NULL DEFAULT ''",                 # 2026-10-17 v2.3.1 官方机器人 AppID
            "ALTER TABLE qq_notify_global ADD COLUMN official_app_secret TEXT NOT NULL DEFAULT ''",             # 2026-10-17 v2.3.1 官方机器人 AppSecret
            "ALTER TABLE qq_notify_global ADD COLUMN command_replies TEXT NOT NULL DEFAULT '{}'",              # 2026-10-17 v2.3.1 群指令自动回复表（属主 live-qq-notify，锁步）
            # 空 = 存量行（分段信息已丢失），API 走单 run 近似并标 approx
            # v0.11 自动全程录制 + 手动切片
            # 签名/删除按行分派桶（'' = 主桶，兼容存量行与归档后的语义）
        ]
        for ddl in _MIGRATIONS:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()
    finally:
        conn.close()


class Db:
    """带轻量线程锁的数据库访问封装。

    采集服务是 asyncio 单线程模型，理论上无需锁；但每日维护任务、
    快照采样等如经 run_in_executor 跑在线程里，加一把锁更稳妥。
    """

    def __init__(self, db_path: Path | str | None = None):
        self._path = db_path or DB_PATH
        self._lock = threading.Lock()
        self._conn = connect(self._path)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """执行单条语句并提交（带锁）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """批量执行并提交（带锁）。写入明细表的唯一入口。"""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """只读查询（带锁，返回全部行）。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
