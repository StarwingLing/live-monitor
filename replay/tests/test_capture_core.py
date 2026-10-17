# -*- coding: utf-8 -*-
"""capture 核心逻辑测试（v2.2 自 bili-monitor tests/test_v0_5_2.py 第 3-5 节迁入）：
短间隔场次合并（标题迁移/幂等）、分层权威口径（maintenance 全量重算）、
分层实时口径（writer UPSERT）。

运行：uv run python tests/test_capture_core.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay.db import Db, init_db, now_iso

import datetime as dt

# 种子数据日期相对今天生成：分层/统计窗口（window_days=30）以当前日期
# 为锚，硬编码 2026-09-XX 的数据在 2026-10-13 起滑出窗口导致分层无行
# （2026-10-14 实测踩坑），改为动态日期保测试长期可跑
_D0 = dt.date.today() - dt.timedelta(days=4)


def _d(off: int) -> str:
    return (_D0 + dt.timedelta(days=off)).strftime("%Y-%m-%d")


results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))



# ---------------------------------------------------------------------------
# 3. 场次标题合并迁移（短间隔场次合并）
# ---------------------------------------------------------------------------
tmp2 = Path(tempfile.mkdtemp(prefix="rp_merge_")) / "m.db"
init_db(tmp2)
mdb = Db(tmp2)
mdb.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
            "VALUES (1, 'A', 1, ?)", (now_iso(),))
# 两场间隔 300s（< 600 阈值，应合并）；两场各有标题，其中"共同标题"重复
mdb.execute("INSERT INTO live_sessions (room_id, start_time, end_time) VALUES (1, ?, ?)",
            (f"{_d(0)}T20:00:00", f"{_d(0)}T20:50:00"))       # session 1
mdb.execute("INSERT INTO live_sessions (room_id, start_time, end_time) VALUES (1, ?, ?)",
            (f"{_d(0)}T20:55:00", f"{_d(0)}T21:30:00"))       # session 2
mdb.execute("INSERT INTO session_titles VALUES (1, '共同标题', ?, ?)",
            (f"{_d(0)}T20:00:05", f"{_d(0)}T20:50:00"))
mdb.execute("INSERT INTO session_titles VALUES (1, '前场独有', ?, ?)",
            (f"{_d(0)}T20:10:00", f"{_d(0)}T20:20:00"))
mdb.execute("INSERT INTO session_titles VALUES (2, '共同标题', ?, ?)",
            (f"{_d(0)}T20:55:05", f"{_d(0)}T21:30:00"))
mdb.execute("INSERT INTO session_titles VALUES (2, '后场独有', ?, ?)",
            (f"{_d(0)}T21:00:00", f"{_d(0)}T21:30:00"))
from replay.capture.maintenance import merge_short_gap_sessions, _recompute_user_stats
merged = merge_short_gap_sessions(mdb)
titles = {(r["session_id"], r["title"]) for r in mdb.query("SELECT session_id, title FROM session_titles")}
check("合并-执行", merged == 1)
check("合并-标题迁移", (1, "前场独有") in titles and (1, "后场独有") in titles
      and (2, "共同标题") not in titles and (2, "后场独有") not in titles, f"实际 {titles}")
check("合并-同标题去重", sum(1 for _, t in titles if t == "共同标题") == 1)
check("合并-幂等", merge_short_gap_sessions(mdb) == 0)


# ---------------------------------------------------------------------------
# 4. 分层权威口径（maintenance 全量重算；v0.5 二次调整：舰队/场均送礼为核心）
# ---------------------------------------------------------------------------
def seed_tier_db(db: Db):
    """构造 9 类用户（数据均在窗口内）：3 个场次（D-4 / D-2 / D-1）。"""
    db.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
               "VALUES (1, 'A', 1, ?)", (now_iso(),))
    for i, off in ((1, 0), (2, 2), (3, 3)):
        db.execute("INSERT INTO live_sessions (room_id, start_time, end_time) VALUES (1, ?, ?)",
                   (f"{_d(off)}T20:00:00", f"{_d(off)}T22:00:00"))
    dm_rows: list[tuple] = []

    def dm(uid: int, sid: int, n: int, off: int):
        for i in range(n):
            dm_rows.append((sid, 1, uid, f"u{uid}", "", 0, 0, 0, f"m{i}", "",
                            f"{_d(off)}T21:0{i % 6}:00"))

    def gift(uid: int, sid: int, value: float, off: int, typ: str = "gift"):
        db.execute("INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type,"
                   " name, text, count, value_yuan, gift_img, ts) "
                   "VALUES (?, 1, 0, ?, ?, ?, '小心心', '', 1, ?, '', ?)",
                   (sid, uid, f"u{uid}", typ, value, f"{_d(off)}T21:00:00"))

    # 101：单场 0.5 元 → 场均 0.5，非核心
    gift(101, 1, 0.5, 0)
    # 102：单场 12 元 → 场均 12 ≥10，核心
    gift(102, 1, 12.0, 0)
    # 103 / 104：单场弹幕 9 / 10 条（活跃阈值边界）
    dm(103, 1, 9, 0)
    dm(104, 1, 10, 0)
    # 105：两场各 6 条弹幕 → 每场不足阈值，普通
    dm(105, 1, 6, 0); dm(105, 2, 6, 2)
    # 106：单场 0.5 元 + 5 条弹幕 → 普通
    gift(106, 1, 0.5, 0); dm(106, 1, 5, 0)
    # 107：三场各 8 元（总 24）→ 场均 8 <10，普通
    gift(107, 1, 8.0, 0); gift(107, 2, 8.0, 2); gift(107, 3, 8.0, 3)
    # 108：两场 15 + 5 元（总 20）→ 场均 10 恰达标，核心
    gift(108, 1, 15.0, 0); gift(108, 2, 5.0, 2)
    # 109：历史提督（guard_level=2 预置，窗口内未再购买）+ 1 条弹幕 → 核心舰队路径
    db.execute("INSERT INTO user_stats (uid, room_id, uname, first_seen, last_seen, guard_level) "
               "VALUES (109, 1, 'u109', ?, ?, 2)", (now_iso(), now_iso()))
    dm(109, 1, 1, 0)
    # 110：窗口内购买舰长（type='guard'）→ 核心（guard_cnt 路径，金额同时达标）
    gift(110, 1, 138.0, 0, typ="guard")

    db.executemany("INSERT INTO danmaku (session_id, room_id, uid, uname, medal_name,"
                   " medal_level, medal_color, privilege_type, text, emots, ts) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", dm_rows)


tmp3 = Path(tempfile.mkdtemp(prefix="rp_tier_")) / "t.db"
init_db(tmp3)
tdb = Db(tmp3)
seed_tier_db(tdb)
_recompute_user_stats(tdb)
tiers = {r["uid"]: r["tier"] for r in tdb.query("SELECT uid, tier FROM user_stats")}
check("分层-单场0.5元普通", tiers[101] == "普通", f"实际 {tiers.get(101)}")
check("分层-单场12元核心", tiers[102] == "核心", f"实际 {tiers.get(102)}")
check("分层-单场9条弹幕普通", tiers[103] == "普通", f"实际 {tiers.get(103)}")
check("分层-单场10条弹幕活跃", tiers[104] == "活跃", f"实际 {tiers.get(104)}")
check("分层-两场各6条弹幕普通", tiers[105] == "普通", f"实际 {tiers.get(105)}")
check("分层-0.5元加5条弹幕普通", tiers[106] == "普通", f"实际 {tiers.get(106)}")
check("分层-三场各8元素场均8普通", tiers[107] == "普通", f"实际 {tiers.get(107)}")
check("分层-两场15加5元素场均10核心", tiers[108] == "核心", f"实际 {tiers.get(108)}")
check("分层-历史提督舰队路径核心", tiers[109] == "核心", f"实际 {tiers.get(109)}")
check("分层-窗口内购舰核心", tiers[110] == "核心", f"实际 {tiers.get(110)}")
# 106 补 10 条弹幕 → 重算升活跃（金额 0.5 不够核心）
dm_more = [(1, 1, 106, "u106", "", 0, 0, 0, f"x{i}", "", f"{_d(0)}T21:30:00")
           for i in range(10)]
tdb.executemany("INSERT INTO danmaku (session_id, room_id, uid, uname, medal_name,"
                " medal_level, medal_color, privilege_type, text, emots, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", dm_more)
_recompute_user_stats(tdb)
tiers = {r["uid"]: r["tier"] for r in tdb.query("SELECT uid, tier FROM user_stats")}
check("分层-重算升级（0.5元+单场10条→活跃）", tiers[106] == "活跃", f"实际 {tiers.get(106)}")


# ---------------------------------------------------------------------------
# 5. 分层实时口径（writer UPSERT）
# ---------------------------------------------------------------------------
import asyncio
from replay.capture.writer import Writer

tmp4 = Path(tempfile.mkdtemp(prefix="rp_writer_")) / "w.db"
init_db(tmp4)
wdb = Db(tmp4)
wdb.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
            "VALUES (1, 'A', 1, ?)", (now_iso(),))
wdb.execute("INSERT INTO live_sessions (room_id, start_time) VALUES (1, ?)",
            (f"{_d(1)}T20:00:00",))                               # session 1
writer = Writer(wdb, asyncio.Queue())


def dm_event(uid, sid=1, ts=None):
    return ("danmaku", {"session_id": sid, "room_id": 1, "uid": uid, "uname": f"u{uid}",
                        "medal_name": "", "medal_level": 0, "medal_color": 0,
                        "privilege_type": 0, "text": "hi", "emots": "",
                        "ts": ts or f"{_d(1)}T20:30:00"})


def gift_event(uid, value, sid=1, privilege_type=0):
    return ("gift", {"session_id": sid, "room_id": 1, "uid": uid, "uname": f"u{uid}",
                     "type": "gift", "name": "小心心", "blind_box": "", "text": "",
                     "count": 1, "value_yuan": value, "gift_img": "",
                     "privilege_type": privilege_type,
                     "ts": f"{_d(1)}T20:31:00"})


def tier_of(uid):
    rows = wdb.query("SELECT tier FROM user_stats WHERE uid = ?", (uid,))
    return rows[0]["tier"] if rows else None


# 5.1 单场 9 条（分两批 4+5）：第二批时库里已有 4 条 + 本批 5 条 = 9 → 仍普通
for e in [dm_event(201) for _ in range(4)]:
    writer.queue.put_nowait(e)
writer._flush()
for e in [dm_event(201) for _ in range(5)]:
    writer.queue.put_nowait(e)
writer._flush()
check("实时-单场累计9条普通", tier_of(201) == "普通", f"实际 {tier_of(201)}")
# 5.2 再来 1 条（累计 10）→ 子查询数到库里 10 条 → 升活跃
writer.queue.put_nowait(dm_event(201))
writer._flush()
check("实时-单场累计10条升活跃", tier_of(201) == "活跃", f"实际 {tier_of(201)}")
# 5.3 另一场 6 条（单场不足，即使两场合计 16 也不升）
for e in [dm_event(201, sid=2, ts=f"{_d(2)}T20:00:00") for _ in range(6)]:
    writer.queue.put_nowait(e)
wdb.execute("INSERT INTO live_sessions (room_id, start_time) VALUES (1, ?)",
            (f"{_d(2)}T20:00:00",))   # session 2（先补场次行，events 里 sid=2）
writer._flush()
check("实时-另一场6条不误升", tier_of(201) == "活跃")   # 已是活跃，验证不报错不降级
# 5.4 同场送礼 5 元 → 普通；再 6 元（单场累计 11 ≥10）→ 核心（场均的实时近似）
writer.queue.put_nowait(gift_event(202, 5.0))
writer._flush()
check("实时-单场5元普通", tier_of(202) == "普通", f"实际 {tier_of(202)}")
writer.queue.put_nowait(gift_event(202, 6.0))
writer._flush()
check("实时-单场累计11元升核心", tier_of(202) == "核心", f"实际 {tier_of(202)}")
# 5.5 场外礼物（session_id=None）：单笔 5 元 → 普通；再 10 元累计 15（权威口径
# 场均 15 应核心）——实时侧按"当条场次"聚合、场外不命中子查询，暂不升级，
# 由每日重算收敛（已知实时/权威差异，宽松方向正确）
writer.queue.put_nowait(gift_event(203, 5.0, sid=None))
writer._flush()
check("实时-场外单笔5元普通", tier_of(203) == "普通", f"实际 {tier_of(203)}")
writer.queue.put_nowait(gift_event(203, 10.0, sid=None))
writer._flush()
check("实时-场外累计15元暂不升级（待重算）", tier_of(203) == "普通", f"实际 {tier_of(203)}")
# 5.6 上舰事件（privilege_type=3 总督）→ 立即核心（舰队路径）
writer.queue.put_nowait(gift_event(204, 0.1, privilege_type=3))
writer._flush()
check("实时-总督消息立即核心", tier_of(204) == "核心", f"实际 {tier_of(204)}")
# 5.7 降级保护：核心用户再发弹幕仍是核心；普通用户不会因单条弹幕变活跃
writer.queue.put_nowait(dm_event(202))
writer._flush()
check("实时-核心不降级", tier_of(202) == "核心")

print("\n汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
