# -*- coding: utf-8 -*-
"""B 站直播间状态查询(公开接口,无需登录/签名)。

接口:GET https://api.live.bilibili.com/room/v1/Room/getInfo?room_id=<id>
取 data.live_status(0=未播 1=直播中 2=轮播)/ title / uname。
口径:**轮播(2)按未播处理**——通常是下播后循环放录播,不属于开播事件。
"""
import httpx

DEFAULT_API = "https://api.live.bilibili.com"
# 带常规浏览器 UA(裸 python UA 偶发被边缘节点拒绝)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36"}


def normalize(live_status: int) -> int:
    """B 站 live_status → 本服务状态口径:轮播(2)归为未播(0)。"""
    return 1 if live_status == 1 else 0


def parse_info(data: dict) -> dict:
    """解析 get_info 的 data 段(纯函数,便于单测)。

    注意(2026-10-14 事故):方法名必须是小写 **get_info**——驼峰 getInfo
    已被 B 站网关下线(返回 code 1000003 data 空,HTTP 仍 200),曾被静默
    解析成"未播"导致整晚漏通知。uname 字段该接口在播时为空,调用方应
    回退共享库 rooms.anchor_name。
    live_time 为"YYYY-MM-DD HH:MM:SS"字符串(真实本场开播时间,未播为
    无效值),转 ISO;解析失败返回空串(调用方回退当前时间)。
    """
    live_time = str(data.get("live_time") or "")
    try:
        if live_time and not live_time.startswith("0000"):
            live_time = live_time.replace(" ", "T")
        else:
            live_time = ""
    except ValueError:
        live_time = ""
    return {
        "status": normalize(int(data.get("live_status") or 0)),
        "title": str(data.get("title") or ""),
        "uname": str(data.get("uname") or ""),
        # 封面:开播时优先直播实时帧(keyframe),回退主播设置的房间封面
        # (user_cover);未开播两者通常为空(下播通知不带图属预期)
        "cover": str(data.get("keyframe") or data.get("user_cover")
                     or data.get("cover") or ""),
        "live_time": live_time,
    }


def fetch_room_status(client: httpx.Client, room_id: int,
                      api_base: str = DEFAULT_API) -> dict:
    """查单房间状态。成功返回 parse_info 的结构;任何失败抛异常
    (调用方按"沿用上轮状态"处理,不翻转)。
    api_base 可被仿直播页(响应同构)覆盖用于测试,默认官方接口。"""
    r = client.get(api_base.rstrip("/") + "/room/v1/Room/get_info",
                   params={"room_id": room_id})
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        # 接口级错误(如 -352 风控/参数错)按查询失败处理,不静默当未播
        raise RuntimeError(f"get_info code={body.get('code')} "
                           f"{str(body.get('message'))[:60]}")
    return parse_info(body.get("data") or {})
