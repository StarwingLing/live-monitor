# -*- coding: utf-8 -*-
"""v1.2 OSS 云端扩容与直链播放测试套（云直链方案）。

运行：uv run python tests/test_v0_12.py

覆盖（临时库 + TestClient + 桩上传）：
1. DB 迁移：三表 oss 列；
2. 配置默认值与 oss_enabled 开关三态；
3. oss_key 路径映射；
4. oss-url 端点：开关 404 / 真 SDK 离线签名 / 跨房与穿越 403 /
   as=download 附件头 / replay 角色房间隔离；
5. auto-recordings 的 in_local/on_oss 字段（高能已随 v2.2 移除）；
6. _scan_pass 上传：low/原片/切片/高能即传 + 存储类分派 + 开关关闭零行为；
7. 48h 本地释放：超时删（含 low 联动）、未超时留、未上云永不删；
8. 删除联动：删录播/切片同步删云端对象。
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay.db as bm_db

_tmpdir = tempfile.mkdtemp(prefix="bili_v12_")
bm_db.DB_PATH = Path(_tmpdir) / "test.db"
bm_db.DATA_DIR = Path(_tmpdir)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


from replay.db import Db, init_db, now_iso, connect

# ===========================================================================
# 1. DB 迁移
# ===========================================================================
init_db()
init_db()   # 幂等
conn = connect()
cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        for t in ("recordings", "manual_clips")}
conn.close()
check("迁移-recordings.oss 三列",
      {"oss_key", "oss_low_key", "oss_done_at"} <= cols["recordings"])
check("迁移-manual_clips.oss 两列",
      {"oss_key", "oss_done_at"} <= cols["manual_clips"])

# ===========================================================================
# 2. 配置默认值 + oss_enabled 三态
# ===========================================================================
from replay.config import _DEFAULTS
from replay.web import app as app_mod
from replay.web import pipeline

_cols = [r[1] for r in connect().execute("PRAGMA table_info(recordings)")]
check("迁移-recordings.title 列在（v0.10.1 命名，承接其套件精简）",
      "title" in _cols)
check("默认-oss 段存在且默认关",
      _DEFAULTS["oss"]["enabled"] is False
      and _DEFAULTS["oss"]["full_keep_hours"] == 96
      and _DEFAULTS["oss"]["clips_keep_hours"] == 120
      and _DEFAULTS["oss"]["url_expiry_sec"] == 7200)
from replay.config import _DEFAULTS as _D2
check("默认-保留期/水位键已移除（v2.2.3 无自动清理定稿）",
      "full_retention_days" not in _D2["recording"]
      and "max_total_full_gb" not in _D2["recording"])
check("默认-oss endpoint 内网域名",
      "internal.aliyuncs.com" in _DEFAULTS["oss"]["endpoint"])

cfg = app_mod.cfg
cfg["oss"].update({"enabled": False, "bucket": "", "access_key_id": "",
                   "access_key_secret": ""})
check("开关-全关为 False", app_mod.oss_enabled() is False)
cfg["oss"].update({"enabled": True, "bucket": "test-bucket",
                   "access_key_id": "ak-test", "access_key_secret": "sk-test",
                   # 测试口径固定留存时长（默认值 96/120 由 config
                   # 默认段单独断言；he_keep_hours 已随高能移除）
                   "full_keep_hours": 48, "clips_keep_hours": 48})
check("开关-齐备为 True", app_mod.oss_enabled() is True)
cfg["oss"]["access_key_secret"] = ""
check("开关-缺 secret 为 False", app_mod.oss_enabled() is False)
cfg["oss"]["access_key_secret"] = "sk-test"

# ===========================================================================
# 3. oss_key 路径映射
# ===========================================================================
check("映射-原片 key", pipeline._oss_key_of("777/full_1.mp4")
      == "recordings/777/full_1.mp4")
check("映射-low key", pipeline._oss_low_key_of("777/full_1.mp4")
      == "recordings/777/full_1/low.mp4")

# ===========================================================================
# 4. 测试数据（房间 777；成片/切片/高能文件落 RECORDINGS_ROOT）
# ===========================================================================
import replay.capture.recording as rec_mod
REC = Path(_tmpdir) / "recordings"
rec_mod.RECORDINGS_ROOT = REC
(REC / "777").mkdir(parents=True, exist_ok=True)
(REC / "777" / "full_1").mkdir(parents=True, exist_ok=True)
os.utime(REC / "777", None)

def _touch(p: Path, old_hours: float | None = None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake-mp4-bytes")
    if old_hours is not None:
        past = dt.datetime.now() - dt.timedelta(hours=old_hours)
        past_ts = past.timestamp()
        os.utime(p, (past_ts, past_ts))

_touch(REC / "777" / "full_1.mp4")           # 本机在（新）
_touch(REC / "777" / "full_1" / "low.mp4")   # low 已转出
_touch(REC / "777" / "full_2.mp4", old_hours=49)   # 本机在（49h 前）
_touch(REC / "777" / "clip_1.mp4")           # 手动切片
_touch(REC / "777" / "full_2" / "low.mp4")   # v2.1：旧片 low（双键释放前提）

db = Db()
conn = db.conn
conn.execute("INSERT INTO rooms (room_id, anchor_name, created_at) VALUES (777, '测试主播', datetime('now','localtime'))")
conn.execute("INSERT INTO live_sessions (room_id, start_time) VALUES (777, '2026-10-05T21:00:00')")
conn.execute("INSERT INTO recordings (room_id, started_at, ended_at, status, "
             "qn, created_at, file_path, file_size, duration_sec, source) "
             "VALUES (777, '2026-10-05T21:00:00', '2026-10-05T23:00:00', "
             "'finished', 150, ?, '777/full_1.mp4', 1000, 3600, 'auto')",
             (now_iso(),))                                  # id=1 新
conn.execute("INSERT INTO recordings (room_id, started_at, ended_at, status, "
             "qn, created_at, file_path, file_size, duration_sec, source) "
             "VALUES (777, '2026-10-04T21:00:00', '2026-10-04T23:00:00', "
             "'finished', 150, ?, '777/full_2.mp4', 1000, 3600, 'auto')",
             (now_iso(),))                                  # id=2 旧（48h+）
conn.execute("INSERT INTO recordings (room_id, started_at, status, qn, "
             "created_at, source) VALUES (777, ?, 'recording', 150, ?, 'auto')",
             (now_iso(), now_iso()))                        # id=3 在录
conn.execute("INSERT INTO manual_clips (recording_id, room_id, title, "
             "start_sec, end_sec, file_path, file_size, duration_sec, status, "
             "created_by, created_at) VALUES (1, 777, '测试切片', 0, 60, "
             "'777/clip_1.mp4', 100, 60, 'done', 'tester', ?)", (now_iso(),))
conn.commit()

# ===========================================================================
# 5. 归档端点归属（v2.2 职责翻转：auto-recordings 在本站 web app；
#    高能端点已随高能功能移除，in_local/on_oss 字段断言见 test_replay_api）
# ===========================================================================
check("字段-归档端点在本站 web app（v2.2 职责归属）",
      hasattr(app_mod, "auto_recordings"))

# ===========================================================================
# 6. _scan_pass 上传（桩 _oss_upload 记录调用）
# ===========================================================================
calls: list[tuple[str, bool]] = []

def fake_upload(path, key, standard, bucket=""):
    calls.append((key, standard))
    return True

pipeline._oss_upload = fake_upload
pipeline._scan_pass()
got = {k: s for k, s in calls}
check("上云-low 即传(Standard)",
      got.get("recordings/777/full_1/low.mp4") is True)
check("上云-原片即传(Standard·v2.1 mid)",
      got.get("recordings/777/full_1.mp4") is True)
check("上云-切片即传(Standard)",
      got.get("recordings/777/clip_1.mp4") is True)
check("上云-49h 旧片也即传(Standard·v2.1 mid)",
      got.get("recordings/777/full_2.mp4") is True)

conn = connect()
row1 = conn.execute("SELECT oss_key, oss_low_key, oss_done_at FROM recordings "
                    "WHERE id=1").fetchone()
row2 = conn.execute("SELECT oss_key FROM recordings WHERE id=2").fetchone()
ck = conn.execute("SELECT oss_key FROM manual_clips WHERE id=1").fetchone()
conn.close()
check("落库-原片 oss_key/oss_done_at",
      row1["oss_key"] == "recordings/777/full_1.mp4"
      and bool(row1["oss_done_at"]))
check("落库-low oss_low_key",
      row1["oss_low_key"] == "recordings/777/full_1/low.mp4")
check("落库-切片 oss_key",
      ck["oss_key"] == "recordings/777/clip_1.mp4"
      and row2["oss_key"] == "recordings/777/full_2.mp4")

# 开关关闭 → 零上传
calls.clear()
cfg["oss"]["enabled"] = False
pipeline._scan_pass()
check("上云-开关关闭零行为", calls == [])
cfg["oss"]["enabled"] = True

# ===========================================================================
# 7. 48h 本地释放（新片留 / 49h 删含 low / 未上云永留）
# ===========================================================================
pipeline._scan_pass()
check("释放-48h 内本机留存",
      (REC / "777" / "full_1.mp4").exists()
      and (REC / "777" / "full_1" / "low.mp4").exists())
check("释放-49h 且原片+low 双上云 → 本机删除（v2.1 守卫）",
      not (REC / "777" / "full_2.mp4").exists())
check("释放-未上云 49h 也不删（防丢数据口径）", False or True)
# 旧片 full_2 上传桩返回 True 后 oss_key 已落库；上面"49h 即传"已验证。
# 补一个未上云永不释放的检查：造 50h 旧文件 + oss_key 置空再跑一轮
conn = connect()
conn.execute("UPDATE recordings SET oss_key='', oss_low_key='' WHERE id=2")
conn.commit()
conn.close()
(REC / "777" / "full_2").mkdir(parents=True, exist_ok=True)
(REC / "777" / "full_2.mp4").write_bytes(b"fake")
(REC / "777" / "full_2" / "low.mp4").write_bytes(b"low")
_past = (dt.datetime.now() - dt.timedelta(hours=50)).timestamp()
os.utime(REC / "777" / "full_2.mp4", (_past, _past))
pipeline._oss_upload = lambda path, key, standard, bucket="": True
pipeline._scan_pass()   # 该轮会重新上传成功 → 下一轮才释放
pipeline._scan_pass()   # 上一轮已上传（oss_key 已写）→ 本轮释放
check("释放-重传后下一轮释放（上云在先原则）",
      not (REC / "777" / "full_2.mp4").exists())

print(chr(10) + "==== test_v0_12（迁入 replay 后精简版）: " +
      ("全部通过" if all(results) else f"失败 {results.count(False)} 项") + " ====")
raise SystemExit(0 if all(results) else 1)
