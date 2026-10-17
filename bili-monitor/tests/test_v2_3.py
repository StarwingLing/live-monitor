# -*- coding: utf-8 -*-
"""v2.3 开播 QQ 群通知(联动 live-qq-notify)测试套。

覆盖:锁步两表建表与默认行 / 全局配置 GET+PATCH(校验与合并) /
NapCat 探测端点(不可达不抛错) / 房间级配置 GET+PATCH+UPSERT /
summary 透出 notify_* 字段。库隔离 + 直插测试房间(不触 B 站网络)。

运行:uv run python tests/test_v2_3.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 库隔离:在任何 bili_monitor 模块 import 前把 DB_PATH 指向临时库 ----
from bili_monitor import db as _dbm
_DB_TMP = Path(tempfile.mkdtemp(prefix="bili_v2_3_test_"))
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


# ---- 0. 锁步 DDL:两表存在 + 全局默认单行 ----
conn = _dbm.connect()
tbls = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("锁步两表已建", {"qq_notify", "qq_notify_global"} <= tbls)
check("全局默认单行(双通知开)",
      tuple(conn.execute("SELECT notify_on, notify_off, debounce_offline "
                         "FROM qq_notify_global WHERE id=1").fetchone())
      == (1, 1, 300))
# 直插两个测试房间(绕过 add_room 的 B 站网络校验;created_at 非空)
conn.executemany("INSERT OR REPLACE INTO rooms(room_id, anchor_name, enabled, created_at) "
                 "VALUES (?, ?, 1, '2026-10-13T00:00:00')",
                 [(111, "测试甲"), (222, "测试乙")])
conn.commit()
conn.close()

# ---- 1. 全局配置 GET ----
r = client.get("/api/qq-notify/global")
check("GET global 200", r.status_code == 200 and r.json()["notify_on"] is True
      and r.json()["onebot_url"].startswith("http://127.0.0.1"))

# ---- 2. 全局 PATCH:非法地址 400 / 合法合并 ----
r = client.patch("/api/qq-notify/global", json={"onebot_url": "ftp://x"})
check("PATCH global 非法 url → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/global",
                 json={"qq_account": "12345", "notify_off": False,
                       "template_on": "🎉 {uname}: {title}\n{cover}"})
check("PATCH global 合并更新", r.status_code == 200 and
      r.json()["qq_account"] == "12345" and r.json()["notify_off"] is False
      and r.json()["notify_on"] is True and
      r.json()["template_on"] == "🎉 {uname}: {title}\n{cover}")
r = client.patch("/api/qq-notify/global", json={"debounce_offline": 99999})
check("PATCH global 防抖越界 → 400", r.status_code == 400)

# ---- 3. NapCat 探测:不可达返回 online=False(不抛 500) ----
# (先把地址指到必关端口,避免撞上本机正在跑的联调 mock:3000)
client.patch("/api/qq-notify/global",
             json={"onebot_url": "http://127.0.0.1:39998"})
r = client.get("/api/qq-notify/status")
check("status 端点(NapCat 未启动→不可达)", r.status_code == 200
      and r.json()["online"] is False and "不可达" in r.json()["message"])
client.patch("/api/qq-notify/global",
             json={"onebot_url": "http://127.0.0.1:3000"})

# ---- 4. 房间级 GET(未配置行 → 默认) ----
r = client.get("/api/rooms/111/qq-notify")
check("GET 房间通知(无行默认)", r.status_code == 200
      and r.json()["enabled"] is False and r.json()["group_ids"] == []
      and "{uname}" in r.json()["global_template_on"])
r = client.get("/api/rooms/999/qq-notify")
check("GET 不存在房间 → 404", r.status_code == 404)

# ---- 5. 房间级 PATCH:UPSERT + 群去重 + 模板校验 ----
r = client.patch("/api/rooms/111/qq-notify",
                 json={"enabled": True, "group_ids": [8888, 4242, 8888, 0]})
check("PATCH 房间(群去重去非正)", r.status_code == 200
      and r.json()["enabled"] is True and r.json()["group_ids"] == [4242, 8888])
r = client.patch("/api/rooms/111/qq-notify", json={"template_on": "房间的"})
check("PATCH 房间专属模板(未传字段保持)", r.status_code == 200
      and r.json()["template_on"] == "房间的"
      and r.json()["enabled"] is True)
r = client.patch("/api/rooms/111/qq-notify", json={"group_ids": ["x"]})
check("PATCH 非整数群 → 422", r.status_code == 422)
r = client.patch("/api/rooms/222/qq-notify", json={"enabled": True})
check("PATCH 222 开关(无群行)", r.status_code == 200
      and r.json()["enabled"] is True and r.json()["group_ids"] == [])

# ---- 6. summary 透出 ----
r = client.get("/api/summary?days=1")
rooms = {x["room_id"]: x for x in r.json()["rooms"]}
check("summary 通知字段透出",
      rooms[111]["notify_enabled"] is True
      and rooms[111]["notify_group_count"] == 2
      and rooms[111]["notify_custom"] is True
      and rooms[222]["notify_group_count"] == 0)

# ---- 6.5 扫码登录端点(内嵌模拟 NapCat WebUI,源码级接口形状) ----
import base64 as _b64
import hashlib as _hl
import json as _json
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
            _wui_cred["v"] = _b64.b64encode(_json.dumps(
                {"CreatedTime": 1, "HashEncoded": _wui_hash()}).encode()).decode()
            body = {"code": 0, "Credential": _wui_cred["v"]}
        elif self.headers.get("Authorization") != "Bearer " + _wui_cred["v"]:
            body = {"code": -1, "message": "Unauthorized"}
        elif self.path == "/api/QQLogin/CheckLoginStatus":
            _wui_calls["check"] += 1
            n_ = _wui_calls["check"]
            online = n_ >= 3   # 首轮 qr、refresh 轮 qr、第三轮 online
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
        out = _json.dumps(body).encode()
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

# 未配置 webui_token → error 提示
r = client.get("/api/qq-notify/qrlogin")
check("qrlogin 未配置 token → error", r.status_code == 200
      and r.json()["phase"] == "error" and "WebUI" in r.json()["message"])
# 配置指向内嵌模拟 → 首轮出码,次轮 online
r = client.patch("/api/qq-notify/global",
                 json={"webui_url": f"http://127.0.0.1:{_port}",
                       "webui_token": _WEBUI_TOKEN})
check("PATCH webui 配置", r.status_code == 200
      and r.json()["webui_token"] == _WEBUI_TOKEN)
r = client.get("/api/qq-notify/qrlogin")
check("qrlogin 首轮出二维码", r.status_code == 200
      and r.json()["phase"] == "qr" and r.json()["qrcode"] == "https://ptlogin.mock/qr")
r = client.get("/api/qq-notify/qrlogin?refresh=1")
check("qrlogin refresh=1 换新码(保鲜)", r.status_code == 200
      and r.json()["qrcode"] == "https://ptlogin.mock/qr-new")
r = client.get("/api/qq-notify/qrlogin")
check("qrlogin 次轮登录成功", r.status_code == 200
      and r.json()["phase"] == "online")
_srv.shutdown()
# 指向已关停端口 → 不可达 error(不 500)
client.patch("/api/qq-notify/global",
             json={"webui_url": f"http://127.0.0.1:{_port}"})
r = client.get("/api/qq-notify/qrlogin")
check("qrlogin WebUI 关停 → error", r.status_code == 200
      and r.json()["phase"] == "error")

# ---- 7. 生产模式权限:未登录写/读 401,旧通道 token 可写 ----
_app_mod.cfg["capture"]["admin_password"] = "secret-v2-3-test"
c2 = TestClient(app)
check("未登录 PATCH 房间通知 → 401", c2.patch(
    "/api/rooms/111/qq-notify", json={"enabled": False}).status_code == 401)
check("未登录 GET global → 401", c2.get(
    "/api/qq-notify/global").status_code == 401)
check("旧通道 token 可写", c2.patch(
    "/api/rooms/111/qq-notify", json={"enabled": False},
    headers={"X-Admin-Token": "secret-v2-3-test"}).status_code == 200)
_app_mod.cfg["capture"]["admin_password"] = ""  # 还原调试模式

# ---- 8. v2.3.1 官方 QQ 机器人通道(双通道配置) ----
# 8.1 默认 channel=napcat / 官方凭据空 透出
r = client.get("/api/qq-notify/global")
check("global 默认 channel=napcat 且官方凭据空",
      r.json()["channel"] == "napcat"
      and r.json()["official_app_id"] == ""
      and r.json()["official_app_secret"] == "")
# 8.2 channel 非法值 → 400
r = client.patch("/api/qq-notify/global", json={"channel": "sms"})
check("PATCH channel 非法值 → 400", r.status_code == 400)

# 8.3 内嵌模拟官方 API(token 换取;响应可切换在线/凭据错)
_OFF_RESP = {"body": {"access_token": "T0KEN", "expires_in": 7200}}


class _MiniOfficial(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        out = _json.dumps(_OFF_RESP["body"]).encode()
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

# 8.4 official 未配置凭据 → 友好提示
client.patch("/api/qq-notify/global", json={"channel": "official"})
r = client.get("/api/qq-notify/status")
check("status official 未配置凭据", r.status_code == 200
      and r.json()["online"] is False and r.json()["channel"] == "official"
      and "未配置" in r.json()["message"])
# 8.5 official 凭据有效 → 在线(mock token 接口)
client.patch("/api/qq-notify/global",
             json={"official_app_id": "1234567890",
                   "official_app_secret": "sec"})
r = client.get("/api/qq-notify/status")
check("status official 凭据有效在线", r.status_code == 200
      and r.json()["online"] is True and r.json()["account"] == "1234567890"
      and "官方通道就绪" in r.json()["message"])
# 8.6 official 凭据错(100016)→ 凭据无效
_OFF_RESP["body"] = {"code": 100016, "message": "appid or secret error"}
r = client.get("/api/qq-notify/status")
check("status official 凭据无效", r.json()["online"] is False
      and "凭据无效" in r.json()["message"])
_OFF_RESP["body"] = {"access_token": "T0KEN", "expires_in": 7200}

# 8.7 房间级 group_openids 已撤(改为群号→openid 映射表方案);channel 仍透出
r = client.get("/api/rooms/111/qq-notify")
check("GET 房间透出 channel", r.status_code == 200
      and r.json()["channel"] == "official")

# 8.8 summary:两通道统一按 group_ids 计数(111 已配 [4242,8888])
r = client.get("/api/summary?days=1")
rooms = {x["room_id"]: x for x in r.json()["rooms"]}
check("summary official 通道仍按群号计数",
      rooms[111]["notify_group_count"] == 2)

# ---- 9. v2.3.1 群映射获取(绑定会话 + 映射表 CRUD) ----
# 9.1 绑定:napcat 通道 → 400(先切回)
client.patch("/api/qq-notify/global", json={"channel": "napcat"})
r = client.post("/api/qq-notify/bind-group", json={"group_id": 4242})
check("bind 在 napcat 通道 → 400", r.status_code == 400)
# 9.2 official 未配置凭据 → 400;非法群号 → 400
client.patch("/api/qq-notify/global",
             json={"channel": "official", "official_app_id": "",
                   "official_app_secret": ""})
r = client.post("/api/qq-notify/bind-group", json={"group_id": 4242})
check("bind 未配置凭据 → 400", r.status_code == 400)
r = client.post("/api/qq-notify/bind-group", json={"group_id": 0})
check("bind 非法群号 → 400", r.status_code == 400)
# 9.3 bind-status 可读(WebSocket 全链路属部署后人工验收,单测不真连)
r = client.get("/api/qq-notify/bind-status")
check("bind-status 可读", r.status_code == 200
      and r.json()["state"] in ("idle", "timeout", "error", "waiting"))
# 9.4 映射表 CRUD:直插两行 → 列表 → 解绑
conn = _dbm.connect()
conn.executemany("INSERT OR REPLACE INTO qq_group_map "
                 "(group_id, group_openid, updated_at) VALUES (?, ?, ?)",
                 [(4242, "OID4242", "2026-10-17T00:00:00"),
                  (8888, "OID8888", "2026-10-17T00:00:00")])
conn.commit()
conn.close()
r = client.get("/api/qq-notify/group-map")
check("group-map 列表", r.status_code == 200
      and [x["group_id"] for x in r.json()["items"]] == [4242, 8888]
      and r.json()["items"][0]["group_openid"] == "OID4242")
r = client.delete("/api/qq-notify/group-map/8888")
check("group-map 解绑", r.status_code == 200
      and client.get("/api/qq-notify/group-map").json()["items"][0]["group_id"] == 4242)
# 9.5 指令回复纯函数:@前缀剥离/精确匹配/未命中空串(常驻监听属人工验收)
from bili_monitor.web.app import match_command_reply as _mcr, QQ_COMMAND_REPLIES as _QR
check("指令表含 /连接 且返回非空", "/连接" in _QR and _QR["/连接"].strip())
check("指令匹配:@前缀剥离后精确命中",
      _mcr("<@!12345> /连接") == _QR["/连接"]
      and _mcr("/连接") == _QR["/连接"])
check("指令匹配:未命中/近形不误回",
      _mcr("<@!1> /连接下") == "" and _mcr("<@!1> 你好") == ""
      and _mcr("") == "")
# 9.6 指令表自定义(PATCH 存库 → GET 生效口径;空对象回退默认)
r = client.patch("/api/qq-notify/global",
                 json={"command_replies": {"/ping": "pong!", "/帮助": "测试帮助"}})
check("PATCH 自定义指令表", r.status_code == 200
      and r.json()["command_replies"] == {"/ping": "pong!", "/帮助": "测试帮助"})
check("自定义表匹配生效",
      _mcr("<@!9> /ping", r.json()["command_replies"]) == "pong!")
r = client.patch("/api/qq-notify/global", json={"command_replies": {}})
check("清空回退默认(GET 生效口径)", r.status_code == 200
      and r.json()["command_replies"] == dict(_QR))
r = client.patch("/api/qq-notify/global", json={"command_replies": {"": "x"}})
check("空指令 → 400", r.status_code == 400)
r = client.patch("/api/qq-notify/global",
                 json={"command_replies": {"/x": "y" * 501}})
check("回复超长 → 400", r.status_code == 400)
client.patch("/api/qq-notify/global",
             json={"command_replies": {"/ping": "pong!", "/帮助": "测试帮助"}})
_app_mod._OFFICIAL_API = _orig_official_api
_off_srv.shutdown()

print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
