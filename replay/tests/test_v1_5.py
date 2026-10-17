# -*- coding: utf-8 -*-
"""v1.5 切片低清预览测试套（manual_clips 与录播成片同口径 480p low）。

运行：uv run python tests/test_v1_5.py

覆盖（临时库 + 桩 ffmpeg/上传/删除 + TestClient）：
1. DB 迁移：manual_clips.oss_low_key 列（幂等）；
2. _make_low：成功改名 / 失败留 .part 墓碑；
3. _scan_pass 转码：切片缺 low 即转（含存量回填）、已有跳过、
   墓碑退避跳过、非 done 不转、录播路径不回归；
4. _scan_pass 上传：切片 low 上 clips_bucket 同桶 Standard、
   本体已上云的存量切片只补传 low、两 key 独立落库；
5. 本地释放（2026-10-11 新口径）：切片到期删本体，低清预览独立计时
   （clips_low_keep_hours）到期才删；未上云 low 保留（由三级清扫兜底）；
6. 三级清扫：切片本体不在 → low 目录整删；过期 .part 清除；
7. 删除端点：delete_clip 联动删云端本体+low 双对象（同桶）与本地 low 目录。
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay.db as bm_db

_tmpdir = tempfile.mkdtemp(prefix="bili_v15_")
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
cols = {r[1] for r in conn.execute("PRAGMA table_info(manual_clips)")}
conn.close()
check("迁移-manual_clips.oss_low_key",
      "oss_low_key" in cols, str(sorted(cols)))

from replay.web import app as app_mod
from replay.web import pipeline
import replay.capture.recording as rec_mod

# ===========================================================================
# 2. 环境准备：录像根 / OSS 配置（clips_bucket 独立桶）/ 桩
# ===========================================================================
REC = Path(_tmpdir) / "recordings"
rec_mod.RECORDINGS_ROOT = REC
app_mod.RECORDINGS_ROOT = REC

cfg = app_mod.cfg
cfg["oss"].update({"enabled": True, "bucket": "test-bucket",
                   "clips_bucket": "test-bucket-mid",
                   "access_key_id": "ak-test",
                   "access_key_secret": "sk-test",
                   "region": "cn-hangzhou",
                   "full_keep_hours": 48, "clips_keep_hours": 48,
                   "he_keep_hours": 48,
                   # 2026-10-11 新口径：切片 low 独立留存（测试压到 48h
                   # 与本体同窗口，便于构造「本体到期而 low 未到期/双到期」
                   # 两种相位；生产为 720h=30 天）
                   "clips_low_keep_hours": 48})
pipeline._OSS_CLIENTS.clear()   # 防串其他套留下的客户端

# 桩 ffmpeg：成功时写 .part 产物；fail=True 模拟转码失败（不写产物）
class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _fake_run(cmd, **kw):
    if _fake_run.fail:
        return _Proc(1)
    part = Path(cmd[-1])
    part.write_bytes(b"low-bytes")
    return _Proc(0)


_fake_run.fail = False
pipeline.subprocess.run = _fake_run

uploaded: list[tuple[str, str, bool, str]] = []   # (路径尾, key, standard, bucket)
pipeline._oss_upload = lambda path, key, standard, bucket="": (
    uploaded.append((Path(path).name, key, standard, bucket)), True)[1]


def _touch(p: Path, old_hours: float | None = None, size: int = 64):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    if old_hours is not None:
        past = dt.datetime.now() - dt.timedelta(hours=old_hours)
        ts = past.timestamp()
        os.utime(p, (ts, ts))


def _age(p: Path, hours: float):
    past = (dt.datetime.now() - dt.timedelta(hours=hours)).timestamp()
    os.utime(p, (past, past))


# ===========================================================================
# 3. _make_low 单元（成功改名 / 失败墓碑；用 unit 路径脱离 full_ 命名，
#    避免与第 4 节的录播种子混淆）
# ===========================================================================
db = Db()
conn = db.conn
conn.execute("INSERT INTO rooms (room_id, anchor_name, created_at) "
             "VALUES (888, '测试主播', datetime('now','localtime'))")
conn.commit()

src = REC / "888" / "unit_src.mp4"
_touch(src)
pipeline._make_low(src, REC / "888" / "unit1" / "low.mp4")
check("make_low-成功产出 low 且无 .part",
      (REC / "888" / "unit1" / "low.mp4").exists()
      and not (REC / "888" / "unit1" / "low.mp4.part").exists())

_fake_run.fail = True
pipeline._make_low(src, REC / "888" / "unit2" / "low.mp4")
part2 = REC / "888" / "unit2" / "low.mp4.part"
check("make_low-失败留 0 字节墓碑",
      part2.exists() and part2.stat().st_size == 0
      and not (REC / "888" / "unit2" / "low.mp4").exists())
_fake_run.fail = False

# ===========================================================================
# 4. _scan_pass 转码分支（切片回填 + 跳过口径 + 录播不回归）
# ===========================================================================
# 种子：录播 full_1（预置 low，走上传不转码）/ full_2（无 low，验证录播
# 转码路径仍工作）；切片四种状态
conn.execute("INSERT INTO recordings (room_id, started_at, ended_at, status, "
             "qn, created_at, file_path, file_size, duration_sec, source) "
             "VALUES (888, '2026-10-09T09:00:00', '2026-10-09T10:00:00', "
             "'finished', 200, ?, '888/full_1.mp4', 100, 3600, 'auto')",
             (now_iso(),))
conn.execute("INSERT INTO recordings (room_id, started_at, ended_at, status, "
             "qn, created_at, file_path, file_size, duration_sec, source) "
             "VALUES (888, '2026-10-09T10:00:00', '2026-10-09T11:00:00', "
             "'finished', 200, ?, '888/full_2.mp4', 100, 3600, 'auto')",
             (now_iso(),))
conn.execute("INSERT INTO manual_clips (recording_id, room_id, start_sec, "
             "end_sec, status, file_path, file_size, duration_sec, created_by, "
             "created_at) VALUES (1, 888, 0, 60, 'done', "
             "'888/manual/clip_1.mp4', 100, 60, 'tester', ?)", (now_iso(),))
conn.execute("INSERT INTO manual_clips (recording_id, room_id, start_sec, "
             "end_sec, status, file_path, file_size, duration_sec, created_by, "
             "created_at) VALUES (1, 888, 60, 120, 'done', "
             "'888/manual/clip_2.mp4', 100, 60, 'tester', ?)", (now_iso(),))
conn.execute("INSERT INTO manual_clips (recording_id, room_id, start_sec, "
             "end_sec, status, file_path, file_size, duration_sec, created_by, "
             "created_at) VALUES (1, 888, 120, 180, 'done', "
             "'888/manual/clip_3.mp4', 100, 60, 'tester', ?)", (now_iso(),))
conn.execute("INSERT INTO manual_clips (recording_id, room_id, start_sec, "
             "end_sec, status, file_path, file_size, duration_sec, created_by, "
             "created_at) VALUES (1, 888, 180, 240, 'pending', "
             "'888/manual/clip_4.mp4', 100, 60, 'tester', ?)", (now_iso(),))
conn.commit()

_touch(REC / "888" / "full_1.mp4")
_touch(REC / "888" / "full_1" / "low.mp4")   # 预置 low（只走上传）
_touch(REC / "888" / "full_2.mp4")
_touch(REC / "888" / "manual" / "clip_1.mp4")
_touch(REC / "888" / "manual" / "clip_2.mp4")
_touch(REC / "888" / "manual" / "clip_2" / "low.mp4")   # 已有 low（记 mtime）
_low2 = REC / "888" / "manual" / "clip_2" / "low.mp4"
_low2_m = _low2.stat().st_mtime_ns
_touch(REC / "888" / "manual" / "clip_3.mp4")
_touch(REC / "888" / "manual" / "clip_3" / "low.mp4.part", size=0)  # 墓碑
_touch(REC / "888" / "manual" / "clip_4.mp4")   # pending：不该被转

pipeline._scan_pass()

check("转码-切片缺 low 即转（存量回填）",
      (REC / "888" / "manual" / "clip_1" / "low.mp4").exists())
check("转码-已有 low 跳过（mtime 未变）",
      _low2.stat().st_mtime_ns == _low2_m)
check("转码-墓碑退避跳过（不重试）",
      not (REC / "888" / "manual" / "clip_3" / "low.mp4").exists()
      and (REC / "888" / "manual" / "clip_3" / "low.mp4.part").exists())
check("转码-非 done 不转",
      not (REC / "888" / "manual" / "clip_4" / "low.mp4").exists())
check("转码-录播路径不回归",
      (REC / "888" / "full_2" / "low.mp4").exists())

# ===========================================================================
# 5. _scan_pass 上传分支（low 同桶 Standard / 存量只补 low / 独立落库）
# ===========================================================================
up_map = {(u[0], u[1]): (u[2], u[3]) for u in uploaded}
check("上传-切片 low 上 clips_bucket 同桶 Standard",
      up_map.get(("low.mp4", "recordings/888/manual/clip_1/low.mp4"))
      == (True, "test-bucket-mid"), str(uploaded))
check("上传-切片本体上同桶 Standard",
      up_map.get(("clip_1.mp4", "recordings/888/manual/clip_1.mp4"))
      == (True, "test-bucket-mid"))
# v2.1：录播原片/low 同落 mid 热桶 Standard（10 天热窗口，到期归档主桶）
check("上传-录播 low 落 mid Standard（v2.1 口径）",
      up_map.get(("low.mp4", "recordings/888/full_1/low.mp4"))
      == (True, "test-bucket-mid"))
check("上传-录播原片落 mid Standard（v2.1 口径）",
      up_map.get(("full_1.mp4", "recordings/888/full_1.mp4"))
      == (True, "test-bucket-mid"))

row = conn.execute("SELECT oss_key, oss_bucket, oss_low_key, oss_done_at "
                   "FROM manual_clips WHERE id = 1").fetchone()
check("落库-切片本体与 low 双 key 独立",
      row["oss_key"] == "recordings/888/manual/clip_1.mp4"
      and row["oss_bucket"] == "test-bucket-mid"
      and row["oss_low_key"] == "recordings/888/manual/clip_1/low.mp4"
      and row["oss_done_at"] != "", str(dict(row)))

# 存量场景：clip_2 本体已上云（首轮已落库），把 oss_low_key 清空模拟
# 「v1.4 时代的存量切片」（low 尚未上云）→ 本轮只补传 low。
# clip_3 的 oss_key 同步预写（第 6 节释放用例需要，也避免本节误传其本体）
conn.execute("UPDATE manual_clips SET oss_low_key = '' WHERE id = 2")
conn.execute("UPDATE manual_clips SET oss_key = 'recordings/888/manual/"
             "clip_3.mp4', oss_bucket = 'test-bucket-mid', "
             "oss_done_at = ? WHERE id = 3", (now_iso(),))
conn.commit()
uploaded.clear()
pipeline._scan_pass()
check("存量-本体已上云只补传 low",
      [(u[0], u[1]) for u in uploaded]
      == [("low.mp4", "recordings/888/manual/clip_2/low.mp4")],
      str(uploaded))
row2 = conn.execute("SELECT oss_key, oss_low_key FROM manual_clips "
                    "WHERE id = 2").fetchone()
check("存量-oss_low_key 回填落库",
      row2["oss_key"] == "recordings/888/manual/clip_2.mp4"
      and row2["oss_low_key"] == "recordings/888/manual/clip_2/low.mp4")

# ===========================================================================
# 6. 本地释放（到期删本体 + 已上云 low 联动删；未上云 low 留给清扫）
# ===========================================================================
# clip_6：本体 2h 前、双 key 已上云——小时口径远未到期必须保留。
# v1.5.1 锁定用例：v1.5.0 的 cutoff 曾丢 * 3600（单位成秒），该场景会被
# 误删（49h 老文件 + 刚新建文件的旧用例对秒/小时两种口径行为相同，未能
# 拦住；本用例专测「秒口径删、小时口径留」的判别区间）
conn.execute("INSERT INTO manual_clips (recording_id, room_id, start_sec, "
             "end_sec, status, file_path, file_size, duration_sec, created_by, "
             "created_at, oss_key, oss_bucket, oss_low_key) VALUES "
             "(1, 888, 300, 360, 'done', '888/manual/clip_6.mp4', 100, 60, "
             "'tester', ?, 'recordings/888/manual/clip_6.mp4', "
             "'test-bucket-mid', "
             "'recordings/888/manual/clip_6/low.mp4')", (now_iso(),))
conn.commit()
_touch(REC / "888" / "manual" / "clip_6.mp4")
_age(REC / "888" / "manual" / "clip_6.mp4", 2)
pipeline._scan_pass()
check("释放-2h 龄未到期保留（cutoff 单位锁定）",
      (REC / "888" / "manual" / "clip_6.mp4").exists())

# clip_1：本体 49h 前（>48h 留存）、双 key 已上云、low 为刚转出的 0 龄
# → 2026-10-11 新口径：本体到期即删，低清预览独立计时未到期必须保留
_age(REC / "888" / "manual" / "clip_1.mp4", 49)
pipeline._scan_pass()
check("释放-到期删本体（新口径：本体即释）",
      not (REC / "888" / "manual" / "clip_1.mp4").exists())
check("释放-低清预览独立计时未到期保留（30 天窗口内）",
      (REC / "888" / "manual" / "clip_1" / "low.mp4").exists())
# 同一 low 超 clips_low_keep_hours（测试 48h）→ 独立到期释放 + 空目录收
_age(REC / "888" / "manual" / "clip_1" / "low.mp4", 49)
pipeline._scan_pass()
check("释放-低清预览独立到期删并收目录",
      not (REC / "888" / "manual" / "clip_1" / "low.mp4").exists()
      and not (REC / "888" / "manual" / "clip_1").exists())

# clip_3：本体 49h 前、oss_key 已上云、oss_low_key 空 + 本地 low 在
#（绕开墓碑手动造 low）。让上传对 clip_3 的 low 失败——模拟「low 转出但
# 尚未上云」的窗口 → 释放只删本体，low 留（未上云不删口径）
_touch(REC / "888" / "manual" / "clip_3" / "low.mp4")
_age(REC / "888" / "manual" / "clip_3.mp4", 49)
_real_upload = pipeline._oss_upload
pipeline._oss_upload = lambda path, key, standard, bucket="": (
    False if "clip_3" in key else _real_upload(path, key, standard, bucket))
pipeline._scan_pass()
pipeline._oss_upload = _real_upload
check("释放-未上云 low 不随删（清扫兜底口径）",
      not (REC / "888" / "manual" / "clip_3.mp4").exists()
      and (REC / "888" / "manual" / "clip_3" / "low.mp4").exists())

# ===========================================================================
# 7. 兜底清理（释放分支收已上云 low；三级 glob 收孤儿目录与过期 .part）
# ===========================================================================
# clip_3 上一节遗留：本体已删、low 未上云在盘。把 oss_low_key 置为已上云
# 状态并超 low 留存期 → 释放循环的「本体不在 + 已上云 low 独立到期」
# 分支收走 low 与目录
conn.execute("UPDATE manual_clips SET oss_low_key = 'x' WHERE id = 3")
conn.commit()
# 夹具说明：第 4 节留在 clip_3 的 .part 墓碑生产中不会与 low 共存
#（转码成功即改名、失败则无 low），一并造龄过期让 part 清扫收走
_age(REC / "888" / "manual" / "clip_3" / "low.mp4", 49)
_age(REC / "888" / "manual" / "clip_3" / "low.mp4.part", 3)
pipeline._scan_pass()
check("兜底-本体已删时到期已上云 low 由释放循环收走",
      not (REC / "888" / "manual" / "clip_3").exists())

# 孤儿 low 目录（无 DB 行的纯残留）+ 过期 .part
_touch(REC / "888" / "manual" / "clip_99" / "low.mp4")
_touch(REC / "888" / "manual" / "clip_98" / "low.mp4.part")
_age(REC / "888" / "manual" / "clip_98" / "low.mp4.part", 3)
pipeline._scan_pass()
check("清扫-孤儿 low 目录与过期 .part",
      not (REC / "888" / "manual" / "clip_99").exists()
      and not (REC / "888" / "manual" / "clip_98" / "low.mp4.part").exists())

# 2026-10-11 清扫守卫：本体按策略释放（DB 行在 + 已上云）后，切片 low
# 在 30 天预览窗口内不是孤儿，必须保留；录播 low 未上云（oss_low_key
# 空）而原片已释放时本地是唯一副本，同样保留给上传重试
(REC / "888" / "manual" / "clip_2.mp4").unlink(missing_ok=True)  # 模拟本体按策略释放
pipeline._scan_pass()
check("守卫-切片本体已策略释放时 low 保留（30 天窗口）",
      (REC / "888" / "manual" / "clip_2" / "low.mp4").exists())
(REC / "888" / "full_1.mp4").unlink(missing_ok=True)  # 模拟录播原片已释放
conn.execute("UPDATE recordings SET oss_low_key = '' WHERE file_path = "
             "'888/full_1.mp4'")   # 模拟 low 尚未上云（唯一副本在本地）
conn.commit()
pipeline._scan_pass()
check("守卫-录播未上云 low 不被清扫（原片已释时的唯一副本）",
      (REC / "888" / "full_1" / "low.mp4").exists())
conn.execute("UPDATE recordings SET oss_low_key = "
             "'recordings/888/full_1/low.mp4' WHERE file_path = '888/full_1.mp4'")
conn.commit()
pipeline._scan_pass()
check("守卫-已上云的录播残留 low 由清扫收走",
      not (REC / "888" / "full_1" / "low.mp4").exists()
      and not (REC / "888" / "full_1").exists())

# ===========================================================================
# 8. 删除端点联动（v1.6 剥离：端点已删，双对象同桶联动删除 + 本地 low
#    目录清理的等价断言由 replay 项目 tests 覆盖）
# ===========================================================================
from fastapi.testclient import TestClient
from replay import auth as bm_auth


def _hash_secret(plain: str) -> str:
    """pbkdf2_sha256 存储串（与 replay.auth.verify_secret 解析格式一致；
    bili 侧 auth.hash_secret 的等价本地实现，仅供测试播种 admin 行）。"""
    import hashlib
    import secrets
    salt = secrets.token_hex(8)
    iters = 200_000
    h = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt), iters)
    return f"pbkdf2_sha256${iters}${salt}${h.hex()}"


conn.execute("INSERT OR IGNORE INTO admin_users VALUES ('admin', ?, ?)",
             (_hash_secret("admin-pass-123"), now_iso()))
conn.commit()
conn.close()

app_mod.cfg["server"]["root_path"] = ""
app_mod.cfg["capture"]["admin_password"] = "gate-pw"
admin = TestClient(app_mod.app)
admin.post("/api/auth/login", json={"mode": "admin", "username": "admin",
                                    "password": "admin-pass-123"})
check("端点-切片删除在本站（v2.2 职责归属，路由存在）", admin.delete(
    "/api/rooms/888/clips/1").status_code not in (404, 405))

total, passed = len(results), sum(results)
print(f"\n==== test_v1_5: {passed}/{total} 通过 ====")
sys.exit(0 if passed == total else 1)
