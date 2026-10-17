# -*- coding: utf-8 -*-
"""v0.2 改动点的进程内测试（不依赖真实网络，数据库用临时文件隔离）。

覆盖：
1. 盲盒收益口径（handler._on_gift）：
   - price>0 → 按开出礼物单价×数量计价；
   - price 缺失 → 盲盒价值映射表兜底（开出礼物名 → 盲盒名 两次尝试）；
   - 都查不到 → 回退盲盒实付价（与 0.1 行为一致）；
   - 普通礼物折算不变；盲盒行带 blind_box 字段。
2. 弹幕表情提取（handler._extract_emots）：混合内嵌表情包 / 纯表情弹幕 /
   基础表情占位符查缓存表。
3. 用户分层实时更新（writer）：新用户首行分层正确；付费后升级核心；
   发弹幕升级活跃且不降级核心。
4. Web API：场次序号（seq）、盲盒价值映射 CRUD、单条礼物记录修正
   （含场次营收冗余合计重算）。

运行：uv run python tests/test_0_2.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import replay 包 ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 测试库隔离：在任何 bili_monitor 模块 import 前把 DB_PATH 指到临时文件 ----
# ignore_cleanup_errors：Windows 下解释器退出时若仍有连接未释放，跳过清理报错
_tmpdir = tempfile.TemporaryDirectory(prefix="bili_monitor_test_", ignore_cleanup_errors=True)
import replay.db as bm_db
bm_db.DB_PATH = Path(_tmpdir.name) / "test.db"
bm_db.DATA_DIR = Path(_tmpdir.name)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


# ===========================================================================
# 1+2. handler：盲盒收益 + 表情提取
# ===========================================================================
from replay.capture.handler import EventRouter, _extract_emots
from replay.capture.caches import RuntimeCaches
from replay.db import Db, init_db


def make_gift(**kw):
    """构造 blivedm GiftMessage 的替身（字段默认值对应真实盲盒消息形态）。"""
    base = dict(gift_name="纯白之爱", num=1, uname="测试用户", uid=10001,
                timestamp=1_760_000_000, gift_id=1, gift_type=0, gift_img_basic="",
                action="赠送", price=22000, rnd="", coin_type="gold", total_coin=15000,
                tid="", medal_level=20, medal_name="心球仪", medal_room_id=1,
                medal_ruid=1, blind_gift_name="心动盲盒", blind_price=15000, guard_level=0)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDanmaku(SimpleNamespace):
    """DanmakuMessage 替身：补上 blivedm 的两个 property（dict 原样返回）。"""

    @property
    def emoticon_options_dict(self):
        v = getattr(self, "_emoticon_options", "")
        return v if isinstance(v, dict) else {}

    @property
    def extra_dict(self):
        v = getattr(self, "_extra", "")
        return v if isinstance(v, dict) else {}


def make_danmaku(**kw):
    """构造 DanmakuMessage 替身。emoticon_options/extra 用 dict 形态
    （blivedm 的 *_dict property 对 dict 原样返回）。"""
    base = dict(mode=1, font_size=25, color=0, timestamp=1_760_000_000_000, rnd=1,
                uid_crc32="", msg_type=0, bubble=0, dm_type=0,
                emoticon_options="", voice_config="",
                mode_info={}, is_mirror=False,
                msg="哈哈[大笑]", uid=10001, uname="测试用户", face="",
                admin=0, vip=0, svip=0, urank=0, mobile_verify=0, uname_color="",
                medal_level=20, medal_name="心球仪", runme="", medal_room_id=1,
                medal_ruid=1, mcolor=0xF973B9, privilege_type=0, wealth_level=0)
    base.update(kw)
    # 把两个 property 数据源挂到内部字段（模拟 blivedm 的 dict 直通行为）
    base["_emoticon_options"] = base.get("emoticon_options", "")
    base["_extra"] = base.pop("extra_dict", "")
    return FakeDanmaku(**base)


init_db(bm_db.DB_PATH)
db = Db(bm_db.DB_PATH)
caches = RuntimeCaches(db)
queue: asyncio.Queue = asyncio.Queue()
router = EventRouter(7734200, queue, caches=caches)
router.session_id = 99


def drain() -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait()[1])
    return out


# 1.1 盲盒：price>0（22000 瓜子=22元 的开出礼物）→ 收益 22 元而非盒价 15 元
router._on_gift(None, make_gift())
p = drain()[0]
check("盲盒按开出礼物计价", p["value_yuan"] == 22.0 and p["name"] == "纯白之爱"
      and p["blind_box"] == "心动盲盒", f"got {p}")

# 1.2 盲盒多数量：price × num
router._on_gift(None, make_gift(num=3))
p = drain()[0]
check("盲盒多数量按开出礼物单价×数量", p["value_yuan"] == 66.0, f"got {p['value_yuan']}")

# 1.3 盲盒 price 缺失 → 映射表兜底（开出礼物名命中）
db.execute("INSERT INTO blind_gift_prices (gift_name, price_yuan, note, updated_at) "
           "VALUES ('神秘礼物', 5.0, '测试', '2026-09-13T00:00:00')")
db.execute("INSERT INTO blind_gift_prices (gift_name, price_yuan, note, updated_at) "
           "VALUES ('羁绊宝盒', 8.0, '测试', '2026-09-13T00:00:00')")
caches.refresh()
router._on_gift(None, make_gift(gift_name="神秘礼物", price=0, num=2,
                                blind_gift_name="羁绊宝盒", total_coin=66000))
p = drain()[0]
check("price缺失按映射表(礼物名)兜底", p["value_yuan"] == 10.0, f"got {p['value_yuan']}")

# 1.4 盲盒 price 缺失且礼物名未命中 → 盲盒名命中
router._on_gift(None, make_gift(gift_name="未知礼物", price=0, num=2,
                                blind_gift_name="羁绊宝盒", total_coin=66000))
p = drain()[0]
check("price缺失按映射表(盲盒名)兜底", p["value_yuan"] == 16.0, f"got {p['value_yuan']}")

# 1.5 全查不到 → 回退盲盒实付价
router._on_gift(None, make_gift(gift_name="未知礼物2", price=0, num=2,
                                blind_gift_name="无名盒", total_coin=66000))
p = drain()[0]
check("全查不到回退盲盒实付价", p["value_yuan"] == 66.0, f"got {p['value_yuan']}")

# 1.6 普通礼物口径不变（total_coin/1000，无 blind_box）
router._on_gift(None, make_gift(blind_gift_name="", price=1000, total_coin=3000, num=3))
p = drain()[0]
check("普通礼物仍按实付价", p["value_yuan"] == 3.0 and p["blind_box"] == "",
      f"got {p}")

# 1.7 免费礼物记 0
router._on_gift(None, make_gift(coin_type="silver", total_coin=600))
p = drain()[0]
check("免费礼物记 0 元", p["value_yuan"] == 0.0)


# 2. 表情提取
# 2.1 基础表情占位符查缓存
db.execute("INSERT INTO emotes (room_id, emoji_key, url, width, height, updated_at) "
           "VALUES (7734200, '大笑', 'http://x/dx.png', 60, 60, '2026-09-13T00:00:00')")
caches.refresh()
m = make_danmaku()
emots = json.loads(_extract_emots(7734200, m, caches))
check("基础表情查缓存表", emots.get("[大笑]", {}).get("url") == "http://x/dx.png",
      f"got {emots}")

# 2.2 混合弹幕内嵌表情包（extra.emots）
m = make_danmaku(msg="看这个[涟洢Lianyi_打call]好玩",
                 mode_info={}, extra_dict={"emots": {
                     "[涟洢Lianyi_打call]": {"url": "http://x/dk.webp",
                                             "width": 60, "height": 60}}})
emots = json.loads(_extract_emots(7734200, m, None))
check("内嵌表情包来自 extra.emots",
      emots.get("[涟洢Lianyi_打call]", {}).get("url") == "http://x/dk.webp", f"got {emots}")

# 2.3 纯表情弹幕（dm_type=1）
m = make_danmaku(dm_type=1, msg="", emoticon_options={
    "url": "http://x/pure.webp", "width": 60, "height": 60,
    "emoticon_unique": "official_13"})
emots = json.loads(_extract_emots(7734200, m, None))
check("纯表情弹幕取 emoticon_options",
      any(v["url"] == "http://x/pure.webp" for v in emots.values()), f"got {emots}")

# 2.4 查不到的表情保留原文、不进 emots
m = make_danmaku(msg="未知[不存在表情X]")

check("未知表情不进 emots", _extract_emots(7734200, m, caches) == "")

# ===========================================================================
# 3. writer：分层实时更新
# ===========================================================================
from replay.capture.writer import Writer

# 明细表外键链：rooms → live_sessions(99) → danmaku/gifts
db.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
           "VALUES (7734200, '测试房间', 1, '2026-09-13T00:00:00')")
db.execute("INSERT INTO live_sessions (session_id, room_id, start_time) "
           "VALUES (99, 7734200, '2026-09-13T12:00:00')")

wq: asyncio.Queue = asyncio.Queue()
writer = Writer(db, wq)
writer.session_id = None  # 不影响

# 新用户首条弹幕 → v0.5 新口径：单场不足 10 条只记"普通"（原 v0.2 即活跃）
wq.put_nowait(("danmaku", {"room_id": 7734200, "session_id": 99, "uid": 20001,
                           "uname": "新观众", "medal_name": "", "medal_level": 0,
                           "medal_color": 0, "privilege_type": 0, "text": "大家好",
                           "emots": "", "ts": "2026-09-13T12:00:00"}))
writer._flush()
row = db.query("SELECT tier FROM user_stats WHERE uid=20001 AND room_id=7734200")[0]
check("新用户单条弹幕记普通（v0.5 单场阈值）", row["tier"] == "普通", f"got {row['tier']}")

# 之后送礼 → 升核心（v0.5 二次调整：单场送礼累计 ≥10 元升核心；1 元已不够）
wq.put_nowait(("gift", {"room_id": 7734200, "session_id": 99, "uid": 20001,
                        "uname": "新观众", "type": "gift", "name": "小花花",
                        "blind_box": "", "count": 1, "value_yuan": 12.0,
                        "gift_img": "", "medal_name": "", "medal_level": 0,
                        "privilege_type": 0, "ts": "2026-09-13T12:01:00"}))
writer._flush()
row = db.query("SELECT tier, gift_value_yuan FROM user_stats WHERE uid=20001 AND room_id=7734200")[0]
check("单场送礼12元升级核心", row["tier"] == "核心" and row["gift_value_yuan"] == 12.0,
      f"got {dict(row)}")

# 已是核心的用户再发弹幕不降级
wq.put_nowait(("danmaku", {"room_id": 7734200, "session_id": 99, "uid": 20001,
                           "uname": "新观众", "medal_name": "", "medal_level": 0,
                           "medal_color": 0, "privilege_type": 0, "text": "再来一条",
                           "emots": "", "ts": "2026-09-13T12:02:00"}))
writer._flush()
row = db.query("SELECT tier, danmaku_count FROM user_stats WHERE uid=20001 AND room_id=7734200")[0]
check("核心用户发弹幕不降级", row["tier"] == "核心" and row["danmaku_count"] == 2,
      f"got {dict(row)}")

# v2.0.0 舰队等级跟随修复：舰级数字越小等级越高（1总督/2提督/3舰长），
# 原 MAX 口径会把舰长(3)升提督(2)锁死在 3。现口径：跟随最新非零观测；
# 非舰队消息（0）不覆盖已知等级
for lv, txt in ((3, "我是舰长"), (2, "升提督啦")):
    wq.put_nowait(("danmaku", {"room_id": 7734200, "session_id": 99, "uid": 20002,
                               "uname": "升舰者", "medal_name": "", "medal_level": 0,
                               "medal_color": 0, "privilege_type": lv, "text": txt,
                               "emots": "", "ts": f"2026-09-13T12:0{lv}:00"}))
writer._flush()
row = db.query("SELECT guard_level, tier FROM user_stats "
               "WHERE uid=20002 AND room_id=7734200")[0]
check("升舰跟随：舰长(3)→提督(2) 及时更新且仍为核心",
      row["guard_level"] == 2 and row["tier"] == "核心", f"got {dict(row)}")
wq.put_nowait(("danmaku", {"room_id": 7734200, "session_id": 99, "uid": 20002,
                           "uname": "升舰者", "medal_name": "", "medal_level": 0,
                           "medal_color": 0, "privilege_type": 0, "text": "没戴牌子",
                           "emots": "", "ts": "2026-09-13T12:05:00"}))
writer._flush()
row = db.query("SELECT guard_level FROM user_stats WHERE uid=20002 AND room_id=7734200")[0]
check("非舰队消息(0)不覆盖已知等级（保留 2）",
      row["guard_level"] == 2, f"got {dict(row)}")

# 盲盒礼物行落库带 blind_box（推一条盲盒事件过 writer 验证字段链路）
wq.put_nowait(("gift", {"room_id": 7734200, "session_id": 99, "uid": 20001,
                        "uname": "新观众", "type": "gift", "name": "纯白之爱",
                        "blind_box": "心动盲盒", "count": 1, "value_yuan": 22.0,
                        "gift_img": "", "medal_name": "", "medal_level": 0,
                        "privilege_type": 0, "ts": "2026-09-13T12:03:00"}))
writer._flush()
g = db.query("SELECT name, blind_box FROM gifts WHERE room_id=7734200 AND blind_box!='' LIMIT 1")
check("盲盒行落库带 blind_box", bool(g) and g[0]["name"] == "纯白之爱"
      and g[0]["blind_box"] == "心动盲盒", f"got {[dict(x) for x in g]}")

db.close()

print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
