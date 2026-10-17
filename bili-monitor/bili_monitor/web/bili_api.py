# -*- coding: utf-8 -*-
"""B站 HTTP API 小工具（v2.2 自 capture/manager.py 抽出，供 web 侧加房校验）。

capture 子包迁往 replay 项目后，本站仅剩的 B站 API 消费点是管理页添加房间
（校验房间存在 + 取主播昵称/头像）。改用**匿名会话**：浏览器 UA 防 412
风控即可——加房是低频管理员操作，匿名接口（get_info / getInfoByRoom 均
匿名可用）的可用性足够；登录 cookie 归 replay capture 侧（防风控的收益
集中在采集端高频轮询）。
"""
import http.cookies

import aiohttp

# B站直播间信息接口（匿名可用）：live_status 1=直播中 0=未开播
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
# 房间聚合信息接口：一次返回房间+主播信息（Master/get_info 接口已要求登录，
# 匿名返回 code=1000003，故主播名从这里取）
ROOM_INDEX_API = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
# 浏览器 UA：B站 HTTP 接口对默认 aiohttp UA 返回 412 风控页（已实测），
# 所有调 B站 HTTP API 的会话必须带
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
}


def parse_cookie(cookie_str: str) -> http.cookies.SimpleCookie:
    """把浏览器复制的原始 Cookie 串（k1=v1; k2=v2; ...）解析为 SimpleCookie。"""
    jar = http.cookies.SimpleCookie()
    jar.load(cookie_str)
    return jar


def build_bili_session() -> aiohttp.ClientSession:
    """构造访问 B站 HTTP API 的匿名会话（浏览器 UA 防风控）。

    unsafe CookieJar：B站接口跨多个子域（api/live/ex.bilibili.com），放宽域
    匹配；不带登录 cookie（见模块 docstring 的取舍说明）。"""
    jar = aiohttp.CookieJar(unsafe=True)
    return aiohttp.ClientSession(cookie_jar=jar, headers=HTTP_HEADERS)


async def fetch_room_info(session: aiohttp.ClientSession, room_id: int) -> dict | None:
    """查询房间信息；返回 {live_status, title, uid, ...}，失败返回 None。"""
    try:
        async with session.get(ROOM_INFO_API, params={"room_id": room_id},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") == 0:
            return data["data"]
    except Exception:
        pass
    return None


async def fetch_anchor_info(session: aiohttp.ClientSession, room_id: int) -> dict:
    """按房间号查主播信息（昵称+头像），走 getInfoByRoom（匿名可用）。
    失败返回空 dict。"""
    try:
        async with session.get(ROOM_INDEX_API, params={"room_id": room_id},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") == 0:
            base = (data["data"].get("anchor_info") or {}).get("base_info") or {}
            return {"uname": base.get("uname", ""), "face": base.get("face", "")}
    except Exception:
        pass
    return {"uname": "", "face": ""}
