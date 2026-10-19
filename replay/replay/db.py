# -*- coding: utf-8 -*-
"""数据层（replay 项目，v2.2 起为共享 SQLite 的表结构属主）。

要点：
- v2.2 架构改版后，capture 采集与转码/上云/释放管线均在本项目内，
  **建表与迁移由本模块提供**（init_db 幂等：CREATE IF NOT EXISTS +
  守卫 ALTER），capture 与 web 启动时各调用一次；bili-monitor web 亦
  持有一份监控类表的 DDL（其自身登录/分析所需），**两份 SCHEMA 锁步
  维护**——任何一侧改动共享表结构，必须同版同步另一侧 db.py 与两项目
  测试的建表子集；
- 高能时刻两张表（he_windows/he_clips）已随 v2.2 功能移除，存量由
  deploy/cleanup_he_v2_2.py 在部署时 DROP；
- 连接参数与 bili-monitor/db.connect 保持同款（WAL + busy_timeout=5000
  + foreign_keys=ON），保证跨进程行为一致；
- 库文件位于本项目 data/live.db（服务器 /opt/replay/data/，v2.2 部署时
  自 /opt/bili-monitor/data 平移）。
"""
import sqlite3
import threading
import time
from pathlib import Path

from .config import DATA_DIR, DB_PATH

# ---------------------------------------------------------------------------
# 建表语句（监控类表与 bili-monitor/db.py 锁步；录制类表为本项目专属）
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
    gift_value_yuan  REAL NOT NULL DEFAULT 0.0,  -- 冗余合计（礼物+SC+舰长）
    cover_url        TEXT NOT NULL DEFAULT ''    -- 开播时直播间封面（v2.5.1 建场写入，
                                                 -- 与 bili-monitor db.py 锁步）
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
    text         TEXT NOT NULL,              -- 原始文本（含基础表情占位符，如"哈哈[大笑]"）
    emots        TEXT NOT NULL DEFAULT '',   -- 表情包 JSON：{占位符: {url,width,height}}（v0.2）
    ts           TEXT NOT NULL               -- 消息时间（ISO8601 北京时间）
);
CREATE INDEX IF NOT EXISTS idx_danmaku_session ON danmaku(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_danmaku_uid ON danmaku(uid);
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
CREATE TABLE IF NOT EXISTS blind_gift_prices (
    gift_name   TEXT PRIMARY KEY,        -- 礼物名 / 盲盒名
    price_yuan  REAL NOT NULL,           -- 对应价值（元/个）
    note        TEXT NOT NULL DEFAULT '',-- 备注（如"心动盲盒开出"）
    updated_at  TEXT NOT NULL
);

-- 表情缓存（v0.2）：B站直播间表情表（GetEmoticons API 定期拉取）。
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
    guard_level     INTEGER NOT NULL DEFAULT 0, -- 舰队等级 0无 1总督 2提督 3舰长（跟随最新非零观测）
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    danmaku_count   INTEGER NOT NULL DEFAULT 0,
    gift_value_yuan REAL NOT NULL DEFAULT 0.0,
    interact_days   INTEGER NOT NULL DEFAULT 0, -- 统计窗口内有互动的自然日数（日任务重算）
    tier            TEXT NOT NULL DEFAULT '普通', -- 普通/活跃/核心
    PRIMARY KEY (uid, room_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tier ON user_stats(room_id, tier);

-- 管理员账户（bili-monitor web 与本站共用；行由 bili 侧种子逻辑与管理接口产生）
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,             -- 限字母/数字/下划线/中划线（签名 Cookie 分隔符安全）
    password_hash TEXT NOT NULL,                -- pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>
    created_at    TEXT NOT NULL
);

-- 站点级键值存储（本站 app_secrets：clipper_password / guest_code 等）
CREATE TABLE IF NOT EXISTS app_secrets (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                         -- 与管理员口令同款 PBKDF2 哈希串
);

-- 主播关键词标签（bili-monitor 总览页"标签"列，其管理页增删）
CREATE TABLE IF NOT EXISTS room_tags (
    room_id    INTEGER NOT NULL REFERENCES rooms(room_id),
    tag        TEXT NOT NULL,                   -- 去首尾空格，单个 ≤ 12 字
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, tag)
);

-- 场次直播标题（主播改标题追加一行；last_seen 随轮询刷新）
CREATE TABLE IF NOT EXISTS session_titles (
    session_id INTEGER NOT NULL REFERENCES live_sessions(session_id),
    title      TEXT NOT NULL,
    first_seen TEXT NOT NULL,                   -- 该标题首次出现时间
    last_seen  TEXT NOT NULL,                   -- 最近一次确认时间
    PRIMARY KEY (session_id, title)
);

-- 整场录制记录（v0.9 引入；web 写行 → capture 2s 扫描执行）。
-- 状态机：requested → recording → stop_requested / rotate_requested →
--         finalizing → finished / failed / interrupted。
CREATE TABLE IF NOT EXISTS recordings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      INTEGER NOT NULL REFERENCES rooms(room_id),
    started_at   TEXT NOT NULL,                -- 录制实际开始（capture 启流成功时）
    ended_at     TEXT,                         -- 录制结束（finalize 完成时写）
    status       TEXT NOT NULL DEFAULT 'requested',
    reason       TEXT NOT NULL DEFAULT '',     -- failed 时的原因（如"当前未开播"）
    qn           INTEGER NOT NULL DEFAULT 200, -- 本次录制清晰度（full_qn，v0.9.1 起 200）
    file_path    TEXT NOT NULL DEFAULT '',     -- 成片相对 data/recordings/ 的路径
    file_size    INTEGER NOT NULL DEFAULT 0,   -- 成片字节数
    duration_sec INTEGER NOT NULL DEFAULT 0,   -- 成片时长（ffprobe 实测）
    title        TEXT NOT NULL DEFAULT '',     -- 管理员命名（v0.10.1，空=未命名，≤60 字）
    created_at   TEXT NOT NULL,                -- 请求创建时间
    restored_at  TEXT NOT NULL DEFAULT ''      -- v2.2.1 回灌时刻（空=非回灌产物；释放分支据此启用独立 restore_keep_hours 窗口）
);
CREATE INDEX IF NOT EXISTS idx_recordings_room ON recordings(room_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings(status);

-- 录制注释（v0.9.2：多人各注各的；录制行被清理时注释级联删除）
CREATE TABLE IF NOT EXISTS recording_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    author       TEXT NOT NULL,                 -- 署名（昵称/用户名，明文）
    content      TEXT NOT NULL,                 -- 注释内容（≤500 字）
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL                  -- 编辑时更新
);
CREATE INDEX IF NOT EXISTS idx_recording_notes ON recording_notes(recording_id, id);

-- 手动切片记录（v0.11：打点切片 / 直播截取共用）。
-- 刻意不设外键：源 recordings 行被保留期/水位清理后，已切出的片段
-- （自包含视频文件）继续存续，板块按片段自身日期独立展示。
CREATE TABLE IF NOT EXISTS manual_clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id  INTEGER NOT NULL,                 -- 来源整场成片（行可能已清理）
    room_id       INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',         -- 切片名字（空=前端默认按起止时间命名）
    start_sec     REAL NOT NULL,                    -- 起点（源成片文件内秒偏移；直播切片为 0）
    end_sec       REAL NOT NULL,                    -- 终点（源成片文件内秒偏移；直播切片为 0）
    start_wall    TEXT NOT NULL DEFAULT '',         -- 直播切片模式：窗口起（ISO 墙钟；空=成片偏移模式）
    end_wall      TEXT NOT NULL DEFAULT '',         -- 直播切片模式：窗口止（ISO 墙钟）
    file_path     TEXT NOT NULL DEFAULT '',         -- 切片文件相对 data/recordings/ 的路径
    file_size     INTEGER NOT NULL DEFAULT 0,
    duration_sec  INTEGER NOT NULL DEFAULT 0,       -- 切片时长（ffprobe 实测）
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/failed
    reason        TEXT NOT NULL DEFAULT '',         -- failed 原因（如源成片缺失）
    created_by    TEXT NOT NULL DEFAULT '',         -- 署名（切片员昵称/管理员用户名）
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_clips_rec ON manual_clips(recording_id, id);
CREATE INDEX IF NOT EXISTS idx_manual_clips_room ON manual_clips(room_id, created_at DESC);

-- 切片注释（v0.11：与录制注释同款多人各注各的；切片删除时随外键级联）
CREATE TABLE IF NOT EXISTS manual_clip_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES manual_clips(id) ON DELETE CASCADE,
    author       TEXT NOT NULL,                 -- 署名（昵称/用户名，明文）
    content      TEXT NOT NULL,                 -- 注释内容（≤500 字）
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_clip_notes ON manual_clip_notes(clip_id, id);
-- v2.3.0 投稿结果落库（replay 专属表）：任务表进程内 2h 过期即失忆，
-- 前端刷新也丢会话态——成功投稿的切片按 clip_id 恒存，归档接口回传
-- submission 供「已投稿」徽标持久显示（刷新/重启后仍在）
CREATE TABLE IF NOT EXISTS clip_submissions (
    clip_id      INTEGER PRIMARY KEY,           -- 切片 id（与 manual_clips 对应）
    bv           TEXT NOT NULL DEFAULT '',      -- 新视频 BV 号 / 分P目标稿件号
    mode         TEXT NOT NULL DEFAULT '',      -- new 新视频 / part 追加分P
    title        TEXT NOT NULL DEFAULT '',      -- 投稿标题 / 分P名
    submitted_at TEXT NOT NULL,                 -- 成功时刻（本地 ISO8601）
    season_note  TEXT NOT NULL DEFAULT '',      -- 加入合集失败等补充说明
    alive        INTEGER NOT NULL DEFAULT 1,    -- v2.3.0 存在性核验：1 在 / 0 已删（稿件或同名分P消失）
    checked_at   TEXT NOT NULL DEFAULT ''       -- v2.3.0 最近一次核验时刻
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
        # 迁移：旧库补列（列已存在时报错忽略）。监控类表的迁移与
        # bili-monitor/db.py 锁步维护；录制类表为本项目专属。
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
            # v0.10 外挂弹幕：墙钟→视频内偏移映射（JSON，build_timeline 产物）
            "ALTER TABLE recordings ADD COLUMN timeline TEXT NOT NULL DEFAULT ''",          # 2026-10-04 v0.10 整场成片时间轴
            "ALTER TABLE recordings ADD COLUMN title TEXT NOT NULL DEFAULT ''",             # 2026-10-04 v0.10.1 录播命名（仅管理员可改）
            # v0.11 自动全程录制 + 手动切片
            "ALTER TABLE rooms ADD COLUMN auto_full INTEGER NOT NULL DEFAULT 0",           # 2026-10-05 v0.11 自动全程录制开关
            "ALTER TABLE recordings ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",     # 2026-10-05 v0.11 录制来源 auto/manual/bridge
            "ALTER TABLE manual_clips ADD COLUMN start_wall TEXT NOT NULL DEFAULT ''",     # 2026-10-05 v0.11 直播切片窗口起
            "ALTER TABLE manual_clips ADD COLUMN end_wall TEXT NOT NULL DEFAULT ''",       # 2026-10-05 v0.11 直播切片窗口止
            "ALTER TABLE recordings ADD COLUMN oss_key TEXT NOT NULL DEFAULT ''",          # 2026-10-06 v1.2 OSS 原片对象 key（空=未上云）
            "ALTER TABLE recordings ADD COLUMN oss_low_key TEXT NOT NULL DEFAULT ''",      # 2026-10-06 v1.2 OSS 低画质副本对象 key（空=未上云）
            "ALTER TABLE recordings ADD COLUMN oss_done_at TEXT NOT NULL DEFAULT ''",      # 2026-10-06 v1.2 OSS 原片上云完成时间
            "ALTER TABLE manual_clips ADD COLUMN oss_key TEXT NOT NULL DEFAULT ''",        # 2026-10-06 v1.2 OSS 上云对象 key（空=未上云）
            "ALTER TABLE manual_clips ADD COLUMN oss_done_at TEXT NOT NULL DEFAULT ''",    # 2026-10-06 v1.2 OSS 上云完成时间
            "ALTER TABLE manual_clips ADD COLUMN oss_bucket TEXT NOT NULL DEFAULT ''",    # 2026-10-08 v1.4 切片独立桶名（空=主桶）
            "ALTER TABLE manual_clips ADD COLUMN oss_low_key TEXT NOT NULL DEFAULT ''",   # 2026-10-09 v1.5 切片低清副本对象 key（空=未上云）
            # v2.1 云存储改版：所有视频处理完成后落 mid 热桶、录播原片 10 天归档主桶，
            # 签名/删除按行分派桶（'' = 主桶，兼容存量行与归档后的语义）
            "ALTER TABLE recordings ADD COLUMN oss_bucket TEXT NOT NULL DEFAULT ''",      # 2026-10-12 v2.1 原片所在桶（'' = 主桶）
            "ALTER TABLE recordings ADD COLUMN oss_low_bucket TEXT NOT NULL DEFAULT ''",  # 2026-10-12 v2.1 low 所在桶（'' = 主桶；新上云恒 mid，不随归档翻回）
            "ALTER TABLE recordings ADD COLUMN restored_at TEXT NOT NULL DEFAULT ''",     # 2026-10-13 v2.2.1 回灌标记（回灌原片独立 24h 本地窗口）
            # v2.3.0 投稿记录存在性核验（长周期任务更新；alive=0 时胶囊不再显示）
            "ALTER TABLE clip_submissions ADD COLUMN alive INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE clip_submissions ADD COLUMN checked_at TEXT NOT NULL DEFAULT ''",
            # v2.5.1 场次封面（建场写入开播时直播间封面；与 bili-monitor db.py 锁步）
            "ALTER TABLE live_sessions ADD COLUMN cover_url TEXT NOT NULL DEFAULT ''",
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
    """带轻量线程锁的数据库访问封装（capture 进程内使用）。"""

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
