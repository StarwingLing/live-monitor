# -*- coding: utf-8 -*-
"""v0.9 录制链路端到端测试（需真实网络 + 本地 ffmpeg，手动运行）。

验证整条录制链路在真实 B 站接口上工作：
1. fetch_playurl 三层兜底能取到直链（走 config.toml 的登录 cookie）；
2. Recorder 起 ffmpeg 分段录制（mpegts，10s/段）真录 ~75 秒；
3. finalize_full 停流 → concat+remux 出整场 mp4（+faststart）→ ffprobe
   读时长 → 清分段目录；
4. 全程用临时库，不动真实 data/live.db。
（v2.2 高能子系统移除后，原第 3 步 record_clip 出片随之删除——该方法
 已不存在，2026-10-14 有房间在播首次跑到该步才暴露。）

运行：uv run python tests/test_e2e_record_v0_9.py [房间号]
（不带参数时自动从本地 data/live.db 的监控名单里挑一个当前在播的房间；
 没有任何房间在播时提示后退出。）
"""
import asyncio
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay import db as dbm
from replay.db import Db, init_db, now_iso

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail else ""))


async def main() -> int:
    from replay.capture.manager import build_bili_session, fetch_room_info
    from replay.capture.recording import (RECORDINGS_ROOT, Recorder,
                                                fetch_playurl)

    # ---- 临时库 + 产物目录（RECORDINGS_ROOT 在 import 时绑定真实 data/，
    #      测试改模块常量指向临时目录，避免污染 data/recordings）----
    import replay.capture.recording as rec_mod
    tmp = Path(tempfile.mkdtemp(prefix="bili_e2e_rec_"))
    rec_mod.RECORDINGS_ROOT = tmp
    # Recorder.finalize/recording_row 里引用的 RECORDINGS_ROOT 是模块全局，
    # finalize_recording_row 用相对路径拼接，改模块常量即全局生效
    dbm.DB_PATH = tmp / "e2e.db"
    dbm.DATA_DIR = tmp
    init_db()

    # ---- 选房间：参数指定，或从本地真实监控库挑一个在播的 ----
    room_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    session = build_bili_session()   # 同步工厂：UA + 登录 cookie（防风控）
    rec = None   # 无房在播的跳过路径不走到 Recorder 赋值，防 finally 引用未绑定
    try:
        if not room_id:
            real_db = Path(__file__).resolve().parent.parent / "data" / "live.db"
            if real_db.exists():
                conn = sqlite3.connect(str(real_db))
                cands = [r[0] for r in conn.execute(
                    "SELECT room_id FROM rooms WHERE enabled = 1 ORDER BY room_id")]
                conn.close()
            else:
                cands = [21452505, 6, 21]   # 常驻大主播房间兜底
            print(f"候选房间：{cands}，逐个查开播状态…")
            for rid in cands:
                info = await fetch_room_info(session, rid)
                if info and info.get("live_status") == 1:
                    room_id = rid
                    break
                await asyncio.sleep(1)
        if not room_id:
            print("[跳过] 当前没有任何候选房间在播，稍后再试")
            return 0
        print(f"使用房间 {room_id}")

        # ---- 1. 拉流直链 ----
        url, cur_qn = await fetch_playurl(session, room_id, 150)
        check("拉流-直链获取", url.startswith("http") and cur_qn > 0,
              f"qn={cur_qn} url={url[:60]}…")

        # ---- 2. Recorder 真录 ----
        class E2EClient:
            def __init__(self):
                self.room_id = room_id
                self.manager = SimpleNamespace(db=Db(dbm.DB_PATH),
                                               http_session=session)
                self.record_enabled = False

        client = E2EClient()
        rec = Recorder(client)
        conn = dbm.connect()
        conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
                     "VALUES (?, 'e2e', 1, ?)", (room_id, now_iso()))
        cur = conn.execute(
            "INSERT INTO recordings (room_id, status, qn, created_at, started_at) "
            "VALUES (?, 'requested', 150, ?, ?)", (room_id, now_iso(), now_iso()))
        conn.commit()
        rec_id = cur.lastrowid
        conn.close()

        t0 = time.monotonic()
        await rec.start_full(rec_id)
        if rec.proc is None or rec.proc.returncode is not None:
            check("录制-ffmpeg启动", False, "进程未存活")
            return 1
        check("录制-ffmpeg启动", True)

        print("录制 75 秒…")
        await asyncio.sleep(75)
        seg_dir = tmp / str(room_id) / f"full_{rec_id}"
        segs = sorted(seg_dir.glob("seg_*.ts"))
        size_mb = sum(f.stat().st_size for f in segs) / 1e6
        check("录制-分段生成", len(segs) >= 5, f"{len(segs)} 段，共 {size_mb:.1f}MB")

        # ---- 3. finalize 整场 ----
        await rec.finalize_full("finished")
        mp4 = tmp / str(room_id) / f"full_{rec_id}.mp4"
        check("整场-成片存在", mp4.exists(), f"{mp4.stat().st_size / 1e6:.1f}MB")
        check("整场-分段目录已清", not seg_dir.exists())
        dur = Recorder._ffprobe_duration(mp4)
        elapsed = time.monotonic() - t0
        check("整场-ffprobe时长合理", dur is not None and elapsed - 20 < dur <= elapsed + 10,
              f"实测 {dur:.1f}s / 墙钟 {elapsed:.0f}s")
        row = dbm.connect().execute(
            "SELECT status, file_size, duration_sec FROM recordings WHERE id = ?",
            (rec_id,)).fetchone()
        check("整场-DB行收尾", tuple(row)[0] == "finished" and tuple(row)[1] > 0)
    finally:
        if rec and rec.proc and rec.proc.returncode is None:
            await rec._terminate_proc()
        await session.close()

    print(f"\n共 {len(results)} 项，通过 {sum(results)}，失败 {len(results) - sum(results)}")
    print(f"产物目录（可手动查看后删除）：{tmp}")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
