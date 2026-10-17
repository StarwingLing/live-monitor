# -*- coding: utf-8 -*-
"""blivedm 事件处理器：把各类消息转成统一的内部事件，投递到内存队列。

回调函数里只做"解析 + 入队"，绝不直接写数据库——所有入库都由
writer 协程批量完成（见 writer.py），保证高频弹幕不会打满 SQLite。

内部事件格式：(event_type: str, payload: dict)
event_type 取值：danmaku / gift / sc / guard / interact / popularity
payload 各字段见对应回调的注释。
"""
import asyncio
import json
import re
import time

import blivedm
import blivedm.models.web as web_models

from ..db import GUARD_LEVEL_NAMES
from .caches import RuntimeCaches


def _patch_popularity_change() -> None:
    """向 blivedm 注册 POPULARITY_CHANGE 消息（B站约 2024 年新增的协议，
    心跳包人气值改为由该消息推送，blivedm 1.1.7 尚未解析，会打 unknown cmd
    日志且 HeartbeatMessage.popularity 恒为 0）。

    做法：向 BaseHandler._CMD_CALLBACK_DICT 追加一个"未绑定方法"形态的回调
    （blivedm 以 callback(handler_instance, client, command) 三参形式调用，
    存入类字典的普通函数会被当作未绑定方法使用），把 data.popularity
    转发给 handler 实例的 _on_popularity_change。
    若未来 blivedm 上游原生支持，本补丁可移除。
    """
    def callback(handler: "EventRouter", client: blivedm.BLiveClient, command: dict) -> None:
        popularity = (command.get("data") or {}).get("popularity")
        if isinstance(popularity, int):
            handler._on_popularity_change(client, popularity)

    blivedm.BaseHandler._CMD_CALLBACK_DICT["POPULARITY_CHANGE"] = callback


def _patch_online_rank_count() -> None:
    """接住被 blivedm 忽略的 ONLINE_RANK_COUNT 消息：data.count 即直播间页面
    右上角显示的"房间观众"数——外部可获取的最真实同接口径（实测与页面一致）。

    blivedm 把该命令放在忽略列表（handlers._IGNORED_cmds）里静默丢弃，
    但忽略列表的判断发生在 _CMD_CALLBACK_DICT 查询之前/之后因版本而异：
    注册同名回调后 blivedm 会优先走字典分发（忽略列表只在字典未命中时兜底），
    因此可以安全接管。若未来 blivedm 原生支持，本补丁可移除。
    """
    def callback(handler: "EventRouter", client: blivedm.BLiveClient, command: dict) -> None:
        count = (command.get("data") or {}).get("count")
        if isinstance(count, int):
            handler._on_online_rank_count(client, count)

    blivedm.BaseHandler._CMD_CALLBACK_DICT["ONLINE_RANK_COUNT"] = callback


def _patch_online_rank_v2() -> None:
    """接住高能榜 ONLINE_RANK_V2 / V3，提取真实 uid + 昵称。

    两种编码并存（2026-09 实测）：
    - V2：JSON，data.list 每项含 uid/uname；
    - V3：protobuf，data.pb（base64）内嵌榜单条目，字段无公开 schema，
      用 _extract_rank_users 通用解析（条目内含 uid varint + 昵称字符串）。

    用途：弹幕隐私用户只给打码昵称（超***）+ uid_crc32 哈希，而 crc32 算法
    已验证 == zlib.crc32(str(uid))。把榜单的 uid→完整昵称写入 uname_map，
    writer 即可把同一个人的打码弹幕还原成全名（见 writer 的 _crc_map）。
    """
    def callback(handler: "EventRouter", client: blivedm.BLiveClient, command: dict) -> None:
        data = command.get("data") or {}
        users: list[dict] = []
        if data.get("pb"):
            users = _extract_rank_users(_b64decode(data["pb"]))
        else:
            items = data.get("list") or data.get("online_list") or []
            for it in items:
                if isinstance(it, dict):
                    uid, uname = it.get("uid"), it.get("uname")
                    if (isinstance(uid, int) and uid > 0
                            and isinstance(uname, str) and "***" not in uname):
                        users.append({"uid": uid, "uname": uname})
        users = [u for u in users if "***" not in u["uname"]]
        if users:
            handler.queue.put_nowait(("rank_users", {
                "room_id": handler.room_id, "users": users}))

    blivedm.BaseHandler._CMD_CALLBACK_DICT["ONLINE_RANK_V2"] = callback
    blivedm.BaseHandler._CMD_CALLBACK_DICT["ONLINE_RANK_V3"] = callback


def _b64decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """读一个 varint，返回 (值, 下一位置)。"""
    result, shift = 0, 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _walk_fields(buf: bytes):
    """产出 protobuf 顶层字段：(field_no, wire_type, 值_bytes_or_int)。"""
    i = 0
    n = len(buf)
    while i < n:
        try:
            key, i = _read_varint(buf, i)
            fno, wt = key >> 3, key & 7
            if wt == 0:  # varint
                val, i = _read_varint(buf, i)
                yield fno, wt, val
            elif wt == 2:  # length-delimited
                ln, i = _read_varint(buf, i)
                yield fno, wt, buf[i:i + ln]
                i += ln
            elif wt == 1:  # 64-bit
                yield fno, wt, buf[i:i + 8]
                i += 8
            elif wt == 5:  # 32-bit
                yield fno, wt, buf[i:i + 4]
                i += 4
            else:  # group 等罕见类型：无法定位边界，放弃剩余
                return
        except (IndexError, ValueError):
            return


def _looks_like_uname(s: str) -> bool:
    """昵称启发式：非空、非纯数字、不含 URL/时间戳、长度合理。"""
    if not s or len(s.encode("utf-8")) > 60 or s.isdigit():
        return False
    if "http" in s or "bilibili" in s or ":" in s:
        return False
    return True


def _extract_rank_users(pb: bytes) -> list[dict]:
    """从 ONLINE_RANK_V3 的 protobuf 里提取 (uid, uname) 对。

    无官方 schema，按实测结构解析：顶层是若干"条目"子消息（高能榜/大航海
    榜等），条目内含 uid varint（B站 uid 均 ≥ 6 位，取首个 ≥10 万的）和
    昵称字符串（取出现在 uid 之后、首个"像昵称"的 UTF-8 串；face 是 URL、
    段位是纯数字、榜史含时间戳，均被启发式排除）。
    只解析一层条目、不递归——避免把 URL 等普通字符串误当子消息解析出
    垃圾映射；条目解析失败只影响该条目。
    """
    out: list[dict] = []
    seen: set[int] = set()

    for fno, wt, val in _walk_fields(pb):
        if wt != 2 or not isinstance(val, bytes):
            continue  # 只关心 length-delimited 的条目子消息
        uid = None
        uname = None
        for _, wt2, val2 in _walk_fields(val):
            if wt2 == 0 and uid is None and isinstance(val2, int) and val2 >= 100_000:
                uid = val2
            elif wt2 == 2 and uid is not None and uname is None:
                try:
                    text = val2.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if _looks_like_uname(text):
                    uname = text
        if uid and uname and uid not in seen:
            seen.add(uid)
            out.append({"uid": uid, "uname": uname})
    return out


_patch_popularity_change()
_patch_online_rank_count()
_patch_online_rank_v2()


class EventRouter(blivedm.BaseHandler):
    """单个房间的 blivedm 回调路由器。

    每个受监控房间一个实例，持有：所属房间的 room_id、当前场次
    session_id（由 RoomClient 维护，未开播时为 None）、事件队列。
    """

    def __init__(self, room_id: int, queue: asyncio.Queue,
                 popularity_sink=None, viewer_sink=None,
                 caches: RuntimeCaches | None = None):
        """
        popularity_sink: 可选回调 f(popularity: int)，人气值直接推给 RoomClient
        更新内存值（不进事件队列——它不需要落明细表，由采样协程按间隔写快照，
        避免高频更新打满队列）。
        viewer_sink: 可选回调 f(count: int)，房间观众数（真实同接）同上处理。
        caches: 运行时只读缓存（v0.2）：盲盒礼物价值映射 + 表情 URL 表，
        未提供时（单元测试）盲盒价值兜底与基础表情解析自动退化为不可用。
        """
        super().__init__()
        self.room_id = room_id
        self.queue = queue
        self.popularity_sink = popularity_sink
        self.viewer_sink = viewer_sink
        self.caches = caches
        # RoomClient 在开播/下播时会更新此值；handler 侧只读
        self.session_id: int | None = None
        # 近期 SC 去重窗（v2.2.5）：B 站偶发把同一条 SC 的广播帧在同一秒
        # 连推两帧（2026-10-16 18:26:09 实证，全库 7.9 万条礼物中唯一一例，
        # 见版本笔记 v2.2.5），采集端原先无去重会重复入库虚增营收。按
        # (uid, price, start_time) 60 秒窗口去重——同键再到的帧视为重复
        # 丢弃。SC 低频（每场个位数）且同用户同价同秒发两条真实 SC 不可能，
        # 内存字典滚动清理即可
        self._recent_sc: dict[tuple[int, int, int], float] = {}

    # ------------------------------------------------------------------
    # 弹幕
    # ------------------------------------------------------------------
    def _on_danmaku(self, client: blivedm.BLiveClient, message: web_models.DanmakuMessage):
        """弹幕消息。timestamp 为 B站服务器时间戳（毫秒）。

        B站隐私策略：部分用户（is_mystery）的弹幕用户名被打码（超***）且 uid=0，
        但 uid_crc32（用户十六进制哈希串）稳定可用——uid 为 0 时把它转成负整数
        伪 uid（避免与真实 uid 撞号），保证用户分层不会把所有隐私用户并成一个人。
        未开启隐私的用户（多数）仍返回真实 uid 与完整昵称。

        v0.2 表情口径：text 保留原始文本（含基础表情占位符，如"哈哈[大笑]"）；
        emots 存 JSON 映射 {占位符: {url,width,height}}，来源三处——
        ① 混合弹幕内嵌表情包（info[0][15].extra.emots，key 即文本占位符）；
        ② 纯表情弹幕（dm_type=1，emoticon_options 直接给 url）；
        ③ 基础表情占位符 [xxx]：查表情缓存表（GetEmoticons 定期拉取）补 URL，
          查不到则不进 emots（前端按原样显示文字 [xxx]）。
        """
        uid = message.uid
        if not uid and message.uid_crc32:
            try:
                uid = -int(message.uid_crc32, 16)
            except (ValueError, TypeError):
                uid = 0
        self.queue.put_nowait(("danmaku", {
            "room_id": self.room_id,
            "session_id": self.session_id,
            "uid": uid,
            "uname": message.uname,
            "medal_name": message.medal_name or "",
            "medal_level": message.medal_level or 0,
            "medal_color": message.mcolor or 0,          # 官方粉丝牌颜色（24-bit RGB）
            "privilege_type": message.privilege_type or 0,  # 舰队等级 0/1总督/2提督/3舰长
            "text": message.msg,
            "emots": _extract_emots(self.room_id, message, self.caches),
            "ts": _ts(message.timestamp),
        }))

    # ------------------------------------------------------------------
    # 礼物（含免费礼物）
    # ------------------------------------------------------------------
    def _on_gift(self, client: blivedm.BLiveClient, message: web_models.GiftMessage):
        """礼物消息。

        折算口径（TECH-SPEC 7.2 + v0.2 修订）：
        - coin_type == 'gold'（金瓜子）：1000 金瓜子 = 1 元；
          total_coin 已是实付总价（单价×数量），普通礼物直接 total_coin/1000；
        - coin_type == 'silver'（银瓜子）：免费礼物，记 0 元；
        - 盲盒礼物（blind_gift 存在，v0.2）：礼物名 gift_name 与单价 price
          均为**盲盒开出的礼物**，而 total_coin 是盲盒的实付价。主播收益
          口径按"开出的礼物"计（v0.1 历史 bug），取值顺序：
            ① price × num / 1000（消息自带的开出礼物单价，通常可靠）；
            ② price 缺失/为 0 → 查盲盒价值映射表（Web 管理页编辑），
               key 依次试 [开出的礼物名] 和 [盲盒名]；
            ③ 仍查不到 → 回退盲盒实付价 total_coin/1000（与 0.1 行为一致，
               至少不低估口径）。
          落库：name=开出的礼物名，blind_box=盲盒名（空=非盲盒）。
        """
        blind_box = message.blind_gift_name or ""
        if message.coin_type == "gold":
            if blind_box:
                # 盲盒：收益按开出的礼物计
                if message.price > 0:
                    value = message.price * message.num / 1000
                else:
                    fallback = None
                    if self.caches is not None:
                        fallback = (self.caches.blind_price(message.gift_name)
                                    if message.gift_name else None)
                        if fallback is None:
                            fallback = self.caches.blind_price(blind_box)
                    # 映射表记录的是单价（元/个），×数量；查不到回退盲盒实付价
                    value = (fallback * message.num if fallback is not None
                             else message.total_coin / 1000)
            else:
                value = message.total_coin / 1000
        else:
            value = 0.0
        self.queue.put_nowait(("gift", {
            "room_id": self.room_id,
            "session_id": self.session_id,
            "uid": message.uid,
            "uname": message.uname,
            "type": "gift",
            # 盲盒记录开出的礼物名（收益口径），盲盒名单独存 blind_box 字段
            "name": message.gift_name,
            "blind_box": blind_box,
            "count": message.num,
            "value_yuan": round(value, 2),
            "gift_img": message.gift_img_basic or "",   # 礼物缩略图（礼物列表展示用）
            "medal_name": message.medal_name or "",
            "medal_level": message.medal_level or 0,
            "privilege_type": message.guard_level or 0,  # 礼物消息的舰队等级
            "ts": _ts(message.timestamp),
        }))

    # ------------------------------------------------------------------
    # 醒目留言（SC）
    # ------------------------------------------------------------------
    def _on_super_chat(self, client: blivedm.BLiveClient, message: web_models.SuperChatMessage):
        """SC 的 price 字段本身就是人民币元（已通过 context7 核对）。

        v2.2.5 去重：同一 (uid, price, start_time) 60 秒内重复的帧直接丢弃
        （B 站偶发双推，见 __init__ 的 _recent_sc 注释），打一行日志便于
        运维在 journalctl 里确认去重命中。
        """
        key = (message.uid, message.price, message.start_time)
        now = time.monotonic()
        last = self._recent_sc.get(key)
        if last is not None and now - last < 60.0:
            print(f"[capture] 房间{self.room_id} SC 重复帧丢弃：uid={message.uid} "
                  f"price={message.price} start_time={message.start_time}")
            return
        self._recent_sc[key] = now
        if len(self._recent_sc) > 64:  # 超 64 条时清掉窗口外的旧键
            self._recent_sc = {k: t for k, t in self._recent_sc.items()
                               if now - t < 60.0}
        self.queue.put_nowait(("gift", {
            "room_id": self.room_id,
            "session_id": self.session_id,
            "uid": message.uid,
            "uname": message.uname,
            "type": "sc",
            "name": f"SC ¥{message.price}",
            "text": message.message,
            "count": 1,
            "value_yuan": float(message.price),
            "ts": _ts(message.start_time),
        }))

    # ------------------------------------------------------------------
    # 上舰 / 续费（以 USER_TOAST_V2 为准，数据比旧版 GUARD_BUY 完整）
    # ------------------------------------------------------------------
    def _on_user_toast_v2(self, client: blivedm.BLiveClient, message: web_models.UserToastV2Message):
        """上舰消息。

        price 字段为**金瓜子**（×1000 金瓜子 = 1 元；实测舰长首月价
        138000 瓜子 = ¥138），须除以 1000 折算人民币。source==2 为赠送，
        不计收入。注意：舰长"自动续费"不触发本消息，无法实时捕获
        （TECH-SPEC 7.2 已注明口径）。
        """
        if message.source == 2:  # 赠送的舰长：受赠方不产生消费，不重复计收入
            return
        level = GUARD_LEVEL_NAMES.get(message.guard_level, f"等级{message.guard_level}")
        self.queue.put_nowait(("gift", {
            "room_id": self.room_id,
            "session_id": self.session_id,
            "uid": message.uid,
            "uname": message.username,
            "type": "guard",
            "name": f"{level}x{message.num}月",
            "count": message.num,
            "value_yuan": round(float(message.price or 0) / 1000, 2),
            # 舰级随事件即时刷新 user_stats（购买/升舰即生效，v2.0.0）
            "privilege_type": message.guard_level or 0,
            "ts": _ts(message.start_time),
        }))

    # ------------------------------------------------------------------
    # 互动（进入/关注/分享/特别关注/互粉/点赞）
    # ------------------------------------------------------------------
    def _on_interact_word_v2(self, client: blivedm.BLiveClient, message: web_models.InteractWordV2Message):
        """msg_type：1进入 2关注 3分享 4特别关注 5互粉 6点赞。

        进入与点赞量极大且分析价值低，只记录 INTERACT_TYPES 中的类型
        （关注/分享/特别关注/互粉），见 db.INTERACT_TYPES。
        """
        from ..db import INTERACT_TYPES
        itype = INTERACT_TYPES.get(message.msg_type)
        if itype is None:
            return
        self.queue.put_nowait(("interact", {
            "room_id": self.room_id,
            "session_id": self.session_id,
            "uid": message.uid,
            "uname": message.username,
            "type": itype,
            "ts": _ts(message.timestamp),
        }))

    # ------------------------------------------------------------------
    # 心跳（人气值）
    # ------------------------------------------------------------------
    def _on_heartbeat(self, client: blivedm.BLiveClient, message: web_models.HeartbeatMessage):
        """心跳人气值只更新 RoomClient 的内存值，由采样协程按间隔落库，
        避免每 30 秒心跳都写一条快照。"""
        self._emit_popularity(message.popularity)

    def _on_popularity_change(self, client: blivedm.BLiveClient, popularity: int):
        """POPULARITY_CHANGE 消息（见模块顶部补丁）：与心跳同渠道处理。"""
        self._emit_popularity(popularity)

    def _emit_popularity(self, popularity: int) -> None:
        if popularity > 0:  # 老协议心跳恒为 0 时忽略，避免覆盖真实值
            if self.popularity_sink is not None:
                self.popularity_sink(popularity)

    def _on_online_rank_count(self, client: blivedm.BLiveClient, count: int):
        """ONLINE_RANK_COUNT（见模块顶部补丁）：房间观众数=真实同接，
        与直播间页面右上角显示一致。"""
        if count > 0 and self.viewer_sink is not None:
            self.viewer_sink(count)


def _ts(unix_ms_or_s: int | float | None) -> str:
    """B站消息时间戳 → ISO8601 本地时间；缺失时用当前时间。

    B站不同消息的时间戳精度不一致：DANMU_MSG 是毫秒（13 位），
    礼物/SC/舰长是秒（10 位），按数量级自适应。
    """
    import time
    if not unix_ms_or_s:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
    v = float(unix_ms_or_s)
    if v > 1e12:  # 毫秒
        v /= 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(v))


# 文本中的表情占位符：[xxx]，xxx 为 1~32 个非方括号非空白字符
_EMOT_TOKEN_RE = re.compile(r"\[([^\[\]\s]{1,32})\]")


def _extract_emots(room_id: int, message: web_models.DanmakuMessage,
                   caches: RuntimeCaches | None) -> str:
    """从弹幕消息中提取表情包映射，返回 JSON 字符串（无表情返回 ''）。

    输出形如 {"[涟洢Lianyi_打call]": {"url": "...", "width": 60, "height": 60}}，
    key 即文本中的占位符原文（含方括号），前端按 key 把文本替换成 <img>。
    纯表情弹幕（dm_type=1）文本通常为空，此时 key 取消息里的占位文本，
    前端对文本为空的弹幕直接平铺显示 emots 里的图片。
    """
    emots: dict[str, dict] = {}

    def add(key: str, url, width=0, height=0) -> None:
        if key and url and key not in emots:
            try:
                emots[key] = {"url": str(url), "width": int(width or 0),
                              "height": int(height or 0)}
            except (TypeError, ValueError):
                pass

    # ① 混合弹幕的内嵌表情包：extra.emots 的 key 就是文本占位符原文
    try:
        inline = (message.extra_dict or {}).get("emots") or {}
    except Exception:
        inline = {}
    if isinstance(inline, dict):
        for key, v in inline.items():
            if isinstance(v, dict) and v.get("url"):
                add(key, v.get("url"), v.get("width"), v.get("height"))

    # ② 纯表情弹幕（dm_type=1）：emoticon_options 直接携带图片信息
    if message.dm_type == 1:
        try:
            eo = message.emoticon_options_dict or {}
        except Exception:
            eo = {}
        if eo.get("url"):
            # key 优先用弹幕文本（部分消息会给 "[名字]" 占位），否则造一个
            key = message.msg if (message.msg or "").strip() else \
                f"[{eo.get('emoticon_unique') or '表情'}]"
            add(key, eo.get("url"), eo.get("width"), eo.get("height"))

    # ③ 基础表情占位符（如 [大笑]）：消息不带 URL，查表情缓存表补图
    if caches is not None:
        for m in _EMOT_TOKEN_RE.finditer(message.msg or ""):
            key = m.group(0)           # 含方括号的原文
            if key in emots:
                continue               # ①② 已带 URL 的不再重复查
            e = caches.emote(room_id, m.group(1))
            if e is not None:
                add(key, e["url"], e["width"], e["height"])

    return json.dumps(emots, ensure_ascii=False) if emots else ""
