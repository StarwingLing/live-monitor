# -*- coding: utf-8 -*-
"""v2.0.1 紧急磁盘清理测试（v2.2 自 bili-monitor tests/test_v2_0_1.py 后半段迁入）：
emergency_disk_cleanup 的「已上云只删本地文件保留行 / 未上云删行 / 已释放跳过
无死循环 / 释够即停」四断言（v2.0.1 云时代口径）。

运行：uv run python tests/test_emergency_cleanup.py
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay.db as dbm
from replay.db import Db, init_db, now_iso
from replay.capture import maintenance as mnt
from replay.config import get_config

cfg = get_config()
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="rp_emg_"))
dbm.DB_PATH = TMP / "test.db"
dbm.DATA_DIR = TMP
init_db()

# ---------------------------------------------------------------------------
# 造 5 条整场（ended_at 升序即清理顺序）：A/C/D 已上云、B 未上云、E 已上云
# 但本地文件早已释放（无可腾空间，须跳过且不死循环）；各带 <stem>/low 目录
from types import SimpleNamespace
from replay.capture import maintenance as mnt
from replay.db import Db

EROOT = TMP / "recordings"
EROOT.mkdir(parents=True)
_orig_data_dir, _orig_du = mnt.DATA_DIR, mnt.shutil.disk_usage
_orig_emg_gb = cfg["recording"].get("emergency_free_gb", 5.0)


def _seed_emg(rec_id, oss_key, ended, on_disk=True, room=990,
              size=1_000_000):
    rel = f"{room}/full_{rec_id}.mp4"
    if on_disk:
        p = EROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        low = EROOT / rel[:-4] / "low.mp4"
        low.parent.mkdir(parents=True, exist_ok=True)
        low.write_bytes(b"l" * (size // 10))
    edb.execute(
        "INSERT INTO recordings (room_id, status, qn, created_at, started_at, "
        "ended_at, file_path, file_size, duration_sec, source, oss_key) "
        "VALUES (?, 'finished', 150, '2026-10-01T00:00:00', "
        "'2026-10-01T10:00:00', ?, ?, ?, 60, 'auto', ?)",
        (room, ended, rel, size, oss_key))


edb = Db(dbm.DB_PATH)
edb.execute("INSERT OR IGNORE INTO rooms (room_id, anchor_name, enabled, "
            "created_at) VALUES (990, '紧急阀测试房', 1, ?)", (now_iso(),))
for rid, key, ended, on_disk in (
        (501, "recordings/990/full_501.mp4", "2026-10-01T12:00:00", True),
        (502, "", "2026-10-02T12:00:00", True),
        (503, "recordings/990/full_503.mp4", "2026-10-03T12:00:00", True),
        (504, "recordings/990/full_504.mp4", "2026-10-04T12:00:00", True),
        (505, "recordings/990/full_505.mp4", "2026-10-05T12:00:00", False)):
    _seed_emg(rid, key, ended, on_disk)


def _rec_of(rel):
    """按 file_path 查行（id 是自增的，不以造数序号为准）。"""
    return edb.query("SELECT id, oss_key FROM recordings WHERE file_path = ?",
                     (rel,))


def _exists(rel):
    return (EROOT / rel).exists() or (EROOT / rel[:-4]).exists()


try:
    mnt.DATA_DIR = EROOT.parent
    # 场景一：阈值 2MB、初始 1MB → 只释最旧的 A（1.1MB）即达标，其余不动
    cfg["recording"]["emergency_free_gb"] = 2_000_000 / 1e9
    mnt.shutil.disk_usage = lambda _p: SimpleNamespace(
        free=1_000_000, total=10 ** 12, used=1)
    mnt.emergency_disk_cleanup(edb)
    check("紧急阀-释够即停（B/C/D 原片仍在盘）",
          _exists("990/full_502.mp4") and _exists("990/full_503.mp4")
          and _exists("990/full_504.mp4"))
    check("紧急阀-已上云 A 只删文件保留行",
          not _exists("990/full_501.mp4") and len(_rec_of("990/full_501.mp4")) == 1)
    # 场景二：阈值拉满 → 全量清：B 未上云连行删，C/D 已上云留行，E 跳过
    cfg["recording"]["emergency_free_gb"] = 5.0
    mnt.emergency_disk_cleanup(edb)
    check("紧急阀-未上云 B 文件+行俱删",
          not _exists("990/full_502.mp4") and len(_rec_of("990/full_502.mp4")) == 0)
    check("紧急阀-已上云 C/D 文件删行保留",
          not _exists("990/full_503.mp4") and len(_rec_of("990/full_503.mp4")) == 1
          and not _exists("990/full_504.mp4") and len(_rec_of("990/full_504.mp4")) == 1)
    check("紧急阀-本地已释放的 E 跳过且行保留（无死循环）",
          len(_rec_of("990/full_505.mp4")) == 1)
finally:
    mnt.DATA_DIR = _orig_data_dir
    mnt.shutil.disk_usage = _orig_du
    cfg["recording"]["emergency_free_gb"] = _orig_emg_gb
    edb.execute("DELETE FROM recordings WHERE room_id = 990")
    edb.close()


print(chr(10) + "==== test_emergency_cleanup: " +
      ("全部通过" if all(results) else f"失败 {results.count(False)} 项") + " ====")
raise SystemExit(0 if all(results) else 1)
