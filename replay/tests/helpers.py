# -*- coding: utf-8 -*-
"""replay 项目测试公共工具。

要点（与 bili-monitor tests 同款约定）：
- 每个测试文件是独立可执行脚本（uv run python tests/test_xxx.py），自带
  断言收集与退出码，不依赖 pytest；
- 库隔离：在任何 replay 模块 import 前，把 replay.db.DB_PATH 指向临时
  文件（connect() 每次调用时读该模块全局，覆盖生效）；
- 建表：生产库由 bili-monitor 独家建表，但测试需要表结构——此处内嵌
  与 bili SCHEMA 一致的 CREATE 语句子集（仅本测试使用，运行期绝不调用）；
- bili Cookie 伪造：SSO 桥接测试用临时密钥文件 + 与 bili-monitor 相同的
  签名格式（"过期|角色|用户名|签名"，昵称段可选）手工造 bili_auth。
"""
import hashlib
import hmac
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 库隔离：指向临时文件（必须在 import replay.* 之前）----
TMP = Path(tempfile.mkdtemp(prefix="replay_test_"))

import replay.db as rdb
rdb.DB_PATH = TMP / "test.db"

import replay.auth as auth
auth.SECRET_PATH = TMP / "replay_secret.key"
auth.BILI_SECRET_PATH = TMP / "bili_secret.key"
auth.BILI_DATA_DIR = TMP

# bili 密钥文件（桥接测试用；内容即密钥字节，格式与 bili web_secret.key 一致）
_BILI_SECRET = os.urandom(32)
auth.BILI_SECRET_PATH.write_bytes(_BILI_SECRET)

# root_path 中和：本地 config.toml 可能带子路径前缀（/replay，构建产物
# 本地联调用）。FastAPI 构造时读它会把自己挂到前缀下，StaticFiles 挂载
# （/recordings、/assets）随之只认带前缀路径，测试的裸路径请求会 404。
# 测试一律按无前缀形态进行；生产「Nginx 传带前缀路径」另有端到端验收覆盖。
import replay.web.app as _app_mod
_app_mod.cfg["server"]["root_path"] = ""
_app_mod.app.root_path = ""

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


def summary(title: str) -> int:
    total, passed = len(results), sum(results)
    print(f"\n==== {title}: {passed}/{total} 通过 ====")
    return 0 if passed == total else 1


def hash_secret(plain: str) -> str:
    """与 bili-monitor 同款 PBKDF2 存储串（admin_users 口令校验用）。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, 120_000)
    return f"pbkdf2_sha256$120000${salt.hex()}${digest.hex()}"


def sign_bili(role: str, username: str = "", nickname: str = "",
              ttl: int = 3600) -> str:
    """按 bili-monitor 会话格式手工签一个 bili_auth Cookie 值（桥接测试）。"""
    payload = f"{int(time.time()) + ttl}|{role}|{username}"
    if nickname:
        from urllib.parse import quote
        payload += "|" + quote(nickname, safe="")
    sig = hmac.new(_BILI_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


# 与 bili-monitor SCHEMA 一致的建表子集（仅供测试；运行期不调用）
SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id     INTEGER PRIMARY KEY,
    short_id    INTEGER NOT NULL DEFAULT 0,
    anchor_name TEXT NOT NULL DEFAULT '',
    avatar_url  TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    record_enabled INTEGER NOT NULL DEFAULT 0,
    replay_code TEXT NOT NULL DEFAULT '',
    auto_full   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id          INTEGER NOT NULL REFERENCES rooms(room_id),
    start_time       TEXT NOT NULL,
    end_time         TEXT,
    peak_popularity  INTEGER NOT NULL DEFAULT 0,
    peak_viewers     INTEGER NOT NULL DEFAULT 0,
    danmaku_count    INTEGER NOT NULL DEFAULT 0,
    gift_value_yuan  REAL NOT NULL DEFAULT 0.0,
    cover_url        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS danmaku (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER,
    room_id      INTEGER NOT NULL,
    uid          INTEGER NOT NULL,
    uname        TEXT NOT NULL,
    medal_name   TEXT NOT NULL DEFAULT '',
    medal_level  INTEGER NOT NULL DEFAULT 0,
    medal_color  INTEGER NOT NULL DEFAULT 0,
    privilege_type INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    emots        TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_secrets (
    key   TEXT PRIMARY KEY,             -- 'guest_code'（bili）/ 'clipper_password'（replay）
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      INTEGER NOT NULL REFERENCES rooms(room_id),
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL DEFAULT 'requested',
    reason       TEXT NOT NULL DEFAULT '',
    qn           INTEGER NOT NULL DEFAULT 200,
    file_path    TEXT NOT NULL DEFAULT '',
    file_size    INTEGER NOT NULL DEFAULT 0,
    duration_sec INTEGER NOT NULL DEFAULT 0,
    title        TEXT NOT NULL DEFAULT '',
    timeline     TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'manual',
    oss_key      TEXT NOT NULL DEFAULT '',
    oss_low_key  TEXT NOT NULL DEFAULT '',
    oss_done_at  TEXT NOT NULL DEFAULT '',
    -- bili v2.1（2026-10-12）行级桶分派：'' = 主桶；原片 10 天热窗口在
    -- mid 桶、归档后回主桶，low 恒 mid。与 bili_monitor/db.py 迁移保持同步。
    oss_bucket      TEXT NOT NULL DEFAULT '',
    oss_low_bucket  TEXT NOT NULL DEFAULT '',
    -- v2.2.1（2026-10-13）：回灌时刻标记，空 = 非回灌产物（释放分支按
    -- restore_keep_hours 独立窗口评估，与 full_keep_hours 互不干扰）
    restored_at     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    author       TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id  INTEGER NOT NULL,
    room_id       INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    start_sec     REAL NOT NULL,
    end_sec       REAL NOT NULL,
    start_wall    TEXT NOT NULL DEFAULT '',
    end_wall      TEXT NOT NULL DEFAULT '',
    file_path     TEXT NOT NULL DEFAULT '',
    file_size     INTEGER NOT NULL DEFAULT 0,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    reason        TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    oss_key       TEXT NOT NULL DEFAULT '',
    oss_done_at   TEXT NOT NULL DEFAULT '',
    oss_bucket    TEXT NOT NULL DEFAULT '',
    oss_low_key   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS manual_clip_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES manual_clips(id) ON DELETE CASCADE,
    author       TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_submissions (
    clip_id      INTEGER PRIMARY KEY,
    bv           TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    season_note  TEXT NOT NULL DEFAULT '',
    alive        INTEGER NOT NULL DEFAULT 1,
    checked_at   TEXT NOT NULL DEFAULT ''
);
"""


def init_tables(conn):
    """建表 + 种子数据（幂等；两次调用安全）。"""
    conn.executescript(SCHEMA)
    c = conn.execute("SELECT count(*) FROM rooms").fetchone()[0]
    if c == 0:
        conn.execute("INSERT INTO rooms (room_id, anchor_name, replay_code, "
                     "auto_full, created_at) VALUES (101, '主播甲', "
                     "'r101-ABCDEF1234', 0, '2026-10-07T10:00:00')")
        conn.execute("INSERT INTO rooms (room_id, anchor_name, replay_code, "
                     "created_at) VALUES (202, '主播乙', 'r202-GHIJKL5678', "
                     "'2026-10-07T10:00:00')")
        conn.execute("INSERT INTO admin_users (username, password_hash, "
                     "created_at) VALUES ('admin1', ?, '2026-10-07T10:00:00')",
                     (hash_secret("password123"),))
        conn.execute("INSERT INTO live_sessions (room_id, start_time) "
                     "VALUES (101, '2026-10-07T10:00:00')")   # 101 在播
        conn.commit()
    # v1.3.x 房间表分离：走生产同款迁移（建 replay_rooms，首次自 rooms
    # 播种，监控码随之可用）；幂等，表已存在时不重播
    _app_mod._ensure_replay_rooms()
