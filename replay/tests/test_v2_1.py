# -*- coding: utf-8 -*-
"""v2.1 云存储与录制分段改版本地测试（v2.1 云存储方案）。

覆盖：
1. DB 迁移：recordings.oss_bucket / oss_low_bucket（he_clips 已随高能移除）；
2. 上云目标：录播原片+low 落 mid 热桶（Standard）且回填行级桶；
3. 10 天归档：到期行复制 mid→主桶(IA)、删 mid 副本、行翻回主桶；
   主桶已有副本跳过复制；未到期不动；
4. 零留存释放守卫：单 oss_key（low 未就绪）不删本地原片；双键齐备即删；
5. /oss-url 按行分派桶（切片 / 录播原片 / 录播 low / 回退主桶）；
6. 磁盘压力快路径：<5GB 时最早未上云片段优先上云→删分段；仍不足则对
   进行中行下发 rotate_requested；
7. 分段轮换：旧段尾分段进过渡目录、新行 recording 同 qn/source、旧行
   finalizing；起新段失败新行 failed 且上下文清空；
8. 桥接片：旧尾+新头拼接成片、source='bridge'、started_at=旧尾首段墙钟
   （归档序居中）、失败落 failed；
9. 打点切片回灌：源片不在本地且已上云 → 尝试回灌；未上云 → failed；
10. manager rotate_requested 孤儿行收尾（无实例持有）。

运行：uv run python tests/test_v2_1.py
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay import db as dbm
from replay.db import init_db, now_iso
from replay.capture import recording as rec_mod
from replay.capture.recording import Recorder, run_manual_clip
from replay.web import app as app_mod
from replay.web import pipeline

results = []
tmpdir = tempfile.mkdtemp(prefix="bili_v21_test_")
TMP_DB = Path(tmpdir) / "live.db"
TMP_ROOT = Path(tmpdir) / "recordings"


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


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


def make_rec(db, rid, status="finished", oss_key="", oss_low_key="",
             oss_bucket="", oss_low_bucket="", days_ago=0, with_segs=False,
             with_mp4=False):
    """造一条 recordings 行（含可选本地分段/成片）。"""
    import datetime as dt
    ts = (dt.datetime.now() - dt.timedelta(days=days_ago)
          ).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "INSERT INTO recordings (id, room_id, status, qn, file_path, "
        "file_size, duration_sec, created_at, started_at, ended_at, source, "
        "oss_key, oss_low_key, oss_bucket, oss_low_bucket, oss_done_at) "
        "VALUES (?,?,?,150,?,?,600,?,?,?,?,?,?,?,?,?)",
        (rid, 101, status,
         "101/full_%d.mp4" % rid if status != "recording" else "",
         1024, ts, ts, ts, "auto", oss_key, oss_low_key,
         oss_bucket, oss_low_bucket,
         ts if oss_key else ""))
    d = TMP_ROOT / "101" / ("full_%d" % rid)
    d.mkdir(parents=True, exist_ok=True)
    if with_segs:
        d.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            f = d / ("seg_20261012T1000%02d.ts" % (10 + i))
            f.write_bytes(b"x" * 16)
    if with_mp4:
        p = TMP_ROOT / "101" / ("full_%d.mp4" % rid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 64)
        import os
        old = time.time() - 86400
        os.utime(p, (old, old))
    return d


# ---- 打桩：OSS 客户端面（exists/copy/delete/upload）与磁盘 ----
OSS_STATE = {"objects": set(), "copies": [], "deletes": [], "uploads": []}


def fake_oss_exists(key, bucket=""):
    # 与真实 _oss_exists 同语义：空桶回退主桶（cfg 配置值）
    bucket = bucket or CFG["oss"].get("bucket") or "main-b"
    return (bucket, key) in OSS_STATE["objects"]


def fake_oss_copy(key, src_b, dst_b, storage_class=""):
    OSS_STATE["copies"].append((key, src_b, dst_b, storage_class))
    OSS_STATE["objects"].add((dst_b, key))
    return True


def fake_oss_delete(key, bucket=""):
    OSS_STATE["deletes"].append((bucket, key))
    OSS_STATE["objects"].discard((bucket, key))
    return None


def fake_oss_upload(path, key, standard, bucket=""):
    OSS_STATE["uploads"].append((str(path), key, standard, bucket))
    OSS_STATE["objects"].add((bucket or "main-b", key))
    return True


DU = {"free": 10 << 30}
_real_disk_usage = app_mod.shutil.disk_usage


def fake_disk_usage(_p):
    return SimpleNamespace(free=DU["free"])


# v2.2：管线函数住在 pipeline 模块（含其 shutil 引用）
for name, fake in (("_oss_exists", fake_oss_exists),
                   ("_oss_copy", fake_oss_copy),
                   ("_oss_delete", fake_oss_delete),
                   ("_oss_upload", fake_oss_upload)):
    setattr(pipeline, name, fake)
pipeline.shutil.disk_usage = fake_disk_usage
rec_mod.RECORDINGS_ROOT = TMP_ROOT
pipeline._clips_bucket = lambda: "mid-b"

# config：mid 桶 + 归档 10 天 + 零留存（记录原值，末尾恢复）
CFG = app_mod.cfg
ORIG = {k: dict(CFG.get(k, {})) for k in ("oss", "recording")}
CFG.setdefault("oss", {})
CFG["oss"].update({"enabled": True, "bucket": "main-b",
                   "access_key_id": "ak", "access_key_secret": "sk",
                   "clips_bucket": "mid-b", "rec_archive_after_days": 10,
                   "full_keep_hours": 0, "clips_low_keep_hours": 0,
                   "he_keep_hours": 0})
CFG.setdefault("recording", {})
CFG["recording"]["emergency_free_gb"] = 5.0
CFG["recording"]["bridge_min"] = 1
CFG["recording"]["segment_sec"] = 10
REAL_BUILD_BRIDGE = Recorder._build_bridge
REAL_FINALIZE_PART = Recorder._finalize_part
REC_CFG = rec_mod.get_config()
REC_CFG["recording"] = CFG["recording"]
REC_CFG["oss"] = CFG["oss"]


# =====================================================================
# 1. 迁移列
# =====================================================================
def test_columns():
    db = fresh_db()
    cols = [r[1] for r in db.query("PRAGMA table_info(recordings)")]
    check("recordings.oss_bucket/oss_low_bucket 列存在",
          "oss_bucket" in cols and "oss_low_bucket" in cols)


# =====================================================================
# 2-4. _scan_pass：上云目标 / 归档 / 释放守卫（经 OSS 桩 + 临时根）
# =====================================================================
def test_scan_pass():
    db = fresh_db()
    OSS_STATE.update(objects=set(), copies=[], deletes=[], uploads=[])
    # A 双键齐备、mid、11 天前 → 归档（主桶无副本 → copy+delete+翻行）
    OSS_STATE["objects"].add(("mid-b", "recordings/101/full_a_low"))
    make_rec(db, 201, oss_key="recordings/101/full_a.mp4",
             oss_low_key="recordings/101/full_a_low",
             oss_bucket="mid-b", oss_low_bucket="mid-b",
             days_ago=11, with_mp4=True)
    # B 仅原片键（low 未就绪）+ 本地原片 + 无 low 文件 → 不释放
    make_rec(db, 202, oss_key="recordings/101/full_b.mp4",
             days_ago=3, with_mp4=True)
    # C 双键、mid、3 天前 → 未到归档期，不动；本地已无文件 → 无事
    make_rec(db, 203, oss_key="recordings/101/full_c.mp4",
             oss_low_key="recordings/101/full_c_low",
             oss_bucket="mid-b", oss_low_bucket="mid-b", days_ago=3)
    # D 未上云 + 本地原片 + 本地 low 文件 → low 先传 mid（standard），
    # 原片同轮即传（齐传）；原片在盘是 low 免遭孤儿清扫的前提
    d = make_rec(db, 204, with_mp4=True)
    (d / "low.mp4").write_bytes(b"y" * 8)

    pipeline._scan_pass()

    # A：归档三步
    check("归档：mid→主桶复制（IA）",
          ("recordings/101/full_a.mp4", "mid-b", "main-b", "IA")
          in OSS_STATE["copies"], str(OSS_STATE["copies"]))
    check("归档：mid 副本删除",
          ("mid-b", "recordings/101/full_a.mp4") in OSS_STATE["deletes"])
    ra = db.query("SELECT oss_bucket FROM recordings WHERE id=201")[0]
    check("归档：行 oss_bucket 翻回主桶（''）", ra["oss_bucket"] == "")
    check("归档：双键齐备 → 本地原片零留存释放",
          not (TMP_ROOT / "101" / "full_201.mp4").exists())
    # B：low 未就绪不释放
    check("释放守卫：单 oss_key（low 未就绪）不删本地原片",
          (TMP_ROOT / "101" / "full_202.mp4").exists())
    # C：未到期不动
    check("归档：未到期行不复制",
          not any(c[0] == "recordings/101/full_c.mp4" for c in OSS_STATE["copies"]))
    rc = db.query("SELECT oss_bucket FROM recordings WHERE id=203")[0]
    check("归档：未到期行桶标记不变", rc["oss_bucket"] == "mid-b")
    # D：low 上传落 mid（standard=True）+ 回填 oss_low_bucket
    low_ups = [u for u in OSS_STATE["uploads"]
               if u[1] == "recordings/101/full_204/low.mp4"]
    check("上云目标：录播 low 落 mid 桶 Standard",
          bool(low_ups) and low_ups[0][2] is True and low_ups[0][3] == "mid-b",
          str(OSS_STATE["uploads"]))
    rd = db.query("SELECT oss_low_bucket FROM recordings WHERE id=204")[0]
    check("上云目标：oss_low_bucket 回填 mid", rd["oss_low_bucket"] == "mid-b")


def test_archive_skip_when_main_exists():
    """主桶已有副本（存量迁移行）→ 只删 mid 副本，不复制（不动主桶资源）。"""
    db = fresh_db()
    OSS_STATE.update(objects={("mid-b", "recordings/101/full_x.mp4"),
                              ("main-b", "recordings/101/full_x.mp4")},
                     copies=[], deletes=[])
    make_rec(db, 210, oss_key="recordings/101/full_x.mp4",
             oss_low_key="recordings/101/full_x_low",
             oss_bucket="mid-b", oss_low_bucket="mid-b", days_ago=11)
    pipeline._scan_pass()
    check("归档：主桶已有副本跳过复制",
          not any("full_x" in c[0] for c in OSS_STATE["copies"]))
    check("归档：主桶已有副本仍删 mid 副本",
          ("mid-b", "recordings/101/full_x.mp4") in OSS_STATE["deletes"])
    check("归档：主桶对象不被删除",
          ("main-b", "recordings/101/full_x.mp4")
          not in [(b, k) for b, k in OSS_STATE["deletes"]])


# =====================================================================
# 5. /oss-url 三表按行分派（直接调端点函数；request 未用）
# =====================================================================
def test_oss_url_dispatch():
    db = fresh_db()
    ORIG_EN = CFG["oss"].get("enabled"), CFG["oss"].get("bucket")
    CFG["oss"].update({"enabled": True, "bucket": "main-b",
                       "access_key_id": "ak", "access_key_secret": "sk"})
    db.execute(
        "INSERT INTO manual_clips (room_id, recording_id, status, title, "
        "start_sec, end_sec, file_path, file_size, created_at, oss_key, "
        "oss_bucket) VALUES (101, 1, 'done', 'c', 0, 0, "
        "'101/manual/clip_1.mp4', 1, ?, 'recordings/101/manual/clip_1.mp4', "
        "'mid-b')", (now_iso(),))
    make_rec(db, 220, oss_key="recordings/101/full_220.mp4",
             oss_low_key="recordings/101/full_220_low",
             oss_bucket="mid-b", oss_low_bucket="mid-b")
    make_rec(db, 221, oss_key="recordings/101/full_221.mp4",
             oss_low_key="recordings/101/full_221_low")   # 存量：'' = 主桶

    async def call(key):
        return await app_mod.oss_url(101, None, key=key)

    u = asyncio.run(call("recordings/101/manual/clip_1.mp4"))
    check("oss-url：切片按行 oss_bucket（mid）", "mid-b." in u["url"], u["url"])
    u = asyncio.run(call("recordings/101/full_220.mp4"))
    check("oss-url：录播原片热窗口 mid", "mid-b." in u["url"], u["url"])
    u = asyncio.run(call("recordings/101/full_220_low"))
    check("oss-url：录播 low 恒 mid（oss_low_bucket）",
          "mid-b." in u["url"], u["url"])
    u = asyncio.run(call("recordings/101/full_221.mp4"))
    check("oss-url：存量/归档行原片回退主桶", "main-b." in u["url"], u["url"])
    u = asyncio.run(call("recordings/101/manual/clip_9.mp4"))
    check("oss-url：无行命中回退主桶", "main-b." in u["url"], u["url"])

    CFG["oss"]["enabled"], CFG["oss"]["bucket"] = ORIG_EN


# =====================================================================
# 6. 磁盘压力快路径
# =====================================================================
def test_pressure_fastpath():
    db = fresh_db()
    OSS_STATE.update(objects=set(), uploads=[])
    DU["free"] = 10 << 30
    # 最早片段：已上云 + 滞留分段（拼接中断遗留）→ 清分段
    OSS_STATE["objects"].add(("mid-b", "recordings/101/full_p1.mp4"))
    make_rec(db, 230, oss_key="recordings/101/full_p1.mp4",
             oss_bucket="mid-b", days_ago=2, with_segs=True)
    # 次早片段：未上云 + 本地原片 + 分段 → 优先上云再清分段
    make_rec(db, 231, days_ago=1, with_segs=True, with_mp4=True)
    # 进行中行（轮换目标候选）
    make_rec(db, 232, status="recording")

    DU["free"] = 4 << 30          # < 5GB 触发
    pipeline._disk_pressure_fastpath()

    check("压力：未上云片段原片优先上云 mid",
          any(u[1] == "recordings/101/full_231.mp4" for u in OSS_STATE["uploads"]),
          str(OSS_STATE["uploads"]))
    up1 = db.query("SELECT oss_key, oss_bucket FROM recordings WHERE id=231")[0]
    check("压力：上云后回填行键与桶",
          up1["oss_key"] == "recordings/101/full_231.mp4"
          and up1["oss_bucket"] == "mid-b")
    segs_left = list((TMP_ROOT / "101" / "full_230").glob("seg_*.ts")) + \
        list((TMP_ROOT / "101" / "full_231").glob("seg_*.ts"))
    check("压力：最早两片段分段均已清理", not segs_left)
    check("压力：本地原片保留（low 未就绪，等零留存释放）",
          (TMP_ROOT / "101" / "full_231.mp4").exists())
    r = db.query("SELECT status FROM recordings WHERE id=232")[0]
    check("压力：仍不足 → 进行中行下发 rotate_requested",
          r["status"] == "rotate_requested")

    # 空间充足时不动作
    DU["free"] = 10 << 30
    OSS_STATE["uploads"] = []
    pipeline._disk_pressure_fastpath()
    check("压力：余量充足零动作", not OSS_STATE["uploads"])


# =====================================================================
# 7. 分段轮换（打桩 _spawn / _finalize_part / _build_bridge / _check_live）
# =====================================================================
class DummyClient:
    def __init__(self, db):
        self.room_id = 101
        self.manager = SimpleNamespace(db=db, http_session=None)


def make_recorder(db):
    r = Recorder(DummyClient(db))
    r.room_dir = TMP_ROOT / "101"
    r.room_dir.mkdir(parents=True, exist_ok=True)
    return r


def test_rotation():
    db = fresh_db()
    make_rec(db, 240, status="recording")
    r = make_recorder(db)
    r.full_rec_id = 240
    r._desired = "full"
    r.seg_dir = r.room_dir / "full_240"
    r.seg_dir.mkdir(parents=True, exist_ok=True)
    for i in range(8):
        (r.seg_dir / ("seg_20261012T1100%02d.ts" % i)).write_bytes(b"s" * 8)

    spawned = {"n": 0}
    fin_parts, bridges = [], []

    async def fake_spawn():
        spawned["n"] += 1
        r.proc = SimpleNamespace(pid=999, returncode=None,
                                 terminate=lambda: None)
        r._log_f = None

    async def noop():
        return None

    async def fake_live():
        return True

    r._spawn = fake_spawn
    r._terminate_proc = noop
    r._check_live = fake_live
    Recorder._finalize_part = lambda self, rid, status, reason="": fin_parts.append(
        (rid, status, reason)) or noop()
    Recorder._build_bridge = lambda self, *a, **k: bridges.append(a) or noop()

    asyncio.run(r._rotate_full("录制满 2 小时自动轮换"))

    check("轮换：新行 recording 且同 qn/source",
          bool(db.query("SELECT 1 FROM recordings WHERE status='recording' "
                        "AND qn=150 AND source='auto' AND id>240")))
    check("轮换：旧行 finalizing",
          db.query("SELECT status FROM recordings WHERE id=240")[0]["status"]
          == "finalizing")
    staging = r.room_dir / "bridge_240"
    staged = sorted(staging.glob("seg_*.ts")) if staging.exists() else []
    check("轮换：旧段尾 6 段（10 分钟）进过渡目录", len(staged) == 6,
          str(len(staged)))
    check("轮换：上下文切到新段", r.full_rec_id > 240
          and r.seg_dir.name == "full_%d" % r.full_rec_id)
    check("轮换：旧段收尾任务已排队（finished + 原因）",
          fin_parts and fin_parts[0][0] == 240 and fin_parts[0][1] == "finished")
    check("轮换：桥接任务已排队", bool(bridges))

    # ---- 起新段失败：新行 failed、上下文清空 ----
    db2 = fresh_db()
    make_rec(db2, 245, status="recording")
    r2 = make_recorder(db2)
    r2.full_rec_id, r2._desired = 245, "full"
    r2.seg_dir = r2.room_dir / "full_245"
    r2.seg_dir.mkdir(parents=True, exist_ok=True)
    (r2.seg_dir / "seg_20261012T120000.ts").write_bytes(b"s" * 8)

    async def bad_spawn():
        raise RuntimeError("取流失败")

    r2._spawn = bad_spawn
    r2._terminate_proc = noop
    Recorder._finalize_part = lambda self, *a, **k: noop()
    Recorder._build_bridge = lambda self, *a, **k: noop()
    asyncio.run(r2._rotate_full("轮换"))
    failed = db2.query("SELECT status FROM recordings WHERE id>245")
    check("轮换失败：新行 failed",
          bool(failed) and failed[0]["status"] == "failed")
    check("轮换失败：上下文清空（交对账循环重开）",
          r2.full_rec_id is None and r2._desired is None)


# =====================================================================
# 8. 桥接片构建（打桩 concat / ffprobe / sleep）
# =====================================================================
def test_bridge():
    db = fresh_db()
    r = make_recorder(db)
    staging = r.room_dir / "bridge_250"
    staging.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (staging / ("seg_20261012T1050%02d.ts" % i)).write_bytes(b"a" * 8)
    new_dir = r.room_dir / "full_251"
    new_dir.mkdir(parents=True, exist_ok=True)
    for i in range(8):
        (new_dir / ("seg_20261012T1100%02d.ts" % i)).write_bytes(b"b" * 8)

    async def fake_concat(files, out):
        out.write_bytes(b"c" * 100)
        return True

    Recorder._concat_remux = staticmethod(fake_concat)
    Recorder._ffprobe_duration = staticmethod(lambda mp4: 1200.0)
    Recorder._build_bridge = REAL_BUILD_BRIDGE
    Recorder._finalize_part = REAL_FINALIZE_PART

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        return None

    asyncio.sleep = fast_sleep
    try:
        asyncio.run(r._build_bridge(staging, 250, 251, 6, 10, 150))
    finally:
        asyncio.sleep = real_sleep

    rows = db.query("SELECT id, status, source, started_at, file_path, "
                    "reason FROM recordings WHERE source='bridge'")
    check("桥接：行 source=bridge 且 finished",
          bool(rows) and rows[0]["status"] == "finished", str(rows))
    if rows:
        check("桥接：started_at=旧尾首段墙钟（归档序居中）",
              rows[0]["started_at"] == "2026-10-12T10:50:00",
              rows[0]["started_at"])
        check("桥接：成片路径与常规段同构",
              rows[0]["file_path"].startswith("101/full_")
              and rows[0]["file_path"].endswith(".mp4"))
    check("桥接：过渡目录已清理", not staging.exists())

    # 素材不足（无新头）→ 跳过且清目录
    staging2 = r.room_dir / "bridge_260"
    staging2.mkdir(parents=True, exist_ok=True)
    (staging2 / "seg_20261012T130000.ts").write_bytes(b"a" * 8)
    asyncio.sleep = fast_sleep
    try:
        asyncio.run(r._build_bridge(staging2, 260, 261, 6, 10, 150))
    finally:
        asyncio.sleep = real_sleep
    check("桥接：素材不足跳过并清过渡目录",
          not staging2.exists()
          and not db.query("SELECT 1 FROM recordings WHERE source='bridge' "
                           "AND reason LIKE '%260%'"))


# =====================================================================
# 9. 打点切片回灌路径
# =====================================================================
def test_clip_restore():
    db = fresh_db()
    restore_calls = []

    async def fake_restore(d, rid):
        restore_calls.append(rid)
        return None   # 模拟未上云/回灌失败

    rec_mod._restore_recording_from_oss = fake_restore
    make_rec(db, 270, oss_key="recordings/101/full_270.mp4")  # 无本地文件
    db.execute(
        "INSERT INTO manual_clips (room_id, recording_id, status, title, "
        "start_sec, end_sec, file_path, file_size, created_at) "
        "VALUES (101, 270, 'pending', 't', 10, 40, '', 0, ?)", (now_iso(),))
    asyncio.run(run_manual_clip(db, 1))
    row = db.query("SELECT status, reason FROM manual_clips WHERE id=1")[0]
    check("切片回灌：源缺失时尝试回灌（按行 id）", restore_calls == [270])
    check("切片回灌：未上云/回灌失败 → failed",
          row["status"] == "failed" and "回灌" in row["reason"], str(dict(row)))

    # 已上云且回灌成功 → 走正常快切（ffmpeg 缺失环境下会 failed 于转码，
    # 但必须已越过「源缺失」这一关：回灌被再次调用）
    async def ok_restore(d, rid):
        restore_calls.append(rid)
        p = TMP_ROOT / "101" / "full_270.mp4"
        p.write_bytes(b"x" * 64)
        return p

    rec_mod._restore_recording_from_oss = ok_restore
    db.execute("UPDATE manual_clips SET status='pending', reason='' WHERE id=1")
    asyncio.run(run_manual_clip(db, 1))
    row = db.query("SELECT status, reason FROM manual_clips WHERE id=1")[0]
    check("切片回灌：回灌成功后进入快切流程",
          restore_calls[-1] == 270 and row["reason"] != "源成片文件缺失"
          and row["status"] in ("done", "failed"), str(dict(row)))


# =====================================================================
# 10. manager rotate_requested 孤儿行收尾
# =====================================================================
def test_manager_orphan_rotate():
    from replay.capture.manager import RoomManager
    db = fresh_db()
    make_rec(db, 280, status="rotate_requested", with_segs=False)
    mgr = object.__new__(RoomManager)
    mgr.db = db
    mgr.clients = {}
    asyncio.run(mgr._scan_recordings())
    row = db.query("SELECT status FROM recordings WHERE id=280")[0]
    check("rotate_requested 孤儿行：收尾 interrupted",
          row["status"] == "interrupted", str(dict(row)))


# =====================================================================
if __name__ == "__main__":
    for fn in (test_columns, test_scan_pass, test_archive_skip_when_main_exists,
               test_oss_url_dispatch,
               test_pressure_fastpath, test_rotation, test_bridge,
               test_clip_restore, test_manager_orphan_rotate):
        print(f"\n---- {fn.__name__} ----")
        fn()
    n_ok = sum(results)
    print(f"\n==== v2.1 测试：{n_ok}/{len(results)} 通过 ====")
    sys.exit(0 if n_ok == len(results) else 1)
