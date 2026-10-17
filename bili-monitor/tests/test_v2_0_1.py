# -*- coding: utf-8 -*-
"""v2.0.1 数据分析页「营收构成」接口本地测试：/analysis/revenue。
（v2.2 架构改版：本套件后半段的紧急磁盘清理测试已随 capture 迁 replay，
见 replay/tests/test_emergency_cleanup.py）

覆盖（口径见 app.py analysis_revenue docstring）：
1. 按用户聚合的舰队（type='guard'）/ 礼物（gift + sc）拆分与总营收；
2. 前 10 名排序、其余用户合并 others（含 guard/gift/value 三字段）；
3. uid=0 脏数据排除、场外收入（is_offline=1）计入、跨房间行不计入；
4. 自然日窗口边界：起点当日 00:00:00 计入、前一日 23:59:59 排除；
5. 总用户 ≤ 10 时 others=null；窗口内无营收时 items 空 / total 0；
6. 金额两位小数舍入；404 / days 边界。

运行：uv run python tests/test_v2_0_1.py
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
# 造数：主房间 RID（12 个付费用户 → 前 10 + others），外加三间对照房。
#   ts 全部落在 days=7 窗口内（起点 = today-6 00:00:00），另造边界行验证。
#   用户表（guard + gift = value，排名即此序）：
#     101 甲总   30 +  80  = 110   106 己界  0 + 40 = 40（5 元边界行计入）
#     102 乙纯礼  0 + 100  = 100   107 庚离  0 + 30 = 30（全部场外收入）
#     103 丙纯舰 90 +   0  =  90   108 辛   12.5 + 12.34（小数舍入）
#     104 丁SC   0 +  90  =  90   109 壬    0 + 20 = 20
#     105 戊窗   0 +  40  =  40   110 癸   18 +  0 = 18
#     （105 的 50 元窗前行不计；104 的 90 = 礼物 35 + SC 55）
#     others = 111 子尾（1.23 + 13.77 = 15）+ 112 丑尾（0 + 10）
#   total = guard 151.73 + gift 436.11 = 587.84
# ---------------------------------------------------------------------------
import datetime as dt

TODAY = dt.date.today()


def day(offset: int, hm: str = "12:00:00") -> str:
    return (TODAY + dt.timedelta(days=offset)).strftime("%Y-%m-%d") + "T" + hm


tmpdir = tempfile.mkdtemp(prefix="bili_v201_test_")
TMP_DB = Path(tmpdir) / "web.db"
init_db(TMP_DB)
dbm.DB_PATH = TMP_DB

conn = dbm.connect(TMP_DB)
RID, RID_FEW, RID_EMPTY, RID_OTHER = 888011, 888013, 888014, 888012
for rid in (RID, RID_FEW, RID_EMPTY, RID_OTHER):
    conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
                 "VALUES (?, '测试主播', 1, ?)", (rid, now_iso()))


def gift(rid: int, uid: int, uname: str, gtype: str, name: str, value: float,
         ts: str, offline: int = 0):
    conn.execute(
        "INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name,"
        " count, value_yuan, ts) VALUES (NULL, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (rid, offline, uid, uname, gtype, name, value, ts))


# 主房间 12 用户（ts 默认 today-1，窗口内）
gift(RID, 101, "甲总", "guard", "舰长", 30, day(-1))
gift(RID, 101, "甲总", "gift", "小心心", 80, day(-1))
gift(RID, 102, "乙纯礼", "gift", "辣条", 100, day(-1))
gift(RID, 103, "丙纯舰", "guard", "提督", 90, day(-1))
gift(RID, 104, "丁SC", "gift", "小花花", 35, day(-1))
gift(RID, 104, "丁SC", "sc", "醒目留言", 55, day(-1))
gift(RID, 105, "戊窗", "gift", "窗口外礼物", 50, day(-7, "23:59:59"))  # 窗前：不计
gift(RID, 105, "戊窗", "gift", "窗口内礼物", 40, day(-1))
gift(RID, 106, "己界", "gift", "普通礼物", 35, day(-1))
gift(RID, 106, "己界", "gift", "边界行礼物", 5, day(-6, "00:00:00"))  # 恰在起点：计入
gift(RID, 107, "庚离", "gift", "场外礼物", 30, day(-1), offline=1)
gift(RID, 108, "辛", "guard", "舰长", 12.5, day(-1))
gift(RID, 108, "辛", "gift", "礼物", 12.34, day(-1))
gift(RID, 109, "壬", "gift", "礼物", 20, day(-1))
gift(RID, 110, "癸", "guard", "舰长", 18, day(-1))
gift(RID, 111, "子尾", "guard", "舰长", 1.234, day(-1))   # 舍入 → 1.23
gift(RID, 111, "子尾", "gift", "礼物", 13.766, day(-1))   # 舍入 → 13.77
gift(RID, 112, "丑尾", "gift", "礼物", 10, day(-1))
gift(RID, 0, "脏数据", "gift", "不该计入", 999, day(-1))  # uid=0：排除
gift(RID_OTHER, 101, "甲总", "gift", "别房间", 500, day(-1))  # 跨房间：不计

# 对照房 1：仅 3 个用户（≤10 → others=null）
for uid in (201, 202, 203):
    gift(RID_FEW, uid, f"u{uid}", "gift", "礼物", 10, day(-1))
# 对照房 2：窗口内无任何营收行
conn.commit()
conn.close()

# 本地调试模式（不强制登录），结束后恢复原值
orig_pw = app_mod.cfg["capture"]["admin_password"]
app_mod.cfg["capture"]["admin_password"] = ""
client = TestClient(app)

# ---------------------------------------------------------------------------
# 1. 主房间：拆分 / 排序 / others / total
# ---------------------------------------------------------------------------
r = client.get(f"/api/rooms/{RID}/analysis/revenue?days=7").json()
items, others, total = r["items"], r["others"], r["total"]

check("条目数-前10", len(items) == 10, f"实际 {len(items)}")
check("排序-按总营收降序",
      [it["uid"] for it in items] == [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
      f"实际 {[it['uid'] for it in items]}")
check("舰队礼物拆分-混合用户",
      items[0] == {"uid": 101, "uname": "甲总", "guard": 30, "gift": 80, "value": 110},
      f"实际 {items[0]}")
check("拆分-纯舰队用户", items[2]["uid"] == 103 and items[2]["guard"] == 90
      and items[2]["gift"] == 0, f"实际 {items[2]}")
check("拆分-SC计入礼物", items[3]["uid"] == 104 and items[3]["gift"] == 90,
      f"实际 {items[3]}")
check("窗口-前一日23:59排除", items[4]["uid"] == 105 and items[4]["value"] == 40,
      f"实际 {items[4]}")
check("窗口-起点当日00:00计入", items[5]["uid"] == 106 and items[5]["value"] == 40,
      f"实际 {items[5]}")
check("口径-场外收入计入", items[6]["uid"] == 107 and items[6]["value"] == 30,
      f"实际 {items[6]}")
check("口径-uid0排除与跨房间排除", total["value"] == 587.84,
      f"实际 {total['value']}（若含 999 或跨房 500 会偏大）")
check("金额-两位小数舍入", items[7]["uid"] == 108 and items[7]["value"] == 24.84,
      f"实际 {items[7]}")
check("others-其余用户合并",
      others == {"uid": 0, "uname": "其他用户", "guard": 1.23, "gift": 23.77,
                 "value": 25.0},
      f"实际 {others}")
check("total-三字段", total == {"guard": 151.73, "gift": 436.11, "value": 587.84},
      f"实际 {total}")

# ---------------------------------------------------------------------------
# 2. 对照房：≤10 用户 / 空房间
# ---------------------------------------------------------------------------
r2 = client.get(f"/api/rooms/{RID_FEW}/analysis/revenue?days=7").json()
check("少用户-无others", r2["others"] is None and len(r2["items"]) == 3
      and r2["total"]["value"] == 30, f"实际 {r2}")
r3 = client.get(f"/api/rooms/{RID_EMPTY}/analysis/revenue?days=7").json()
check("空房间-空items", r3["items"] == [] and r3["others"] is None
      and r3["total"]["value"] == 0, f"实际 {r3}")

# ---------------------------------------------------------------------------
# 3. 边界：404 / days 参数
# ---------------------------------------------------------------------------
check("边界-房间不存在404",
      client.get("/api/rooms/1/analysis/revenue").status_code == 404)
check("边界-days=1", client.get(f"/api/rooms/{RID}/analysis/revenue?days=1").status_code == 200)
check("边界-days超限422",
      client.get(f"/api/rooms/{RID}/analysis/revenue?days=366").status_code == 422)

# ---------------------------------------------------------------------------
# 磁盘紧急清理云时代口径（v2.0.1 第二特性：已上云只删本地文件、保留行）
app_mod.cfg["capture"]["admin_password"] = orig_pw
print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
