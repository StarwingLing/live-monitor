# -*- coding: utf-8 -*-
"""v2.3.4 弹幕词云接口测试：/api/rooms/{room}/sessions/{sid}/wordcloud
（详情页场次模式「弹幕词云」板块数据源）。

覆盖：
1. 预处理口径：表情弹幕还原短名（带包名括号/纯文本大表情两种键形）、
   URL 丢弃、同字连刷折叠（哈×N→哈哈）、停用词与单字/纯符号过滤、
   自定义词表成词（结算动画）；
2. 输出形态：words 词频降序且 ≤ TOP80、emotes 携带 url 与短名、
   danmaku_total/unique_tokens 汇总；
3. 非法 emots JSON 行防御性跳过（不炸）；
4. 进程内缓存：弹幕计数不变 → 命中（同一对象）；计数变化 → 自动重算；
5. 边界：跨房 404、场次不存在 404。

运行：uv run python tests/test_v2_3_4.py
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


TMP = Path(tempfile.mkdtemp(prefix="bili_v234_test_"))

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
conn.execute("INSERT INTO live_sessions (room_id, start_time, end_time) "
             "VALUES (123456, '2026-10-16T20:00:00', '2026-10-16T21:00:00')")

EMOT_PKG = '{"[测试包_坐好了]": {"url": "https://example.com/a.png", "width": 20, "height": 20}}'
EMOT_BIG = '{"打call喵": {"url": "https://example.com/b.png", "width": 162, "height": 162}}'
# 弹幕种子：每类若干条（text, emots）
SEED = [
    ("结算动画", ""),                       # ×3 自定义词表
    ("结算动画", ""),
    ("结算动画", ""),
    ("[测试包_坐好了]", EMOT_PKG),           # ×2 包名表情 → 短名“坐好了”→分词“坐好”
    ("[测试包_坐好了]", EMOT_PKG),
    ("哈哈哈哈哈哈", ""),                    # 连刷折叠→哈哈→停用词
    ("的", ""),                              # ×3 停用词
    ("的", ""),
    ("的", ""),
    ("好听！！！！！！！！", ""),            # 折叠+标点过滤 → “好听”
    ("看下 https://example.com/x 链接", ""),  # URL 丢弃 → “链接”
    ("打call喵", EMOT_BIG),                  # 纯文本大表情键（无括号）
    ("？？？？", ""),                         # 纯符号 → 无词元
    ("残缺", "{broken json"),                # 非法 emots 行 → 跳过不炸
]
for text, emots in SEED:
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts, emots) "
                 "VALUES (1, 123456, 1, 'u', ?, '2026-10-16T20:10:00', ?)",
                 (text, emots))
conn.commit()
conn.close()

# ---- 1/2：compute 口径 ----
from bili_monitor.web.wordcloud import compute

c = dbm.connect()
r1 = compute(c, 1)
names = [w["name"] for w in r1["words"]]
vals = [w["value"] for w in r1["words"]]
check("词频降序且≤80", vals == sorted(vals, reverse=True) and len(vals) <= 80)
check("自定义词成词", "结算动画" in names
      and r1["words"][names.index("结算动画")]["value"] == 3)
check("表情还原短名入词", "坐好" in names, str(names))
check("标点折叠-好听", "好听" in names)
check("URL丢弃-链接", "链接" in names)
check("停用词/单字/纯符号/连刷均滤除",
      not any(n in names for n in ("的", "哈", "哈哈", "？？", "！！！！"))
      and all(len(n) >= 2 for n in names))
check("汇总计数", r1["danmaku_total"] == len(SEED) and r1["unique_tokens"] > 0)

em = {e["short"]: e for e in r1["emotes"]}
check("表情榜-包名表情短名", "坐好了" in em and em["坐好了"]["count"] == 2
      and em["坐好了"]["url"] == "https://example.com/a.png", str(r1["emotes"]))
check("表情榜-纯文本大表情键", "打call喵" in em and em["打call喵"]["count"] == 1)
check("非法emots行未计入", "残缺" not in em and len(r1["emotes"]) == 2)

# ---- 4：缓存（计数键）----
r2 = compute(c, 1)
check("缓存命中-同对象", r2 is r1)
conn2 = dbm.connect()
conn2.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts, emots) "
              "VALUES (1, 123456, 2, 'u2', '结算动画', '2026-10-16T20:20:00', '')")
conn2.commit()
conn2.close()
r3 = compute(c, 1)
names3 = {w["name"]: w["value"] for w in r3["words"]}
check("计数变化自动重算", names3.get("结算动画") == 4 and r3["danmaku_total"] == len(SEED) + 1)
c.close()

# ---- 端点（含 5 边界）----
app_mod.cfg["capture"]["admin_password"] = ""   # 调试模式直连
client = TestClient(app_mod.app)
# 清进程缓存避免与上面直调结果混淆（端点另有房间校验路径）
app_mod.wordcloud._CACHE.clear() if hasattr(app_mod, "wordcloud") else None

r = client.get("/api/rooms/123456/sessions/1/wordcloud")
d = r.json()
check("端点200与形态", r.status_code == 200
      and d["session_id"] == 1 and isinstance(d["words"], list)
      and isinstance(d["emotes"], list) and d["danmaku_total"] == len(SEED) + 1,
      r.text[:160])
check("端点-跨房404", client.get(
    "/api/rooms/654321/sessions/1/wordcloud").status_code == 404)
check("端点-场次不存在404", client.get(
    "/api/rooms/123456/sessions/999/wordcloud").status_code == 404)

print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
