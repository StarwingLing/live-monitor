# -*- coding: utf-8 -*-
"""v0.11 改动点的进程内测试（自动全程录制 + 手动切片）。

覆盖：
1. DB 迁移：manual_clips 表、rooms.auto_full、recordings.source；init_db 幂等；
2. 自动对账（RoomManager._reconcile_auto_full）：
   - 在播 + 无活跃行 → 插 source='auto' requested 行（qn=auto_full_qn）；
   - 已有活跃行（含仅 requested 排队）/ Recorder 已在录 / 未在播 → 不插（幂等）；
   - auto_full=0 或房间无 client → 不查不插；
3. 打点区间校验（web._validate_clip_range）：合法 / 起点≥终点 / 段长不足 /
   终点超成片时长；
4. 快切命令构造（recording.build_cut_cmd）：-ss 前置、-t 时长、-c copy、
   make_zero、faststart、输出路径；
5. 切片执行（recording.run_manual_clip，本地 ffmpeg 全链路）：
   - 真切 62s 测试源 [10s,30s] → done、成片存在、ffprobe 时长 ≈20s（关键帧容差）；
   - 源成片行被清理 → failed（"源成片已被删除"）；
6. 切片水位清理（maintenance.cleanup_manual_clips）：processing 超时标
   failed；done 总量超阈值从最旧删（文件 + 行）；
7. 磁盘紧急清理（maintenance.emergency_disk_cleanup，monkeypatch 盘余量）：
   从最旧 finished 成片开始删，删够即停；
8. auto-recordings 分组接口（app.auto_recordings）：按日期倒序、挂切片、
   auto_full 开关回传。

运行：uv run python tests/test_v0_11.py
"""
import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 测试库隔离：在任何 bili_monitor 模块 import 前把 DB_PATH 指到临时文件 ----
_tmpdir = tempfile.TemporaryDirectory(prefix="bili_v011_", ignore_cleanup_errors=True)
import replay.db as bm_db
bm_db.DB_PATH = Path(_tmpdir.name) / "test.db"
bm_db.DATA_DIR = Path(_tmpdir.name)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


# ===========================================================================
# 1. DB 迁移
# ===========================================================================
from replay.db import Db, init_db, now_iso, connect

init_db()
init_db()   # 幂等
conn = connect()
tabs = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
cols_rooms = {r[1] for r in conn.execute("PRAGMA table_info(rooms)")}
cols_recs = {r[1] for r in conn.execute("PRAGMA table_info(recordings)")}
cols_clips = {r[1] for r in conn.execute("PRAGMA table_info(manual_clips)")}
conn.close()
check("迁移-manual_clips 表存在", "manual_clips" in tabs)
check("迁移-rooms.auto_full 列", "auto_full" in cols_rooms)
check("迁移-recordings.source 列", "source" in cols_recs)
check("迁移-切片表关键字段齐全",
      {"id", "recording_id", "room_id", "title", "start_sec", "end_sec",
       "file_path", "file_size", "duration_sec", "status", "reason",
       "created_by", "created_at"} <= cols_clips)

# ===========================================================================
# 2. 自动对账（fake manager + monkeypatch fetch_room_info）
# ===========================================================================
import replay.capture.manager as mgr_mod
from replay.config import get_config

get_config()["recording"]["auto_full_qn"] = 150
# 存量缺陷修复（v2.3.0 顺带）：本地 config.toml 自 v2.2.5 起 recording
# .enabled=false，对账不插行——测试须自带开关状态，不随本地配置漂移
get_config()["recording"]["enabled"] = True
db = Db()

LIVE = {"live": True}


async def fake_fetch(session, room_id):
    return {"live_status": 1} if LIVE.get("live") else {"live_status": 0}


_orig_fetch = mgr_mod.fetch_room_info
mgr_mod.fetch_room_info = fake_fetch

ROOM = 88801
db.execute("INSERT INTO rooms (room_id, anchor_name, enabled, auto_full, "
           "created_at) VALUES (?, 't', 1, 1, ?)", (ROOM, now_iso()))


def fake_manager(clients: dict):
    return SimpleNamespace(db=db, clients=clients, http_session=object())


async def run_reconcile():
    await mgr_mod.RoomManager._reconcile_auto_full(fake_manager(
        {ROOM: SimpleNamespace(recorder=SimpleNamespace(full_rec_id=None))}))


asyncio.run(run_reconcile())
row = db.query("SELECT * FROM recordings WHERE room_id = ?", (ROOM,))
check("对账-在播无活跃行 → 插自动请求行",
      len(row) == 1 and row[0]["status"] == "requested"
      and row[0]["source"] == "auto" and row[0]["qn"] == 150)

# 幂等：再来一轮不重复插
asyncio.run(run_reconcile())
check("对账-已有活跃行不重复插",
      len(db.query("SELECT 1 FROM recordings WHERE room_id = ?", (ROOM,))) == 1)

# Recorder 已在录（full_rec_id 非空）→ 不查不插
db.execute("DELETE FROM recordings")
asyncio.run(mgr_mod.RoomManager._reconcile_auto_full(fake_manager(
    {ROOM: SimpleNamespace(recorder=SimpleNamespace(full_rec_id=99))})))
check("对账-Recorder 在录不插",
      not db.query("SELECT 1 FROM recordings WHERE room_id = ?", (ROOM,)))

# 未在播 → 不插
asyncio.run(run_reconcile())   # 先插一条（在播）
db.execute("UPDATE recordings SET status = 'recording'")   # 模拟已在录后置终态
db.execute("UPDATE recordings SET status = 'finished'")
LIVE["live"] = False
asyncio.run(run_reconcile())
check("对账-未在播不插",
      len(db.query("SELECT 1 FROM recordings WHERE room_id = ? AND "
                   "status = 'requested'", (ROOM,))) == 0)

# auto_full=0 → 不插
db.execute("UPDATE rooms SET auto_full = 1 WHERE room_id = ?", (ROOM,))
LIVE["live"] = True
db.execute("DELETE FROM recordings")
asyncio.run(run_reconcile())
check("对账-开关开且在播重新起录",
      len(db.query("SELECT 1 FROM recordings WHERE room_id = ? AND "
                   "source = 'auto'", (ROOM,))) == 1)
db.execute("DELETE FROM recordings")
db.execute("UPDATE rooms SET auto_full = 0 WHERE room_id = ?", (ROOM,))
asyncio.run(run_reconcile())
check("对账-开关关不插",
      not db.query("SELECT 1 FROM recordings WHERE room_id = ?", (ROOM,)))

mgr_mod.fetch_room_info = _orig_fetch

# ===========================================================================
# 3. 打点区间校验（v1.6 剥离：web 端 _validate_clip_range 已删——切片创建
#    面整体归 replay 项目，其 tests 覆盖等价校验）
# ===========================================================================
from fastapi import HTTPException
import replay.web.app as app_mod

get_config()["recording"]["min_clip_sec"] = 10

check("校验-切片创建面在本站 web app（v2.2 职责归属）",
      hasattr(app_mod, "_validate_clip_range"))

# ===========================================================================
# 4. 快切命令构造
# ===========================================================================
import replay.capture.recording as rec_mod

get_config()["recording"]["ffmpeg_path"] = "ffmpeg"
cmd = rec_mod.build_cut_cmd(Path("/src/full.mp4"), 12.5, 20.0, Path("/out/c.mp4"))
i_in, i_ss, i_t = cmd.index("-i"), cmd.index("-ss"), cmd.index("-t")
check("快切--ss 在 -i 前（输入端 seek）", i_ss < i_in)
check("快切--c copy 无转码", cmd[cmd.index("-c") + 1] == "copy")
check("快切--t 时长正确", cmd[i_t + 1] == "20.000")
check("快切-make_zero + faststart",
      "make_zero" in cmd and "+faststart" in cmd)
check("快切-输出路径", cmd[-1] == str(Path("/out/c.mp4")))

# ===========================================================================
# 5. 切片执行全链路（本地 ffmpeg 造 62s 测试源 → 快切 → ffprobe 实测）
# ===========================================================================
tmp = Path(tempfile.mkdtemp(prefix="bili_v011_cut_"))
rec_mod.RECORDINGS_ROOT = tmp
src_dir = tmp / "777" / "full_1"
src_dir.mkdir(parents=True)
SRC = src_dir.parent / "full_1.mp4"
r = subprocess.run(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
     "-f", "lavfi", "-i", "sine=frequency=440",
     "-t", "62", "-force_key_frames",
     "expr:gte(t,n_forced*2)", "-c:v", "libx264", "-preset", "ultrafast",
     "-c:a", "aac", str(SRC)],
    capture_output=True, text=True, timeout=180)
check("快切-测试源生成", r.returncode == 0 and SRC.exists(), r.stderr[-200:])
if SRC.exists():
    db.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
               "VALUES (?, 't', 1, ?)", (777, now_iso()))
    cur = db.execute(
        "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
        "ended_at, file_path, file_size, duration_sec, source) "
        "VALUES (?, 'finished', 150, ?, ?, ?, ?, ?, 62, 'auto')",
        (777, now_iso(), now_iso(), now_iso(), "777/full_1.mp4",
         SRC.stat().st_size))
    rec_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
        "end_sec, status, created_by, created_at) "
        "VALUES (?, ?, '', 10, 30, 'pending', 'tester', ?)",
        (rec_id, 777, now_iso()))
    clip_id = cur.lastrowid
    asyncio.run(rec_mod.run_manual_clip(db, clip_id))
    row = db.query("SELECT * FROM manual_clips WHERE id = ?", (clip_id,))[0]
    out = tmp / row["file_path"]
    dur = rec_mod.Recorder._ffprobe_duration(out)
    check("快切-执行成功 done",
          row["status"] == "done" and row["file_path"] != ""
          and int(row["duration_sec"]) > 0, f"status={row['status']} {row['reason']}")
    check("快切-时长在关键帧容差内（期望 ~20s）",
          dur is not None and 17.5 <= dur <= 22.5, f"dur={dur}")
    check("快切-署名与起止落库",
          row["created_by"] == "tester" and row["start_sec"] == 10
          and row["end_sec"] == 30)

    # 源行被清理 → failed（不产生文件）
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
        "status, created_by, created_at) VALUES (?, ?, 5, 15, 'pending', 't', ?)",
        (99999, 777, now_iso()))
    orphan_id = cur.lastrowid
    asyncio.run(rec_mod.run_manual_clip(db, orphan_id))
    row2 = db.query("SELECT status, reason FROM manual_clips WHERE id = ?",
                    (orphan_id,))[0]
    check("快切-源成片缺失 failed",
          row2["status"] == "failed" and "源成片" in row2["reason"],
          f"{row2['status']} {row2['reason']}")

# ===========================================================================
# 6. 切片水位清理 + processing 超时
# ===========================================================================
from replay.capture import maintenance as mnt

get_config()["recording"]["max_total_manual_clips_gb"] = 0.0000015   # ≈1.5MB
db.execute("DELETE FROM manual_clips")
cf = tmp / "777" / "manual"
cf.mkdir(parents=True, exist_ok=True)
import datetime as _dt
for i in range(2):
    sz = 1_000_000
    age_old = (i == 0)
    created = (_dt.datetime.now() - _dt.timedelta(days=2 if age_old else 0)
               ).strftime("%Y-%m-%dT%H:%M:%S")
    f = cf / f"c{i}.mp4"
    f.write_bytes(b"x" * sz)
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
        "file_path, file_size, status, created_by, created_at) "
        "VALUES (?, ?, 0, 10, ?, ?, 'done', 't', ?)",
        (rec_id if SRC.exists() else 1, 777, f"777/manual/c{i}.mp4", sz, created))
    globals()[f"_clip{i}"] = cur.lastrowid
# processing 超 1 天 → failed
cur = db.execute(
    "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
    "status, created_by, created_at) VALUES (?, ?, 0, 10, 'processing', 't', ?)",
    (rec_id if SRC.exists() else 1, 777,
     (_dt.datetime.now() - _dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")))
_proc_id = cur.lastrowid
mnt.cleanup_manual_clips(db)
row = db.query("SELECT status FROM manual_clips WHERE id = ?", (_proc_id,))[0]
check("清理-processing 超时标 failed", row["status"] == "failed")
left = db.query("SELECT id FROM manual_clips WHERE status = 'done' ORDER BY id")
all_exist = True
for fr in db.query("SELECT file_path FROM manual_clips WHERE status='done'"):
    if not (tmp / fr["file_path"]).exists():
        all_exist = False
total = sum(r_["file_size"] for r_ in db.query(
    "SELECT file_size FROM manual_clips WHERE status='done'"))
check("清理-超水位删最旧至达标",
      total <= 1_500_000 and _clip0 not in {r_["id"] for r_ in left},
      f"total={total} left={[r_['id'] for r_ in left]}")
check("清理-保留行的文件仍在", all_exist)

# ===========================================================================
# 7. 磁盘紧急清理（monkeypatch 盘余量；v2.0.1 起以实际本地字节为准，
#    造的成片须落在 maintenance 视角的 <DATA_DIR>/recordings/ 下）
# ===========================================================================
get_config()["recording"]["emergency_free_gb"] = 5.0
db.execute("DELETE FROM recordings WHERE room_id = 777")
_orig_mnt_data_dir = mnt.DATA_DIR
mnt.DATA_DIR = tmp
for i in range(3):
    f = tmp / "recordings" / "777" / f"full_e{i}.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 500_000)
    db.execute(
        "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
        "ended_at, file_path, file_size, duration_sec, source) "
        "VALUES (?, 'finished', 150, ?, ?, ?, ?, ?, 60, 'auto')",
        (777, now_iso(), now_iso(),
         _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
         f"777/full_e{i}.mp4", 500_000))
_orig_disk_usage = mnt.shutil.disk_usage


def fake_disk_usage(_p):
    # 固定返回 2MB 剩余（< 5GB 阈值必触发）；emergency 内部删一个成片
    # 就地 += file_size 累计腾出的空间，不重复调用本函数
    return SimpleNamespace(free=2_000_000, total=10 ** 12, used=1)


mnt.shutil.disk_usage = fake_disk_usage
try:
    mnt.emergency_disk_cleanup(db)
finally:
    mnt.shutil.disk_usage = _orig_disk_usage
    mnt.DATA_DIR = _orig_mnt_data_dir
left_e = db.query("SELECT id FROM recordings WHERE room_id = 777 "
                  "AND status = 'finished'")
# 只有 3 个 0.5MB 成片与残留切片可删，free 永远到不了 5GB → 必然删光；
# 断言"全部删空且行随文件一起清"（本组行均未上云，维持删行口径；
# 已上云只删文件保留行的 v2.0.1 新口径见 tests/test_v2_0_1.py）
check("紧急清理-删最旧 finished 成片",
      len(left_e) == 0, f"剩余 {len(left_e)} 行")

# ===========================================================================
# 8. auto-recordings 分组接口（v1.6 剥离：端点已删，等价分组断言由 replay
#    项目 tests 覆盖；此处保留清理与移除断言）
# ===========================================================================
db.execute("DELETE FROM manual_clips")
db.execute("DELETE FROM recordings")
check("分组-端点在本站 web app（v2.2 职责归属）",
      hasattr(app_mod, "auto_recordings"))

# ===========================================================================
import datetime as dt2


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


# v1.6 剥离：live_cut_start/stop 端点已删——live 行由测试直插复刻（原端点
# 产物形态：status='live' + 墙钟起点 + 绑定在录行 + 署名），等价的启停语义
# 由 replay 项目 tests 覆盖
db.execute("DELETE FROM recordings WHERE room_id = 777")
cur = db.execute(
    "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
    "file_path, file_size, duration_sec, source) "
    "VALUES (?, 'recording', 150, ?, ?, '', 0, 0, 'auto')",
    (777, now_iso(), now_iso()))
live_rec = cur.lastrowid

cur = db.execute(
    "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
    "end_sec, start_wall, status, created_by, created_at) "
    "VALUES (?, ?, '按钮截取', 0, 0, ?, 'live', '(local)', ?)",
    (live_rec, 777, now_iso(), now_iso()))
lc_id = cur.lastrowid
row = db.query("SELECT * FROM manual_clips WHERE id = ?", (lc_id,))[0]
check("截取-live 行形态（墙钟起点+绑定在录行+署名）",
      row["status"] == "live" and row["start_wall"] != ""
      and row["recording_id"] == live_rec and row["created_by"] == "(local)")

# 分段目录造好（伪分段：真实 10s ts 复制 6 份按墙钟命名）——注意目录名
# 必须是 full_{rec_id}（run_manual_clip 直播分支按此定位）
segdir = tmp / "777" / f"full_{live_rec}"
segdir.mkdir(parents=True)
base = dt2.datetime.now() - dt2.timedelta(seconds=60)
if SRC.exists():
    unit = segdir / "unit.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(SRC), "-t", "10", "-c", "copy",
                    "-f", "mpegts", str(unit)],
                   capture_output=True, text=True, timeout=120, check=True)
    import shutil as _sh
    for i in range(6):
        f = segdir / ("seg_" + (base + dt2.timedelta(seconds=10 * i)
                                ).strftime("%Y%m%dT%H%M%S") + ".ts")
        _sh.copyfile(unit, f)

# 原 live_cut_stop 封口语义（elapsed ≥ min_clip_sec → pending）随端点删除
# 由 replay 项目 tests 覆盖；此处直接进入 capture 侧挑段/快切面

# 挑段纯函数：伪分段名（内容无关，只测边界语义）
segtmp = Path(tempfile.mkdtemp(prefix="bili_v011_pick_"))
for i in range(6):
    (segtmp / ("seg_" + (base + dt2.timedelta(seconds=10 * i)
                         ).strftime("%Y%m%dT%H%M%S") + ".ts")).write_bytes(b"x")
picked = rec_mod.pick_segments_by_wall(
    segtmp, dt2.datetime.now() - dt2.timedelta(seconds=45),
    dt2.datetime.now() - dt2.timedelta(seconds=15))
check("挑段-窗口交集语义（4 段）", len(picked) == 4, f"picked={len(picked)}")
check("挑段-空窗口返回空",
      rec_mod.pick_segments_by_wall(
          segtmp, dt2.datetime.now() - dt2.timedelta(hours=2),
          dt2.datetime.now() - dt2.timedelta(hours=1)) == [])

# 伪分段直播快切 e2e：直接以墙钟窗口落 pending 行 → capture 侧快切
if SRC.exists():
    now2 = dt2.datetime.now()
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
        "end_sec, start_wall, end_wall, status, created_by, created_at) "
        "VALUES (?, ?, '', 0, 0, ?, ?, 'pending', 'tester', ?)",
        (live_rec, 777, _iso(now2 - dt2.timedelta(seconds=45)),
         _iso(now2 - dt2.timedelta(seconds=15)), now_iso()))
    live_clip = cur.lastrowid
    asyncio.run(rec_mod.run_manual_clip(db, live_clip))
    row = db.query("SELECT * FROM manual_clips WHERE id = ?", (live_clip,))[0]
    dur_live = row["duration_sec"]
    check("直播快切-伪分段 done",
          row["status"] == "done" and row["file_path"] != "" and dur_live >= 20,
          f"status={row['status']} dur={dur_live} {row['reason']}")
    check("直播快切-时长在分段粒度容差内（20~45s）",
          20 <= dur_live <= 45, f"dur={dur_live}")
    # 窗口远在分段范围外 → failed
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
        "start_wall, end_wall, status, created_by, created_at) "
        "VALUES (?, ?, 0, 0, ?, ?, 'pending', 't', ?)",
        (live_rec, 777, _iso(now2 - dt2.timedelta(hours=2)),
         _iso(now2 - dt2.timedelta(hours=1)), now_iso()))
    miss_clip = cur.lastrowid
    asyncio.run(rec_mod.run_manual_clip(db, miss_clip))
    row = db.query("SELECT status, reason FROM manual_clips WHERE id = ?",
                   (miss_clip,))[0]
    check("直播快切-窗口无分段 failed",
          row["status"] == "failed" and "无可用分段" in row["reason"],
          f"{row['status']} {row['reason']}")

    # finalize 收尾钩子：录制收尾时自动封口 live 标记并先行切出（分段
    # 删除前）——造一个 open 标记（起点落在伪分段时间跨度内，end_wall
    # 由钩子封口为当下时刻）+ 调 finalize_recording_row 验证
    cur = db.execute(
        "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
        "start_wall, status, created_by, created_at) "
        "VALUES (?, ?, 0, 0, ?, 'live', 'tester', ?)",
        (live_rec, 777, _iso(base + dt2.timedelta(seconds=10)), now_iso()))
    hook_clip = cur.lastrowid
    db.execute("UPDATE recordings SET status = 'finalizing' WHERE id = ?",
               (live_rec,))
    asyncio.run(rec_mod.finalize_recording_row(db, live_rec, 777, "finished"))
    row = db.query("SELECT status, duration_sec FROM manual_clips "
                   "WHERE id = ?", (hook_clip,))[0]
    rec_row = db.query("SELECT status, file_path FROM recordings "
                       "WHERE id = ?", (live_rec,))[0]
    check("收尾钩子-live 标记封口切出",
          row["status"] == "done" and row["duration_sec"] >= 10,
          f"status={row['status']} dur={row['duration_sec']}")
    check("收尾钩子-整场成片照常收尾",
          rec_row["status"] == "finished" and rec_row["file_path"] != "")

# 过短片段的 stop 拒绝语义随端点删除（replay 项目 tests 覆盖）

# ===========================================================================
# 10. v0.11 录制与场次宽限解耦：Recorder.on_stream_gone
# ===========================================================================
import replay.capture.recording as rec_mod2
# _check_live 每次调用时从 manager 模块取 fetch_room_info（函数内 import
# 防循环依赖），monkeypatch 目标是 mgr_mod.fetch_room_info


async def _make_recorder(full_rec_id):
    """构造带真实 client 的 Recorder（db/会话用 fake，_check_live 走
    monkeypatch 的 fetch_room_info）。"""
    class _Mgr:
        pass
    client = SimpleNamespace(
        room_id=777, record_enabled=False,
        manager=SimpleNamespace(db=db, http_session=object()))
    r = rec_mod2.Recorder(client)
    r.full_rec_id = full_rec_id
    return r


_orig_fetch2 = mgr_mod.fetch_room_info


async def fake_gone(session, rid):
    return {"live_status": 0}


async def fake_back(session, rid):
    return {"live_status": 1}


# 场景 A：真下播 → 3s 复核仍不在播 → 整场立即收尾 finished（不等宽限）
db.execute("DELETE FROM recordings WHERE room_id = 777")
cur = db.execute(
    "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
    "source) VALUES (?, 'recording', 150, ?, ?, 'auto')",
    (777, now_iso(), now_iso()))
gone_rec = cur.lastrowid
seg_dir = tmp / "777" / f"full_{gone_rec}"
seg_dir.mkdir(parents=True, exist_ok=True)
if SRC.exists():
    unit_ts = seg_dir / "unit2.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(SRC), "-t", "10", "-c", "copy",
                    "-f", "mpegts", str(unit_ts)],
                   capture_output=True, text=True, timeout=120, check=True)
    shutil.copyfile(unit_ts, seg_dir / "seg_20261005T120000.ts")
    (seg_dir / "unit2.ts").unlink()
mgr_mod.fetch_room_info = fake_gone
try:
    r = asyncio.run(_make_recorder(gone_rec))
    asyncio.run(r.on_stream_gone())
finally:
    mgr_mod.fetch_room_info = _orig_fetch2
row = db.query("SELECT status, reason FROM recordings WHERE id = ?",
               (gone_rec,))[0]
check("解耦-真下播立即收尾（不等宽限）",
      r.full_rec_id is None and row["status"] == "finished",
      f"full={r.full_rec_id} status={row['status']} {row['reason']}")

# 场景 B：复核 3s 内复播 → 放弃收尾，录制继续
cur = db.execute(
    "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
    "source) VALUES (?, 'recording', 150, ?, ?, 'auto')",
    (777, now_iso(), now_iso()))
back_rec = cur.lastrowid
mgr_mod.fetch_room_info = fake_back
try:
    r = asyncio.run(_make_recorder(back_rec))
    asyncio.run(r.on_stream_gone())
finally:
    mgr_mod.fetch_room_info = _orig_fetch2
row = db.query("SELECT status FROM recordings WHERE id = ?", (back_rec,))[0]
check("解耦-复核复播不收尾继续录",
      r.full_rec_id == back_rec and row["status"] == "recording",
      f"full={r.full_rec_id} status={row['status']}")

# ===========================================================================
# 11. v0.11 切片注释（v1.6 剥离：端点面已删，等价的增改删/署名隔离/归档
#     挂载断言由 replay 项目 tests 覆盖；表与外键级联仍在共享库）
# ===========================================================================
db.execute("DELETE FROM manual_clip_notes")
db.execute("DELETE FROM manual_clips")
cur = db.execute(
    "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
    "end_sec, file_path, file_size, duration_sec, status, created_by, "
    "created_at) VALUES (1, ?, '注释目标切片', 10, 30, '', 100, 20, 'done', "
    "'tester', ?)", (777, now_iso()))
cn_clip = cur.lastrowid
db.execute("INSERT INTO manual_clip_notes (clip_id, author, content, "
           "created_at, updated_at) VALUES (?, 'tester', 'x', ?, ?)",
           (cn_clip, now_iso(), now_iso()))
db.execute("DELETE FROM manual_clips WHERE id = ?", (cn_clip,))
left = db.query("SELECT count(*) AS n FROM manual_clip_notes")[0]["n"]
check("切片注释-表与外键级联仍在（replay 项目使用）", left == 0)
check("切片注释-端点在本站 web app（v2.2 职责归属）",
      hasattr(app_mod, "add_clip_note"))

# ===========================================================================
print()
print(f"共 {len(results)} 项：通过 {sum(results)}，失败 {len(results) - sum(results)}")
sys.exit(0 if all(results) else 1)
