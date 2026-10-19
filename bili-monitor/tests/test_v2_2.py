# -*- coding: utf-8 -*-
"""v2.2 收益/人气曲线接口测试：/api/rooms/{room}/sessions/{sid}/curves
（详情页原高能时刻板块位置的替代板块数据源）。

覆盖：
1. 分钟桶对齐：gifts（gift/sc/guard 全口径）按分钟聚合 value_yuan、
   人气取 popularity_snapshots.viewers（WS ONLINE_RANK_COUNT 房间观众数，
   v2.5.1 起为本板块标准，取代 v2.2~v2.5.0 的 anchor_snapshots.online
   加权热度）就近取值铺满网格，无礼物分钟 rev=null；
2. 汇总字段 total_rev / peak_pop（峰值只计场次窗口内快照）；
3. 超长场次（>2000 分钟）退化为只输出有值分钟；
4. 边界：房间不符 404、场次不存在 404、空场次（无礼物无快照）返回空网格
   （起止各放宽 1 分钟）；
5. 归属断言：高能三端点（record 开关 / high-energy / clips danmaku）与
   /recordings 挂载、/oss-url 已随 v2.2 移除（404/405）；
6. v2.5.1 语义：人气快照按 session_id 直查——在播场次只取本场次快照，
   不再穿场借用其他场次的采样。

运行：uv run python tests/test_v2_2.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bili_monitor import db as dbm

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="bili_v22_test_"))

from bili_monitor.db import init_db, now_iso
from bili_monitor.web import app as app_mod
from bili_monitor.web import auth

auth.CRED_FILE = TMP / "web_credentials.txt"
dbm.DB_PATH = TMP / "test.db"
dbm.DATA_DIR = TMP
init_db()

conn = dbm.connect()
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (123456, '测试甲', 1, ?)", (now_iso(),))
# 场次 1：21:00-21:05（5 分钟）；礼物两笔同分钟（应聚合）、一笔下一分钟；
# 人气快照四条（popularity_snapshots.viewers，60s 采样口径，session_id=1
# 归属）+ 一条窗口外（20:58:50）验证就近取兜边界、且窗外值不计入峰值
conn.execute("INSERT INTO live_sessions (room_id, start_time, end_time) "
             "VALUES (123456, '2026-10-12T21:00:00', '2026-10-12T21:05:00')")
for ts, val, typ in (("2026-10-12T21:00:30", 10.0, "gift"),
                     ("2026-10-12T21:00:50", 5.5, "sc"),
                     ("2026-10-12T21:02:10", 138.0, "guard")):
    conn.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, "
                 "type, name, text, count, value_yuan, gift_img, blind_box, ts) "
                 "VALUES (1, 123456, 0, 1, 'u', ?, 'n', '', 1, ?, '', '', ?)",
                 (typ, val, ts))
for ts, viewers in (("2026-10-12T20:58:50", 50),   # 窗口外（session 归属在）
                    ("2026-10-12T21:00:20", 100),
                    ("2026-10-12T21:01:20", 300),
                    ("2026-10-12T21:02:20", 200)):
    conn.execute("INSERT INTO popularity_snapshots "
                 "(room_id, session_id, popularity, viewers, ts) "
                 "VALUES (123456, 1, 1, ?, ?)", (viewers, ts))
# 场次 2：已结束的空场次，无任何归属本场次的人气快照 → 零汇总空网格；
# 场次 3：在播空场次（起点昨天，窗口到当前）——v2.5.1 起人气按 session_id
# 直查，不再穿场借用场次 1 的快照，只取本场次自己的采样
conn.execute("INSERT INTO live_sessions (room_id, start_time, end_time) "
             "VALUES (123456, '2026-10-11T22:00:00', '2026-10-11T22:30:00')")
cur = conn.execute("INSERT INTO live_sessions (room_id, start_time) "
                   "VALUES (123456, '2026-10-11T22:00:00')")
sid_empty = cur.lastrowid - 1   # 场次 2（上一行）
sid_live = cur.lastrowid
# 在播场次自己的两条快照（验证 v2.5.1 只取本场次采样、不穿场）
for ts, viewers in (("2026-10-11T22:10:00", 77), ("2026-10-11T22:20:00", 88)):
    conn.execute("INSERT INTO popularity_snapshots "
                 "(room_id, session_id, popularity, viewers, ts) "
                 "VALUES (123456, ?, 1, ?, ?)", (sid_live, viewers, ts))
conn.commit()
conn.close()

app_mod.cfg["capture"]["admin_password"] = ""   # 调试模式直连
client = TestClient(app_mod.app)

# ---- 1/2：分钟桶与汇总 ----
r = client.get("/api/rooms/123456/sessions/1/curves")
check("curves-200", r.status_code == 200, r.text[:120])
d = r.json()
pts = {p["t"]: p for p in d["points"]}
check("curves-汇总", d["total_rev"] == 153.5 and d["peak_pop"] == 300, str(d)[:120])
m0 = pts.get("2026-10-12T21:00:00")
m2 = pts.get("2026-10-12T21:02:00")
m1 = pts.get("2026-10-12T21:01:00")
check("curves-同分钟礼物聚合", m0 and m0["rev"] == 15.5, str(m0))
check("curves-无礼物分钟rev-null", m0 and m0["pop"] == 100
      and m1 and m1["rev"] is None and m1["pop"] == 300
      and m2 and m2["rev"] == 138.0 and m2["pop"] == 200,
      f"{m0} {m1} {m2}")
mb = pts.get("2026-10-12T20:59:00")
m3 = pts.get("2026-10-12T21:03:00")
check("curves-就近取-边界分钟取窗外最近快照", mb and mb["pop"] == 50, str(mb))
check("curves-就近取-快照间隔分钟向后铺满", m3 and m3["pop"] == 200, str(m3))
check("curves-窗口边界放宽", "2026-10-12T20:59:00" in pts
      and "2026-10-12T21:06:00" in pts)

# ---- 3：空数据场次 → 非空网格（有边界分钟）、汇总零；在播场次（v2.5.1）
#      人气按 session_id 归属，只取本场次快照，不穿场借其他场次采样 ----
r2 = client.get(f"/api/rooms/123456/sessions/{sid_empty}/curves")
d2 = r2.json()
check("curves-空场次网格", r2.status_code == 200 and d2["points"]
      and d2["total_rev"] == 0 and d2["peak_pop"] == 0
      and all(p["pop"] is None for p in d2["points"]), r2.text[:160])
r3 = client.get(f"/api/rooms/123456/sessions/{sid_live}/curves")
d3 = r3.json()
check("curves-在播场次只取本场次快照", r3.status_code == 200
      and d3["peak_pop"] == 88
      and all(p["pop"] in (77, 88, None) for p in d3["points"]),
      str(d3)[:160])

# ---- 4：边界 404 ----
check("curves-跨房404", client.get(
    "/api/rooms/654321/sessions/1/curves").status_code == 404)
check("curves-场次不存在404", client.get(
    "/api/rooms/123456/sessions/999/curves").status_code == 404)

# ---- 4b：场次封面透传（v2.5.1 live_sessions.cover_url → dashboard
#       session_info.cover_url，详情页封面板块数据源；空值场次回空串） ----
conn = dbm.connect()
conn.execute("UPDATE live_sessions SET cover_url = ? WHERE session_id = 1",
             ("https://i0.hdslb.com/bfs/live/cover-test.jpg",))
conn.commit(); conn.close()
d4 = client.get("/api/rooms/123456/dashboard?session_id=1").json()
check("dashboard-场次封面透传",
      d4.get("session_info", {}).get("cover_url") == "https://i0.hdslb.com/bfs/live/cover-test.jpg",
      str(d4.get("session_info"))[:120])
d5 = client.get(f"/api/rooms/123456/dashboard?session_id={sid_empty}").json()
check("dashboard-无封面场次回空串",
      d5.get("session_info", {}).get("cover_url") == "",
      str(d5.get("session_info"))[:120])

# ---- 5：高能/回放遗产端点移除断言 ----
check("移除-record开关端点", client.post(
    "/api/rooms/123456/record", json={"enabled": True}).status_code in (404, 405))
check("移除-high-energy端点", client.get(
    "/api/rooms/123456/high-energy").status_code in (404, 405))
check("移除-片段弹幕端点", client.get(
    "/api/rooms/123456/clips/1/danmaku").status_code in (404, 405))
check("移除-oss-url端点", client.get(
    "/api/rooms/123456/oss-url?key=x").status_code in (404, 405))
# StaticFiles 挂载已删：该路径落到 SPA 兜底返回页面 shell（text/html），
# 而非挂载直出的 video/mp4
_r = client.get("/recordings/123456/full_1.mp4")
check("移除-recordings挂载（SPA 兜底而非视频直出）",
      _r.headers.get("content-type", "").startswith("text/html"))

print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
