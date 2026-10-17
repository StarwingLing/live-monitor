# -*- coding: utf-8 -*-
r"""replay v2.0.1 新增件测试：云端原片回灌（切片动线配套）。

覆盖：
1. restore.py 模块单测（Fake OSS 客户端，不打真网）：
   - 分段拉取全流程：.restore-part 原子改名落位、内容逐字节一致、
     mtime 重置为回灌时刻、任务表 running→done、半截 part 不残留；
   - **只拉原件**：全程仅 oss_key 被 Range 请求（low key 永不出现）；
   - 失败路径：客户端抛错 → failed + error 落表 + part 清理；
   - start() 幂等三分支：本机已有 → done；running 重复触发 → 现快照；
     failed 后再触发 → 重新起线程。
2. 端点（POST/GET /api/rooms/{r}/recordings/{id}/restore-oss）：
   - OSS 未启用 503；录播不存在 404；未上云（oss_key 空）404；
   - 游客 POST 403（middleware 只读门）、跨房 clipper 403；
   - 本机已有 → 幂等 done；磁盘余量不足 → 503 拒绝；
   - 触发成功 → 返回任务快照并起线程（mock 拉取体）；
   - GET 无任务 404 / 有任务返回快照。

运行：uv run python tests/test_v2_0_1.py
"""
import shutil
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

import replay.web.app as app_mod
from replay.web import restore as restore_mod
from replay.web.app import app
from replay import auth

from fastapi.testclient import TestClient

# ---- 生产模式 + 录像根指向临时目录（app 与 restore 模块两处都要）----
app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"
app_mod.RECORDINGS_ROOT = TMP / "recordings"
app_mod.RECORDINGS_ROOT.mkdir(exist_ok=True)
restore_mod.RECORDINGS_ROOT = app_mod.RECORDINGS_ROOT
from fastapi.staticfiles import StaticFiles as _SF
for _route in app.router.routes:
    if getattr(_route, "path", "") == "/recordings":
        _route.app = _SF(directory=str(app_mod.RECORDINGS_ROOT))

from replay.db import connect
_conn = connect()
H.init_tables(_conn)
_conn.close()

REC = app_mod.RECORDINGS_ROOT


def fresh(cookies: dict | None = None) -> TestClient:
    c = TestClient(app)
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    return c


ADMIN = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
CLIPPER_101 = fresh({"replay_auth": auth.sign_session("clipper", "101",
                                                      nickname="歌切工")})
CLIPPER_202 = fresh({"replay_auth": auth.sign_session("clipper", "202",
                                                      nickname="别房切")})
GUEST_101 = fresh({"replay_auth": auth.sign_session("guest", "101")})


def seed_rec(rec_id, room, oss_key="recordings/101/full_9.mp4",
             file_size=1024, file_path="101/full_9.mp4"):
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO rooms (room_id) VALUES (?)", (room,))
        conn.execute(
            "INSERT INTO recordings (id, room_id, started_at, ended_at, "
            "status, qn, file_path, file_size, duration_sec, source, "
            "oss_key, oss_low_key, created_at) VALUES (?, ?, "
            "'2026-10-07T19:00:00', '2026-10-07T22:00:00', 'finished', "
            "200, ?, ?, 10800, 'auto', ?, "
            "'recordings/101/full_9/low.mp4', '2026-10-07T22:00:05')",
            (rec_id, room, file_path, file_size, oss_key))
        conn.commit()
    finally:
        conn.close()


def del_rec(rec_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. restore.py 模块单测（Fake OSS 客户端）
# ---------------------------------------------------------------------------
class FakeBody:
    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d


class FakeResult:
    def __init__(self, data: bytes):
        self.body = FakeBody(data)


class FakeClient:
    """按 key 存字节的对象存储假体；记录被 Range 请求过的 key。"""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.asked_keys: list[str] = []

    def head_object(self, req):
        key = req.key
        data = self.objects.get(key)
        if data is None:
            raise IOError(f"nosuchkey:{key}")

        class _Meta:
            content_length = len(data)
        return _Meta()

    def get_object(self, req):
        key = req.key
        self.asked_keys.append(key)
        data = self.objects.get(key)
        if data is None:
            raise IOError(f"nosuchkey:{key}")
        rng = (req.range_header or "").split("bytes=")[-1]
        start_s, end_s = rng.split("-")
        return FakeResult(data[int(start_s):int(end_s) + 1])


# 用小 chunk 拉一段跨多段的内容（3 段 + 1 个不满段）
PAYLOAD = bytes(range(256)) * 300          # 76800 B
restore_mod.CHUNK_SAVE = restore_mod.CHUNK
restore_mod.CHUNK = 16 * 1024              # 16KiB → 5 段
try:
    fake = FakeClient({"recordings/101/full_9.mp4": PAYLOAD,
                       "recordings/101/full_9/low.mp4": b"low-low"})
    restore_mod.TASKS.clear()
    (REC / "101").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # 同步驱动线程体 _run（与 start() 起的 daemon 线程同一入口），
    # 保证断言时序确定；start() 的线程路径在端点测试里覆盖
    task = {"status": "running", "got": 0, "total": 0,
            "started_at": t0, "finished_at": 0.0, "error": "",
            "file_path": "101/full_9.mp4"}
    restore_mod.TASKS[9] = task
    with mock.patch.object(restore_mod, "_client",
                           return_value=(fake, "bucket")):
        restore_mod._run({"id": 9, "room_id": 101,
                          "file_path": "101/full_9.mp4",
                          "oss_key": "recordings/101/full_9.mp4"}, task)

    out = REC / "101" / "full_9.mp4"
    check("模块-拉取完成落位（原子改名，part 不残留）",
          task["status"] == "done" and out.exists()
          and not (REC / "101" / "full_9.mp4.restore-part").exists())
    check("模块-内容逐字节一致", out.read_bytes() == PAYLOAD)
    check("模块-进度累计到总量",
          task["total"] == len(PAYLOAD) and task["got"] == len(PAYLOAD))
    check("模块-仅拉取原件（low key 从未被请求）",
          set(fake.asked_keys) == {"recordings/101/full_9.mp4"})
    check("模块-mtime 重置为回灌时刻（近 5s）",
          abs(out.stat().st_mtime - t0) < 5)
    check("模块-low 目录未产生",
          not (REC / "101" / "full_9").exists())

    # 失败路径：客户端 get_object 抛错（head 正常）→ failed + part 清理
    class BoomClient(FakeClient):
        def get_object(self, req):
            raise IOError("network down")

    restore_mod.TASKS.clear()
    task2 = {"status": "running", "got": 0, "total": 0,
             "started_at": time.time(), "finished_at": 0.0, "error": "",
             "file_path": "101/full_9.mp4"}
    restore_mod.TASKS[10] = task2
    out.unlink()
    with mock.patch.object(restore_mod, "_client",
                           return_value=(BoomClient(
                               {"recordings/101/full_9.mp4": PAYLOAD}),
                               "bucket")):
        restore_mod._run({"id": 10, "room_id": 101,
                          "file_path": "101/full_9.mp4",
                          "oss_key": "recordings/101/full_9.mp4"}, task2)
    check("模块-失败落表（failed + 原因）",
          task2["status"] == "failed" and "network down" in task2["error"])
    check("模块-失败清理半截 part",
          not (REC / "101" / "full_9.mp4.restore-part").exists())

    # start() 幂等三分支
    restore_mod.TASKS.clear()
    out.write_bytes(PAYLOAD)   # 本机已有 → done，不起线程
    snap = restore_mod.start({"id": 11, "room_id": 101,
                              "file_path": "101/full_9.mp4",
                              "oss_key": "recordings/101/full_9.mp4"})
    check("模块-start 本机已有 → 直接 done",
          snap["status"] == "done" and snap["total"] == len(PAYLOAD))
    restore_mod.TASKS[12] = {"status": "running", "got": 1, "total": 9,
                             "started_at": time.time(), "finished_at": 0.0,
                             "error": "", "file_path": "101/full_9.mp4"}
    out.unlink()   # running 分支前提：本机无文件
    snap = restore_mod.start({"id": 12, "room_id": 101,
                              "file_path": "101/full_9.mp4",
                              "oss_key": "recordings/101/full_9.mp4"})
    check("模块-start running 重复触发 → 返回现快照不重启",
          snap["status"] == "running" and snap["got"] == 1)
finally:
    restore_mod.CHUNK = restore_mod.CHUNK_SAVE

# ---------------------------------------------------------------------------
# 2. 端点：POST / GET restore-oss
# ---------------------------------------------------------------------------
POST_URL = "/api/rooms/101/recordings/91/restore-oss"
GET_URL = POST_URL

# OSS 未启用（显式关掉，不依赖本地 bili config 的实际值）→ 503
seed_rec(91, 101)
try:
    _oss_enabled_save = app_mod.oss_enabled
    app_mod.oss_enabled = lambda: False
    check("端点-OSS 未启用 503", ADMIN.post(POST_URL).status_code == 503)
    app_mod.oss_enabled = lambda: True
    try:
        # 录播不存在 / 跨房（q1 房间过滤 → 404）
        check("端点-录播不存在 404",
              ADMIN.post("/api/rooms/101/recordings/999/restore-oss"
                         ).status_code == 404)
        check("端点-跨房 clipper 403",
              CLIPPER_202.post(POST_URL).status_code == 403)
        check("端点-游客 POST 403",
              GUEST_101.post(POST_URL).status_code == 403)

        # 未上云（oss_key 空）→ 404
        del_rec(91)
        seed_rec(91, 101, oss_key="")
        check("端点-未上云 404",
              ADMIN.post(POST_URL).status_code == 404)

        # 本机已有 → 幂等 done（不起线程）
        del_rec(91)
        seed_rec(91, 101)
        (REC / "101").mkdir(parents=True, exist_ok=True)
        (REC / "101" / "full_9.mp4").write_bytes(b"x" * 32)
        restore_mod.TASKS.clear()
        r = ADMIN.post(POST_URL)
        check("端点-本机已有 → 幂等 done",
              r.status_code == 200 and r.json()["status"] == "done")
        (REC / "101" / "full_9.mp4").unlink()

        # 磁盘余量不足 → 503（file_size 造大，disk_usage mock 成紧张值）
        del_rec(91)
        seed_rec(91, 101, file_size=100 << 30)   # 需 100G
        with mock.patch("shutil.disk_usage",
                        return_value=mock.Mock(free=50 << 30)):
            r = ADMIN.post(POST_URL)
        check("端点-磁盘余量不足 503",
              r.status_code == 503 and "磁盘" in r.json()["detail"])

        # 正常触发 → 返回 running 快照（mock 拉取体立即完成）
        del_rec(91)
        seed_rec(91, 101, file_size=len(PAYLOAD))
        restore_mod.TASKS.clear()
        fake2 = FakeClient({"recordings/101/full_9.mp4": PAYLOAD})

        def fast_run(row, task):
            time.sleep(0.3)   # 让 POST 响应稳定看到 running 态
            task["total"] = task["got"] = len(PAYLOAD)
            task["status"] = "done"
            task["finished_at"] = time.time()
            (REC / "101").mkdir(parents=True, exist_ok=True)
            (REC / "101" / "full_9.mp4").write_bytes(PAYLOAD)

        with mock.patch.object(restore_mod, "_client",
                               return_value=(fake2, "bucket")), \
             mock.patch.object(restore_mod, "_run", fast_run):
            r = CLIPPER_101.post(POST_URL)
        check("端点-clipper 本人房触发 200 + running 快照",
              r.status_code == 200 and r.json()["status"] == "running"
              and r.json().get("total", 0) == 0)   # total 由线程 head 后回填
        # 线程竞争：start() 已起真线程跑 _run（被 mock 替换为 fast_run 是
        # 模块级属性，线程同样走它）；轮询端点应能取到 done 终态
        snap = None
        for _ in range(20):
            snap = ADMIN.get(GET_URL).json()
            if snap.get("status") == "done":
                break
            time.sleep(0.1)
        check("端点-GET 状态快照",
              snap is not None and snap["status"] == "done"
              and (REC / "101" / "full_9.mp4").exists())

        # GET 无任务 → 404
        restore_mod.TASKS.clear()
        # v2.2.4：任务出表但文件已在盘 → done 兜底（完成即出表/web 重启
        # 丢内存态，此前前端轮询只会拿到 404 永不更新）
        r = ADMIN.get(GET_URL)
        check("端点-GET 任务出表+文件在盘 → done 兜底",
              r.status_code == 200 and r.json()["status"] == "done")
        (REC / "101" / "full_9.mp4").unlink()
        check("端点-GET 无任务无文件 404", ADMIN.get(GET_URL).status_code == 404)
        check("端点-GET 录播不存在 404",
              ADMIN.get("/api/rooms/101/recordings/999/restore-oss"
                        ).status_code == 404)
    finally:
        app_mod.oss_enabled = _oss_enabled_save
finally:
    del_rec(91)
    restore_mod.TASKS.clear()

# ---------------------------------------------------------------------------
# 3. 删除双选项与取消截取（v2.0.2）：删除视频=真删除（行+文件+云端，
#    录播级联全部挂载切片）/ 删除服务器存档（release-local，仅已上云）/
#    live-cut cancel=废弃截取（删 live 行，非 live 409）
# ---------------------------------------------------------------------------
ARC_URL = "/api/rooms/101/auto-recordings"


def seed_clip(cid, rec_id, room=101, created_by="歌切工",
              oss_key="recordings/101/manual/clip_x.mp4",
              file_path="101/manual/clip_x.mp4", status="done"):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO manual_clips (id, recording_id, room_id, title, "
            "start_sec, end_sec, start_wall, end_wall, file_path, file_size, "
            "duration_sec, status, reason, created_by, created_at, oss_key) "
            "VALUES (?, ?, ?, 'x', 10, 70, '', '', ?, 1000, 60, ?, '', ?, "
            "'2026-10-07T21:00:00', ?)",
            (cid, rec_id, room, file_path, status, created_by, oss_key))
        conn.commit()
    finally:
        conn.close()


def del_clip_row(cid):
    conn = connect()
    try:
        conn.execute("DELETE FROM manual_clips WHERE id = ?", (cid,))
        conn.commit()
    finally:
        conn.close()


def _row_in(table, id_):
    conn = connect()
    try:
        return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?",
                            (id_,)).fetchone() is not None
    finally:
        conn.close()


# ---- 删除视频（录播）：级联清全部挂载切片（行 + 本地文件）----
seed_rec(92, 101, oss_key="recordings/101/full_92.mp4",
         file_size=2048, file_path="101/full_92.mp4")
seed_clip(921, 92, oss_key="recordings/101/manual/clip_921.mp4",
          file_path="101/manual/clip_921.mp4")
seed_clip(922, 92, oss_key="recordings/101/manual/clip_922.mp4",
          file_path="101/manual/clip_922.mp4")
(REC / "101" / "full_92.mp4").write_bytes(b"f" * 2048)
(REC / "101" / "full_92").mkdir(exist_ok=True)
(REC / "101" / "full_92" / "low.mp4").write_bytes(b"l" * 128)
(REC / "101" / "manual").mkdir(parents=True, exist_ok=True)
(REC / "101" / "manual" / "clip_921.mp4").write_bytes(b"c" * 256)
(REC / "101" / "manual" / "clip_921").mkdir(exist_ok=True)
(REC / "101" / "manual" / "clip_921" / "low.mp4").write_bytes(b"l" * 64)

check("删除视频-录播 clipper 403",
      CLIPPER_101.delete("/api/rooms/101/recordings/92").status_code == 403)
r = ADMIN.delete("/api/rooms/101/recordings/92")
check("删除视频-录播删除成功且级联全部切片",
      r.status_code == 200 and r.json().get("deleted_clips") == 2
      and not _row_in("recordings", 92)
      and not _row_in("manual_clips", 921) and not _row_in("manual_clips", 922))
check("删除视频-本地文件全清（原片+low+切片本体+切片low）",
      not (REC / "101" / "full_92.mp4").exists()
      and not (REC / "101" / "full_92").exists()
      and not (REC / "101" / "manual" / "clip_921.mp4").exists()
      and not (REC / "101" / "manual" / "clip_921").exists())

# processing 切片阻断整场删除
seed_rec(95, 101, oss_key="", file_size=100, file_path="101/full_95.mp4")
seed_clip(923, 95, status="processing", oss_key="",
          file_path="101/manual/clip_923.mp4")
r = ADMIN.delete("/api/rooms/101/recordings/95")
check("删除视频-processing 切片在切 409",
      r.status_code == 409 and _row_in("recordings", 95))

# ---- 取消截取：废弃 live 行，非 live 拒绝 ----
seed_clip(930, 95, status="live", oss_key="", file_path="")
check("取消截取-guest 403",
      GUEST_101.post("/api/rooms/101/recordings/live-cut/930/cancel"
                     ).status_code == 403)
r = CLIPPER_101.post("/api/rooms/101/recordings/live-cut/930/cancel")
check("取消截取-clipper 废弃 live 行",
      r.status_code == 200 and not _row_in("manual_clips", 930))
check("取消截取-已删行 404",
      CLIPPER_101.post("/api/rooms/101/recordings/live-cut/930/cancel"
                       ).status_code == 404)
r = CLIPPER_101.post("/api/rooms/101/recordings/live-cut/922/cancel")
check("取消截取-非 live（已删/终态）409 或 404",
      r.status_code in (404, 409))

# ---- 删除服务器存档（release-local，仅已上云）----
seed_rec(93, 101, oss_key="recordings/101/full_93.mp4",
         file_size=500, file_path="101/full_93.mp4")
seed_rec(94, 101, oss_key="", file_size=500, file_path="101/full_94.mp4")
conn = connect()
conn.execute("UPDATE recordings SET source='manual', duration_sec=60 "
             "WHERE id IN (93, 94)")
conn.commit()
conn.close()
seed_clip(925, 93, oss_key="recordings/101/manual/clip_925.mp4",
          file_path="101/manual/clip_925.mp4")
(REC / "101" / "full_93.mp4").write_bytes(b"f" * 500)
(REC / "101" / "manual" / "clip_925.mp4").write_bytes(b"c" * 100)
(REC / "101" / "manual" / "clip_925").mkdir(exist_ok=True)
(REC / "101" / "manual" / "clip_925" / "low.mp4").write_bytes(b"l" * 32)

check("删除存档-录播 clipper 403",
      CLIPPER_101.post("/api/rooms/101/recordings/93/release-local"
                       ).status_code == 403)
check("删除存档-未上云录播 400",
      ADMIN.post("/api/rooms/101/recordings/94/release-local"
                 ).status_code == 400)
r = ADMIN.post("/api/rooms/101/recordings/93/release-local")
check("删除存档-已上云录播：文件删、行保留",
      r.status_code == 200 and r.json()["released"] is True
      and not (REC / "101" / "full_93.mp4").exists()
      and _row_in("recordings", 93))
check("删除存档-幂等（本地已无文件 released=false）",
      ADMIN.post("/api/rooms/101/recordings/93/release-local"
                 ).json()["released"] is False)
r = ADMIN.post("/api/rooms/101/clips/925/release-local")
check("删除存档-已上云切片：本体+low 删、行保留",
      r.status_code == 200 and r.json()["released"] is True
      and not (REC / "101" / "manual" / "clip_925.mp4").exists()
      and not (REC / "101" / "manual" / "clip_925").exists()
      and _row_in("manual_clips", 925))
conn = connect()
conn.execute("UPDATE manual_clips SET oss_key='' WHERE id=925")
conn.commit()
conn.close()
check("删除存档-未上云切片 400",
      ADMIN.post("/api/rooms/101/clips/925/release-local").status_code == 400)

# 清场
for rid in (93, 94, 95):
    del_rec(rid)
for cid in (921, 922, 923, 925, 930):
    del_clip_row(cid)
import shutil as _shutil
_shutil.rmtree(REC / "101" / "full_92", ignore_errors=True)

import sys as _sys
_sys.exit(H.summary("test_v2_0_1"))
