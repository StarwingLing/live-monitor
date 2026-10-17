# -*- coding: utf-8 -*-
"""v1.4 测试套：OOM 崩溃循环修复 + 切片独立桶（双 bucket）。

背景（2026-10-08 事故）：8.7GB 整场成片的 480p 转码在 web 进程内拉起
ffmpeg，撞单元 MemoryMax=200M 被 cgroup OOM 杀死 → systemd 判单元失败
快速重启 → worker 启动即扫再拉 ffmpeg → 13 秒一轮的崩溃紧循环（13 小时
重启 3000+ 次，low.mp4 永远转不出，整机 CPU 被拖满）。

修复三道防线：① MemoryMax 200M→500M；② worker 启动先睡满一个扫描周期
再扫（切断「快速重启 × 启动即扫」的紧循环）；③ 转码失败留 0 字节 .part
墓碑（2 小时过期才重试，配合既有清扫轮天然退避）。

双 bucket：手动切片上云改走 [oss].clips_bucket 独立桶（标准存储，高频
浏览/下载），录播原片/low/高能留主桶；manual_clips.oss_bucket 落库，
oss-url 签名与删除联动按行路由；空 = 主桶（v1.2 单桶行为，存量不迁移）。

运行：uv run python tests/test_v1_4.py
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay.db as bm_db

_tmpdir = tempfile.mkdtemp(prefix="bili_v14_")
bm_db.DB_PATH = Path(_tmpdir) / "test.db"
bm_db.DATA_DIR = Path(_tmpdir)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}"
          + (f"  {detail}" if detail and not ok else ""))


from replay.db import Db, init_db, now_iso, connect

# ===========================================================================
# 1. 防线①：部署单元 MemoryMax（v2.2 管线迁入 replay-web：500M→800M，
#    叠加本站投稿/回灌子进程口径；capture 单元另有 800M）
# ===========================================================================
unit = (Path(__file__).resolve().parent.parent / "deploy"
        / "replay-web.service").read_text(encoding="utf-8")
check("部署-MemoryMax=800M（200M 曾致 ffmpeg OOM 崩溃循环，v2.2 迁站后含投稿子进程余量）",
      "MemoryMax=800M" in unit)

# ===========================================================================
# 2. DB 迁移：manual_clips.oss_bucket
# ===========================================================================
init_db()
init_db()   # 幂等
conn = connect()
cols = {r[1] for r in conn.execute("PRAGMA table_info(manual_clips)")}
conn.close()
check("迁移-manual_clips.oss_bucket", "oss_bucket" in cols)

from replay.web import app as app_mod
from replay.web import pipeline

cfg = app_mod.cfg
cfg["server"]["root_path"] = ""
cfg["capture"]["admin_password"] = "gate-pw"   # 生产口径登录门

import replay.capture.recording as rec_mod
REC = Path(_tmpdir) / "recordings"
rec_mod.RECORDINGS_ROOT = REC


def _touch(p: Path, old_hours: float | None = None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake-mp4-bytes")
    if old_hours is not None:
        past = (dt.datetime.now() - dt.timedelta(hours=old_hours)).timestamp()
        os.utime(p, (past, past))


# ===========================================================================
# 3. 防线②：worker 启动先睡满一个扫描周期再扫
# ===========================================================================
class _Stop(Exception):
    pass


order: list[tuple] = []
_real_sleep = app_mod.time.sleep
_real_scan = pipeline._scan_pass


def _fake_sleep(s):
    order.append(("sleep", s))
    raise _Stop()


pipeline._scan_pass = lambda: order.append(("scan", None))
app_mod.time.sleep = _fake_sleep
try:
    pipeline._low_quality_worker()
except _Stop:
    pass
finally:
    app_mod.time.sleep = _real_sleep
    pipeline._scan_pass = _real_scan
check("worker-启动先睡满周期再扫（被中断时未发生任何扫描）",
      order and order[0][0] == "sleep"
      and ("scan", None) not in order, str(order))

# ===========================================================================
# 4. 防线③：转码失败墓碑（0 字节 .part 挡住重试，2h 过期后清扫放行）
# ===========================================================================
conn = connect()
conn.execute("INSERT INTO rooms (room_id, anchor_name, created_at) "
             "VALUES (777, '测试主播', datetime('now','localtime'))")
conn.execute(
    "INSERT INTO recordings (room_id, started_at, ended_at, status, qn, "
    "created_at, file_path, file_size, duration_sec, source) VALUES "
    "(777, '2026-10-08T19:00:00', '2026-10-08T22:00:00', 'finished', 150, ?, "
    "'777/full_9.mp4', 1000, 3600, 'manual')", (now_iso(),))
conn.commit()
conn.close()
_touch(REC / "777" / "full_9.mp4")

ffmpeg_calls: list[list] = []
_real_run = pipeline.subprocess.run


def _fail_run(cmd, **kw):
    ffmpeg_calls.append(cmd)

    class _P:
        returncode = 1
    return _P()


def _ok_run(cmd, **kw):
    ffmpeg_calls.append(cmd)
    Path(cmd[-1]).write_bytes(b"low-bytes")   # 模拟 ffmpeg 写出 .part

    class _P:
        returncode = 0
    return _P()


part = REC / "777" / "full_9" / "low.mp4.part"
dst = REC / "777" / "full_9" / "low.mp4"

pipeline.subprocess.run = _fail_run
try:
    pipeline._scan_pass()
finally:
    pipeline.subprocess.run = _real_run
check("墓碑-失败留 0 字节 .part", part.exists() and part.stat().st_size == 0)
check("墓碑-ffmpeg 恰执行一次", len(ffmpeg_calls) == 1)

pipeline.subprocess.run = _fail_run
try:
    pipeline._scan_pass()
finally:
    pipeline.subprocess.run = _real_run
check("墓碑-新鲜期内不再重试", len(ffmpeg_calls) == 1 and part.exists())

_past = (dt.datetime.now() - dt.timedelta(hours=3)).timestamp()
os.utime(part, (_past, _past))
pipeline.subprocess.run = _fail_run
try:
    pipeline._scan_pass()
finally:
    pipeline.subprocess.run = _real_run
check("墓碑-过期（>2h）后清扫放行并重试", len(ffmpeg_calls) == 2
      and part.exists() and part.stat().st_size == 0)

os.utime(part, (_past, _past))
pipeline.subprocess.run = _ok_run
try:
    pipeline._scan_pass()
finally:
    pipeline.subprocess.run = _real_run
check("转码-成功后 .part 原子替换为 low.mp4",
      dst.exists() and not part.exists() and len(ffmpeg_calls) == 3)

# ===========================================================================
# 5. 双 bucket：_clips_bucket 解析 + _scan_pass 上传路由 + oss_bucket 落库
# ===========================================================================
cfg["oss"].update({"enabled": True, "bucket": "test-bucket",
                   "clips_bucket": "test-bucket-mid",
                   "access_key_id": "ak-test",
                   "access_key_secret": "sk-test"})
check("双桶-配置了独立桶", pipeline._clips_bucket()
      == "test-bucket-mid")
cfg["oss"]["clips_bucket"] = ""
check("双桶-未配置回落主桶", pipeline._clips_bucket()
      == "test-bucket")
cfg["oss"]["clips_bucket"] = "test-bucket-mid"

conn = connect()
conn.execute(
    "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
    "end_sec, file_path, file_size, duration_sec, status, created_by, "
    "created_at) VALUES (1, 777, '新切片', 0, 60, '777/clip_9.mp4', 100, 60, "
    "'done', 'tester', ?)", (now_iso(),))
conn.commit()
conn.close()
_touch(REC / "777" / "clip_9.mp4")

up_calls: list[tuple] = []


def fake_upload(path, key, standard, bucket=""):
    up_calls.append((key, standard, bucket))
    return True


pipeline._oss_upload = fake_upload
pipeline._scan_pass()
got = {k: (s, b) for k, s, b in up_calls}
check("上云-切片走独立桶(Standard)",
      got.get("recordings/777/clip_9.mp4")
      == (True, "test-bucket-mid"), str(up_calls))
check("上云-原片落 mid 热桶(Standard·v2.1)",
      got.get("recordings/777/full_9.mp4") == (True, "test-bucket-mid"))
check("上云-low 落 mid 热桶(Standard·v2.1)",
      got.get("recordings/777/full_9/low.mp4") == (True, "test-bucket-mid"))
conn = connect()
row = conn.execute("SELECT oss_key, oss_bucket FROM manual_clips "
                   "WHERE file_path='777/clip_9.mp4'").fetchone()
conn.close()
check("落库-切片 oss_bucket=独立桶",
      row["oss_key"] == "recordings/777/clip_9.mp4"
      and row["oss_bucket"] == "test-bucket-mid")

# 回退场景：clips_bucket 未配置 → 切片也上主桶（v1.2 行为）
cfg["oss"]["clips_bucket"] = ""
conn = connect()
conn.execute(
    "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
    "end_sec, file_path, file_size, duration_sec, status, created_by, "
    "created_at) VALUES (1, 777, '回退切片', 0, 60, '777/clip_10.mp4', 100, "
    "60, 'done', 'tester', ?)", (now_iso(),))
conn.commit()
conn.close()
_touch(REC / "777" / "clip_10.mp4")
up_calls.clear()
pipeline._scan_pass()
check("上云-未配置独立桶时切片回落主桶",
      ("recordings/777/clip_10.mp4", True, "test-bucket")
      in up_calls, str(up_calls))

# ===========================================================================
# 6. oss-url 双桶签名（TestClient + 真 SDK 离线 presign；同 test_v0_12 §8）
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


conn = connect()
conn.execute("INSERT OR IGNORE INTO admin_users VALUES ('admin', ?, ?)",
             (_hash_secret("admin-pass-123"), now_iso()))
conn.commit()
conn.close()
admin = TestClient(app_mod.app)
admin.post("/api/auth/login", json={"mode": "admin", "username": "admin",
                                    "password": "admin-pass-123"})

r = admin.get("/api/rooms/777/oss-url?key=recordings/777/clip_9.mp4")
check("签名-切片按行 oss_bucket 签独立桶", r.status_code == 200
      and "test-bucket-mid.oss-cn-hangzhou.aliyuncs.com"
      in r.json().get("url", ""), r.text[:200])
r = admin.get("/api/rooms/777/oss-url?key=recordings/777/clip_10.mp4")
check("签名-主桶落库的切片签主桶", r.status_code == 200
      and "test-bucket.oss-cn-hangzhou.aliyuncs.com"
      in r.json().get("url", "")
      and "test-bucket-mid" not in r.json().get("url", ""))
r = admin.get("/api/rooms/777/oss-url?key=recordings/777/full_9.mp4")
check("签名-录播原片按行签 mid（v2.1）", r.status_code == 200
      and "test-bucket-mid.oss-cn-hangzhou.aliyuncs.com"
      in r.json().get("url", ""), str(r.json()))
r = admin.get("/api/rooms/777/oss-url?key=recordings/777/full_9/low.mp4")
check("签名-low 副本按行签 mid（v2.1）", r.status_code == 200
      and "test-bucket-mid.oss-cn-hangzhou.aliyuncs.com"
      in r.json().get("url", ""), str(r.json()))

# ===========================================================================
# 7. 删除端点归属（v2.2 职责翻转：删除动线在本站；按行 oss_bucket 路由
#    的云端联动全场景断言见 test_replay_api / test_v2_0 系）
# ===========================================================================
check("删除-切片/录播端点在本站（路由存在且鉴权可达）", admin.delete(
    "/api/rooms/777/clips/1").status_code not in (401, 404, 405))

# ===========================================================================
# 汇总
# ===========================================================================
n_fail = sum(1 for x in results if not x)
print(f"\nv1.4 测试：{len(results) - n_fail}/{len(results)} 通过")
sys.exit(1 if n_fail else 0)
