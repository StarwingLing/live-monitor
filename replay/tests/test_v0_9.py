# -*- coding: utf-8 -*-
"""v0.9 功能本地测试（v2.2 迁入 replay 后精简版，仅 capture 侧）。

保留覆盖：临时库种子、_snapshot_all 单房间异常隔离（v0.9.3）、同房间
录制防重第三道防线（Recorder 幂等拒绝）、maintenance 整场保留期清理、
db 迁移幂等（录制四表在、高能两表已移除）。
随 v2.2 架构改版移除的部分：高能 Tracker（功能整体下线）、writer
he_window、bili web 权限矩阵（录制 API 动线在本站，由 test_replay_api
覆盖；bili 侧门由其自有套件覆盖）。

运行：uv run python tests/test_v0_9.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay import db as dbm
from replay.db import Db, init_db, now_iso

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="rp_v09_test_"))

# ===========================================================================
# 2. 临时库 + 种子数据（两个房间 123456 / 654321；654321 留监控码字段
#    验证共享库兼容——列属 replay 项目使用，本站不再读写）
# ===========================================================================
dbm.DB_PATH = TMP / "test.db"
dbm.DATA_DIR = TMP          # auth 的凭据文件 / RECORDINGS_ROOT 均指向临时目录
init_db()
conn = dbm.connect()
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (123456, '测试甲', 1, ?)", (now_iso(),))
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (654321, '测试乙', 1, ?)", (now_iso(),))
conn.commit()
conn.close()

# ===========================================================================
# 3.5 _snapshot_all 单房间异常隔离（v0.9.3：一个房间的快照/高能结算抛错
#      不得中断本轮循环，否则迭代顺序靠后的房间连带停写人气快照）
# ===========================================================================
from replay.capture.manager import RoomManager

_snap_calls: list[int] = []


class _SnapRoom:
    """maybe_snapshot 的最小替身（boom=True 时抛错；v2.2 高能 on_tick 已移除）。"""

    def __init__(self, room_id: int, boom: bool = False):
        self.room_id = room_id
        self._boom = boom

    def maybe_snapshot(self) -> None:
        if self._boom:
            raise RuntimeError("boom")
        _snap_calls.append(self.room_id)


_fake_mgr = SimpleNamespace(clients={1: _SnapRoom(1, boom=True), 2: _SnapRoom(2)})
RoomManager._snapshot_all(_fake_mgr)
# 房间 1 抛错被隔离；房间 2 正常执行
check("采样-单房间异常隔离", _snap_calls == [2])

# --- 同房间录制防重（并发安全三道防线中的第三道：Recorder 幂等拒绝） ---
# 第一道（web 原子 INSERT + 409）已由"录制-重复start409"覆盖；此处验证
# Recorder 已在录时对第二个请求行直接作废，不覆盖上下文、不杀流
from replay.capture.recording import Recorder as _Rec


class _RClient:
    room_id = 654321

    def __init__(self):
        self.manager = SimpleNamespace(db=Db(dbm.DB_PATH))
        self.record_enabled = False


async def _dup_guard():
    rc = _Rec(_RClient())
    rc.full_rec_id = 777                     # 模拟正在录
    await rc.start_full(778)                  # 第二个请求行 → 应被拒绝
    row = tuple(dbm.connect().execute(
        "SELECT status, reason FROM recordings WHERE id = 778"
    ).fetchone() or ())
    check("防重-Recorder拒绝重复请求", row == ("failed", "该房间已有进行中的录制")
          and rc.full_rec_id == 777)


# 请求行 778 需先存在（finalize_recording_row 只 UPDATE 不 INSERT）
_c2 = dbm.connect()
_c2.execute("INSERT INTO recordings (id, room_id, status, qn, created_at, started_at) "
            "VALUES (778, 654321, 'requested', 200, ?, ?)", (now_iso(), now_iso()))
_c2.execute("INSERT INTO recordings (id, room_id, status, qn, created_at, started_at) "
            "VALUES (777, 654321, 'recording', 200, ?, ?)", (now_iso(), now_iso()))
_c2.commit()
_c2.close()
asyncio.run(_dup_guard())

# ===========================================================================
# 6. maintenance：录制产物清理（v2.2.3 无自动删行回归）
# ===========================================================================
from replay.capture import maintenance

maintenance.DATA_DIR = TMP   # 产物根目录指向临时目录（import 时绑定值需覆盖）
rec_root = TMP / "recordings"
# v2.2.3 留存策略定稿：保留期/总量水位自动删行已移除——超期行与未到期行
# 一律保留（行 + 文件均不动），本节验证 _cleanup_recordings 不再清行
for rid, status, ended, size in [
        (9001, "finished", "2026-08-01T10:00:00", 1000),   # 远超旧 30 天界
        (9002, "finished", "2026-09-25T10:00:00", 2000)]:
    p = rec_root / "123456" / f"full_{rid}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    _c = dbm.connect()
    _c.execute("INSERT INTO recordings (id, room_id, status, ended_at, file_path, "
               "file_size, duration_sec, created_at, started_at) "
               "VALUES (?, 123456, ?, ?, ?, ?, 60, ?, ?)",
               (rid, status, ended, str(p.relative_to(rec_root)), size, ended, ended))
    _c.commit()
    _c.close()
maintenance._cleanup_recordings(Db(dbm.DB_PATH))
_c = dbm.connect()
left = _c.execute("SELECT id FROM recordings WHERE room_id = 123456 "
                  "AND status = 'finished' ORDER BY id").fetchall()
_c.close()
check("清理-超期整场行保留（v2.2.3 无自动删行）",
      [x[0] for x in left] == [9001, 9002])
check("清理-超期文件保留", (rec_root / "123456" / "full_9001.mp4").exists())
check("清理-未到期文件保留", (rec_root / "123456" / "full_9002.mp4").exists())

# ===========================================================================
# 7. 迁移幂等：init_db 二次执行不炸、新表在
# ===========================================================================
init_db()
_c = dbm.connect()
tables = {r[0] for r in _c.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
cols = [r[1] for r in _c.execute("PRAGMA table_info(rooms)").fetchall()]
_c.close()
check("迁移-录制表存在", {"recordings", "recording_notes",
                       "manual_clips", "manual_clip_notes"} <= tables)
check("迁移-高能两表已移除", "he_windows" not in tables and "he_clips" not in tables)
check("迁移-rooms共享列存在", "replay_code" in cols and "auto_full" in cols
      and "record_enabled" not in cols)


# ===========================================================================
# 汇总
# ===========================================================================
print(chr(10) + f"共 {len(results)} 项，通过 {sum(results)}，失败 {len(results) - sum(results)}")
sys.exit(0 if all(results) else 1)
