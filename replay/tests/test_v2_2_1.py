# -*- coding: utf-8 -*-
"""replay v2.2.1 新增件测试：回灌留存窗口独立计时（用户 2026-10-13 定稿）。

背景：录播零留存（full_keep_hours=0，录制+low 转码+双上云完毕即释本地）
之下，切片动线从 OSS 回灌的原片原先活不过一轮扫描。v2.2.1 给 recordings
加 restored_at 标记：回灌即写，释放分支对标记行按 [oss].restore_keep_hours
（默认 24h）独立评估，与 full_keep_hours 互不干扰。

覆盖：
1. DB 迁移：recordings.restored_at 列存在（新建库）；config 默认值 24；
2. _scan_pass 释放分支三分支（full_keep_hours=0）：
   - 无标记行 → 维持零留存即释（回归）；
   - 标记行窗口内（mtime=now）→ 原片+low 均保留；
   - 标记行窗口外（mtime/restored_at=25h 前）→ 释放 + restored_at 清空 +
     联动删本地 low；
3. restore.py 写标记：_mark_restored 直写；start() 幂等分支（文件已在
   本地）补写标记；完整下载路径（mock _run_download）落位后写标记。

运行：uv run python tests/test_v2_2_1.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay.db as dbm
from replay.db import init_db, now_iso
from replay.config import _DEFAULTS
from replay.capture import recording as rec_mod
from replay.web import app as app_mod
from replay.web import pipeline
from replay.web import restore as restore_mod

results = []
tmpdir = tempfile.mkdtemp(prefix="replay_v221_test_")
TMP_DB = Path(tmpdir) / "live.db"
TMP_ROOT = Path(tmpdir) / "recordings"


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}"
          + (f"  {detail}" if detail and not ok else ""))


CUR_DB = None


def fresh_db():
    global CUR_DB
    if CUR_DB is not None:
        try:
            CUR_DB._conn.close()   # Windows：旧连接不关会锁文件
        except Exception:
            pass
    if TMP_DB.exists():
        TMP_DB.unlink()
    init_db(TMP_DB)
    dbm.DB_PATH = TMP_DB
    CUR_DB = dbm.Db(TMP_DB)
    CUR_DB.execute("INSERT OR IGNORE INTO rooms (room_id, enabled, created_at) "
                   "VALUES (101, 1, ?)", (now_iso(),))
    return CUR_DB


def make_rec(db, rid, restored_at="", mtime_age_h=24.0):
    """造一条双键齐备（已上云）的终态 recordings 行 + 本地原片/low。

    mtime_age_h：本地原片的 mtime 距今小时数（释放判定基准）。"""
    import datetime as dt
    ts = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    rel = "101/full_%d.mp4" % rid
    db.execute(
        "INSERT INTO recordings (id, room_id, status, qn, file_path, "
        "file_size, duration_sec, created_at, started_at, ended_at, source, "
        "oss_key, oss_low_key, oss_bucket, oss_low_bucket, oss_done_at, "
        "restored_at) VALUES (?,?,?,150,?,1024,600,?,?,?,'auto',?,?,?,?,?,?)",
        (rid, 101, "finished", rel, ts, ts, ts,
         "recordings/101/full_%d.mp4" % rid,
         "recordings/101/full_%d/low.mp4" % rid,
         "mid-b", "mid-b", ts, restored_at))
    src = TMP_ROOT / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 64)
    low_dir = TMP_ROOT / ("101/full_%d" % rid)
    low_dir.mkdir(parents=True, exist_ok=True)
    (low_dir / "low.mp4").write_bytes(b"y" * 8)
    old = time.time() - mtime_age_h * 3600
    os.utime(src, (old, old))
    return src


# ---- 打桩：OSS 面与磁盘（与 test_v2_1 同款；释放路径只读对象不真删云）----
OSS_STATE = {"objects": set()}


def fake_oss_exists(key, bucket=""):
    bucket = bucket or CFG["oss"].get("bucket") or "main-b"
    return (bucket, key) in OSS_STATE["objects"]


def fake_oss_copy(key, src_b, dst_b, storage_class=""):
    OSS_STATE["objects"].add((dst_b, key))
    return True


def fake_oss_delete(key, bucket=""):
    OSS_STATE["objects"].discard((bucket, key))
    return None


def fake_oss_upload(path, key, standard, bucket=""):
    OSS_STATE["objects"].add((bucket or "main-b", key))
    return True


def fake_disk_usage(_p):
    return SimpleNamespace(free=10 << 30)


for name, fake in (("_oss_exists", fake_oss_exists),
                   ("_oss_copy", fake_oss_copy),
                   ("_oss_delete", fake_oss_delete),
                   ("_oss_upload", fake_oss_upload)):
    setattr(pipeline, name, fake)
pipeline.shutil.disk_usage = fake_disk_usage
rec_mod.RECORDINGS_ROOT = TMP_ROOT
restore_mod.RECORDINGS_ROOT = TMP_ROOT
pipeline._clips_bucket = lambda: "mid-b"

CFG = app_mod.cfg
ORIG_OSS = dict(CFG.get("oss", {}))
CFG.setdefault("oss", {})
CFG["oss"].update({"enabled": True, "bucket": "main-b",
                   "access_key_id": "ak", "access_key_secret": "sk",
                   "clips_bucket": "mid-b", "rec_archive_after_days": 10,
                   "full_keep_hours": 0, "clips_keep_hours": 0,
                   "clips_low_keep_hours": 0,
                   "restore_keep_hours": 24})


# =====================================================================
# 1. 迁移列与配置默认值
# =====================================================================
def test_columns_and_defaults():
    db = fresh_db()
    cols = [r[1] for r in db.query("PRAGMA table_info(recordings)")]
    check("recordings.restored_at 列存在（新建库）", "restored_at" in cols)
    check("config 默认 restore_keep_hours = 24",
          _DEFAULTS["oss"].get("restore_keep_hours") == 24)


# =====================================================================
# 2. _scan_pass 释放分支：回灌窗口独立计时（full_keep_hours=0）
# =====================================================================
def test_scan_release():
    db = fresh_db()
    OSS_STATE.update(objects=set())
    # A 无标记 + mtime 1 天前 → 零留存即释（回归：改版不破坏原语义）
    make_rec(db, 301)
    # B 标记 + 刚回灌（mtime=now）→ 24h 窗口内，原片与 low 均保留
    make_rec(db, 302, restored_at=now_iso(), mtime_age_h=0.0)
    # C 标记 + 25h 前 → 窗口外，释放 + 标记清空 + 联动删 low
    import datetime as dt
    stale = (dt.datetime.now() - dt.timedelta(hours=25)
             ).strftime("%Y-%m-%dT%H:%M:%S")
    make_rec(db, 303, restored_at=stale, mtime_age_h=25.0)

    pipeline._scan_pass()

    check("无标记行：零留存即释（回归）",
          not (TMP_ROOT / "101" / "full_301.mp4").exists())
    check("标记行窗口内：本地原片保留",
          (TMP_ROOT / "101" / "full_302.mp4").exists())
    check("标记行窗口内：本地 low 联动保留",
          (TMP_ROOT / "101" / "full_302" / "low.mp4").exists())
    check("标记行窗口外：原片释放",
          not (TMP_ROOT / "101" / "full_303.mp4").exists())
    check("标记行窗口外：本地 low 联动删除",
          not (TMP_ROOT / "101" / "full_303" / "low.mp4").exists())
    r303 = db.query("SELECT restored_at FROM recordings WHERE id=303")[0]
    check("释放后 restored_at 清空", r303["restored_at"] == "",
          str(r303["restored_at"]))
    r302 = db.query("SELECT restored_at FROM recordings WHERE id=302")[0]
    check("窗口内行 restored_at 保持", r302["restored_at"] != "")


# =====================================================================
# 3. restore.py 写标记
# =====================================================================
def test_restore_marks():
    db = fresh_db()
    make_rec(db, 401, mtime_age_h=48.0)          # 本地已有（模拟待释放态）

    # 3a. start() 幂等分支（文件已在本地）→ 补写标记
    row = db.query("SELECT id, file_path, oss_key FROM recordings "
                   "WHERE id=401")[0]
    restore_mod.TASKS.clear()
    snap = restore_mod.start(dict(row))
    r = db.query("SELECT restored_at FROM recordings WHERE id=401")[0]
    check("start() 幂等分支：任务直接 done", snap["status"] == "done")
    check("start() 幂等分支：restored_at 已写", r["restored_at"] != "")

    # 3b. _mark_restored 直写 + 手动清空回位
    restore_mod._mark_restored(401)
    r = db.query("SELECT restored_at FROM recordings WHERE id=401")[0]
    check("_mark_restored：写入成功", r["restored_at"] != "")

    # 3c. 完整下载路径（mock _run_download）→ 落位 + mtime 重置 + 写标记
    make_rec(db, 402, mtime_age_h=48.0)
    (TMP_ROOT / "101" / "full_402.mp4").unlink()   # 造「本地无原件」态
    row2 = dict(db.query("SELECT id, file_path, oss_key FROM recordings "
                         "WHERE id=402")[0])

    def fake_download(r_, task):
        data = b"z" * 128
        part = TMP_ROOT / (r_["file_path"] + ".restore-part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(data)
        task["total"] = len(data)
        task["got"] = len(data)
        return len(data)

    real_dl = restore_mod._run_download
    restore_mod._run_download = fake_download
    try:
        t = restore_mod.start(row2)
        deadline = time.time() + 5
        while (t := restore_mod.status(402))["status"] == "running" \
                and time.time() < deadline:
            time.sleep(0.02)
    finally:
        restore_mod._run_download = real_dl
    f = TMP_ROOT / "101" / "full_402.mp4"
    r2 = db.query("SELECT restored_at FROM recordings WHERE id=402")[0]
    check("下载路径：任务 done", t["status"] == "done", str(t))
    check("下载路径：原片落位且内容一致", f.read_bytes() == b"z" * 128)
    check("下载路径：mtime 已重置为回灌时刻",
          time.time() - f.stat().st_mtime < 60)
    check("下载路径：restored_at 已写", r2["restored_at"] != "")


if __name__ == "__main__":
    test_columns_and_defaults()
    test_scan_release()
    test_restore_marks()
    print(f"\n共 {len(results)} 项：通过 {sum(results)} / 失败 "
          f"{len(results) - sum(results)}")
    sys.exit(0 if all(results) else 1)
