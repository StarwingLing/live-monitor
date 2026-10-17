# -*- coding: utf-8 -*-
"""v2.2.5 改动点的进程内测试：SC 双推去重（handler._on_super_chat）。

背景：2026-10-16 18:26:09 房间 1024 的 ¥1000 SC 被 B 站在同一秒
连推两帧 SUPER_CHAT_MESSAGE，采集端原先无去重重复入库（gifts 表
id 78354/78355 两行完全相同，虚增营收 ¥1000）。修复按
(uid, price, start_time) 60 秒窗口去重。本套覆盖：
1. 同键背靠背两帧 → 只入队一条；
2. 窗口外（>60s）同键再来 → 正常入队（真实补发/极端巧合不被误杀）；
3. 不同 start_time（新 SC）→ 各自入队；
4. 不同 price（同用户同秒另一档 SC）→ 各自入队；
5. 入队 payload 口径不变（type=sc、value_yuan=price、text=message）。

运行：uv run python tests/test_v2_2_5.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import replay 包 ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


from replay.capture.handler import EventRouter

queue: asyncio.Queue = asyncio.Queue()
router = EventRouter(1024, queue)   # caches 缺省 None：SC 路径不使用
router.session_id = 197


def make_sc(**kw):
    """构造 blivedm SuperChatMessage 替身（只含 _on_super_chat 用到的字段）。"""
    base = dict(uid=10001, uname="测试用户", price=1000,
                message="满月纪念！", start_time=1_792_146_369)
    base.update(kw)
    return SimpleNamespace(**base)


def drain() -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait()[1])
    return out


# 1. 同键背靠背两帧（B 站双推形态：同 uid/price/start_time，间隔毫秒级）
router._on_super_chat(None, make_sc())
router._on_super_chat(None, make_sc())
events = drain()
check("同键双推只入队一条", len(events) == 1, f"got {len(events)} 条")
check("payload 口径不变", events and events[0]["type"] == "sc"
      and events[0]["value_yuan"] == 1000.0 and events[0]["text"] == "满月纪念！"
      and events[0]["session_id"] == 197, f"got {events}")

# 2. 窗口外同键再来 → 入队（把已记录的时间戳拨回 61 秒前模拟超窗）
router._on_super_chat(None, make_sc(price=50))
drain()
key = (10001, 50, 1_792_146_369)
router._recent_sc[key] -= 61.0
router._on_super_chat(None, make_sc(price=50))
events = drain()
check("窗口外同键正常入队", len(events) == 1, f"got {len(events)} 条")

# 3. 不同 start_time（新一条 SC）→ 各自入队
router._on_super_chat(None, make_sc(price=30, start_time=1_792_147_000))
router._on_super_chat(None, make_sc(price=30, start_time=1_792_147_030))
events = drain()
check("不同 start_time 各自入队", len(events) == 2, f"got {len(events)} 条")

# 4. 同用户同秒不同价（两档 SC 极端并发）→ 键不同，各自入队
router._on_super_chat(None, make_sc(price=100, start_time=1_792_148_000))
router._on_super_chat(None, make_sc(price=200, start_time=1_792_148_000))
events = drain()
check("不同 price 各自入队", len(events) == 2
      and {e["value_yuan"] for e in events} == {100.0, 200.0}, f"got {events}")

# 5. 去重窗滚动清理：清理只针对窗口外（>60s）的过期键，全新鲜的键保留
# （收缩在键数 >64 时触发）。把既有键整体回拨到 61 秒前，再灌 60 个新键
# ——插入过程中总数越过 64 触发收缩，过期键应被清光，最终只剩 60 个新键
router._recent_sc = {k: t - 61.0 for k, t in router._recent_sc.items()}
for i in range(60):
    router._on_super_chat(None, make_sc(price=5000 + i, start_time=1_792_149_000 + i))
drain()
check("去重窗滚动清理过期键", len(router._recent_sc) == 60,
      f"got {len(router._recent_sc)}")

print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
