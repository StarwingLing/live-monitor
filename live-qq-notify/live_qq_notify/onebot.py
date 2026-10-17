# -*- coding: utf-8 -*-
"""NapCat(OneBot 11)HTTP 客户端。

本进程只用到两个标准 API:
- POST /send_group_msg   {"group_id", "message"} → 发群消息
- GET  /get_login_info   → 探活 + 实际登录账号(与预留 QQ 号校验)

鉴权:Authorization: Bearer <token>(token 由 NapCat WebUI 网络配置生成,
存共享库 qq_notify_global,管理页可改)。
"""
import httpx


class OneBotError(RuntimeError):
    """OneBot 调用失败(网络层或业务码非 0)。"""


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_login_info(client: httpx.Client, base_url: str, token: str) -> dict:
    """探活:返回 {user_id, nickname};失败(连接拒绝/超时/业务错)抛 OneBotError。"""
    try:
        r = client.get(base_url.rstrip("/") + "/get_login_info",
                       headers=_headers(token))
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPError as e:
        raise OneBotError(f"NapCat 不可达({e.__class__.__name__})") from e
    if body.get("status") != "ok" or not isinstance(body.get("data"), dict):
        raise OneBotError(f"NapCat 应答异常: {str(body)[:120]}")
    d = body["data"]
    return {"user_id": int(d.get("user_id") or 0),
            "nickname": str(d.get("nickname") or "")}


def send_group_msg(client: httpx.Client, base_url: str, token: str,
                   group_id: int, message: str) -> None:
    """发一条群文本消息;失败抛 OneBotError(由调用方重试/记日志)。"""
    try:
        r = client.post(base_url.rstrip("/") + "/send_group_msg",
                        headers=_headers(token),
                        json={"group_id": group_id, "message": message})
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPError as e:
        raise OneBotError(f"发送请求失败({e.__class__.__name__})") from e
    if body.get("status") != "ok":
        raise OneBotError(f"发送被拒: {str(body)[:120]}")
