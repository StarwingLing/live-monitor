# -*- coding: utf-8 -*-
"""replay v0.1 业务 API 测试：录制 / 注释 / 切片 / 直播截取 / 归档分组 /
外挂弹幕 / 管理页接口。

覆盖：
1. 录制列表：注释挂载、活跃行实时体积、成片 file_url；
2. 起停状态机：同房并发防重 409、requested 取消转 failed；
3. 录播命名/删除：仅 admin（切片员 403）；删除联动成片/分段/低画质目录与行；
4. 录制注释：署名=会话身份（clipper 昵称 / admin 用户名）、仅本人可改删、
   内容非空与长度上限；
5. 打点切片：区间校验（起点/段长/超时长）、pending 行 created_by；
6. 切片改名/删除：创建者本人或 admin（他人 403）、processing 不可删；
7. 切片注释：与录制注释同口径；
8. 直播截取：需在录整场、同房唯一 409、过短 failed、正常 pending；
9. auto-recordings 分组：full（自动行）/standalone（短手动行）/orphan
   （源录播已清）、日期倒序、auto_full/live/in_local/on_oss 回传；
10. 外挂弹幕：timeline runs 精确映射（窗外丢弃）、无 timeline 近似模式；
11. 管理 API：房间列表字段、监控码重置（旧码立即失效）。

运行：uv run python tests/test_replay_api.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

from replay.web import app as app_mod
from replay.web.app import app
from replay import auth

from fastapi.testclient import TestClient

# ---- 生产模式 + 录像根指向临时目录（同 test_replay_auth）----
app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"
# 存量缺陷修复（v2.3.0 顺带）：本地 config.toml 自 v2.2.5 起 recording
# .enabled=false（托底采集对齐），起录端点会 400——测试须自带开关状态，
# 不随本地配置漂移
app_mod.cfg["recording"]["enabled"] = True
app_mod.RECORDINGS_ROOT = TMP / "recordings"
app_mod.RECORDINGS_ROOT.mkdir(exist_ok=True)
from fastapi.staticfiles import StaticFiles as _SF
for _route in app.router.routes:
    if getattr(_route, "path", "") == "/recordings":
        _route.app = _SF(directory=str(app_mod.RECORDINGS_ROOT))

from replay.db import connect
_conn = connect()
H.init_tables(_conn)
_conn.close()

REC = app_mod.RECORDINGS_ROOT


def seed_rec(room, status, source="manual", duration=3600, file_path="",
             started="2026-10-07T10:00:00", timeline="", title="",
             oss_key=""):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO recordings (room_id, status, source, duration_sec, "
            "file_path, file_size, started_at, ended_at, timeline, title, "
            "oss_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (room, status, source, duration, file_path, 12345 if file_path else 0,
             started, started if status == "finished" else None,
             timeline, title, oss_key, started))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def seed_clip(room, rec_id, status="done", file_path="", by="歌切工",
              start=100.0, end=160.0, created="2026-10-07T11:00:00"):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clips (recording_id, room_id, start_sec, end_sec, "
            "status, file_path, file_size, duration_sec, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec_id, room, start, end, status, file_path,
             999 if file_path else 0, int(end - start) if file_path else 0,
             by, created))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- 种子：录制/切片/文件 ----
rec1 = seed_rec(101, "finished", file_path="101/full_1.mp4", duration=3600,
                timeline=json.dumps({"runs": [
                    {"wall": "2026-10-07T10:00:00", "off": 0, "dur": 600}]}))
rec2 = seed_rec(101, "recording", source="auto", started="2026-10-07T12:00:00")
rec3 = seed_rec(101, "finished", duration=120, file_path="101/full_3.mp4",
                started="2026-10-06T20:00:00")
rec_del = seed_rec(101, "finished", file_path="101/full_9.mp4", duration=3000,
                   started="2026-10-05T20:00:00")
clip1 = seed_clip(101, rec1, file_path="101/clips/c1.mp4")
clip_orphan = seed_clip(101, 99999, file_path="101/clips/orphan.mp4",
                        created="2026-10-04T21:00:00")
for p in ("101/full_1.mp4", "101/full_3.mp4", "101/full_9.mp4", "101/clips/c1.mp4",
          "101/clips/orphan.mp4"):
    f = REC / p
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"0" * 4096)
(REC / "101" / f"full_{rec2}" / "seg_0.ts").parent.mkdir(parents=True, exist_ok=True)
(REC / "101" / f"full_{rec2}" / "seg_0.ts").write_bytes(b"0" * 1111)
# 弹幕（rec1 timeline：runs 覆盖 10:00:00–10:10:00）
conn = connect()
conn.executemany(
    "INSERT INTO danmaku (room_id, uid, uname, text, privilege_type, ts) "
    "VALUES (?,?,?,?,?,?)",
    [(101, 2, "观众A", "窗口内", 0, "2026-10-07T10:05:00"),
     (101, 3, "观众B", "开始前", 0, "2026-10-07T09:59:00"),
     (101, 4, "观众C", "结束后", 0, "2026-10-07T10:20:00"),
     (101, 5, "舰长君", "舰长弹幕", 3, "2026-10-07T10:06:30")])
conn.commit()
conn.close()

# ---- 客户端 ----
def fresh(cookies=None):
    c = TestClient(app)
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    return c


admin = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
clipA = fresh({"replay_auth": auth.sign_session("clipper", "101", nickname="歌切工")})
clipB = fresh({"replay_auth": auth.sign_session("clipper", "101", nickname="另一人")})
guest = fresh({"replay_auth": auth.sign_session("guest", "101")})

# ===========================================================================
# 1. 录制列表
# ===========================================================================
r = clipA.get(f"/api/rooms/101/recordings")
rows = {x["id"]: x for x in r.json()["recordings"]}
check("录制列表 200", r.status_code == 200)
check("成片行带 file_url", rows[rec1]["file_url"].endswith("/recordings/101/full_1.mp4"))
check("活跃行带实时体积", rows[rec2]["live_size"] == 1111)
conn = connect()
conn.execute("INSERT INTO recording_notes (recording_id, author, content, "
             "created_at, updated_at) VALUES (?,?,?,?,?)",
             (rec1, "歌切工", "第一段整场", "2026-10-07T11:00:00", "2026-10-07T11:00:00"))
conn.commit()
conn.close()
r = clipA.get(f"/api/rooms/101/recordings")
rows = {x["id"]: x for x in r.json()["recordings"]}
check("注释随录制行返回",
      bool(rows[rec1]["notes"]) and rows[rec1]["notes"][0]["author"] == "歌切工")

# ===========================================================================
# 2. 起停状态机
# ===========================================================================
r = clipA.post("/api/rooms/101/recordings/start")
check("同房已有进行中录制 409（种子 rec2 在录）", r.status_code == 409)
r = clipA.post("/api/rooms/202/recordings/start")
check("切片员跨房起录 403（中间件）", r.status_code == 403)
r = admin.post("/api/rooms/202/recordings/start")
check("admin 在空闲房间起录 200", r.status_code == 200)
rid = r.json()["id"]
r = admin.post("/api/rooms/202/recordings/start")
check("同房重复起录 409", r.status_code == 409)
r = admin.post(f"/api/rooms/202/recordings/{rid}/stop")
check("取消 requested → failed", r.status_code == 200)
conn = connect()
st = conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()
conn.close()
check("取消后行为 failed（已取消）", st["status"] == "failed")

# ===========================================================================
# 3. 录播命名 / 删除
# ===========================================================================
r = clipA.patch(f"/api/rooms/101/recordings/{rec1}", json={"title": "歌会全程"})
check("切片员命名录播 403", r.status_code == 403)
r = admin.patch(f"/api/rooms/101/recordings/{rec1}", json={"title": "歌会全程"})
check("admin 命名录播", r.status_code == 200 and r.json()["title"] == "歌会全程")
r = admin.patch(f"/api/rooms/101/recordings/{rec1}", json={"title": ""})
check("admin 清除名字", r.status_code == 200 and r.json()["title"] == "")
r = clipA.delete(f"/api/rooms/101/recordings/{rec_del}")
check("切片员删录播 403", r.status_code == 403)
r = admin.delete(f"/api/rooms/101/recordings/{rec_del}")
check("admin 删录播", r.status_code == 200)
check("删除联动成片消失", not (REC / "101" / "full_9.mp4").exists())
conn = connect()
gone = conn.execute("SELECT count(*) FROM recordings WHERE id=?", (rec_del,)).fetchone()[0]
conn.close()
check("删除联动行消失", gone == 0)

# ===========================================================================
# 4. 录制注释
# ===========================================================================
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/notes",
               json={"content": "开场清唱"})
check("切片员加注释（署名昵称）",
      r.status_code == 200 and r.json()["note"]["author"] == "歌切工")
nid = r.json()["note"]["id"]
r = clipB.patch(f"/api/rooms/101/recordings/{rec1}/notes/{nid}",
                json={"content": "改别人的"})
check("改他人注释 403", r.status_code == 403)
r = clipA.patch(f"/api/rooms/101/recordings/{rec1}/notes/{nid}",
                json={"content": "开场清唱（修正）"})
check("改自己注释", r.status_code == 200)
r = clipB.delete(f"/api/rooms/101/recordings/{rec1}/notes/{nid}")
check("删他人注释 403", r.status_code == 403)
r = clipA.delete(f"/api/rooms/101/recordings/{rec1}/notes/{nid}")
check("删自己注释", r.status_code == 200)
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/notes", json={"content": "  "})
check("空注释 400", r.status_code == 400)
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/notes",
               json={"content": "长" * 501})
check("超长注释 400", r.status_code == 400)
r = guest.post(f"/api/rooms/101/recordings/{rec1}/notes", json={"content": "x"})
check("游客写注释 403（中间件）", r.status_code == 403)

# ===========================================================================
# 5/6/7. 打点切片 / 改名删除 / 切片注释
# ===========================================================================
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/clips",
               json={"start_sec": 50, "end_sec": 40})
check("起点≥终点 400", r.status_code == 400)
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/clips",
               json={"start_sec": 50, "end_sec": 55})
check("段长不足 400", r.status_code == 400)
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/clips",
               json={"start_sec": 100, "end_sec": 9999})
check("终点超时长 400", r.status_code == 400)
r = clipA.post(f"/api/rooms/101/recordings/{rec1}/clips",
               json={"start_sec": 100, "end_sec": 160, "title": "第一首歌"})
check("切片创建 pending", r.status_code == 200)
new_clip = r.json()["id"]
conn = connect()
row = conn.execute("SELECT created_by, status FROM manual_clips WHERE id=?",
                   (new_clip,)).fetchone()
conn.close()
check("切片行署名+状态", row["created_by"] == "歌切工" and row["status"] == "pending")

r = clipB.patch(f"/api/rooms/101/clips/{clip1}", json={"title": "别人的"})
check("他人改切片名 403", r.status_code == 403)
r = clipA.patch(f"/api/rooms/101/clips/{clip1}", json={"title": "歌切·一"})
check("本人改切片名", r.status_code == 200)
r = admin.patch(f"/api/rooms/101/clips/{clip1}", json={"title": "歌切· administrated"})
check("admin 改任意切片名", r.status_code == 200)
conn = connect()
conn.execute("UPDATE manual_clips SET status='processing' WHERE id=?", (clip1,))
conn.commit()
conn.close()
r = admin.delete(f"/api/rooms/101/clips/{clip1}")
check("processing 不可删 409", r.status_code == 409)
conn = connect()
conn.execute("UPDATE manual_clips SET status='done' WHERE id=?", (clip1,))
conn.commit()
conn.close()
r = clipA.delete(f"/api/rooms/101/clips/{clip1}")
check("创建者删切片（文件联动）", r.status_code == 200
      and not (REC / "101" / "clips" / "c1.mp4").exists())

r = clipA.post(f"/api/rooms/101/clips/{clip_orphan}/notes", json={"content": "孤儿切片注释"})
check("切片注释署名", r.status_code == 200 and r.json()["note"]["author"] == "歌切工")
cnid = r.json()["note"]["id"]
r = clipB.delete(f"/api/rooms/101/clips/{clip_orphan}/notes/{cnid}")
check("删他人切片注释 403", r.status_code == 403)
r = clipA.delete(f"/api/rooms/101/clips/{clip_orphan}/notes/{cnid}")
check("删自己切片注释", r.status_code == 200)

# ===========================================================================
# 8. 直播截取
# ===========================================================================
r = clipA.post("/api/rooms/101/recordings/live-cut/start", json={})
check("在录房间可开始截取", r.status_code == 200)
lc = r.json()["id"]
r = clipB.post("/api/rooms/101/recordings/live-cut/start", json={})
check("同房重复截取 409", r.status_code == 409)
r = clipA.post(f"/api/rooms/101/recordings/live-cut/{lc}/stop")
check("停止截取（刚开 0 秒 → failed 400）", r.status_code == 400)
r = clipA.post(f"/api/rooms/101/recordings/live-cut/{lc}/stop")
check("停止截取幂等", r.status_code == 200)
r = clipA.post("/api/rooms/999/recordings/live-cut/start", json={})
check("截取房间不存在/跨房 403", r.status_code == 403)

# ===========================================================================
# 9. 归档分组（v2.2.2：同日整场行按日合并为单 full 组 recs[]，桥接片入组，
#    场次分隔 session_end 标记）
# ===========================================================================
# 上午一场：rec1（10:00 起 3600s）→ 桥接窗口 10:50（跨接缝）→ rec2 在录
# （11:02 起接续段，进行中无 duration）；晚间一场：rec_eve（20:00 起 2000s）
rec_bridge = seed_rec(101, "finished", source="bridge", duration=1206,
                      file_path="101/full_br.mp4",
                      started="2026-10-07T10:50:00")
rec_eve = seed_rec(101, "finished", source="auto", duration=2000,
                   started="2026-10-07T20:00:00")
conn = connect()
conn.execute(
    "UPDATE recordings SET started_at = '2026-10-07T11:02:00' WHERE id = ?",
    (rec2,))
conn.execute(
    "UPDATE recordings SET oss_key = ?, oss_low_key = ? WHERE id = ?",
    ("recordings/101/full_br.mp4", "recordings/101/full_br/low.mp4",
     rec_bridge))
conn.commit()
conn.close()
r = guest.get("/api/rooms/101/auto-recordings")
d = r.json()
check("游客可读归档（只读）", r.status_code == 200 and "groups" in d)
dates = [x["date"] for x in d["groups"]]
check("日期倒序", dates == sorted(dates, reverse=True))
by_date = {x["date"]: x["groups"] for x in d["groups"]}
today = by_date["2026-10-07"]
fulls = [g for g in today if g["kind"] == "full"]
standalones = [g for g in by_date.get("2026-10-06", []) if g["kind"] == "standalone"]
check("同日整场行合并为单 full 组（v2.2.2）", len(fulls) == 1)
ids_in_group = [rr["id"] for rr in fulls[0]["recs"]]
check("auto/进行中/长手动/桥接同组且按开始时间倒序",
      ids_in_group == [rec_eve, rec2, rec_bridge, rec1], str(ids_in_group))
sess = {rr["id"]: bool(rr.get("session_end")) for rr in fulls[0]["recs"]}
check("场次分隔-两场边界标在早场末段（rec2），同场轮换段/桥接不拆",
      sess == {rec_eve: False, rec2: True, rec_bridge: False, rec1: False},
      str(sess))
br = next(rr for rr in fulls[0]["recs"] if rr["id"] == rec_bridge)
check("桥接片携带 oss 键（本地已释放仍可云端播放）",
      br["oss_key"] == "recordings/101/full_br.mp4"
      and br["oss_low_key"] == "recordings/101/full_br/low.mp4"
      and br["on_oss"] is True and br["in_local"] is False)
check("短手动录制 → standalone 组", any(
    any(c["etype"] == "rec" and c["id"] == rec3 for c in g["clips"])
    for g in standalones))
check("桥接不落 standalone 桶", not any(
    any(c.get("id") == rec_bridge for c in g["clips"])
    for g in today if g["kind"] == "standalone"))
orphans = [g for g in by_date.get("2026-10-04", []) if g["kind"] == "orphan"]
check("孤儿切片 → orphan 组",
      bool(orphans) and orphans[0]["clips"][0]["id"] == clip_orphan)
full_rec1 = next(rr for rr in fulls[0]["recs"] if rr["id"] == rec1)
check("full 组挂切片", any(c["id"] == new_clip for c in full_rec1["clips"]))
check("in_local/on_oss 回传", full_rec1["in_local"] is True
      and full_rec1["on_oss"] is False)
check("auto_full/live 回传", d["auto_full"] in (True, False) and d["live"] is True)

# ===========================================================================
# 10. 外挂弹幕
# ===========================================================================
r = guest.get(f"/api/rooms/101/recordings/{rec1}/danmaku")
d = r.json()
check("弹幕精确映射（approx=false）", d["approx"] is False and d["count"] == 2)
ts_list = [x["t"] for x in d["items"]]
check("映射时间正确（300s/390s）", ts_list == [300.0, 390.0]
      and d["items"][1]["guard"] == 3)
rec_no_tl = seed_rec(101, "finished", duration=600,
                     file_path="101/full_nt.mp4", started="2026-10-06T10:00:00")
(REC / "101" / "full_nt.mp4").parent.mkdir(parents=True, exist_ok=True)
(REC / "101" / "full_nt.mp4").write_bytes(b"0")
conn = connect()
conn.execute("INSERT INTO danmaku (room_id, uid, uname, text, privilege_type, ts) "
             "VALUES (101, 6, '观众D', '近似窗内', 0, '2026-10-06T10:03:00')")
conn.commit()
conn.close()
r = guest.get(f"/api/rooms/101/recordings/{rec_no_tl}/danmaku")
d = r.json()
check("无 timeline 走近似（approx=true）",
      d["approx"] is True and d["count"] == 1 and d["items"][0]["t"] == 180.0)

# ===========================================================================
# 11. 管理页 API
# ===========================================================================
r = admin.get("/api/admin/rooms")
rooms = {x["room_id"]: x for x in r.json()["rooms"]}
check("管理列表含在播/开关/监控码",
      rooms[101]["live"] is True and rooms[202]["auto_full"] in (True, False)
      and rooms[101]["replay_code"] == "r101-ABCDEF1234")
r = clipA.get("/api/admin/rooms")
check("切片员禁读管理列表", r.status_code == 403)
r = admin.post("/api/rooms/101/replay-code/reset")
check("重置监控码返回新码",
      r.status_code == 200 and bool(r.json()["replay_code"]))
new_code = r.json()["replay_code"]
check("新码可解析", auth.resolve_code(new_code) == 101)
check("旧码立即失效", auth.resolve_code("r101-ABCDEF1234") is None)
# 切片员密码明文回显端点（v0.1.2）
r = admin.get("/api/admin/clipper-password")
check("明文读取切片员密码（未播种为空）",
      r.status_code == 200 and r.json()["clipper_password"] == "")
auth.set_clipper_password("api-pw-123")
r = admin.get("/api/admin/clipper-password")
check("明文读取切片员密码（设置后回显）",
      r.status_code == 200 and r.json()["clipper_password"] == "api-pw-123")
r = clipA.get("/api/admin/clipper-password")
check("切片员禁读密码明文", r.status_code == 403)
r = admin.post("/api/rooms/202/auto-full", json={"enabled": False})
check("关闭自动全程录制", r.status_code == 200)

# ===========================================================================
# 12. v1.1 双 bucket：oss-url 签名与删除联动按切片行 oss_bucket 路由
#     （切片上独立桶 [oss].clips_bucket 由 bili v1.4 上云写入，本端只按
#      行内 oss_bucket 路由；空 = 主桶，兼容 v1.0 存量切片）
# ===========================================================================
app_mod.cfg["oss"].update({
    "enabled": True, "bucket": "test-bucket",
    "access_key_id": "ak-test", "access_key_secret": "sk-test",
    "region": "cn-hangzhou",
})

# 桩公网签名客户端：presign 捕获 (bucket, key)
signed: list[tuple[str, str]] = []


class _FakePresignClient:
    def presign(self, req, expires=None, **kw):
        signed.append((req.bucket, req.key))

        class _R:
            url = (f"https://{req.bucket}.oss-cn-hangzhou.aliyuncs.com/"
                   f"{req.key}?x-oss-signature=fake")
        return _R()


app_mod._OSS_PUB_CLIENT.clear()
app_mod._OSS_PUB_CLIENT.append(_FakePresignClient())

clip_mid = seed_clip(101, rec1, file_path="101/clips/mid.mp4")
clip_old = seed_clip(101, rec1, file_path="101/clips/old.mp4")
conn = connect()
conn.execute("UPDATE manual_clips SET oss_key = ?, oss_bucket = ? WHERE id = ?",
             ("recordings/101/clips/mid.mp4", "test-bucket-mid",
              clip_mid))
conn.execute("UPDATE manual_clips SET oss_key = ?, oss_bucket = '' WHERE id = ?",
             ("recordings/101/clips/old.mp4", clip_old))
conn.commit()
conn.close()

r = admin.get("/api/rooms/101/oss-url?key=recordings/101/clips/mid.mp4")
check("签名-切片按行 oss_bucket 路由独立桶", r.status_code == 200
      and signed[-1] == ("test-bucket-mid",
                         "recordings/101/clips/mid.mp4"), r.text[:200])
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/clips/old.mp4")
check("签名-空 oss_bucket 防御回退主桶", r.status_code == 200
      and signed[-1] == ("test-bucket",
                         "recordings/101/clips/old.mp4"))
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/full_1.mp4")
check("签名-录播 key 走主桶", r.status_code == 200
      and signed[-1] == ("test-bucket", "recordings/101/full_1.mp4"))
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/full_1/low.mp4")
check("签名-low 副本 key 走主桶", r.status_code == 200
      and signed[-1] == ("test-bucket",
                         "recordings/101/full_1/low.mp4"))
# bili v2.1（2026-10-12）行级桶：录播原片 10 天热窗口在 mid 桶（行
# oss_bucket=mid）、low 恒 mid（oss_low_bucket）；'' 仍回退主桶（上面
# 两条即空值回退语义）
conn = connect()
conn.execute("UPDATE recordings SET oss_bucket = ?, oss_low_bucket = ?, "
             "oss_key = ?, oss_low_key = ? WHERE id = ?",
             ("test-bucket-mid", "test-bucket-mid",
              "recordings/101/full_1.mp4", "recordings/101/full_1/low.mp4",
              rec1))
conn.commit()
conn.close()
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/full_1.mp4")
check("签名-v2.1 录播原片热窗口签 mid", r.status_code == 200
      and signed[-1] == ("test-bucket-mid",
                         "recordings/101/full_1.mp4"), r.text[:200])
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/full_1/low.mp4")
check("签名-v2.1 录播 low 恒签 mid", r.status_code == 200
      and signed[-1] == ("test-bucket-mid",
                         "recordings/101/full_1/low.mp4"), r.text[:200])
# 2026-10-11 修复回归：切片 low 键（oss_low_key）同样按行路由独立桶
#（此前只匹配 oss_key，low 键误签主桶 404）
conn = connect()
conn.execute("UPDATE manual_clips SET oss_low_key = ? WHERE id = ?",
             ("recordings/101/clips/mid/low.mp4", clip_mid))
conn.commit()
conn.close()
r = admin.get("/api/rooms/101/oss-url?key=recordings/101/clips/mid/low.mp4")
check("签名-切片 low 键按行路由独立桶", r.status_code == 200
      and signed[-1] == ("test-bucket-mid",
                         "recordings/101/clips/mid/low.mp4"), r.text[:200])

# 删除联动按行 oss_bucket 传 bucket
deleted2: list[tuple[str, str]] = []
app_mod._oss_delete = lambda key, bucket="": deleted2.append((key, bucket))
r = admin.delete(f"/api/rooms/101/clips/{clip_mid}")
check("删除-切片联动删独立桶", r.status_code == 200
      and ("recordings/101/clips/mid.mp4", "test-bucket-mid")
      in deleted2, str(deleted2))
r = admin.delete(f"/api/rooms/101/clips/{clip_old}")
check("删除-空 bucket 回退主桶", r.status_code == 200
      and ("recordings/101/clips/old.mp4", "") in deleted2, str(deleted2))

# ===========================================================================
# 13. v1.2 切片低清预览：auto-recordings 回传 oss_low_key + 删除联动
#     （切片 low 由 bili v1.5 转码线程生产并上其 oss_bucket 同桶；本端
#      透传字段给前端做 OSS low 优先候选，删除时本地 low 目录与云端
#      low 对象一并清）
# ===========================================================================
clip_low = seed_clip(101, rec1, file_path="101/manual/clip_low.mp4")
conn = connect()
conn.execute(
    "UPDATE manual_clips SET oss_key = ?, oss_bucket = ?, oss_low_key = ? "
    "WHERE id = ?",
    ("recordings/101/manual/clip_low.mp4", "test-bucket-mid",
     "recordings/101/manual/clip_low/low.mp4", clip_low))
conn.commit()
conn.close()
# 本地：切片本体 + low 目录（真实布局 <房>/manual/clip_<id>/low.mp4）
(REC / "101" / "manual").mkdir(parents=True, exist_ok=True)
(REC / "101" / "manual" / "clip_low.mp4").write_bytes(b"0" * 4096)
low_dir = REC / "101" / "manual" / "clip_low"
low_dir.mkdir()
(low_dir / "low.mp4").write_bytes(b"low")

r = admin.get("/api/rooms/101/auto-recordings")
found = [c for d in r.json()["groups"] for g in d["groups"]
         for rr in (g.get("recs") or [])
         for c in (rr.get("clips") or [])
         if c["id"] == clip_low]
check("回传-切片行带 oss_low_key",
      bool(found) and found[0]["oss_low_key"]
      == "recordings/101/manual/clip_low/low.mp4", str(found))

deleted3: list[tuple[str, str]] = []
app_mod._oss_delete = lambda key, bucket="": deleted3.append((key, bucket))
r = admin.delete(f"/api/rooms/101/clips/{clip_low}")
check("删除-切片本体与 low 双对象同桶联动删", r.status_code == 200
      and ("recordings/101/manual/clip_low.mp4", "test-bucket-mid")
      in deleted3
      and ("recordings/101/manual/clip_low/low.mp4",
           "test-bucket-mid") in deleted3, str(deleted3))
check("删除-本地切片与 low 目录一并清",
      not (REC / "101" / "manual" / "clip_low.mp4").exists()
      and not low_dir.exists())

sys.exit(H.summary("test_replay_api"))
