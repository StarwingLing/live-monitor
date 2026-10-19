# -*- coding: utf-8 -*-
"""只读验证脚本（v0.2.2 ×N 展示修复）：用 TestClient 检查场次详情接口
礼物流 / 礼物聚合两个字段的返回，不写库、无副作用。

运行：uv run python tests/test_gift_display_check.py
"""
import sys
from pathlib import Path

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import bili_monitor ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bili_monitor.web.app import app

from bili_monitor.web import app as _app_mod
_app_mod.cfg["capture"]["admin_password"] = ""  # 进程内切调试模式（不依赖本地 config 口令，服务不受影响）

client = TestClient(app)

ROOM_ID = 7734200        # 替换为你本地开发库中已监控的房间号
SESSION_ID = 6           # 替换为对应场次（本脚本需本地开发库含真实数据才可跑）

resp = client.get(f"/api/rooms/{ROOM_ID}/dashboard", params={"session_id": SESSION_ID})
assert resp.status_code == 200, resp.text
d = resp.json()

ok = True

# --- 礼物聚合（giftList 数据源）--------------------------------------------
# 上舰行应带 type='guard' 与 orders（笔数）；总督x3月 单笔 → orders=1
guard_rows = [g for g in d["gift_agg"] if g["type"] == "guard"]
print("=== gift_agg 中的上舰行 ===")
for g in guard_rows:
    print(g)
gov = next((g for g in guard_rows if g["name"] == "总督x3月"), None)
if gov is None:
    print("[失败] gift_agg 未找到 总督x3月"); ok = False
else:
    # orders=1（单笔）→ 前端 guardTimes 输出空串，展示为"总督x3月"（无 ×N）
    if gov["orders"] == 1 and gov["cnt"] == 3 and gov["value"] == 47994.0:
        print(f"[通过] 总督x3月: orders={gov['orders']} cnt={gov['cnt']} value={gov['value']}"
              " → 页面将显示「总督x3月」（不带 ×3）")
    else:
        print(f"[失败] 总督x3月 字段异常: {gov}"); ok = False

# --- 礼物流（弹幕流合并展示的数据源）----------------------------------------
# recent_gifts 的 guard 行 count=3，前端 giftLineHtml 对 guard 不再追加 ×count
rg_gov = [g for g in d["recent_gifts"] if g.get("type") == "guard"]
print("\n=== recent_gifts 中的上舰行 ===")
for g in rg_gov:
    print(g)

# --- 场次收入一致性 ----------------------------------------------------------
print(f"\n场次礼物流水 gift_value = {d['session_info']['gift_value']}"
      "（其中总督 47994 + 提督 1998 + 舰长 138 + 礼物 2790.4 + SC 2）")

print("\n汇总:", "全部通过" if ok else "存在失败")
raise SystemExit(0 if ok else 1)
