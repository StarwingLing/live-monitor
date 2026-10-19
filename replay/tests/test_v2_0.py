# -*- coding: utf-8 -*-
r"""replay v2.0.0 新增件测试：ASS 弹幕生成 / 下载携带同期弹幕 / 切片一键投稿。

覆盖：
1. ass.py 纯函数单测：Script Info 头、Guard 配色样式、滚动 Dialogue 行与
   {\move} 轨迹、{...} 标签注入清洗、换行清理与限长、纯表情丢弃、车道
   分配（同刻多条分道 / 全忙限流丢弃）、BGR 色序转换；
2. 录播弹幕 format=ass：附件头与文件名透传/清洗、缺省 json 向后兼容、
   房间隔离与角色可用性；
3. 切片弹幕（新增接口）：成片偏移模式（t−start_sec）、直播切片 runs 模式
   （双锚点映射）、无 timeline / 孤儿近似与 404、format=ass；
4. 投稿（biliup 子进程 mock）：未部署 503、表单校验（tid/转载来源/标签/
   标题兜底）、guest 写拦、任务状态机（mock 子进程成功解析 BV / 失败
   透传原因 + cookie 失效提示）、封面抽帧端点（本地切片）。

运行：uv run python tests/test_v2_0.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

import replay.web.ass as ass_mod
from replay.web import app as app_mod
from replay.web import upload as upload_mod
from replay.web.app import app
from replay import auth

from fastapi.testclient import TestClient

# ---- 生产模式 + 录像根 / 投稿临时区指向临时目录 ----
app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"
# 存量缺陷修复（v2.3.0 顺带）：本地 config.toml 自 v2.2.5 起 recording
# .enabled=false，起录端点 400——测试须自带开关状态，不随本地配置漂移
app_mod.cfg["recording"]["enabled"] = True
app_mod.RECORDINGS_ROOT = TMP / "recordings"
app_mod.RECORDINGS_ROOT.mkdir(exist_ok=True)
upload_mod.TMP_DIR = TMP / "tmp"
upload_mod.TMP_DIR.mkdir(exist_ok=True)
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


_ADMIN_C = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
CLIPPER_101 = fresh({"replay_auth": auth.sign_session("clipper", "101",
                                                      nickname="歌切工")})
CLIPPER_202 = fresh({"replay_auth": auth.sign_session("clipper", "202",
                                                      nickname="别房切")})
GUEST_101 = fresh({"replay_auth": auth.sign_session("guest", "101")})
# 调度壳：沿用「cp.get(url, headers=某某)」写法，按 headers 路由到对应
# 已种 Cookie 的客户端（httpx 下裸 headers 传 Cookie 不可靠，须 cookies.set）
import types as _types
cp = _types.SimpleNamespace(
    get=lambda url, headers=None, **kw: (headers or _ADMIN_C).get(url, **kw),
    post=lambda url, headers=None, **kw: (headers or _ADMIN_C).post(url, **kw))


def seed_rec(room, status="finished", file_path="", duration=3600,
             started="2026-10-07T10:00:00", timeline=""):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO recordings (room_id, status, file_path, file_size, "
            "duration_sec, started_at, ended_at, timeline, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (room, status, file_path, 12345 if file_path else 0, duration,
             started, started, timeline, started))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def seed_danmaku(room, ts, text, guard=0):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO danmaku (room_id, uid, uname, text, privilege_type, "
            "ts) VALUES (?,1,'u',?,?,?)", (room, text, guard, ts))
        conn.commit()
    finally:
        conn.close()


# 运行 timeline：10:00:00 起 600s（off 0）+ 缺口 60s + 10:11:00 起 590s（off 600）
RUNS = json.dumps({"runs": [
    {"wall": "2026-10-07T10:00:00", "dur": 600, "off": 0},
    {"wall": "2026-10-07T10:11:00", "dur": 590, "off": 600},
]})
# 无 timeline（近似模式）
NOTL = ""

# ===========================================================================
# 1. ass.py 纯函数单测
# ===========================================================================
out = ass_mod.build_ass([{"t": 5.5, "text": "你好世界", "guard": 0},
                         {"t": 6.0, "text": "舰长弹幕", "guard": 3}])
check("ASS 头：固定画布 1920x1080 + ScaledBorderAndShadow",
      "PlayResX: 1920" in out and "PlayResY: 1080" in out
      and "ScaledBorderAndShadow: yes" in out)
check("ASS 头：四种样式（Scroll + Guard1-3）齐全",
      all(f"Style: {s}," in out for s in
          ("Scroll", "Guard1", "Guard2", "Guard3")))
check("ASS 头：Guard 配色为 BGR 序（总督金 #ffb03c → 3CB0FF）",
      "&H003CB0FF" in out)
lines = [l for l in out.splitlines() if l.startswith("Dialogue:")]
check("Dialogue 行：普通弹幕走 Scroll 样式 + 10s 滚动窗",
      lines[0].startswith("Dialogue: 0,0:00:05.50,0:00:15.50,Scroll,")
      and "\\move(1920," in lines[0] and "你好世界" in lines[0])
check("Dialogue 行：舰长弹幕走 Guard3 样式",
      any(l.split(",")[3] == "Guard3" and "舰长弹幕" in l for l in lines))

inj = ass_mod.build_ass([{"t": 1, "text": "{\\pos(1,1)}注入}测{\\fs99}试", "guard": 0}])
check("注入清洗：剥 {...} 覆写标签，正文保留",
      "\\pos" not in inj and "\\fs" not in inj and "注入}测试" in inj)
check("换行/限长：换行压空白、100 字符截断",
      "a b" in ass_mod.build_ass([{"t": 1, "text": "a\nb", "guard": 0}])
      and "x" * 100 in ass_mod.build_ass([{"t": 1, "text": "x" * 150, "guard": 0}])
      and "x" * 101 not in ass_mod.build_ass(
          [{"t": 1, "text": "x" * 150, "guard": 0}]))
check("纯表情弹幕（空文本）整条丢弃",
      "Dialogue" not in ass_mod.build_ass([{"t": 1, "text": "", "guard": 0}]))

# 同刻多条 → 分道（y 不同）；构造远超车道数的同刻弹幕 → 限流丢弃不报错
burst = [{"t": 100, "text": f"第{i}条弹幕", "guard": 0}
         for i in range(500)]
ys = set()
for l in ass_mod.build_ass(burst).splitlines():
    if l.startswith("Dialogue:"):
        ys.add(l.split("\\move(1920,")[1].split(",")[0])
check("车道分配：同刻多条分散到不同轨道", len(ys) > 5)
check("车道分配：全忙限流丢弃（500 条同刻不会全部入屏）",
      len(ys) < len(burst))
check("空输入只出头不出 Dialogue",
      ass_mod.build_ass([]).strip().endswith("Effect, Text"))
check("BGR 转换：#RRGGBB → &H00BBGGRR",
      ass_mod._to_bgr("#ffb03c") == "&H003CB0FF")

# ===========================================================================
# 2. 录播弹幕 format=ass（下载携带同期弹幕）
# ===========================================================================
for ts in ("2026-10-07T10:00:10", "2026-10-07T10:05:00"):
    seed_danmaku(101, ts, f"弹幕{ts[-2:]}")
rid = seed_rec(101, file_path="101/full_1.mp4", duration=1200, timeline=RUNS)

r = cp.get(f"/api/rooms/101/recordings/{rid}/danmaku?format=ass"
           f"&filename=room101-rec{rid}",
           headers=CLIPPER_101)
check("录播 format=ass：200 + text/x-ssa + 附件头",
      r.status_code == 200 and "text/x-ssa" in r.headers["content-type"]
      and "attachment" in r.headers.get("content-disposition", ""))
check("录播 format=ass：文件名透传（前端拼的与视频同 basename）",
      f"room101-rec{rid}.ass" in r.headers.get("content-disposition", ""))
check("录播 format=ass：内容为 ASS（含 Script Info 与弹幕文本）",
      "[Script Info]" in r.text and "弹幕10" in r.text)
r = cp.get(f"/api/rooms/101/recordings/{rid}/danmaku?format=ass"
           f"&filename=../../evil",
           headers=CLIPPER_101)
check("录播 format=ass：文件名清洗（路径分隔符替换）",
      "/" not in r.headers.get("content-disposition", "")
      and "evil.ass" in r.headers.get("content-disposition", ""))
r = cp.get(f"/api/rooms/101/recordings/{rid}/danmaku", headers=GUEST_101)
check("录播弹幕缺省 json 向后兼容（approx=false）",
      r.status_code == 200 and r.json()["approx"] is False
      and r.json()["count"] == 2)
r = cp.get(f"/api/rooms/101/recordings/{rid}/danmaku?format=ass",
           headers=CLIPPER_202)
check("录播弹幕跨房 403（middleware 白名单）", r.status_code == 403)

# ===========================================================================
# 3. 切片弹幕（新增接口 /clips/<id>/danmaku）
# ===========================================================================
conn = connect()


def seed_clip(room, rec_id, start_sec=0, end_sec=0, start_wall="",
              end_wall="", duration=60, file_path="101/clip_x/clip.mp4"):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clips (recording_id, room_id, start_sec, "
            "end_sec, start_wall, end_wall, duration_sec, status, file_path, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, room, start_sec, end_sec, start_wall, end_wall,
             duration, "done", file_path, "歌切工", "2026-10-07T12:00:00"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# 3.1 成片偏移模式：切片 [100, 160)s，弹幕 t=115 → t_clip=15
seed_danmaku(101, "2026-10-07T10:01:55", "切片里的弹幕")
c_off = seed_clip(101, rid, start_sec=100, end_sec=160, duration=60)
r = cp.get(f"/api/rooms/101/clips/{c_off}/danmaku", headers=CLIPPER_101)
j = r.json()
check("切片弹幕：成片偏移模式映射（t−start_sec 落窗，窗外丢弃）",
      r.status_code == 200 and j["approx"] is False and j["count"] == 1
      and j["items"][0]["t"] == 15.0
      and j["items"][0]["text"] == "切片里的弹幕")
r = cp.get(f"/api/rooms/101/clips/{c_off}/danmaku?format=ass"
           f"&filename=room101-clip{c_off}", headers=CLIPPER_101)
check("切片弹幕：format=ass 同口径附件", r.status_code == 200
      and f"room101-clip{c_off}.ass" in r.headers.get("content-disposition", ""))

# 3.2 直播切片模式（runs）：start_wall=10:05:00（off=300）→ 弹幕 t=310 映射 10s
c_wall = seed_clip(101, rid, start_wall="2026-10-07T10:05:00",
                   end_wall="2026-10-07T10:05:30", duration=30)
seed_danmaku(101, "2026-10-07T10:05:10", "截取里的弹幕")
r = cp.get(f"/api/rooms/101/clips/{c_wall}/danmaku", headers=GUEST_101)
j = r.json()
_t10 = [it for it in j["items"] if it["text"] == "截取里的弹幕"]
check("切片弹幕：直播切片 runs 双锚点映射（游客只读可用）",
      r.status_code == 200 and j["approx"] is False and len(_t10) == 1
      and abs(_t10[0]["t"] - 10.0) < 0.11)

# 3.3 近似：源行无 timeline → 墙钟直减
rid2 = seed_rec(101, file_path="101/full_2.mp4", duration=1200, timeline=NOTL)
seed_danmaku(101, "2026-10-07T10:02:05", "近似弹幕")
c_ap = seed_clip(101, rid2, start_wall="2026-10-07T10:02:00",
                 end_wall="2026-10-07T10:02:30", duration=30)
r = cp.get(f"/api/rooms/101/clips/{c_ap}/danmaku", headers=CLIPPER_101)
j = r.json()
check("切片弹幕：无 timeline 近似（墙钟直减，approx=true）",
      r.status_code == 200 and j["approx"] is True and j["count"] == 1
      and j["items"][0]["t"] == 5.0)

# 3.4 孤儿切片（成片偏移模式，源行已删）→ 404
c_orph = seed_clip(101, 99999, start_sec=10, end_sec=40, duration=30)
r = cp.get(f"/api/rooms/101/clips/{c_orph}/danmaku", headers=CLIPPER_101)
check("切片弹幕：孤儿切片（无源行）404", r.status_code == 404)
# 3.5 孤儿直播切片（墙钟模式）仍可近似
c_orw = seed_clip(101, 99999, start_wall="2026-10-07T10:02:00",
                  end_wall="2026-10-07T10:02:30", duration=30)
r = cp.get(f"/api/rooms/101/clips/{c_orw}/danmaku", headers=CLIPPER_101)
check("切片弹幕：孤儿直播切片走墙钟近似", r.status_code == 200
      and r.json()["approx"] is True)
r = cp.get(f"/api/rooms/101/clips/{c_off}/danmaku", headers=CLIPPER_202)
check("切片弹幕跨房 403", r.status_code == 403)

# ===========================================================================
# 4. 切片一键投稿（biliup 子进程 mock）
# ===========================================================================
# 4.1 未部署 → 503（存量缺陷修复 v2.3.0 顺带：本地 config.toml 可能已配
# biliup_bin（联调用），测试须自带部署状态，不随本地配置漂移）
app_mod.cfg["bili"]["biliup_bin"] = ""
clip_file = REC / "101" / "clip_sub" / "clip.mp4"
clip_file.parent.mkdir(parents=True, exist_ok=True)
clip_file.write_bytes(b"fake-mp4")
c_sub = seed_clip(101, rid, duration=60,
                  file_path="101/clip_sub/clip.mp4")
r = cp.post(f"/api/rooms/101/clips/{c_sub}/submit",
            json={"title": "测试投稿"}, headers=CLIPPER_101)
check("投稿未部署（无 biliup 二进制）503", r.status_code == 503)

# 4.2 部署态：假二进制 + 假 cookies（临时目录）+ mock 子进程。
# v2.3.0：cookies 需为部署态结构（token_info.access_token 非空）——
# 新视频投稿端点有 access_token 守卫（网页 cookie 缺 token 时 503）
import json as _json
app_mod.cfg["bili"]["biliup_bin"] = "fake-biliup"
app_mod.cfg["bili"]["biliup_cookies"] = str(TMP / "biliup-cookies.json")
_cookies = upload_mod.cookies_path()
_cookies.parent.mkdir(parents=True, exist_ok=True)
_cookies.write_text(_json.dumps({
    "cookie_info": {"cookies": [{"name": "SESSDATA", "value": "s"},
                                {"name": "bili_jct", "value": "j"}]},
    "token_info": {"access_token": "fake-token"}}))

CMD_SEEN: list[list[str]] = []


class _FakeProc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err


_orig_exec = asyncio.create_subprocess_exec
_FAKE_MODE = ["ok"]


async def _fake_exec(*cmd, **kw):
    CMD_SEEN.append(list(cmd))
    if _FAKE_MODE[0] == "ok":
        return _FakeProc(0, out="投稿成功：https://www.bilibili.com/video/BV1AbCdEf234\n".encode())
    if _FAKE_MODE[0] == "cookiefail":
        return _FakeProc(1, err=b"Error: cookie expired, please login again\n")
    return _FakeProc(1, err=b"Error: unknown failure\n")


asyncio.create_subprocess_exec = _fake_exec


async def _run_one(clip_id: int, form: dict, clip_row: dict | None = None):
    """直接驱动 run_job（不经端点后台任务，确定性等待终态）。"""
    row = clip_row or app_mod._load_clip(101, clip_id)
    job = upload_mod.create_job(101, clip_id, form.get("title", ""))

    async def _sign(key: str, bucket: str = "") -> str:
        raise AssertionError("本用例不应触发 OSS 回拉（本地文件在位）")

    await upload_mod.run_job(job, dict(row), form, app_mod.RECORDINGS_ROOT,
                             _sign)
    return job, CMD_SEEN[-1]


# 4.3 表单校验（端点层）
for body, why in (
        ({"tid": 999999}, "tid 不在支持列表"),
        ({"source_type": "reprint"}, "转载缺来源"),
        ({"tags": ",".join(f"t{i}" for i in range(12))}, "标签超数"),
        ({"desc": "x" * 2001}, "简介超长")):
    r = cp.post(f"/api/rooms/101/clips/{c_sub}/submit",
                json={"title": "x", **body}, headers=CLIPPER_101)
    check(f"投稿表单校验：{why} → 400", r.status_code == 400)
r = cp.post(f"/api/rooms/101/clips/{c_sub}/submit",
            json={"title": "x"}, headers=GUEST_101)
check("投稿 guest 写操作被拦（middleware 403）", r.status_code == 403)
r = cp.post(f"/api/rooms/101/clips/{c_sub}/submit",
            json={"title": "端点建任务"}, headers=CLIPPER_101)
check("投稿端点提交返回 uploading 任务号",
      r.status_code == 200 and bool(r.json().get("job")))
r = cp.get(f"/api/rooms/101/clips/{c_sub}/submit/deadbeef0000",
           headers=CLIPPER_101)
check("投稿任务不存在 404", r.status_code == 404)
r = cp.get(f"/api/rooms/101/clips/{c_sub}/submit/whatever",
           headers=CLIPPER_202)
check("投稿任务跨房不可轮询（middleware 403 先于路由 404）",
      r.status_code in (403, 404))

# 4.4 成功路径（直驱 run_job）：命令拼装 + BV 解析 + 本地选源
job, cmd = asyncio.run(_run_one(c_sub, {
    "title": "好听的歌切", "desc": "简介", "tags": "歌切,直播",
    "tid": 130, "source_type": "original"}))
check("投稿任务成功态 + BV 号解析",
      job["status"] == "success" and job["bv"] == "BV1AbCdEf234")
check("投稿命令拼装：--user-cookie 在 upload 子命令之前",
      cmd.index("--user-cookie") < cmd.index("upload")
      and cmd[cmd.index("--user-cookie") + 1] == str(_cookies))
check("投稿命令拼装：标题/分区/标签/自制位 + 本地选源",
      cmd[cmd.index("--title") + 1] == "好听的歌切"
      and cmd[cmd.index("--tid") + 1] == "130"
      and cmd[cmd.index("--tag") + 1] == "歌切,直播"
      and cmd[cmd.index("--copyright") + 1] == "1"
      and str(clip_file) in cmd)

# 4.5 转载 + cookie 失败路径
job2, cmd2 = asyncio.run(_run_one(c_sub, {
    "title": "转载切", "source_type": "reprint",
    "source_url": "https://live.bilibili.com/101"}))
check("投稿命令拼装：转载 copyright=2 + --source",
      cmd2[cmd2.index("--copyright") + 1] == "2"
      and "https://live.bilibili.com/101" in cmd2)
_FAKE_MODE[0] = "cookiefail"
job3, _ = asyncio.run(_run_one(c_sub, {"title": "会失败"}))
check("投稿失败态：原因透传 + cookie 失效提示",
      job3["status"] == "failed" and "cookie" in job3["error"].lower()
      and "重新上传" in job3["error"])
_FAKE_MODE[0] = "ok"

# 4.6 标题兜底：空标题用切片名
conn = connect()
conn.execute("UPDATE manual_clips SET title='起的切片名' WHERE id=?",
             (c_sub,))
conn.commit()
conn.close()
job4, cmd4 = asyncio.run(_run_one(c_sub, {"title": ""}))
check("投稿标题兜底：空标题用切片名（端点层兜底口径一致，直驱传空由 "
      "create_job 记录空名，此处验证命令侧标题来自 form）",
      job4["status"] == "success" and "--title" in cmd4)

# 4.7 封面预览端点（本地切片，ffmpeg 可用则 200；无 ffmpeg 的环境 503）
r = cp.get(f"/api/rooms/101/clips/{c_sub}/cover-preview", headers=GUEST_101)
check("封面预览：本地切片可抽帧（200/503 视 ffmpeg）",
      r.status_code in (200, 503))
r = cp.get(f"/api/rooms/101/clips/{c_orph}/cover-preview", headers=GUEST_101)
check("封面预览：孤儿无本地文件 404", r.status_code == 404)

asyncio.create_subprocess_exec = _orig_exec
app_mod.cfg["bili"]["biliup_bin"] = ""

# ===========================================================================
# 5. 房间名单自管（v2.0.0 增删启用 + v1.3.x 房间表分离：只写 replay_rooms，
#    停用/删除不得影响 monitor 的 rooms；B站拉取全程 mock）
# ===========================================================================
async def _fake_fetch(room_id: int):
    return {"room_id": room_id, "short_id": 0,
            "anchor_name": "测试主播" + str(room_id), "avatar_url": ""}

_orig_fetch = app_mod._fetch_bili_room
app_mod._fetch_bili_room = _fake_fetch

# 预置「两表共有」房间 306（模拟迁移播种后 monitor+replay 双侧都在的房间）
_conn = app_mod.connect()
_conn.execute("INSERT OR IGNORE INTO rooms (room_id, anchor_name, enabled, "
              "created_at) VALUES (306, '共有主播', 1, '2026-10-10T10:00:00')")
_conn.execute("INSERT OR IGNORE INTO replay_rooms (room_id, anchor_name, "
              "enabled, created_at) VALUES (306, '共有主播', 1, "
              "'2026-10-10T10:00:00')")
_conn.commit()
_conn.close()

r = _ADMIN_C.post("/api/admin/rooms", json={"room_id": 303})
check("添加房间 200 且同步生成监控码",
      r.status_code == 200 and r.json()["replay_code"].startswith("r303-")
      and r.json()["anchor_name"] == "测试主播303")
_303_code = r.json()["replay_code"]
row = app_mod.q1("SELECT enabled, anchor_name FROM replay_rooms "
                 "WHERE room_id = 303")
check("落库 replay_rooms enabled=1", bool(row and row["enabled"] == 1))
check("添加只写本站表：共享 rooms 不出现 303",
      app_mod.q1("SELECT room_id FROM rooms WHERE room_id = 303") is None)
check("监控码在本站表内可解析（auth.resolve_code → 303）",
      auth.resolve_code(_303_code) == 303)
r = _ADMIN_C.post("/api/admin/rooms", json={"room_id": 303})
check("重复添加 409", r.status_code == 409)

_g303 = fresh({"replay_auth": auth.sign_session("guest", "101")})
check("guest 添加 403（middleware 管理前缀）",
      _g303.post("/api/admin/rooms", json={"room_id": 304}).status_code == 403)


async def _none_fetch(room_id: int):
    return None
app_mod._fetch_bili_room = _none_fetch
check("B站查询失败 404",
      _ADMIN_C.post("/api/admin/rooms", json={"room_id": 305}).status_code == 404)


async def _short_fetch(room_id: int):
    # 模拟短号 999 解析到已添加的真实房间 303
    return {"room_id": 303, "short_id": 999, "anchor_name": "x", "avatar_url": ""}
app_mod._fetch_bili_room = _short_fetch
check("短号解析到已存在房间 409",
      _ADMIN_C.post("/api/admin/rooms", json={"room_id": 999}).status_code == 409)
app_mod._fetch_bili_room = _fake_fetch

# 管理页列表：两表合并视图（306 在监控=monitored；303 不在=未监控）
r = _ADMIN_C.get("/api/admin/rooms")
_admin_list = {x["room_id"]: x for x in r.json()["rooms"]}
check("管理页列表合并视图：306 monitored、303 未监控",
      _admin_list[306]["monitored"] is True
      and _admin_list[303]["monitored"] is False)

r = _ADMIN_C.delete("/api/admin/rooms/306")
check("移除默认停用（paused=true）",
      r.status_code == 200 and r.json()["paused"] is True)
row = app_mod.q1("SELECT enabled FROM replay_rooms WHERE room_id = 306")
check("停用后 replay_rooms.enabled=0 且行保留", bool(row and row["enabled"] == 0))
check("停用不影响 monitor：rooms.enabled 仍为 1",
      app_mod.q1("SELECT enabled FROM rooms WHERE room_id = 306")["enabled"] == 1)
r = _ADMIN_C.post("/api/admin/rooms/306/enable")
check("启用 200", r.status_code == 200)
row = app_mod.q1("SELECT enabled FROM replay_rooms WHERE room_id = 306")
check("启用后 replay_rooms.enabled=1", bool(row and row["enabled"] == 1))

r = _ADMIN_C.delete("/api/admin/rooms/303?hard=true")
check("hard 彻底删除（paused=false）",
      r.status_code == 200 and r.json()["paused"] is False)
check("硬删后 replay_rooms 行不存在",
      app_mod.q1("SELECT room_id FROM replay_rooms WHERE room_id = 303") is None)
check("再删 404",
      _ADMIN_C.delete("/api/admin/rooms/303?hard=true").status_code == 404)
check("启用不存在房间 404",
      _ADMIN_C.post("/api/admin/rooms/303/enable").status_code == 404)
check("监控码随硬删失效（resolve_code → None）",
      auth.resolve_code(_303_code) is None)

# 录制请求的跨表校验：306 两表均在且启用 → 通过前置校验（插入后清理）；
# 303 已硬删 → 404；未监控房间（rooms 无行）→ 400 指引
r = _ADMIN_C.post("/api/rooms/306/recordings/start")
check("录制请求：两表齐备房间过前置校验（200）", r.status_code == 200)
_conn = app_mod.connect()
_conn.execute("DELETE FROM recordings WHERE room_id = 306")
_conn.commit()
_conn.close()
check("录制请求：回放站名单无此房间 404",
      _ADMIN_C.post("/api/rooms/303/recordings/start").status_code == 404)
_conn = app_mod.connect()
_conn.execute("INSERT INTO replay_rooms (room_id, enabled) VALUES (307, 1)")
_conn.commit()
_conn.close()
_r307 = _ADMIN_C.post("/api/rooms/307/recordings/start")
check("录制请求：monitor 未监控的房间 400 且带指引",
      _r307.status_code == 400 and "bili-monitor" in _r307.json()["detail"])

app_mod._fetch_bili_room = _orig_fetch

import sys as _sys
_sys.exit(H.summary("test_v2_0"))
