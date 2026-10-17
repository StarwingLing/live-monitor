# -*- coding: utf-8 -*-
"""Web API 进程内测试：用 FastAPI TestClient 验证房间增删/恢复逻辑
（不走真实网络，绕开本机回环端口耗尽问题；真实网络路径已另行验证）。
v1.5.1：补库隔离（此前直连本地快照库——房间增删经 API 写真库，失败中断
会残留脏行，且与「本地库来自服务器快照」的口径漂移）。

运行：uv run python tests/test_web_api.py
"""
import sys
import tempfile
from pathlib import Path

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import bili_monitor ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 库隔离：在任何 bili_monitor 模块 import 前把 DB_PATH 指向临时库 ----
from bili_monitor import db as _dbm
_DB_TMP = Path(tempfile.mkdtemp(prefix="bili_webapi_test_"))
_dbm.DB_PATH = _DB_TMP / "test.db"
_dbm.init_db(_dbm.DB_PATH)

from fastapi.testclient import TestClient

from bili_monitor.web.app import app

from bili_monitor.web import app as _app_mod
_app_mod.cfg["capture"]["admin_password"] = ""  # 进程内切调试模式（不依赖本地 config 口令，服务不受影响）

client = TestClient(app)


def check(name: str, resp, expect: int):
    ok = "通过" if resp.status_code == expect else f"失败(期望{expect})"
    print(f"[{name}] HTTP {resp.status_code} {ok} {resp.text[:120]}")
    return resp.status_code == expect


results = []
# 1. 无效房间 → 404
results.append(check("无效房间", client.post("/api/rooms", json={"room_id": 99999999}), 404))
# 2. 正常添加（房间 23058，真实存在）→ 200
r = client.post("/api/rooms", json={"room_id": 23058})
results.append(check("正常添加", r, 200))
# 3. 重复添加 → 409
results.append(check("重复添加", client.post("/api/rooms", json={"room_id": 23058}), 409))
# 4. 暂停移除（保留历史）→ 200
results.append(check("暂停移除", client.delete("/api/rooms/23058?keep_history=true"), 200))
# 5. 恢复监控 → 200
results.append(check("恢复监控", client.post("/api/rooms/23058/enable"), 200))
# 6. 彻底删除 → 200
results.append(check("彻底删除", client.delete("/api/rooms/23058?keep_history=false"), 200))
# 7. 再删不存在的 → 404
results.append(check("删除不存在", client.delete("/api/rooms/23058"), 404))

print("\n汇总:", "全部通过" if all(results) else "存在失败")
raise SystemExit(0 if all(results) else 1)
