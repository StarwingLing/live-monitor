# -*- coding: utf-8 -*-
"""v1.1.1 修复测试：下播收尾链路（中间状态 + 轮询解耦）。

背景（2026-10-06 实测缺陷，房间 <房间号>）：主播 21:39 下播后页面
「正在录制」持续到 21:48——整场 10.4GB concat 同步执行约 7.5 分钟且无
finalizing 中间态；且 21:40:50-21:58:30 全部房间状态轮询停摆 18 分钟
——收尾与 600s 场次宽限同步跑在轮询协程里，拖住 _poll_all 的 gather。

覆盖：
1. finalize_recording_row 前置 'finalizing'：拼接执行期间行状态为
   finalizing（前端 saving 判定据此显示「保存中」），完成后落 finished；
2. 成片直采分支：无分段但成片已在盘（拼接完成、落库前进程中断窗口）→
   采用现成成片，不误判"无可用分段"；
3. 无分段且无成片 → failed（原语义回归）；
4. poll_live_status 下播分支解耦：收尾序列为独立任务——检测轮询即时
   返回（不被收尾拖住）；收尾完成后场次关闭、end_time 落库、_ending
   复位、高能/录制侧 on_confirmed_end 被调；
5. 宽限期内复播：场次不关闭（原语义回归）；
6. RoomClient.stop() 取消进行中的收尾任务。

运行：uv run python tests/test_v1_1_1.py
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 测试库隔离：在任何 bili_monitor 模块 import 前把 DB_PATH 指到临时文件 ----
_tmpdir = tempfile.TemporaryDirectory(prefix="bili_v111_", ignore_cleanup_errors=True)
import replay.db as bm_db
bm_db.DB_PATH = Path(_tmpdir.name) / "test.db"
bm_db.DATA_DIR = Path(_tmpdir.name)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


from replay.db import Db, init_db, now_iso

init_db()
db = Db()

import replay.capture.recording as rec_mod
import replay.capture.manager as mgr_mod
from replay.config import get_config

tmp = Path(_tmpdir.name)
rec_mod.RECORDINGS_ROOT = tmp   # 录制产物指到临时目录（与 test_v0_11 同口径）
get_config()["recording"]["enabled"] = True   # 解耦测试需要收尾链路可走

# 测试房间（recordings/live_sessions 对 rooms 有外键）
for rid in (777, 888):
    db.execute("INSERT INTO rooms (room_id, anchor_name, created_at) "
               "VALUES (?, ?, ?)", (rid, f"测试房间{rid}", now_iso()))


def _insert_recording(room_id: int, status: str = "recording") -> int:
    cur = db.execute(
        "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
        "source) VALUES (?, ?, 150, ?, ?, 'auto')",
        (room_id, status, now_iso(), now_iso()))
    return cur.lastrowid


# ===========================================================================
# 1. finalize 前置 finalizing：拼接执行期间行状态已流转
# ===========================================================================
room = 777
rec1 = _insert_recording(room)
seg_dir = tmp / str(room) / f"full_{rec1}"
seg_dir.mkdir(parents=True)
(seg_dir / "seg_20261006T180000.ts").write_bytes(b"x" * 1024)

concat_states: list[str] = []


async def fake_concat(files, out_mp4):
    """假拼接：执行中抓取行状态（应为 finalizing），写出假成片。"""
    row = db.query("SELECT status FROM recordings WHERE id = ?", (rec1,))[0]
    concat_states.append(row["status"])
    out_mp4.write_bytes(b"y" * 2048)
    return True


_orig_concat = rec_mod.Recorder._concat_remux
rec_mod.Recorder._concat_remux = staticmethod(fake_concat)
try:
    asyncio.run(rec_mod.finalize_recording_row(db, rec1, room, "finished"))
finally:
    rec_mod.Recorder._concat_remux = _orig_concat

row = db.query("SELECT status, file_path, file_size FROM recordings "
               "WHERE id = ?", (rec1,))[0]
check("finalizing-拼接期间状态已前置",
      concat_states == ["finalizing"],
      f"拼接中状态={concat_states}")
check("finalizing-完成后落 finished",
      row["status"] == "finished"
      and row["file_path"].replace("\\", "/") == f"{room}/full_{rec1}.mp4"
      and row["file_size"] == 2048,
      f"status={row['status']} path={row['file_path']} size={row['file_size']}")
check("finalizing-完成后分段目录已清", not seg_dir.exists())


# ===========================================================================
# 2. 成片直采分支：无分段但成片已在盘（中断恢复窗口）
# ===========================================================================
rec2 = _insert_recording(room)
(tmp / str(room) / f"full_{rec2}.mp4").write_bytes(b"z" * 4096)
asyncio.run(rec_mod.finalize_recording_row(db, rec2, room,
                                           "interrupted", "capture 重启收尾"))
row = db.query("SELECT status, file_path, file_size FROM recordings "
               "WHERE id = ?", (rec2,))[0]
check("直采-采用已存在成片不误判 failed",
      row["status"] == "interrupted"
      and row["file_path"].replace("\\", "/") == f"{room}/full_{rec2}.mp4"
      and row["file_size"] == 4096,
      f"status={row['status']} path={row['file_path']} size={row['file_size']}")

# ===========================================================================
# 3. 无分段且无成片 → failed（原语义回归）
# ===========================================================================
rec3 = _insert_recording(room)
asyncio.run(rec_mod.finalize_recording_row(db, rec3, room, "finished"))
row = db.query("SELECT status, reason FROM recordings WHERE id = ?", (rec3,))[0]
check("回归-无分段无成片落 failed",
      row["status"] == "failed" and "无可用分段" in row["reason"],
      f"status={row['status']} reason={row['reason']}")

# ===========================================================================
# 4-6. poll_live_status 下播分支解耦（RoomClient 跳过 __init__ 构造，
#      依赖 stub + monkeypatch 接口函数）
# ===========================================================================
_orig_metrics = mgr_mod.fetch_room_metrics
_orig_info = mgr_mod.fetch_room_info


async def fake_metrics_offline(session, room_id):
    """状态轮询接口：未开播（触发下播分支）。"""
    return {"live_status": 0, "attention": 5, "online": 5,
            "watched": 0, "guards": 0, "title": "t"}


def make_client(session_id: int, gone_delay: float = 0.8):
    """构造最小可用 RoomClient（不建 WS/tracker 真实依赖）。"""
    c = mgr_mod.RoomClient.__new__(mgr_mod.RoomClient)
    c.room_id = 888
    c.manager = SimpleNamespace(
        db=db, http_session=object(),
        cfg={"end_grace_sec": 1, "poll_interval_sec": 1})
    c.session_id = session_id
    c._ending = False
    c._stream_end_task = None
    c._ws_task = None
    c._last = {"attention": None, "watched": None, "guards": None}
    c._last_title = None
    c._anchor_info_filled = True
    c._last_snapshot_ts = 0.0

    ev = SimpleNamespace(gone_done=[], session_end=[], confirmed_end=[])

    async def fake_on_stream_gone():
        await asyncio.sleep(gone_delay)   # 模拟分钟级整场拼接
        ev.gone_done.append(time.monotonic())

    async def fake_on_confirmed_end():
        ev.confirmed_end.append(1)

    async def fake_shutdown():
        pass

    c.recorder = SimpleNamespace(on_stream_gone=fake_on_stream_gone,
                                 on_confirmed_end=fake_on_confirmed_end,
                                 shutdown=fake_shutdown)
    # v2.2：高能 tracker 已随高能功能移除，客户端仅剩 handler/recorder
    c.handler = SimpleNamespace(session_id=session_id)
    return c, ev


async def _open_session(room_id: int) -> int:
    cur = db.execute(
        "INSERT INTO live_sessions (room_id, start_time) VALUES (?, ?)",
        (room_id, now_iso()))
    return cur.lastrowid


# ---- 4. 解耦：轮询即时返回；收尾任务完成后场次关闭、状态复位 ----
mgr_mod.fetch_room_metrics = fake_metrics_offline


async def fake_info_offline(session, room_id):
    return {"live_status": 0}   # 宽限复核：仍未开播 → 关场


async def scenario_decoupled():
    sid = await _open_session(888)
    c, ev = make_client(sid)
    await c.poll_live_status()          # 检测轮询：应即时返回
    poll_ok = (c._stream_end_task is not None
               and not c._stream_end_task.done()
               and c._ending is True)
    await c._stream_end_task            # 等收尾序列完成
    end_time = db.query("SELECT end_time FROM live_sessions "
                        "WHERE session_id = ?", (sid,))[0]["end_time"]
    closed_ok = (end_time not in (None, "") and c.session_id is None
                 and c._ending is False and len(ev.confirmed_end) == 1)
    return poll_ok, closed_ok


mgr_mod.fetch_room_info = fake_info_offline
try:
    poll_ok, closed_ok = asyncio.run(scenario_decoupled())
finally:
    mgr_mod.fetch_room_metrics = _orig_metrics
    mgr_mod.fetch_room_info = _orig_info
check("解耦-轮询即时返回不被收尾拖住", poll_ok)
check("解耦-收尾完成后场次关闭且状态复位", closed_ok)

# ---- 5. 宽限期内复播：场次不关闭（原语义回归） ----
async def fake_info_back(session, room_id):
    return {"live_status": 1}   # 宽限复核：复播 → 不关场


async def scenario_back_live():
    sid = await _open_session(888)
    c, ev = make_client(sid)
    mgr_mod.fetch_room_metrics = fake_metrics_offline
    mgr_mod.fetch_room_info = fake_info_back
    await c.poll_live_status()
    await c._stream_end_task
    end_time = db.query("SELECT end_time FROM live_sessions "
                        "WHERE session_id = ?", (sid,))[0]["end_time"]
    return (end_time in (None, "") and c.session_id is not None
            and c._ending is False and len(ev.session_end) == 0)


mgr_mod.fetch_room_metrics = fake_metrics_offline
mgr_mod.fetch_room_info = fake_info_back
try:
    back_ok = asyncio.run(scenario_back_live())
finally:
    mgr_mod.fetch_room_metrics = _orig_metrics
    mgr_mod.fetch_room_info = _orig_info
check("回归-宽限期内复播场次不关闭", back_ok)

# ---- 6. stop() 取消进行中的收尾任务 ----
async def scenario_stop():
    c, _ = make_client(None)
    t = asyncio.create_task(asyncio.sleep(10))
    c._stream_end_task = t
    await c.stop()   # 取消并把 _stream_end_task 置 None
    return t


task = asyncio.run(scenario_stop())
check("stop-取消进行中的收尾任务", task.cancelled())

# ===========================================================================
# 汇总
# ===========================================================================
print()
if all(results):
    print(f"全部通过（{len(results)} 项）")
    sys.exit(0)
print(f"存在失败（{results.count(False)}/{len(results)} 项）")
sys.exit(1)
