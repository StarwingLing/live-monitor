# -*- coding: utf-8 -*-
"""v2.3~v2.5 QQ 机器人联动(live-qq-notify)测试套。

覆盖(v2.5.0 多机器人改造后):
- 锁步 DDL:qq_notify / qq_bots / qq_group_map(复合主键) 建表与迁移种子
  (qq_notify_global 冻结表值 → 机器人 1;群映射存量行归机器人 1);
- 机器人列表 / 新建 / 改名 / 配置 GET·PATCH(校验与合并);
- 指令表归一(旧格式兼容) / 房间级三态 + join_check + bot_id;
- status / qrlogin / 群映射(绑定会话、备注、按机器人过滤);
- AI 智能回复:请求形状 / 门控 / 频控可配置 / 失败重试;
- 入群申请核查:_join_answer / _join_lookup / _handle_join_request 端到端。

运行:uv run python tests/test_v2_3.py
"""
import asyncio as _aio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 库隔离:在任何 bili_monitor 模块 import 前把 DB_PATH 指向临时库 ----
from bili_monitor import db as _dbm
_DB_TMP = Path(tempfile.mkdtemp(prefix="bili_v2_5_test_"))
_dbm.DB_PATH = _DB_TMP / "test.db"
_dbm.init_db(_dbm.DB_PATH)

from fastapi.testclient import TestClient

from bili_monitor.web.app import app
from bili_monitor.web import app as _app_mod
_app_mod.cfg["capture"]["admin_password"] = ""  # 进程内调试模式(免登录)

client = TestClient(app)
results = []


def check(name: str, ok: bool, extra: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name} {extra}")


def _ai_runs(coro):
    _aio.run(coro)


# ---- 0. 锁步 DDL + v2.5.0 迁移种子:机器人 1 承接旧全局默认 ----
conn = _dbm.connect()
tbls = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("锁步三表已建", {"qq_notify", "qq_bots", "qq_group_map"} <= tbls)
check("迁移种子:机器人 1=默认机器人(双通知开/防抖300/napcat)",
      tuple(conn.execute("SELECT name, channel, notify_on, notify_off, "
                         "debounce_offline FROM qq_bots WHERE id=1").fetchone())
      == ("默认机器人", "napcat", 1, 1, 300))
check("旧全局表冻结保留(迁移后仍可读)",
      tuple(conn.execute("SELECT notify_on, debounce_offline "
                         "FROM qq_notify_global WHERE id=1").fetchone())
      == (1, 300))
# 直插两个测试房间(绕过 add_room 的 B 站网络校验;created_at 非空)
conn.executemany("INSERT OR REPLACE INTO rooms(room_id, anchor_name, enabled, created_at) "
                 "VALUES (?, ?, 1, '2026-10-13T00:00:00')",
                 [(111, "测试甲"), (222, "测试乙")])
conn.commit()
conn.close()

# ---- 1. 机器人列表 / 新建 / 改名 ----
r = client.get("/api/qq-notify/bots")
check("GET bots 列表(迁移种子仅机器人1)", r.status_code == 200
      and [x["id"] for x in r.json()["items"]] == [1]
      and r.json()["items"][0]["name"] == "默认机器人")
r = client.post("/api/qq-notify/bots", json={})
check("POST 新建机器人(空名自动「机器人N」)", r.status_code == 200
      and r.json()["id"] == 2 and r.json()["name"] == "机器人2")
r = client.patch("/api/qq-notify/bots/2", json={"name": "备用号"})
check("PATCH 机器人改名", r.status_code == 200 and r.json()["name"] == "备用号")
r = client.patch("/api/qq-notify/bots/2", json={"name": "  "})
check("PATCH 空名 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bots/2", json={"name": "x" * 21})
check("PATCH 超长名(>20) → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bots/999", json={"name": "y"})
check("PATCH 不存在机器人 → 404", r.status_code == 404)

# ---- 2. 机器人配置 GET/PATCH(旧全局用例迁移) ----
r = client.get("/api/qq-notify/bot/1")
check("GET bot/1 默认(账号空/双通知开/模板/防抖300/频控默认)",
      r.status_code == 200 and r.json()["notify_on"] is True
      and r.json()["onebot_url"].startswith("http://127.0.0.1")
      and r.json()["debounce_offline"] == 300
      and r.json()["ai_reply_enabled"] is False
      and r.json()["ai_rate_limit"] == 3 and r.json()["ai_rate_window"] == 300)
r = client.patch("/api/qq-notify/bot/1", json={"onebot_url": "ftp://x"})
check("PATCH 非法 url → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1",
                 json={"qq_account": "12345", "notify_off": False,
                       "template_on": "🎉 {uname}: {title}\n{cover}"})
check("PATCH 合并更新", r.status_code == 200 and
      r.json()["qq_account"] == "12345" and r.json()["notify_off"] is False
      and r.json()["notify_on"] is True and
      r.json()["template_on"] == "🎉 {uname}: {title}\n{cover}")
r = client.patch("/api/qq-notify/bot/1", json={"debounce_offline": 99999})
check("PATCH 防抖越界 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1", json={"channel": "sms"})
check("PATCH channel 非法值 → 400", r.status_code == 400)
r = client.get("/api/qq-notify/bot/999")
check("GET 不存在机器人 → 404", r.status_code == 404)

# ---- 2.5 指令表归一与回环(旧 9.6 迁移) ----
r = client.patch("/api/qq-notify/bot/1",
                 json={"command_replies": {"/ping": "pong!", "/帮助": "测试帮助"}})
check("PATCH 旧格式(纯字符串)归一存库", r.status_code == 200
      and r.json()["command_replies"] == {
          "/ping": {"reply": "pong!", "groups": []},
          "/帮助": {"reply": "测试帮助", "groups": []}})
r = client.patch("/api/qq-notify/bot/1", json={
    "command_replies": {
        "/ping": {"reply": "pong!", "groups": [4242, 8888]},
        "/帮助": {"reply": "测试帮助", "groups": []}}})
check("PATCH 新格式带限定群(GET 回显)", r.status_code == 200
      and r.json()["command_replies"]["/ping"] ==
      {"reply": "pong!", "groups": [4242, 8888]})
r = client.patch("/api/qq-notify/bot/1",
                 json={"command_replies": {"/x": {"reply": "y", "groups": [0, -1]}}})
check("限定群号非正整数 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1",
                 json={"command_replies": {"/x": {"reply": "y", "groups": ["abc"]}}})
check("限定群号非整数列表 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1", json={"command_replies": {}})
check("清空回退默认(GET 生效口径,新格式形状)",
      r.status_code == 200
      and r.json()["command_replies"] ==
      {"/连接": {"reply": _app_mod.QQ_COMMAND_REPLIES["/连接"]["reply"],
                 "groups": []}})
client.patch("/api/qq-notify/bot/1",
             json={"command_replies": {"/ping": "pong!", "/帮助": "测试帮助"}})

# ---- 3. status 探测:bot 维度(先指到必关端口避免撞本地 mock) ----
client.patch("/api/qq-notify/bot/1",
             json={"onebot_url": "http://127.0.0.1:39998"})
r = client.get("/api/qq-notify/status?bot_id=1")
check("status 端点(NapCat 未启动→不可达)", r.status_code == 200
      and r.json()["online"] is False and "不可达" in r.json()["message"])
client.patch("/api/qq-notify/bot/1",
             json={"onebot_url": "http://127.0.0.1:3000"})

# ---- 4. 房间级:三态 + join_check + bot_id ----
r = client.get("/api/rooms/111/qq-notify")
check("GET 房间通知(无行默认;含 join_check/bot_id)", r.status_code == 200
      and r.json()["enabled"] is False and r.json()["group_ids"] == []
      and r.json()["join_check"] is False and r.json()["bot_id"] is None
      and "{uname}" in r.json()["global_template_on"])
r = client.patch("/api/rooms/111/qq-notify",
                 json={"enabled": True, "group_ids": [8888, 4242, 8888, 0],
                       "join_check": True, "bot_id": 2})
check("PATCH 房间(群去重去非正+核查开关+归属机器人2)", r.status_code == 200
      and r.json()["enabled"] is True and r.json()["group_ids"] == [4242, 8888]
      and r.json()["join_check"] is True and r.json()["bot_id"] == 2
      and r.json()["global_template_on"].startswith("📺"))   # bot2 出厂模板
r = client.patch("/api/rooms/111/qq-notify", json={"template_on": "房间的"})
check("PATCH 房间专属模板(未传字段保持)", r.status_code == 200
      and r.json()["template_on"] == "房间的"
      and r.json()["join_check"] is True and r.json()["bot_id"] == 2)
r = client.patch("/api/rooms/111/qq-notify",
                 json={"notify_on": False, "notify_off": True,
                       "report_offline": True, "debounce_offline": 120})
check("房间 PATCH-个人配置覆盖写入", r.status_code == 200
      and r.json()["notify_on"] is False and r.json()["notify_off"] is True
      and r.json()["report_offline"] is True
      and r.json()["debounce_offline"] == 120)
r = client.patch("/api/rooms/111/qq-notify",
                 json={"notify_on": None, "debounce_offline": None, "bot_id": None})
check("房间 PATCH-null 恢复跟随/默认机器人", r.status_code == 200
      and r.json()["notify_on"] is None and r.json()["debounce_offline"] is None
      and r.json()["bot_id"] is None and r.json()["join_check"] is True)
r = client.patch("/api/rooms/111/qq-notify", json={"bot_id": 999})
check("房间 PATCH-机器人不存在 400", r.status_code == 400)
r = client.patch("/api/rooms/111/qq-notify", json={"group_ids": ["x"]})
check("PATCH 非整数群 → 422", r.status_code == 422)
r = client.patch("/api/rooms/222/qq-notify", json={"enabled": True})
check("PATCH 222 开关(无群行)", r.status_code == 200
      and r.json()["enabled"] is True and r.json()["group_ids"] == []
      and r.json()["join_check"] is False)
# 卡片列表:含 join_check/bot_id
client.patch("/api/rooms/222/qq-notify", json={"enabled": False})
r = client.get("/api/qq-notify/rooms")
body = r.json()
row111 = next(x for x in body["rooms"] if x["room_id"] == 111)
check("GET /api/qq-notify/rooms 卡片数据(核查开关/归属机器人)",
      r.status_code == 200 and [x["room_id"] for x in body["rooms"]] == [111]
      and row111["join_check"] is True and row111["bot_id"] is None)
client.patch("/api/rooms/111/qq-notify", json={"enabled": False})
client.patch("/api/rooms/222/qq-notify", json={"enabled": False})

# ---- 5. summary 透出(回归) ----
client.patch("/api/rooms/111/qq-notify", json={"enabled": True, "bot_id": 1})
r = client.get("/api/summary?days=1")
rooms = {x["room_id"]: x for x in r.json()["rooms"]}
check("summary 通知字段透出(回归)",
      rooms[111]["notify_enabled"] is True
      and rooms[111]["notify_group_count"] == 2
      and rooms[111]["notify_custom"] is True)
client.patch("/api/rooms/111/qq-notify", json={"enabled": False})

# ---- 6. 扫码登录端点(bot 1 的 WebUI 配置;内嵌模拟) ----
import base64 as _b64
import hashlib as _hl
import threading as _threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_WEBUI_TOKEN = "test-webui-token"
_wui_calls = {"check": 0}
_wui_cred = {"v": ""}


def _wui_hash():
    return _hl.sha256((_WEBUI_TOKEN + ".napcat").encode()).hexdigest()


class _MiniWebUI(BaseHTTPRequestHandler):
    """NapCat WebUI 最小子集:auth/login + CheckLoginStatus(两轮后 online)。"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if self.path == "/api/auth/login":
            _wui_cred["v"] = _b64.b64encode(json.dumps(
                {"CreatedTime": 1, "HashEncoded": _wui_hash()}).encode()).decode()
            body = {"code": 0, "Credential": _wui_cred["v"]}
        elif self.headers.get("Authorization") != "Bearer " + _wui_cred["v"]:
            body = {"code": -1, "message": "Unauthorized"}
        elif self.path == "/api/QQLogin/CheckLoginStatus":
            _wui_calls["check"] += 1
            online = _wui_calls["check"] >= 3   # 首轮 qr、refresh 轮 qr、第三轮 online
            body = {"code": 0, "data": {
                "isLogin": online, "qrLoginAccepted": False,
                "qrcodeurl": "" if online else "https://ptlogin.mock/qr",
                "loginError": ""}}
        elif self.path == "/api/QQLogin/GetQQLoginQrcode":
            body = {"code": 0, "data": {"qrcode": "https://ptlogin.mock/qr"}}
        elif self.path == "/api/QQLogin/RefreshQRcode":
            body = {"code": 0, "data": {"qrcodeurl": "https://ptlogin.mock/qr-new"}}
        else:
            body = {"code": -1, "message": "no such api"}
        out = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


_srv = ThreadingHTTPServer(("127.0.0.1", 0), _MiniWebUI)
_port = _srv.server_address[1]
_threading.Thread(target=_srv.serve_forever, daemon=True).start()

r = client.get("/api/qq-notify/qrlogin?bot_id=1")
check("qrlogin 未配置 token → error", r.status_code == 200
      and r.json()["phase"] == "error" and "WebUI" in r.json()["message"])
client.patch("/api/qq-notify/bot/1",
             json={"webui_url": f"http://127.0.0.1:{_port}",
                   "webui_token": _WEBUI_TOKEN})
r = client.get("/api/qq-notify/qrlogin?bot_id=1")
check("qrlogin 首轮出二维码", r.status_code == 200
      and r.json()["phase"] == "qr" and r.json()["qrcode"] == "https://ptlogin.mock/qr")
r = client.get("/api/qq-notify/qrlogin?refresh=1&bot_id=1")
check("qrlogin refresh=1 换新码(保鲜)", r.status_code == 200
      and r.json()["qrcode"] == "https://ptlogin.mock/qr-new")
r = client.get("/api/qq-notify/qrlogin?bot_id=1")
check("qrlogin 次轮登录成功", r.status_code == 200
      and r.json()["phase"] == "online")
_srv.shutdown()
client.patch("/api/qq-notify/bot/1", json={"webui_token": ""})

# ---- 7. 生产模式权限:未登录写/读 401,旧通道 token 可写 ----
_app_mod.cfg["capture"]["admin_password"] = "secret-v2-5-test"
c2 = TestClient(app)
check("未登录 PATCH 房间通知 → 401", c2.patch(
    "/api/rooms/111/qq-notify", json={"enabled": False}).status_code == 401)
check("未登录 GET bot 配置 → 401", c2.get(
    "/api/qq-notify/bot/1").status_code == 401)
check("未登录 GET bots 列表 → 401", c2.get(
    "/api/qq-notify/bots").status_code == 401)
check("旧通道 token 可写", c2.patch(
    "/api/rooms/111/qq-notify", json={"enabled": False},
    headers={"X-Admin-Token": "secret-v2-5-test"}).status_code == 200)
_app_mod.cfg["capture"]["admin_password"] = ""  # 还原调试模式

# ---- 8. 官方通道 status + 群映射(按机器人) ----
_OFF_RESP = {"body": {"access_token": "T0KEN", "expires_in": 7200}}


class _MiniOfficial(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        out = json.dumps(_OFF_RESP["body"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


_off_srv = ThreadingHTTPServer(("127.0.0.1", 0), _MiniOfficial)
_off_port = _off_srv.server_address[1]
_threading.Thread(target=_off_srv.serve_forever, daemon=True).start()
_orig_official_api = _app_mod._OFFICIAL_API
_app_mod._OFFICIAL_API = f"http://127.0.0.1:{_off_port}"

# 8.1 official 未配置凭据 → 友好提示
client.patch("/api/qq-notify/bot/1", json={"channel": "official"})
r = client.get("/api/qq-notify/status?bot_id=1")
check("status official 未配置凭据", r.status_code == 200
      and r.json()["online"] is False and r.json()["channel"] == "official"
      and "未配置" in r.json()["message"])
# 8.2 official 凭据有效 → 在线(mock token 接口)
client.patch("/api/qq-notify/bot/1",
             json={"official_app_id": "1905812103",
                   "official_app_secret": "sec"})
r = client.get("/api/qq-notify/status?bot_id=1")
check("status official 凭据有效在线", r.status_code == 200
      and r.json()["online"] is True and r.json()["account"] == "1905812103"
      and "官方通道就绪" in r.json()["message"])
_OFF_RESP["body"] = {"code": 100016, "message": "appid or secret error"}
r = client.get("/api/qq-notify/status?bot_id=1")
check("status official 凭据无效", r.json()["online"] is False
      and "凭据无效" in r.json()["message"])
_OFF_RESP["body"] = {"access_token": "T0KEN", "expires_in": 7200}

# 8.3 绑定:napcat 机器人 → 400(bot2 出厂 napcat)
r = client.post("/api/qq-notify/bind-group",
                json={"group_id": 4242, "bot_id": 2})
check("bind 在 napcat 机器人 → 400", r.status_code == 400)
r = client.post("/api/qq-notify/bind-group",
                json={"group_id": 0, "bot_id": 1})
check("bind 非法群号 → 400", r.status_code == 400)
r = client.get("/api/qq-notify/bind-status?bot_id=1")
check("bind-status 可读", r.status_code == 200
      and r.json()["state"] in ("idle", "timeout", "error", "waiting"))
# 8.4 映射表 CRUD(机器人维度):直插两行(bot1) + 一行(bot2) → 列表过滤
conn = _dbm.connect()
conn.executemany("INSERT OR REPLACE INTO qq_group_map "
                 "(bot_id, group_id, group_openid, updated_at) VALUES (?, ?, ?, ?)",
                 [(1, 4242, "OID4242", "2026-10-17T00:00:00"),
                  (1, 8888, "OID8888", "2026-10-17T00:00:00"),
                  (2, 4242, "OID2-4242", "2026-10-17T00:00:00")])
conn.commit()
conn.close()
r = client.get("/api/qq-notify/group-map?bot_id=1")
check("group-map 按机器人过滤(bot1 两行,不含 bot2 同群行)", r.status_code == 200
      and [x["group_id"] for x in r.json()["items"]] == [4242, 8888]
      and r.json()["items"][0]["group_openid"] == "OID4242"
      and r.json()["items"][0]["remark"] == "")
r = client.patch("/api/qq-notify/group-map/4242?bot_id=1", json={"remark": "粉丝群"})
check("group-map PATCH 备注(机器人维度)", r.status_code == 200
      and r.json()["remark"] == "粉丝群")
conn = _dbm.connect()
conn.execute(  # 与 _bind_worker 落库同款 upsert(重绑同群同机器人):不触碰 remark
    "INSERT INTO qq_group_map (bot_id, group_id, group_openid, updated_at) "
    "VALUES (1, 4242, 'OID4242', '2026-10-18T01:00:00') "
    "ON CONFLICT(bot_id, group_id) DO UPDATE SET "
    "group_openid = excluded.group_openid, updated_at = excluded.updated_at")
conn.commit()
check("重新绑定 upsert 不清备注",
      conn.execute("SELECT remark FROM qq_group_map "
                   "WHERE bot_id=1 AND group_id=4242").fetchone()[0] == "粉丝群")
check("bot2 同群行备注不受 bot1 操作影响",
      conn.execute("SELECT remark FROM qq_group_map "
                   "WHERE bot_id=2 AND group_id=4242").fetchone()[0] == "")
conn.close()
r = client.patch("/api/qq-notify/group-map/4242?bot_id=1", json={"remark": "x" * 51})
check("备注超长(>50) → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/group-map/7777?bot_id=1", json={"remark": "y"})
check("未绑定群 PATCH 备注 → 404", r.status_code == 404)
r = client.delete("/api/qq-notify/group-map/8888?bot_id=1")
check("group-map 解绑(仅 bot1)", r.status_code == 200
      and [x["group_id"] for x in
           client.get("/api/qq-notify/group-map?bot_id=1").json()["items"]]
      == [4242]
      and [x["group_id"] for x in
           client.get("/api/qq-notify/group-map?bot_id=2").json()["items"]]
      == [4242])
conn = _dbm.connect()
conn.execute("INSERT OR REPLACE INTO qq_group_map "
             "(bot_id, group_id, group_openid, updated_at) "
             "VALUES (1, 8888, 'OID8888', '2026-10-18T00:00:00')")
conn.commit()
conn.close()

# ---- 9. 指令回复纯函数 ----
from bili_monitor.web.app import (match_command_reply as _mcr,
                                  _group_openids_for as _gof,
                                  QQ_COMMAND_REPLIES as _QR)
check("指令表含 /连接 且回复非空",
      "/连接" in _QR and _QR["/连接"]["reply"].strip())
check("指令匹配:@前缀剥离后精确命中(元组,groups 空=全部群)",
      _mcr("<@!12345> /连接") == (_QR["/连接"]["reply"], [])
      and _mcr("/连接") == (_QR["/连接"]["reply"], []))
check("指令匹配:未命中/近形不误回(返回 None)",
      _mcr("<@!1> /连接下") is None and _mcr("<@!1> 你好") is None
      and _mcr("") is None)
check("限定群号→openid 集合(按机器人换算,未绑定群为空集)",
      _gof(1, [4242, 8888]) == {"OID4242", "OID8888"}
      and _gof(1, [9999]) == set() and _gof(2, [4242]) == {"OID2-4242"})

# ---- 10. AI 智能回复(bot 维度) ----
client.patch("/api/qq-notify/bot/1", json={
    "ai_reply_enabled": True, "ai_api_key": "sk-test",
    "ai_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "ai_model": "glm-4.5-flash", "ai_system_prompt": "测试人设",
    "ai_rate_limit": 5, "ai_rate_window": 600})
r = client.get("/api/qq-notify/bot/1")
check("PATCH AI 字段回环(含频控)", r.status_code == 200
      and r.json()["ai_reply_enabled"] is True
      and r.json()["ai_api_key"] == "sk-test"
      and r.json()["ai_rate_limit"] == 5 and r.json()["ai_rate_window"] == 600)
r = client.patch("/api/qq-notify/bot/1", json={"ai_base_url": "ftp://x"})
check("PATCH ai_base_url 非法前缀 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1", json={"ai_rate_limit": 0})
check("PATCH ai_rate_limit 越下界 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/bot/1", json={"ai_rate_window": 10})
check("PATCH ai_rate_window 越下界 → 400", r.status_code == 400)

# 在播房间播种一条(live_sessions end_time NULL=在播)供状态行注入验证
conn = _dbm.connect()
conn.execute("INSERT INTO live_sessions(room_id, start_time, end_time) "
             "VALUES (111, '2026-10-18T10:00:00', NULL)")
conn.commit()
conn.close()


class _FakeResp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


_captured = {}


def _fake_urlopen(req, timeout=None):
    _captured["url"] = req.full_url
    _captured["auth"] = req.headers.get("Authorization")
    _captured["body"] = json.loads(req.data.decode())
    return _FakeResp(json.dumps(
        {"choices": [{"message": {"content": "  你好呀~  "}}]}).encode())


_orig_urlopen = _app_mod._urlreq.urlopen
_app_mod._urlreq.urlopen = _fake_urlopen
out = _app_mod._ai_reply_text("今天谁开播?", _app_mod._qq_bot_row(1))
check("AI 请求形状(URL 拼接/Bearer 头/负载)与回复清洗",
      _captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
      and _captured["auth"] == "Bearer sk-test"
      and _captured["body"]["model"] == "glm-4.5-flash"
      and _captured["body"]["max_tokens"] == 300
      and _captured["body"]["messages"][1]["content"] == "今天谁开播?"
      and out == "你好呀~")
check("system prompt 含自定义人设+实时开播状态行",
      _captured["body"]["messages"][0]["content"].startswith("测试人设")
      and "当前开播：测试甲" in _captured["body"]["messages"][0]["content"])
out = _app_mod._ai_reply_text("hi", {"id": 1, "ai_api_key": "k",
                                     "ai_base_url": "", "ai_model": "",
                                     "ai_system_prompt": ""})
check("空配置走内置默认(标准 API 地址/免费模型/内置人设)",
      _captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
      and _captured["body"]["model"] == "glm-4.5-flash"
      and "通知机器人" in _captured["body"]["messages"][0]["content"])
_app_mod._urlreq.urlopen = _orig_urlopen

# 门控(monkeypatch 配置/LLM/发送,不起 WebSocket)
_bot = _app_mod._qq_bot_row(1)
_llm_calls = {"n": 0}
_sent = {"reply": None, "oid": None, "mid": None}


def _fake_llm(text, g):
    _llm_calls["n"] += 1
    return "AI回复:" + text


def _fake_rgt(bot, oid, content, msg_id):
    _sent.update(reply=content, oid=oid, mid=msg_id)


_orig_llm, _orig_rgt = _app_mod._ai_reply_text, _app_mod._reply_group_text
_app_mod._ai_reply_text = _fake_llm
_app_mod._reply_group_text = _fake_rgt
_evt = {"content": "<@!999> 今天心情不好", "group_openid": "OID4242", "id": "MSG1"}
_bot["ai_reply_enabled"] = True
_bot["ai_api_key"] = "sk-test"
_bot["ai_rate_limit"] = 3
_bot["ai_rate_window"] = 300
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 兜底:开关开 → LLM 生成并以 msg_id 被动回复",
      _llm_calls["n"] == 1 and _sent["reply"] == "AI回复:今天心情不好"
      and _sent["oid"] == "OID4242" and _sent["mid"] == "MSG1")
for _ in range(5):   # 同群连发:窗口内超出上限后静默
    _ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 频控:窗口内上限 3 次(计次在调用前)",
      _llm_calls["n"] == 3)
_app_mod._ai_rates.clear()
_bot["ai_rate_limit"] = 1
_llm_calls["n"] = 0
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 频控可配置:limit=1 时第 2 次拦截", _llm_calls["n"] == 1)
_bot["ai_rate_limit"] = 3
_app_mod._ai_rates.clear()
_llm_calls["n"] = 0
_bot["ai_reply_enabled"] = False
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 门控:开关关不调用", _llm_calls["n"] == 0)
_bot["ai_reply_enabled"] = True
_ai_runs(_app_mod._maybe_ai_reply(_bot,
        {"content": "<@!1>", "group_openid": "O", "id": "M"}))
check("AI 门控:纯 @ 空正文不调用", _llm_calls["n"] == 0)


def _boom_llm(text, g):
    _llm_calls["n"] += 1
    raise RuntimeError("接口超时")


_app_mod._AI_RETRY_WAIT = 0   # 测试里重试不等待(生产行为=5s 间隔重试 1 次)
_app_mod._ai_reply_text = _boom_llm
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 失败:两次均失败静默放弃(5s 后重试 1 次,不发送不抛错)",
      _llm_calls["n"] == 2 and _sent["reply"] == "AI回复:今天心情不好")
_llm_calls["n"] = 0


def _flaky_llm(text, g):
    _llm_calls["n"] += 1
    if _llm_calls["n"] == 1:
        raise RuntimeError("瞬时抖动")
    return "AI回复(重试):" + text


_app_mod._ai_reply_text = _flaky_llm
_ai_runs(_app_mod._maybe_ai_reply(_bot, _evt))
check("AI 重试:首败次成仍发出回复", _llm_calls["n"] == 2
      and _sent["reply"] == "AI回复(重试):今天心情不好")
_app_mod._ai_reply_text = _orig_llm
_app_mod._reply_group_text = _orig_rgt
# 还原 AI 配置
client.patch("/api/qq-notify/bot/1", json={
    "ai_reply_enabled": False, "ai_api_key": "", "ai_base_url": "",
    "ai_model": "", "ai_system_prompt": "",
    "ai_rate_limit": 3, "ai_rate_window": 300})

# ---- 11. 入群申请核查 ----
from bili_monitor.web.app import (_join_answer as _jqa,
                                  _join_lookup as _jql,
                                  _handle_join_request as _hjr)
# 11.1 答案提取:qa 列表拼接 / 验证消息 / 均空
check("_join_answer:admin_review_qa 多答案拼接",
      _jqa({"verify_info": {"method": "admin_review_qa", "review_qa_list": [
          {"question": "B站ID?", "answer": "10001"},
          {"question": "从哪知道的?", "answer": "直播间"}]}}) == "10001；直播间")
check("_join_answer:verify_message 原文 / 空事件返回空串",
      _jqa({"verify_info": {"method": "verify_message",
                            "verify_message": "我是老粉"}}) == "我是老粉"
      and _jqa({"verify_info": {}}) == "")
# 11.2 弹幕/粉丝牌查询:播种带牌弹幕 → 按 uid 命中 / 昵称回退 / 无命中
conn = _dbm.connect()
conn.executemany(
    "INSERT INTO danmaku (room_id, uid, uname, medal_name, medal_level, text, ts) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (111, 10001, "汤圆", "汤圆团子", 12, "主播晚上好", "2026-10-18T20:00:01"),
        (111, 10001, "汤圆", "汤圆团子", 12, "今天状态不错", "2026-10-18T20:00:02"),
        (111, 10002, "路人", "", 0, "路过", "2026-10-18T20:00:03"),
    ])
conn.commit()
conn.close()
lk = _jql(111, "10001")
check("_join_lookup:按 uid 命中(最新粉丝牌/近5条弹幕)",
      lk["matched"] and lk["medal_name"] == "汤圆团子" and lk["medal_level"] == 12
      and len(lk["danmaku"]) == 2 and lk["uname"] == "汤圆")
lk = _jql(111, "汤圆")
check("_join_lookup:按昵称命中", lk["matched"] and lk["medal_level"] == 12)
lk = _jql(111, "99999")
check("_join_lookup:数字ID无命中时回退昵称仍无 → matched False",
      not lk["matched"] and lk["medal_name"] == "")
# 11.3 端到端:monkeypatch 主动发送 → 开关开启且群匹配的房间收到报告
_sent_join = {"oid": None, "content": None, "n": 0}


def _fake_active(bot, oid, content):
    _sent_join.update(oid=oid, content=content, n=_sent_join["n"] + 1)


_orig_active = _app_mod._bot_send_active_text
_app_mod._bot_send_active_text = _fake_active
# 房间 111:开启通知+核查开关+群 4242(已绑定 bot1 OID4242);房间 222:开核查但群 7777
client.patch("/api/rooms/111/qq-notify",
             json={"enabled": True, "join_check": True, "group_ids": [4242]})
client.patch("/api/rooms/222/qq-notify",
             json={"enabled": True, "join_check": True, "group_ids": [7777]})
_bot1 = _app_mod._qq_bot_row(1)
_ai_runs(_hjr(_bot1, {"group_openid": "OID4242", "username": "新粉",
                      "verify_info": {"method": "admin_review_qa",
                                      "review_qa_list": [
                                          {"question": "B站ID?", "answer": "10001"}]}}))
check("入群核查:命中房间发通知(含粉丝牌/弹幕/申请人/回答)",
      _sent_join["n"] == 1 and _sent_join["oid"] == "OID4242"
      and "汤圆团子 Lv.12" in _sent_join["content"]
      and "主播晚上好" in _sent_join["content"]
      and "新粉" in _sent_join["content"] and "10001" in _sent_join["content"])
# 未填答案 → 跳过不发送
_sent_join["n"] = 0
_ai_runs(_hjr(_bot1, {"group_openid": "OID4242", "username": "空答",
                      "verify_info": {}}))
check("入群核查:未填答案跳过(不发)", _sent_join["n"] == 0)
# 来源群不在任何开启房间的群列表 → 静默
_ai_runs(_hjr(_bot1, {"group_openid": "OID-NOBIND", "username": "异群",
                      "verify_info": {"method": "verify_message",
                                      "verify_message": "10001"}}))
check("入群核查:来源群未绑定映射/无匹配房间 → 静默(不发)",
      _sent_join["n"] == 0)
_app_mod._bot_send_active_text = _orig_active
client.patch("/api/rooms/111/qq-notify", json={"enabled": False, "join_check": False})
client.patch("/api/rooms/222/qq-notify", json={"enabled": False, "join_check": False})
_app_mod._OFFICIAL_API = _orig_official_api
_off_srv.shutdown()

print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
