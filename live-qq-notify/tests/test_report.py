# -*- coding: utf-8 -*-
"""report.py(下播直播报告图生成)单测——v0.3.0 阶段一。

覆盖:
- find_session:按通知器 state.since 定位场次的覆盖窗口/容差/找不到;
- build_freqs:分词口径(表情还原短名/URL 丢弃/同字连刷折叠/停用词/
  自定义黑话词/长度与纯数字过滤);
- load_report:场次统计(弹幕量/发言人数/TOP8/舰队等级取最高/表情榜解析
  与"有图剔词、无图留词"规则);
- _place_holes / _render_cloud:表情原图孔位互不重叠 + 混合词云渲染;
- render_report + CLI:无头像离线出图冒烟(文件生成、宽度、空场次兜底、
  --room 找不到场次退出码 2)。

运行:uv run python tests/test_report.py(全部离线,fetch_avatars=False
走占位圆/纯文字词云,不访问 B 站)。
"""
import io
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name: str, ok: bool):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}")


# ---------------------------------------------------------------------------
# 测试库:最小 schema(report.py 只读这些列)
# ---------------------------------------------------------------------------
EMOT_DAXIAO = ('{"[鸣潮_大笑]": {"url": "https://e.example/daxiao.png", '
               '"width": 20, "height": 20}}')
EMOT_DOG = '{"[dog]": {"url": "https://e.example/dog.png", ' \
           '"width": 20, "height": 20}}'


def make_db(base: Path) -> Path:
    db = base / "test_report.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE rooms (room_id INTEGER PRIMARY KEY,
                            anchor_name TEXT NOT NULL DEFAULT '',
                            avatar_url TEXT NOT NULL DEFAULT '');
        CREATE TABLE live_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            peak_viewers INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE danmaku (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER, room_id INTEGER NOT NULL,
            uid INTEGER NOT NULL, uname TEXT NOT NULL,
            privilege_type INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL,
            emots TEXT NOT NULL DEFAULT '');
        INSERT INTO rooms VALUES (23058, '测试主播', 'https://example.com/a.jpg');
        -- 场次 1:19:00-22:00(已收尾)
        INSERT INTO live_sessions (room_id, start_time, end_time, peak_viewers)
            VALUES (23058, '2026-10-10T19:00:00', '2026-10-10T22:00:00', 88);
        -- 场次 2:更早的旧场
        INSERT INTO live_sessions (room_id, start_time, end_time, peak_viewers)
            VALUES (23058, '2026-10-09T19:00:00', '2026-10-09T20:00:00', 5);
    """)
    dm = [
        # (uid, uname, guard, text, emots, 条数)
        (1, "甲", 0, "好听", "", 6),
        (2, "乙", 3, "[鸣潮_大笑]", EMOT_DAXIAO, 4),   # 表情弹幕 → 还原"大笑"
        (3, "丙", 3, "哈哈哈哈哈哈", "", 3),             # 连刷折叠 → 哈哈(停用词,滤)
        (4, "丁", 2, "打call", "", 2),                 # 自定义黑话词
        (5, "戊", 0, "https://example.com/x 666", "", 2),  # URL 丢弃,666 滤
        (6, "己", 1, "来了来了", EMOT_DOG, 1),
    ]
    for uid, uname, g, text, emots, n in dm:
        for _ in range(n):
            conn.execute(
                "INSERT INTO danmaku (session_id, room_id, uid, uname, "
                "privilege_type, text, emots) VALUES (1, 23058, ?, ?, ?, ?, ?)",
                (uid, uname, g, text, emots))
    # 旧场弹幕:不应计入场次 1
    conn.execute(
        "INSERT INTO danmaku (session_id, room_id, uid, uname, privilege_type, "
        "text) VALUES (2, 23058, 1, '甲', 0, '旧场弹幕')")
    conn.commit()
    conn.close()
    return db


tmp = Path(tempfile.mkdtemp(prefix="lqn_report_test_"))

# ---- find_session:覆盖窗口与容差 -------------------------------------------
db1 = make_db(tmp)
conn = sqlite3.connect(db1)
conn.row_factory = sqlite3.Row
check("定位-场次中段命中",
      report.find_session(conn, 23058, "2026-10-10T20:30:00") == 1)
check("定位-since 早于开播但在容差内(时钟差)",
      report.find_session(conn, 23058, "2026-10-10T18:52:00") == 1)
check("定位-两场之间远离(宁跳不出错图)",
      report.find_session(conn, 23058, "2026-10-10T12:00:00") is None)
check("定位-非法时间", report.find_session(conn, 23058, "not-a-time") is None)
conn.execute(
    "INSERT INTO live_sessions (room_id, start_time, end_time) "
    "VALUES (23058, '2026-10-11T19:00:00', NULL)")
conn.commit()
check("定位-未收尾场次按覆盖匹配",
      report.find_session(conn, 23058, "2026-10-11T20:00:00") == 3)
conn.close()

# ---- 表情短名解析 ------------------------------------------------------------
check("表情短名-带包名取尾段",
      report._emote_short("[鸣潮·致予新世界静态表情包_打call！]") == "打call！")
check("表情短名-普通括号表情", report._emote_short("[大笑]") == "大笑")
check("表情短名-大表情无括号", report._emote_short("打call喵") == "打call喵")

# ---- build_freqs:分词口径 ---------------------------------------------------
freqs = report.build_freqs([
    "[鸣潮_大笑]",                 # 表情还原短名"大笑"
    "哈哈哈哈哈哈",               # 折叠"哈哈"→停用词滤除
    "打call",                    # 自定义词表命中
    "https://example.com/x 666",  # URL 滤除;666 纯数字滤除
    "好听好听",
    "的",                        # 停用词
])
check("分词-表情还原短名", freqs.get("大笑") == 1)
check("分词-自定义黑话词", freqs.get("打call") == 1)
check("分词-连刷折叠后停用词滤除", "哈哈" not in freqs)
check("分词-URL 与纯数字滤除", "666" not in freqs and "example" not in freqs)
check("分词-单字停用词滤除", "的" not in freqs)
check("分词-空输入", report.build_freqs([]) == {})

# ---- load_report:场次统计与表情榜 --------------------------------------------
conn = sqlite3.connect(db1)
conn.row_factory = sqlite3.Row
rep = report.load_report(conn, 1, fetch_avatars=False)
check("统计-房间与主播", rep.room_id == 23058 and rep.anchor == "测试主播")
check("统计-弹幕量(不含旧场)", rep.danmaku == 18)
check("统计-发言人数", rep.speakers == 6)
check("统计-最高同接", rep.peak_viewers == 88)
check("排行-TOP3 顺序", [u.uid for u in rep.top[:3]] == [1, 2, 3])
guard_map = {u.uid: u.guard for u in rep.top}
check("舰队-等级取场内最高", guard_map[6] == 1 and guard_map[4] == 2
      and guard_map[2] == 3)
check("统计-无收益字段", not hasattr(rep, "gift_yuan"))
# 表情榜:按出现次数降序(大笑 4 条消息 > dog 1);离线未抓图 → 短名保留为文字词
check("表情榜-解析与排序",
      [(e.name, e.count) for e in rep.emotes] == [("大笑", 4), ("dog", 1)]
      and rep.emotes[0].url == "https://e.example/daxiao.png")
check("表情榜-离线无图不剔词",
      all(e.img is None for e in rep.emotes) and "大笑" in rep.freqs)

try:
    report.load_report(conn, 999, fetch_avatars=False)
    check("异常-场次不存在抛错", False)
except ValueError:
    check("异常-场次不存在抛错", True)
conn.close()

# ---- _place_holes:孔位互不重叠且不出界 ----------------------------------------
holes = report._place_holes([(130, 130), (110, 90), (90, 70), (70, 60)],
                            764, 400)
boxes = [b for b in holes if b]
check("孔位-全部放下且在画布内",
      len(boxes) == 4 and all(0 <= x0 and x1 <= 764 and 0 <= y0 and y1 <= 400
                              for x0, y0, x1, y1 in boxes))
check("孔位-两两留缝不重叠",
      all(report._rects_apart(a, b, 16) for i, a in enumerate(boxes)
          for b in boxes[i + 1:]))
check("孔位-确定性(同参数同结果)",
      report._place_holes([(130, 130), (110, 90), (90, 70), (70, 60)],
                          764, 400) == holes)

# ---- _render_cloud:文字 + 合成表情混合 ----------------------------------------
from PIL import Image  # noqa: E402


def fake_emote_png(color, size=80):
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color + (255,)).save(buf, "PNG")
    return buf.getvalue()


emotes = [
    report.EmoteItem("大红", 100, url="x", img=fake_emote_png((255, 0, 0)),
                     aspect=1.0),
    report.EmoteItem("大蓝", 50, url="y", img=fake_emote_png((0, 0, 255), 64),
                     aspect=1.0),
    report.EmoteItem("无图", 30, url="z", img=None, aspect=1.0),
]
cloud = report._render_cloud({"测试": 5, "词组": 3}, 764, 400, emotes)
check("混合词云-渲染成功且画布尺寸正确",
      cloud is not None and cloud.size == (764, 400))
cloud2 = report._render_cloud({}, 764, 400, emotes[:2])
check("混合词云-仅表情无词条可渲染", cloud2 is not None)
check("纯文字词云-无表情路径", report._render_cloud({"词": 1}, 764, 400)
      is not None)
check("空输入返回 None", report._render_cloud({}, 764, 400, []) is None)

# ---- render_report:离线出图冒烟 ----------------------------------------------
conn = sqlite3.connect(db1)
conn.row_factory = sqlite3.Row
rep = report.load_report(conn, 1, fetch_avatars=False)
out = tmp / "r.png"
report.render_report(rep, out)
with Image.open(out) as im:
    check("渲染-宽度 900 且版式完整",
          im.width == report.W and im.height > 1000)
    check("渲染-RGBA 圆角透明边", im.mode == "RGBA")
conn.execute("DELETE FROM danmaku")
conn.commit()
rep0 = report.load_report(conn, 1, fetch_avatars=False)
out0 = tmp / "empty.png"
report.render_report(rep0, out0)
check("渲染-零弹幕场次兜底出图",
      out0.exists() and out0.stat().st_size > 10_000
      and rep0.freqs == {} and not rep0.top)
conn.close()

# ---- CLI:--session 出图 / --room 未命中退出码 2 ------------------------------
r = subprocess.run(
    [sys.executable, "-m", "live_qq_notify.report", "--db", str(db1),
     "--session", "1", "--no-avatar", "--out", str(tmp / "c.png")],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
check("CLI---session 正常出图", r.returncode == 0 and (tmp / "c.png").exists())
r2 = subprocess.run(
    [sys.executable, "-m", "live_qq_notify.report", "--db", str(db1),
     "--room", "23058", "--since", "2026-10-10T12:00:00",
     "--no-avatar", "--out", str(tmp / "c2.png")],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
check("CLI---room 未覆盖场次退出码 2",
      r2.returncode == 2 and "未找到覆盖" in r2.stderr)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} 项通过")
sys.exit(0 if all(results) else 1)
