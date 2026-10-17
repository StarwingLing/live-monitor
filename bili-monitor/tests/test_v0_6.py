# -*- coding: utf-8 -*-
"""v0.6 数据分析页增强本地测试：观众留存 / 直播活跃度（任一互动口径）/ 数据问答 / 7日窗口。

覆盖（口径详见 v0.6 设计方案；留存为第三轮调整后的"前三天"口径）：
1. 留存：与前 3 天（开播前 72 小时内所有已结束场次）观众**并集**的重合度——
   多场并集、72h 边界（恰好 3 天整计入）、超窗排除、窗口外对照场、
   uid=0 排除、弹幕/礼物/互动三表并集、一日多场分组、无直播日留空；
2. 活跃度：分钟"任一互动去重人数 ÷ 分钟平均同接"（含"仅送礼/仅互动也计入"）、
   viewers=0 分钟跳过、无快照分钟丢弃、无快照场次 null、日聚合平均/最高、
   >100% 不截断；
3. 数据问答：12 类条目按造数精确断言（答案字符串全匹配），days=7 可用；
4. 404 / days 边界。

运行：uv run python tests/test_v0_6.py
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
# 造数：房间 888001，7 场已结束直播（日期相对今天生成，避免硬编码）
#   F：today-8  20:00  观众 {11,12}（弹幕；7 日窗口外的对照场，恰在 A 前 72h 整）
#   G：today-6  10:00  观众 {1,2,3,11}（弹幕）—— 窗口内首场，对照=F（窗口外）
#   A：today-5  20:00  观众 {9,10}（弹幕）—— 与 F 恰好相隔 3 天整（边界计入）
#   B：today-3  20:00  观众 {1,2,3}（弹幕；另有一条 uid=0 脏数据不计数）
#   C：today-2  20:00  观众 {2,3,4,5,7}（弹幕 2/3/4 + 礼物 5 + 互动 7）
#   D：today-1  14:00  观众 {1,4}（弹幕）
#   E：today-1  20:00  观众 {1,2,3,4,5,6}（弹幕 1/2/3/4 + 礼物 5 + 互动 6）
# ---------------------------------------------------------------------------
import datetime as dt

TODAY = dt.date.today()


def day(offset: int, hm: str = "00:00:00") -> str:
    return (TODAY + dt.timedelta(days=offset)).strftime("%Y-%m-%d") + "T" + hm


tmpdir = tempfile.mkdtemp(prefix="bili_v06_test_")
TMP_DB = Path(tmpdir) / "web.db"
init_db(TMP_DB)
dbm.DB_PATH = TMP_DB

conn = dbm.connect(TMP_DB)
RID = 888001
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (?, '测试主播', 1, ?)", (RID, now_iso()))

# 场次：danmaku_count / gift_value_yuan 冗余计数与 peak_viewers 一并造好
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
conn.execute("INSERT INTO session_titles VALUES (4, '唱歌杂谈', ?, ?)",
             (day(-3, "20:00:10"), day(-3, "21:00:00")))


def dm(sid: int, uid: int, ts: str, text: str = "hi"):
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (sid, RID, uid, f"u{uid}", text, ts))


# F：观众 11,12
dm(1, 11, day(-8, "20:10:00")); dm(1, 12, day(-8, "20:20:00"))
# G：观众 1,2,3,11
for i, uid in enumerate((1, 2, 3, 11)):
    dm(2, uid, day(-6, f"10:0{i + 5}:00"))
# A：观众 9,10
dm(3, 9, day(-5, "20:10:00")); dm(3, 10, day(-5, "20:20:00"))
# B：观众 1,2,3（5 条）+ uid=0 脏数据一条（不计数）
dm(4, 1, day(-3, "20:10:00")); dm(4, 1, day(-3, "20:11:00")); dm(4, 1, day(-3, "20:12:00"))
dm(4, 2, day(-3, "20:13:00")); dm(4, 3, day(-3, "20:14:00"))
dm(4, 0, day(-3, "20:15:00"), "uid0脏数据")
# C：观众 2,3,4（弹幕 4 条，其中 20:05 无快照分钟应丢弃）+ 礼物 5 + 互动 7
dm(5, 2, day(-2, "20:00:10")); dm(5, 3, day(-2, "20:00:20"))
dm(5, 4, day(-2, "20:03:10")); dm(5, 2, day(-2, "20:05:10"))
conn.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name,"
             " count, value_yuan, ts) VALUES (?, ?, 0, 5, 'u5', 'gift', '小心心', 1, 10.0, ?)",
             (5, RID, day(-2, "20:02:00")))
conn.execute("INSERT INTO interactions (session_id, room_id, uid, uname, type, ts) "
             "VALUES (?, ?, 7, 'u7', 'follow', ?)", (5, RID, day(-2, "20:03:30")))
# D：观众 1,4（弹幕 3 条）
dm(6, 1, day(-1, "14:00:10")); dm(6, 4, day(-1, "14:00:20")); dm(6, 1, day(-1, "14:30:00"))
# E：观众 1,2,3,4（弹幕，uid4 在无快照分钟）+ 礼物 5 + 互动 6
for uid in (1, 2, 3):
    dm(7, uid, day(-1, "20:00:10"))
dm(7, 4, day(-1, "20:10:00"))
conn.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name,"
             " count, value_yuan, ts) VALUES (?, ?, 0, 5, 'u5', 'gift', '小花花', 1, 20.0, ?)",
             (7, RID, day(-1, "20:00:30")))
conn.execute("INSERT INTO interactions (session_id, room_id, uid, uname, type, ts) "
             "VALUES (?, ?, 6, 'u6', 'share', ?)", (7, RID, day(-1, "20:00:40")))

# 同接快照（活跃度分母；viewers=0 与无快照分钟测试跳过逻辑）
snap = [  # (sid, 相对今天偏移, 分钟, viewers)
    (5, -2, "20:00", 10), (5, -2, "20:01", 0), (5, -2, "20:02", 5), (5, -2, "20:03", 4),  # C
    (6, -1, "14:00", 8),                                                                   # D
    (7, -1, "20:00", 2),                                                                   # E（5人/2同接 → 250%）
]
for sid, off, minute, viewers in snap:
    conn.execute("INSERT INTO popularity_snapshots (room_id, session_id, popularity, viewers, ts) "
                 "VALUES (?, ?, 0, ?, ?)", (RID, sid, viewers, day(off, minute + ":05")))

# 主播指标快照（粉丝/大航海问答：+50 粉丝、+1 大航海）
conn.execute("INSERT INTO anchor_snapshots (room_id, attention, online, watched, guards, ts) "
             "VALUES (?, 1000, 0, 0, 3, ?)", (RID, day(-5, "12:00:00")))
conn.execute("INSERT INTO anchor_snapshots (room_id, attention, online, watched, guards, ts) "
             "VALUES (?, 1050, 0, 0, 4, ?)", (RID, day(0, "12:00:00")))

# 用户画像（新观众占比问答：uid 7/9/10/11 为窗口内新观众，其余为老观众）
for uid in (1, 2, 3, 4, 5, 6):
    conn.execute("INSERT INTO user_stats (uid, room_id, uname, first_seen, last_seen) "
                 "VALUES (?, ?, ?, '2020-01-01T00:00:00', '2020-01-01T00:00:00')", (uid, RID, f"u{uid}"))
for uid, off in ((7, -1), (9, -5), (10, -5), (11, -6)):
    conn.execute("INSERT INTO user_stats (uid, room_id, uname, first_seen, last_seen) "
                 "VALUES (?, ?, ?, ?, ?)", (uid, RID, f"u{uid}", day(off), day(off)))
conn.commit()
conn.close()

# 本地调试模式（不强制登录），结束后恢复原值
orig_pw = app_mod.cfg["capture"]["admin_password"]
app_mod.cfg["capture"]["admin_password"] = ""
client = TestClient(app)


def d_cn(offset: int) -> str:
    d = TODAY + dt.timedelta(days=offset)
    return f"{d.month}月{d.day}日"


def date_of(offset: int) -> str:
    return (TODAY + dt.timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------------
# 1. 观众留存（第三轮口径：与前 3 天所有场次的观众并集比较）
# ---------------------------------------------------------------------------
r = client.get(f"/api/rooms/{RID}/analysis/retention?days=7").json()
dl = r["days_list"]
check("留存-窗口天数", len(dl) == 7 and dl[0]["date"] == date_of(-6) and dl[-1]["date"] == date_of(0))
empty_days = [x for x in dl if not x["sessions"]]
check("留存-无直播日留空", len(empty_days) == 2
      and {x["date"] for x in empty_days} == {date_of(-4), date_of(0)},
      f"实际 {len(empty_days)} 天为空")
# G（today-6 10:00）：对照=窗口外 F{11,12}，交集{11} → 1/2
g = next(x for x in dl if x["date"] == date_of(-6))["sessions"][0]
check("留存-窗口外对照场", g["retention"] == 0.5 and g["prev_total"] == 2
      and g["cur_total"] == 4 and g["overlap"] == 1, f"实际 {g}")
# A（today-5 20:00）：对照 = F{11,12}（恰在其前 72 小时整，边界计入）∪ G{1,2,3,11}
# = {1,2,3,11,12} 5 人（若 F 被排除则只有 3 人）；交集 0 → 0/5
a = next(x for x in dl if x["date"] == date_of(-5))["sessions"][0]
check("留存-72h边界场次计入", a["retention"] == 0.0 and a["prev_total"] == 5
      and a["cur_total"] == 2, f"实际 {a}")
# B（today-3 20:00）：对照=A{9,10}（G 距 B 3 天 10 小时，超窗排除）→ 0/2；uid=0 不计数
b = next(x for x in dl if x["date"] == date_of(-3))["sessions"][0]
check("留存-超窗排除与uid0排除", b["retention"] == 0.0 and b["prev_total"] == 2
      and b["cur_total"] == 3, f"实际 {b}")
check("留存-标题带出", b["titles"] == "唱歌杂谈")
# C（today-2 20:00）：对照=A{9,10}（恰 72h 边界）∪ B{1,2,3} = 5 人，交集{2,3} → 2/5
c = next(x for x in dl if x["date"] == date_of(-2))["sessions"][0]
check("留存-多场并集(含边界)", c["retention"] == 0.4 and c["prev_total"] == 5
      and c["cur_total"] == 5 and c["overlap"] == 2, f"实际 {c}")
# D/E（today-1 同日两场）：对照=B∪C（∪D）= {1,2,3,4,5,7}
de = next(x for x in dl if x["date"] == date_of(-1))["sessions"]
check("留存-一日多场分组", len(de) == 2 and de[0]["start_time"] < de[1]["start_time"])
check("留存-礼物/互动计入并集", de[0]["retention"] == round(2 / 6, 4)
      and de[0]["prev_total"] == 6, f"实际 {de[0]}")   # D{1,4} vs 6 人并集 → 2/6
check("留存-满重合与互动并集", de[1]["retention"] == round(5 / 6, 4)
      and de[1]["cur_total"] == 6 and de[1]["overlap"] == 5, f"实际 {de[1]}")   # E vs 并集 → 5/6

r30 = client.get(f"/api/rooms/{RID}/analysis/retention?days=30").json()
f_row = next(s for x in r30["days_list"] for s in x["sessions"]
             if s["start_time"] == day(-8, "20:00:00"))
check("留存-30天窗口含F且最早场null", f_row["retention"] is None and len(r30["days_list"]) == 30,
      f"实际 {f_row}")

# ---------------------------------------------------------------------------
# 2. 直播活跃度（任一互动口径）
# ---------------------------------------------------------------------------
r = client.get(f"/api/rooms/{RID}/analysis/activity?days=7").json()
dl = r["days_list"]
for off, sid in ((-6, "G"), (-5, "A"), (-3, "B")):
    x = next(x for x in dl if x["date"] == date_of(off))
    check(f"活跃-{sid}无快照场次null", x["avg_activity"] is None and x["sessions"] == [],
          f"实际 {x}")
a2 = next(x for x in dl if x["date"] == date_of(-2))
# C 有效分钟：20:00 弹幕2人/10=0.2；20:01 viewers=0 跳过；20:02 仅礼物1人/5=0.2；
#             20:03 弹幕1人+关注1人=2/4=0.5；20:05 无快照丢弃 → 场均 (0.2+0.2+0.5)/3=0.3
check("活跃-分钟口径(礼物/互动计入,无效分钟跳过)",
      a2["avg_activity"] == 0.3 and a2["max_activity"] == 0.3
      and a2["sessions"][0]["avg_activity"] == 0.3, f"实际 {a2}")
a3 = next(x for x in dl if x["date"] == date_of(-1))
check("活跃-日聚合平均/最高(含>100%不截断)",
      a3["avg_activity"] == 1.375 and a3["max_activity"] == 2.5 and len(a3["sessions"]) == 2,
      f"实际 {a3}")   # D=2/8=0.25、E=5/2=2.5 → 日均 1.375 / 最高 2.5

# ---------------------------------------------------------------------------
# 3. 数据问答（逐条精确断言）
# ---------------------------------------------------------------------------
qa = client.get(f"/api/rooms/{RID}/analysis/qa?days=7").json()["items"]
qa_map = {it["q"]: it["a"] for it in qa}
expect = {
    f"近 7 天哪天营收最高？": d_cn(-1),                       # today-1 ¥20 > today-2 ¥10
    "哪场直播弹幕最多？": f"{d_cn(-3)}场 · 5 条",
    "最常几点开播？": "20 点档（4 场）",                       # A/B/C/E 在 20 点档
    "平均每场直播多长？": "1.0 小时",                          # 窗口内 6 场各 1h（F 在窗口外）
    "同接最高的一场？": "100 人",
    "单场营收最高多少？": "¥20.00",
    "弹幕密度如何？": "3.7 条/小时",                           # 22 条 / 6 小时
    "多少观众送过礼？": "1 人 · 付费率 10.0%",                 # 送礼{5} / 活跃{1..7,9,10,11}
    "人均消费多少？": "¥30.00",                               # 30 元 / 1 人
    "粉丝涨了多少？": "+50",                                  # 1000 → 1050
    "新观众占比多少？": "40.0%",                              # 新{7,9,10,11} / 活跃 10 人
    "大航海人数变化？": "+1",                                 # 3 → 4
}
for q, a in expect.items():
    check(f"问答-{q}", qa_map.get(q) == a, f"实际 {qa_map.get(q)!r}")
check("问答-分组字段", {it["group"] for it in qa} == {"revenue", "audience"})

# ---------------------------------------------------------------------------
# 4. 边界：404 / days 参数
# ---------------------------------------------------------------------------
check("边界-房间不存在404",
      client.get("/api/rooms/1/analysis/retention").status_code == 404
      and client.get("/api/rooms/1/analysis/activity").status_code == 404
      and client.get("/api/rooms/1/analysis/qa").status_code == 404)
check("边界-days=1", client.get(f"/api/rooms/{RID}/analysis/retention?days=1").status_code == 200)
check("边界-days超限422", client.get(f"/api/rooms/{RID}/analysis/retention?days=366").status_code == 422)

app_mod.cfg["capture"]["admin_password"] = orig_pw
print("\n汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
