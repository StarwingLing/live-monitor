# -*- coding: utf-8 -*-
"""本地联调用 NapCat 模拟端(OneBot 11 HTTP + WebUI 扫码登录)。

两个监听:
- 127.0.0.1:3000  OneBot 11 HTTP(get_login_info / send_group_msg,
  收到消息打印并计数,不真发 QQ)
- 127.0.0.1:6099  NapCat WebUI API 最小子集(auth/login + QQLogin 扫码
  状态机),供 bili-monitor「开播账号管理」扫码登录联调

WebUI 模拟行为(与 NapCat 源码核对过的接口形状一致):
- POST /api/auth/login {"hash"} → {"code":0,"data":{"Credential": base64}},
  hash = sha256_hex(WEBUI_TOKEN + ".napcat")(凭证 = base64 JSON,1 小时语义)
- POST /api/QQLogin/GetQQLoginQrcode → data {"qrcode": "https://x.cn/ptlogin?mock"}
- POST /api/QQLogin/CheckLoginStatus → 模拟扫码自动推进:第 1~2 次 qr →
  第 3 次 qrLoginAccepted → 第 4 次起 isLogin(把请求轮数当"有人扫码"演示用)
- 鉴权:除 auth/login 外校验 Authorization: Bearer <Credential>

页面联调:管理页「开播账号管理」WebUI 地址填 http://127.0.0.1:6099、
WebUI Token 填下方 WEBUI_TOKEN,点「开始扫码登录」即可看二维码与状态流转。

运行:uv run python tests/mock_onebot.py
"""
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_OB = ("127.0.0.1", 3000)      # OneBot 11
LISTEN_WUI = ("127.0.0.1", 6099)     # NapCat WebUI
LOGIN_QQ = 10000                     # get_login_info 返回的模拟登录号
WEBUI_TOKEN = "mock-webui-token"     # 页面「WebUI Token」填这个
SENT: list[tuple[int, str]] = []

MOCK_QRCODE = "https://ssl.ptlogin2.qq.com/ptqrshow?appid=716027609&mock=1"
_cred = {"value": ""}                # 已签发凭证(单值,后签发覆盖先签发)
_checks = {"n": 0}                   # CheckLoginStatus 调用轮数(扫码推进)


def _sha_login_hash(token: str) -> str:
    """NapCat WebUI 登录 hash:sha256_hex(token + ".napcat")(源码核对)。"""
    return hashlib.sha256((token + ".napcat").encode()).hexdigest()


def _issue_credential() -> str:
    """签发模拟凭证:base64(JSON),与 NapCat signCredential 的形状一致。"""
    inner = {"CreatedTime": int(time.time()), "HashEncoded": _sha_login_hash(WEBUI_TOKEN)}
    return base64.b64encode(json.dumps(inner).encode()).decode()


def _send_json(handler, obj: dict, code: int = 200) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _ok(data: dict) -> dict:
    """NapCat WebUI 成功响应包装(源码核对:{code,message,data})。"""
    return {"code": 0, "message": "success", "data": data}


def _read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    try:
        return json.loads(handler.rfile.read(length) or b"{}")
    except ValueError:
        return {}


def _authed(handler) -> bool:
    auth = handler.headers.get("Authorization", "")
    return auth == "Bearer " + _cred["value"]


class OneBotHandler(BaseHTTPRequestHandler):
    """OneBot 11 HTTP 模拟(3000)。"""

    def do_GET(self):
        if self.path.startswith("/get_login_info"):
            _send_json(self, {"status": "ok", "retcode": 0,
                              "data": {"user_id": LOGIN_QQ, "nickname": "mock小号"}})
        else:
            _send_json(self, {"status": "failed", "retcode": 404,
                              "wording": "no such api"}, 404)

    def do_POST(self):
        if self.path.startswith("/send_group_msg"):
            payload = _read_body(self)
            gid = payload.get("group_id")
            msg = payload.get("message", "")
            SENT.append((gid, msg))
            print(f"[MOCK 发送] 群 {gid}: {msg!r}(累计 {len(SENT)} 条)", flush=True)
            _send_json(self, {"status": "ok", "retcode": 0,
                              "data": {"message_id": len(SENT)}})
        else:
            _send_json(self, {"status": "failed", "retcode": 404,
                              "wording": "no such api"}, 404)

    def log_message(self, *args):
        pass


class WebUiHandler(BaseHTTPRequestHandler):
    """NapCat WebUI API 模拟(6099):登录 + 扫码状态机。"""

    def do_GET(self):
        _send_json(self, {"code": -1, "message": "POST only"})

    def do_POST(self):
        p = self.path
        if p.startswith("/api/auth/login"):
            body = _read_body(self)
            if body.get("hash") == _sha_login_hash(WEBUI_TOKEN):
                _cred["value"] = _issue_credential()
                print(f"[MOCK WebUI] 登录成功,签发凭证", flush=True)
                # NapCat 实际形状(4.18.38 实测):sendSuccess 把 Credential 包在 data 里
                _send_json(self, {"code": 0, "message": "success",
                                  "data": {"Credential": _cred["value"]}})
            else:
                _send_json(self, {"code": -1, "message": "token is invalid"})
            return
        if not _authed(self):
            _send_json(self, {"code": -1, "message": "Unauthorized"})
            return
        if p.startswith("/api/QQLogin/CheckLoginStatus"):
            _checks["n"] += 1
            n = _checks["n"]
            scanned, online = n >= 3, n >= 4   # 自动"扫码"推进(演示用)
            _send_json(self, _ok({
                "isLogin": online, "isOffline": False,
                "loginPhase": "ready" if online else "waiting",
                "qrLoginAccepted": scanned, "coreReady": True,
                "qrcodeurl": "" if online else MOCK_QRCODE,
                "loginError": "",
            }))
            print(f"[MOCK WebUI] CheckLoginStatus 第 {n} 次 → "
                  f"{'已登录' if online else '已扫码待确认' if scanned else '待扫码'}",
                  flush=True)
            return
        if p.startswith("/api/QQLogin/GetQQLoginQrcode"):
            _send_json(self, _ok({"qrcode": MOCK_QRCODE}))
            return
        if p.startswith("/api/QQLogin/RefreshQRcode"):
            _checks["n"] = 0
            # 带时间戳的码驱动前端重绘(模拟真实 ptqrshow 每次生成新 URL)
            fresh = f"{MOCK_QRCODE}&ts={int(time.time())}"
            _send_json(self, _ok({"qrcodeurl": fresh}))
            return
        _send_json(self, {"code": -1, "message": "no such api"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import threading
    print(f"mock NapCat 启动:")
    print(f"  OneBot 11 → http://{LISTEN_OB[0]}:{LISTEN_OB[1]}(登录号 {LOGIN_QQ})")
    print(f"  WebUI     → http://{LISTEN_WUI[0]}:{LISTEN_WUI[1]}(token: {WEBUI_TOKEN})")
    print("扫码模拟:CheckLoginStatus 第 3 次起视为已扫码、第 4 次起登录成功;Ctrl+C 退出")
    ob = ThreadingHTTPServer(LISTEN_OB, OneBotHandler)
    wui = ThreadingHTTPServer(LISTEN_WUI, WebUiHandler)
    threading.Thread(target=ob.serve_forever, daemon=True).start()
    wui.serve_forever()
