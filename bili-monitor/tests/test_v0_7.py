# -*- coding: utf-8 -*-
"""v0.7 观众留存对照窗口切换测试：retention 接口新增 ref_days 参数。

背景：数据分析页"观众留存"板块右上角说明文字改为 3/7/30/90 日切换按钮，
默认跟随页面统计窗口天数；后端对照窗口由写死的 RETENTION_REF_DAYS=3 改为
可传参（缺省仍为 3，保持 v0.6 口径与既有调用兼容）。

覆盖（沿用 tests/test_v0_6.py 的造数场景，见该文件头部场次图）：
1. 缺省口径保持：不带 ref_days → 响应回显 ref_days=3，B 场对照仅 A（前 2 人）；
2. ref_days=7 拓宽：B 场对照 = A∪G∪F 共 7 人（G 82h、F 120h 均入 168h 窗），
   overlap=3 → 3/7；响应回显 ref_days=7；
3. ref_days=1 收窄：24h 边界整（C 对照 B）计入、超窗（A 距 C 48h）排除；
   D/E 的对照池随之变化；
4. ref_days=30：窗口起点再前推 30 天的对照池（F 仍为最早场 → retention=null）；
5. 参数校验：ref_days=0/366 → 422，ref_days=1/90 → 200；房间不存在 404。

运行：uv run python tests/test_v0_7.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bili_monitor import db as dbm
from bili_monitor.db import init_db, now_iso
from bili_monitor.web import app as app_mod

app = app_mod.app
results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# 造数：房间 888002，与 test_v0_6.py 相同的 7 场已结束直播与观众构成（省略
# 活跃度快照 / 问答快照 / 用户画像——本套只测留存）：
#   F：today-8  20:00  观众 {11,12}（弹幕）
#   G：today-6  10:00  观众 {1,2,3,11}（弹幕）
#   A：today-5  20:00  观众 {9,10}（弹幕）
#   B：today-3  20:00  观众 {1,2,3}（弹幕；uid=0 脏数据不计数）
#   C：today-2  20:00  观众 {2,3,4,5,7}（弹幕 2/3/4 + 礼物 5 + 互动 7）
#   D：today-1  14:00  观众 {1,4}（弹幕）
#   E：today-1  20:00  观众 {1,2,3,4,5,6}（弹幕 1/2/3/4 + 礼物 5 + 互动 6）
# ---------------------------------------------------------------------------
import datetime as dt

TODAY = dt.date.today()


def day(offset: int, hm: str = "00:00:00") -> str:
    return (TODAY + dt.timedelta(days=offset)).strftime("%Y-%m-%d") + "T" + hm


tmpdir = tempfile.mkdtemp(prefix="bili_v07_test_")
TMP_DB = Path(tmpdir) / "web.db"
init_db(TMP_DB)
dbm.DB_PATH = TMP_DB

conn = dbm.connect(TMP_DB)
RID = 888002
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (?, '测试主播', 1, ?)", (RID, now_iso()))

sess = [  # (相对今天偏移, start, end, dm_cnt, gift_val, peak_v)
    (-8,  "T20:00:00", "T21:00:00", 2, 0.0, 0),     # F id=1
    (-6,  "T10:00:00", "T11:00:00", 4, 0.0, 0),     # G id=2
    (-5,  "T20:00:00", "T21:00:00", 2, 0.0, 0),     # A id=3
    (-3,  "T20:00:00", "T21:00:00", 5, 0.0, 0),     # B id=4
    (-2,  "T20:00:00", "T21:00:00", 4, 10.0, 10),   # C id=5
    (-1,  "T14:00:00", "T15:00:00", 3, 0.0, 100),   # D id=6
    (-1,  "T20:00:00", "T21:00:00", 4, 20.0, 2),    # E id=7
]
for off, s, e, dm, gv, pv in sess:
    conn.execute("INSERT INTO live_sessions (room_id, start_time, end_time, peak_popularity,"
                 " peak_viewers, danmaku_count, gift_value_yuan) VALUES (?, ?, ?, 0, ?, ?, ?)",
                 (RID, day(off, s[1:]), day(off, e[1:]), pv, dm, gv))


def dm(sid: int, uid: int, ts: str):
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (?, ?, ?, ?, 'hi', ?)", (sid, RID, uid, f"u{uid}", ts))


dm(1, 11, day(-8, "20:10:00")); dm(1, 12, day(-8, "20:20:00"))            # F
for i, uid in enumerate((1, 2, 3, 11)):
    dm(2, uid, day(-6, f"10:0{i + 5}:00"))                                 # G
dm(3, 9, day(-5, "20:10:00")); dm(3, 10, day(-5, "20:20:00"))             # A
dm(4, 1, day(-3, "20:10:00")); dm(4, 1, day(-3, "20:11:00"))
dm(4, 2, day(-3, "20:13:00")); dm(4, 3, day(-3, "20:14:00"))
dm(4, 0, day(-3, "20:15:00"))                                             # B（uid=0 不计数）
dm(5, 2, day(-2, "20:00:10")); dm(5, 3, day(-2, "20:00:20"))
dm(5, 4, day(-2, "20:03:10"))
conn.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name,"
             " count, value_yuan, ts) VALUES (?, ?, 0, 5, 'u5', 'gift', '小心心', 1, 10.0, ?)",
             (5, RID, day(-2, "20:02:00")))
conn.execute("INSERT INTO interactions (session_id, room_id, uid, uname, type, ts) "
             "VALUES (?, ?, 7, 'u7', 'follow', ?)", (5, RID, day(-2, "20:03:30")))
dm(6, 1, day(-1, "14:00:10")); dm(6, 4, day(-1, "14:00:20"))              # D
for uid in (1, 2, 3, 4):
    dm(7, uid, day(-1, f"20:00:{uid * 10:02d}"))
conn.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name,"
             " count, value_yuan, ts) VALUES (?, ?, 0, 5, 'u5', 'gift', '小花花', 1, 20.0, ?)",
             (7, RID, day(-1, "20:00:30")))
conn.execute("INSERT INTO interactions (session_id, room_id, uid, uname, type, ts) "
             "VALUES (?, ?, 6, 'u6', 'share', ?)", (7, RID, day(-1, "20:00:40")))
conn.commit()
conn.close()

# 本地调试模式（不强制登录），结束后恢复原值
orig_pw = app_mod.cfg["capture"]["admin_password"]
app_mod.cfg["capture"]["admin_password"] = ""
client = TestClient(app)


def date_of(offset: int) -> str:
    return (TODAY + dt.timedelta(days=offset)).isoformat()


def sess_at(resp: dict, offset: int, hm: str) -> dict:
    """取 days_list 中指定日期 + 开播时刻的场次条目。"""
    return next(s for x in resp["days_list"] if x["date"] == date_of(offset)
                for s in x["sessions"] if s["start_time"] == day(offset, hm))


# ---------------------------------------------------------------------------
# 1. 缺省口径保持（不带 ref_days = v0.6 的前 3 天）
# ---------------------------------------------------------------------------
r = client.get(f"/api/rooms/{RID}/analysis/retention?days=7").json()
check("缺省-响应回显ref_days=3", r["ref_days"] == 3, f"实际 {r.get('ref_days')}")
b3 = sess_at(r, -3, "20:00:00")
# B 对照仅 A{9,10}（G 距 82h、F 距 120h，均超 72h）；观众 {1,2,3} 交集 0 → 0/2
check("缺省-对照窗口仍为3天", b3["retention"] == 0.0 and b3["prev_total"] == 2
      and b3["overlap"] == 0, f"实际 {b3}")

# ---------------------------------------------------------------------------
# 2. ref_days=7 拓宽对照池
# ---------------------------------------------------------------------------
r7 = client.get(f"/api/rooms/{RID}/analysis/retention?days=7&ref_days=7").json()
check("ref7-响应回显ref_days=7", r7["ref_days"] == 7 and len(r7["days_list"]) == 7)
b7 = sess_at(r7, -3, "20:00:00")
# B 对照 = A{9,10} ∪ G{1,2,3,11}（82h 入 168h 窗）∪ F{11,12}（120h 入窗）= 7 人
check("ref7-拓宽对照池并集", b7["prev_total"] == 7 and b7["overlap"] == 3
      and b7["retention"] == round(3 / 7, 4), f"实际 {b7}")
g7 = sess_at(r7, -6, "10:00:00")
# 窗口首场 G 对照 = F（距 2 天 10h 入窗）→ 与 v0.6 相同：1/2
check("ref7-窗口首场跨窗对照", g7["retention"] == 0.5 and g7["prev_total"] == 2, f"实际 {g7}")

# ---------------------------------------------------------------------------
# 3. ref_days=1 收窄对照池（days=3：窗口含 C/D/E，B/A 只作对照）
# ---------------------------------------------------------------------------
r1 = client.get(f"/api/rooms/{RID}/analysis/retention?days=3&ref_days=1").json()
c1 = sess_at(r1, -2, "20:00:00")
# C 对照 = B（恰 24h 整，边界计入）{1,2,3}；A 距 48h 超窗排除 → 交集{2,3} → 2/3
check("ref1-24h边界计入", c1["retention"] == round(2 / 3, 4) and c1["prev_total"] == 3
      and c1["overlap"] == 2, f"实际 {c1}")
d1 = sess_at(r1, -1, "14:00:00")
# D 对照 = C（18h 入窗）{2,3,4,5,7}；B 距 43h 超窗 → 交集{4} → 1/5
check("ref1-收窄后对照池", d1["retention"] == 0.2 and d1["prev_total"] == 5
      and d1["overlap"] == 1, f"实际 {d1}")
e1 = sess_at(r1, -1, "20:00:00")
# E 对照 = D（6h 入窗）{1,4} ∪ C（恰 24h 整，边界计入）{2,3,4,5,7} = 6 人，
# E 观众 {1,2,3,4,5,6} 交集 {1,2,3,4,5} → 5/6
check("ref1-同日后场对照", e1["retention"] == round(5 / 6, 4) and e1["prev_total"] == 6
      and e1["overlap"] == 5, f"实际 {e1}")

# ---------------------------------------------------------------------------
# 4. ref_days=30：对照池起点再前推 30 天（F 仍为最早场 → retention=null）
# ---------------------------------------------------------------------------
r30 = client.get(f"/api/rooms/{RID}/analysis/retention?days=30&ref_days=30").json()
f30 = next(s for x in r30["days_list"] for s in x["sessions"]
           if s["start_time"] == day(-8, "20:00:00"))
check("ref30-最早场无对照为null", f30["retention"] is None and f30["prev_total"] == 0
      and len(r30["days_list"]) == 30, f"实际 {f30}")

# ---------------------------------------------------------------------------
# 5. 边界：参数校验 / 404
# ---------------------------------------------------------------------------
check("边界-ref_days=1/90", client.get(f"/api/rooms/{RID}/analysis/retention?days=1&ref_days=1").status_code == 200
      and client.get(f"/api/rooms/{RID}/analysis/retention?days=1&ref_days=90").status_code == 200)
check("边界-ref_days超限422",
      client.get(f"/api/rooms/{RID}/analysis/retention?ref_days=0").status_code == 422
      and client.get(f"/api/rooms/{RID}/analysis/retention?ref_days=366").status_code == 422)
check("边界-房间不存在404",
      client.get("/api/rooms/1/analysis/retention?ref_days=7").status_code == 404)

app_mod.cfg["capture"]["admin_password"] = orig_pw
print("\n汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
