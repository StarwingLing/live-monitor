# -*- coding: utf-8 -*-
"""房间管理器：采集服务的中枢。

职责（对应 TECH-SPEC 6.2 状态机）：
- 为每个 enabled 房间维持一个 7×24 常驻的 blivedm WS 连接（未开播也
  保持，以捕获场外礼物/弹幕），断线指数退避重连；
- 周期轮询房间 API 判定开播状态：开播→新建场次，下播→宽限后关闭场次；
- 周期采样人气值写入 popularity_snapshots；
- 每 30s 热加载 rooms 表（页面增删房间无需重启服务）。
"""
import asyncio
import datetime as dt
import http.cookies
import time

import aiohttp

from ..config import DATA_DIR, get_config
from ..db import Db, now_iso
from .caches import RuntimeCaches
from .handler import EventRouter
from .recording import Recorder, finalize_recording_row, run_manual_clip

# B站直播间信息接口（匿名可用）：live_status 1=直播中 0=未开播
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
# 房间聚合信息接口：一次返回房间+主播信息（Master/get_info 接口已要求登录，
# 匿名会返回 code=1000003，故主播名从这里取）
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


# 登录态检查接口：isLogin=false 说明 cookie 失效（弹幕将退回匿名打码态）
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
# 保活访问目标：访问首页会触发 B站下发 Set-Cookie（刷新 bili_ticket 等
# 短时效凭据），aiohttp 的 cookie jar 自动接收更新
KEEPALIVE_URL = "https://www.bilibili.com/"
# 运行时 cookie 持久化文件：保存保活过程中刷新出的新 cookie 值，
# 重启后与 config 的静态 cookie 合并（文件值优先），跨重启延续登录态
RUNTIME_COOKIES_PATH = DATA_DIR / "cookies.json"


def _load_runtime_cookie_overrides() -> dict[str, str]:
    """读取上次运行持久化的 cookie 覆盖值（保活刷新产物）。"""
    import json
    if RUNTIME_COOKIES_PATH.exists():
        try:
            return json.loads(RUNTIME_COOKIES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def build_bili_session() -> aiohttp.ClientSession:
    """构造访问 B站 HTTP API 的会话：浏览器 UA（防 412）+ 登录 cookie（防风控）。

    cookie 来源合并：config.toml 的静态 cookie 打底，data/cookies.json 里
    保活刷新出的新值覆盖（见 _cookie_keepalive_loop）。capture 的轮询与
    web 的房间校验共用本工厂。
    unsafe CookieJar：B站接口跨多个子域（api/live/ex.bilibili.com），需放宽域匹配。
    """
    cfg = get_config()["capture"]
    jar = aiohttp.CookieJar(unsafe=True)
    cookies: dict[str, str] = {}
    if cfg.get("cookie"):
        cookies = {k: m.value for k, m in parse_cookie(cfg["cookie"]).items()}
    cookies.update(_load_runtime_cookie_overrides())
    return aiohttp.ClientSession(cookie_jar=jar, cookies=cookies,
                                 headers=HTTP_HEADERS)


def persist_runtime_cookies(session: aiohttp.ClientSession) -> int:
    """把会话 jar 中 bilibili.com 域的 cookie 持久化到 data/cookies.json。

    只保存会随保活刷新的短时效键（bili_ticket 系列 + buvid 系列 + SESSDATA），
    返回保存的键数。"""
    import json
    keep = {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid",
            "buvid3", "buvid4", "buvid_fp", "b_nut", "bili_ticket",
            "bili_ticket_expires", "LIVE_BUVID", "b_lsid"}
    out: dict[str, str] = {}
    for cookie in session.cookie_jar:
        if "bilibili" in (cookie.get("domain") or "") and cookie.key in keep:
            out[cookie.key] = cookie.value or ""
    RUNTIME_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_COOKIES_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return len(out)


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


async def fetch_room_metrics(session: aiohttp.ClientSession, room_id: int) -> dict:
    """聚合两个房间接口，返回轮询所需的全部指标。

    - get_info：attention（粉丝数）、live_status
    - getInfoByRoom：watched_show.num（本场累计看过人数·真实计数）、
      guard_info.count（大航海人数·真实计数）
    任一接口失败时对应字段回退为 None，由调用方沿用旧值。
    """
    out: dict = {"attention": None, "online": None, "watched": None, "guards": None}
    base = await fetch_room_info(session, room_id)
    if base is not None:
        out["live_status"] = base.get("live_status")
        out["attention"] = base.get("attention")
        out["online"] = base.get("online")
        out["real_room_id"] = base.get("room_id", room_id)
        # v0.5：透传直播标题（getInfoByRoom 返回值自带，零新增请求），
        # poll_live_status 据此在标题变化时写 session_titles
        out["title"] = base.get("title")
        # v2.5.1：透传直播间封面（get_info 自带，零新增请求）——建场时写入
        # live_sessions.cover_url（详情页历史场次封面板块数据源）；主播设置
        # 封面（user_cover）优先，回退系统封面（cover），均无则空串
        out["cover"] = base.get("user_cover") or base.get("cover") or ""
    try:
        async with session.get(ROOM_INDEX_API, params={"room_id": room_id},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") == 0:
            d = data["data"]
            out["watched"] = (d.get("watched_show") or {}).get("num")
            out["guards"] = (d.get("guard_info") or {}).get("count")
            out.setdefault("live_status", d["room_info"].get("live_status"))
            out.setdefault("real_room_id", d["room_info"].get("room_id", room_id))
    except Exception:
        pass
    return out


async def fetch_anchor_info(session: aiohttp.ClientSession, room_id: int) -> dict:
    """按房间号查主播信息（昵称+头像），走 getInfoByRoom（匿名可用，
    Master/get_info 已要求登录）。失败返回空 dict。"""
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

# B站直播间表情表接口（v0.2）：返回该房间可用的全部表情（基础表情 [dog]/[大笑] 等
# + 房间专属表情包，如 [涟洢Lianyi_打call]），每项含图片 URL。匿名也可访问，
# 带 cookie 更稳。用于弹幕表情的前端图片渲染（写入 emotes 表，handler 查缓存）。
EMOTICON_API = ("https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/"
                "GetEmoticons")


async def fetch_emotes(session: aiohttp.ClientSession, room_id: int) -> list[dict]:
    """拉取某房间的表情表，返回 [{emoji_key(不带方括号), url, width, height}, ...]。

    失败返回空列表（表情属增强功能，失败不影响采集主流程）。
    API 响应结构：data.data = [ {emoticons: [ {emoji, url, width, height, ...} ]}, ... ]
    ——按"表情组"分组（基础组/房间组/其他联动组），拍平即可。
    """
    try:
        async with session.get(EMOTICON_API,
                               params={"platform": "pc", "room_id": room_id, "app_type": 1},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        out: list[dict] = []
        if data.get("code") == 0:
            for group in ((data.get("data") or {}).get("data") or []):
                for e in group.get("emoticons") or []:
                    key = (e.get("emoji") or "").strip().strip("[]")
                    if key and e.get("url"):
                        out.append({"emoji_key": key, "url": e["url"],
                                    "width": int(e.get("width") or 0),
                                    "height": int(e.get("height") or 0)})
        return out
    except Exception:
        return []


def _auto_full_hold(last_rows: list[dict], backoff_sec: float,
                    now: dt.datetime) -> bool:
    """连续起流失败的退避判定（纯函数，可单测）。

    自动全程对账每轮（auto_full_poll_sec）在播即插一条 requested 行；
    若拉流地址持续发放不出来（延迟直播空窗、B 站接口故障等），每轮
    必失败的 requested 行会刷爆归档列表（2026-10-19 某主播 300s 延迟
    直播 70 分钟 138 条 failed 实证）。本判定在「本房最近两条 auto 行
    均为 failed」时按退避期压住新行插入；单条失败仍保持原 30s 快重试，
    瞬时抖动不受影响。backoff_sec<=0 = 关闭退避（回到老行为）。
    """
    if backoff_sec <= 0 or len(last_rows) < 2:
        return False
    if any(r["status"] != "failed" for r in last_rows):
        return False
    try:
        fail_dt = dt.datetime.strptime(last_rows[0]["ended_at"] or "",
                                       "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return (now - fail_dt).total_seconds() < backoff_sec


class RoomClient:
    """单个房间的采集单元：WS 连接 + 直播状态跟踪 + 人气采样。"""

    def __init__(self, manager: "RoomManager", room_id: int):
        self.manager = manager
        self.room_id = room_id
        # 人气值/房间观众数由 handler 通过 sink 直接推送到这里（WS 消息），
        # 采样协程按间隔把内存值落库
        self.popularity: int = 0
        self.viewers: int = 0
        # v0.9：录制器（整场录制，一路流；v2.2 高能时刻移除，缓冲流随之退役）。
        self.recorder = Recorder(self)
        self.handler = EventRouter(room_id, manager.queue,
                                   popularity_sink=self._set_popularity,
                                   viewer_sink=self._set_viewers,
                                   caches=manager.caches)
        r0 = manager.db.query(
            "SELECT anchor_name, avatar_url FROM rooms WHERE room_id = ?",
            (room_id,))
        # 当前场次 id（None=未在直播）；由状态轮询维护
        self.session_id: int | None = None
        self._last_snapshot_ts: float = 0.0
        # 下播宽限进行中标记：防止轮询循环重复触发 _handle_stream_end
        self._ending = False
        # v1.1.1：下播收尾序列任务（录制即时收尾 + 场次宽限复核）——独立
        # 于轮询协程执行，stop() 时统一取消
        self._stream_end_task: asyncio.Task | None = None
        # 上一次轮询拿到的指标快照（接口失败时沿用旧值，避免曲线锯齿）
        self._last: dict = {"attention": None, "watched": None, "guards": None}
        # 上一次记录入库的直播标题（v0.5：变化时写 session_titles；下播清空，
        # 复播即使标题相同也会作为新场次的首个标题记录）
        self._last_title: str | None = None
        # 主播昵称/头像是否已确认齐备（库里已有则无需补全，见 poll_live_status）
        self._anchor_info_filled = bool(r0 and r0[0]["anchor_name"] and r0[0]["avatar_url"])
        self._client = None            # blivedm.BLiveClient
        self._ws_task: asyncio.Task | None = None
        self._reconnect_wait = 1.0     # 当前重连退避时长

    # ------------------------------------------------------------------
    # WS 连接管理（常驻 + 指数退避重连）
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._ws_task = asyncio.create_task(self._ws_loop(), name=f"ws-{self.room_id}")

    async def stop(self) -> None:
        """断开连接并关闭进行中的场次（房间被移除/服务停止时调用）。"""
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        # v1.1.1：先取消下播收尾任务（若在宽限 sleep 中，场次留给下面的
        # _close_session_if_open 关闭；若在 concat 中，中断后行卡
        # finalizing，重启恢复按既有语义收尾，与 v1.1 中断行为一致）
        if self._stream_end_task is not None and not self._stream_end_task.done():
            self._stream_end_task.cancel()
            try:
                await self._stream_end_task
            except asyncio.CancelledError:
                pass
        self._stream_end_task = None
        # 停录制器（整场收尾为 interrupted 保存已录部分），最后关场次
        await self.recorder.shutdown()
        await self._close_session_if_open()

    async def _ws_loop(self) -> None:
        """WS 主循环：连接 → join 等待断开 → 退避重连。"""
        import blivedm
        max_wait = float(self.manager.cfg["reconnect_max_wait"])
        while True:
            try:
                # 注意：不能给 BLiveClient 传登录 uid——WS 鉴权会要求 getDanmuInfo
                # 返回的 token（匿名连接拿不到），导致无限重连（已实测）。
                # uid=0 匿名连接对弹幕用户打码（超***、uid=0），用 uid_crc32 兜底区分。
                self._client = blivedm.BLiveClient(
                    self.room_id, session=self.manager.http_session)
                self._client.set_handler(self.handler)
                self._client.start()
                await self._client.join()   # 正常情况下阻塞到连接断开
            except asyncio.CancelledError:
                await self._safe_close()
                raise
            except Exception as e:
                print(f"[capture] 房间{self.room_id} WS 异常: {e!r}")
            # 走到这里说明连接断了：退避后重连
            print(f"[capture] 房间{self.room_id} 断线，{self._reconnect_wait:.0f}s 后重连")
            await asyncio.sleep(self._reconnect_wait)
            self._reconnect_wait = min(self._reconnect_wait * 2, max_wait)

    async def _safe_close(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop_and_close()
            except Exception:
                pass
            self._client = None

    def _set_popularity(self, value: int) -> None:
        """handler 的 popularity_sink 回调目标。"""
        self.popularity = value

    def _set_viewers(self, value: int) -> None:
        """handler 的 viewer_sink 回调目标（房间观众数·真实同接）。"""
        self.viewers = value

    # ------------------------------------------------------------------
    # 直播状态机（轮询驱动）
    # ------------------------------------------------------------------
    async def poll_live_status(self) -> None:
        """查一次房间 API：维护场次开闭 + 记录主播指标快照。

        快照字段口径（详见 TECH-SPEC）：
        - attention 粉丝数；online 人气值（B站加权，非真实同接）；
        - watched 本场累计看过人数（真实计数）；guards 大航海人数（真实计数）。
        接口失败沿用上一次的值（None 不写），避免曲线锯齿。
        """
        m = await fetch_room_metrics(self.manager.http_session, self.room_id)
        live = m.get("live_status")
        if live is None:
            return  # 两个接口都失败，保持现状等下一轮
        self.manager.db.execute(
            "INSERT INTO anchor_snapshots (room_id, attention, online, watched, guards, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.room_id, int(m.get("attention") or self._last.get("attention") or 0),
             int(m.get("online") or 0),
             int(m.get("watched") or self._last.get("watched") or 0),
             int(m.get("guards") or self._last.get("guards") or 0), now_iso()))
        self._last = m  # 记住本次值供下次失败时沿用

        if live == 1 and self.session_id is None:
            await self._open_or_adopt_session(
                cover=m.get("cover") or self._last.get("cover") or "")
            # 整场录制由扫描循环独立触发（自动全程对账 / 手动请求行），
            # 开播本身不再起任何流（v2.2 高能缓冲流已随高能功能移除）
        elif live != 1 and self.session_id is not None and not self._ending:
            # v0.11：录制与场次宽限解耦——检测到未开播即让整场录制即时
            # 收尾（内部 3s 复核防 API 抖动误切；宽限内复播则不拆段继续），
            # 场次本身仍走宽限期复核（数据分析口径不拆场不变）。
            # v1.1.1：收尾序列改为独立任务——整场 concat 可达分钟级（实测
            # 10.4GB 拼接 7.5min）、宽限循环内联 sleep 至 600s，同步 await
            # 会把 _poll_all 的 gather 拖住，实测全部房间状态轮询停摆 18
            # 分钟（快照断档实证）。_ending 先行同步置位，防任务启动前的
            # 下一轮轮询重入本分支。
            self._ending = True
            self._stream_end_task = asyncio.create_task(
                self._stream_end_sequence(), name=f"stream-end-{self.room_id}")
        elif live != 1 and self.session_id is None:
            # 首次轮询发现未开播但库里有遗留未关闭场次（上次运行崩溃/被杀）：
            # 补关闭（结束时间取该场次最后一条人气快照）
            orphan = self.manager.db.query(
                "SELECT session_id FROM live_sessions WHERE room_id = ? AND end_time IS NULL",
                (self.room_id,))
            for r in orphan:
                last = self.manager.db.query(
                    "SELECT ts FROM popularity_snapshots WHERE session_id = ? "
                    "ORDER BY ts DESC LIMIT 1", (r["session_id"],))
                self.manager.db.execute(
                    "UPDATE live_sessions SET end_time = ? WHERE session_id = ?",
                    (last[0]["ts"] if last else now_iso(), r["session_id"]))
                print(f"[capture] 房间{self.room_id} 补关闭遗留场次 #{r['session_id']}（未开播）")

        # 主播昵称/头像缺失时补全（房间可能是直接写库加入的，没走添加接口）
        if not self._anchor_info_filled:
            info = await fetch_anchor_info(self.manager.http_session, self.room_id)
            if info.get("uname") or info.get("face"):
                self.manager.db.execute(
                    "UPDATE rooms SET anchor_name = CASE WHEN ? != '' THEN ? ELSE anchor_name END,"
                    "                avatar_url  = CASE WHEN ? != '' THEN ? ELSE avatar_url  END"
                    " WHERE room_id = ?",
                    (info["uname"], info["uname"],
                     info["face"], info["face"], self.room_id))
                print(f"[capture] 房间{self.room_id} 主播信息补全："
                      f"{info.get('uname') or '(无名)'}")
                self._anchor_info_filled = bool(info.get("uname") and info.get("face"))
            # 缺一样就下轮轮询再试；都齐了不再查

        # v0.5：直播标题记录（放在场次开闭处理之后——新建场次当轮即可记录首个
        # 标题，不用等下一轮轮询）。直播中每轮对比，标题变化即追加一行
        # session_titles；同一标题重复推送只刷新 last_seen。低频写（每 120s
        # 最多一条），直接 db.execute 不进 writer 队列。
        title = m.get("title")
        if live != 1:
            self._last_title = None   # 下播清空：复播同标题也作为新场次首标题入库
        elif self.session_id is not None and title and title != self._last_title:
            self.manager.db.execute(
                "INSERT INTO session_titles (session_id, title, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, title) DO UPDATE SET last_seen = excluded.last_seen",
                (self.session_id, title, now_iso(), now_iso()))
            self._last_title = title

    async def _open_or_adopt_session(self, cover: str = "") -> None:
        """检测到开播：按优先级"收养"已有场次，没有才新建。

        1. 遗留未关闭场次（服务重启、崩溃恢复后直播仍在进行）→ 沿用不拆场；
        2. 上一场刚结束不足 session_merge_gap_sec（默认 600s，v0.2.1）→
           视作同一场误判拆场：重开该场次（end_time 置空继续记），
           主播短时间下播又复播（网络波动/换设备）不会产生碎片场次；
        3. 都不满足 → 新建场次。

        cover 为本次轮询透传的直播间封面（v2.5.1）：仅新建分支写入
        （收养/重开沿用该场开播时的原始封面，直播中途换封面不覆盖）。
        """
        orphan = self.manager.db.query(
            "SELECT session_id FROM live_sessions WHERE room_id = ? AND end_time IS NULL "
            "ORDER BY session_id DESC LIMIT 1", (self.room_id,))
        if orphan:
            self.session_id = orphan[0]["session_id"]
            self.handler.session_id = self.session_id
            print(f"[capture] 房间{self.room_id} 仍直播中，沿用遗留场次 "
                  f"#{self.session_id}（重启期间数据缺失，不拆场）")
            return
        # v0.2.1：短间隔复播 → 沿用刚结束的场次
        import datetime as dt
        gap = float(self.manager.cfg.get("session_merge_gap_sec", 600))
        recent = self.manager.db.query(
            "SELECT session_id, end_time FROM live_sessions WHERE room_id = ? "
            "AND end_time IS NOT NULL ORDER BY end_time DESC LIMIT 1", (self.room_id,))
        if recent:
            try:
                gap_sec = (dt.datetime.now()
                           - dt.datetime.strptime(recent[0]["end_time"], "%Y-%m-%dT%H:%M:%S")
                           ).total_seconds()
            except ValueError:
                gap_sec = None
            if gap_sec is not None and gap_sec <= gap:
                self.session_id = recent[0]["session_id"]
                self.handler.session_id = self.session_id
                self.manager.db.execute(
                    "UPDATE live_sessions SET end_time = NULL WHERE session_id = ?",
                    (self.session_id,))
                print(f"[capture] 房间{self.room_id} 复播间隔 {gap_sec:.0f}s（≤{gap:.0f}s），"
                      f"重开场次 #{self.session_id}（不拆场）")
                return
        cur = self.manager.db.execute(
            "INSERT INTO live_sessions (room_id, start_time, cover_url) "
            "VALUES (?, ?, ?)",
            (self.room_id, now_iso(), cover))
        self.session_id = cur.lastrowid
        self.handler.session_id = self.session_id
        print(f"[capture] 房间{self.room_id} 开播，场次 #{self.session_id}")

    async def _stream_end_sequence(self) -> None:
        """下播收尾序列（v1.1.1 起独立任务，不占共享轮询协程）：
        录制即时收尾（整场 concat + 落库，可达分钟级）→ 场次宽限复核
        → 关场。_ending 在任务全程保持 True（_handle_stream_end 内部
        也管理此标记），宽限期内复播则场次不关、录制由对账循环接续。"""
        try:
            if get_config()["recording"].get("enabled", True):
                try:
                    await self.recorder.on_stream_gone()
                except Exception as e:
                    print(f"[capture] 房间{self.room_id} 录制即时收尾异常: {e!r}")
            await self._handle_stream_end()
        finally:
            self._ending = False

    async def _handle_stream_end(self) -> None:
        """检测到下播：进入宽限期，期间按轮询间隔反复复核——主播临时调网、
        本机/网络短暂波动导致的短暂"假下播"不关场次；宽限期结束仍未复播
        才真正关闭。宽限时长 config: capture.end_grace_sec（默认 10 分钟）。"""
        self._ending = True
        grace = float(self.manager.cfg["end_grace_sec"])
        interval = float(self.manager.cfg["poll_interval_sec"])
        waited = 0.0
        try:
            while waited < grace:
                await asyncio.sleep(min(interval, grace - waited))
                waited += interval
                info = await fetch_room_info(self.manager.http_session, self.room_id)
                if info is not None and info.get("live_status") == 1:
                    print(f"[capture] 房间{self.room_id} 宽限期内复播"
                          f"（已等 {waited:.0f}s），场次不关闭")
                    return
            await self._close_session_if_open()
        finally:
            self._ending = False

    async def _close_session_if_open(self) -> None:
        if self.session_id is None:
            return
        self.manager.db.execute(
            "UPDATE live_sessions SET end_time = ? WHERE session_id = ?",
            (now_iso(), self.session_id))
        print(f"[capture] 房间{self.room_id} 下播，场次 #{self.session_id} 已关闭")
        self.session_id = None
        self.handler.session_id = None
        # 录制侧整场收尾保存（跟随场次宽限：宽限期内复播不会走到这里，
        # 录制由 watcher 自动续上）
        if get_config()["recording"].get("enabled", True):
            await self.recorder.on_confirmed_end()

    # ------------------------------------------------------------------
    # 人气采样（由 manager 的采样协程按全局间隔调用）
    # ------------------------------------------------------------------
    def maybe_snapshot(self) -> None:
        """距上次落库超过 snapshot_interval 才写一条，避免心跳频率绑架采样密度。"""
        interval = float(self.manager.cfg["snapshot_interval_sec"])
        now = time.monotonic()
        if now - self._last_snapshot_ts < interval:
            return
        self._last_snapshot_ts = now
        self.manager.db.execute(
            "INSERT INTO popularity_snapshots (room_id, session_id, popularity, viewers, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.room_id, self.session_id, self.popularity, self.viewers, now_iso()))
        # 顺带刷新场次峰值（人气 + 房间观众）
        if self.session_id is not None:
            self.manager.db.execute(
                "UPDATE live_sessions SET peak_popularity = MAX(peak_popularity, ?), "
                "peak_viewers = MAX(peak_viewers, ?) WHERE session_id = ?",
                (self.popularity, self.viewers, self.session_id))


class RoomManager:
    """管理全部 RoomClient 与三个常驻循环（状态轮询 / 人气采样 / 名单热加载）。"""

    def __init__(self, db: Db, queue: asyncio.Queue):
        self.db = db
        self.queue = queue
        self.cfg = get_config()["capture"]
        self.clients: dict[int, RoomClient] = {}
        self.http_session: aiohttp.ClientSession | None = None
        # v0.2：盲盒价值映射 + 表情 URL 的进程内缓存（热加载循环每 30s 刷新）
        self.caches = RuntimeCaches(db)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """初始化 HTTP 会话（带 UA 与登录 cookie，见 build_bili_session）并启动全部后台循环。

        遗留场次不在此处统一关闭：由各房间首次状态轮询按"直播中→收养沿用，
        未开播→补关闭"处理（见 RoomClient.poll_live_status）。
        """
        self.http_session = build_bili_session()
        self.caches.refresh()  # 启动即加载一次（盲盒映射/表情表）
        await self._reload_rooms()  # 启动时立即加载一次名单
        # v0.9：capture 重启后对"上次还在录"的整场行收尾（已录分段保存为
        # interrupted 成片，不自动续录——用户在按钮页重新点击即可）
        await self._recover_recordings()
        poll = float(self.cfg["poll_interval_sec"])
        reload_iv = float(self.cfg["reload_interval_sec"])
        keepalive_h = float(self.cfg.get("cookie_keepalive_hours", 6))
        emoticon_h = float(self.cfg.get("emoticon_refresh_hours", 24))
        self._tasks = [
            asyncio.create_task(self._loop("状态轮询", poll, self._poll_all)),
            asyncio.create_task(self._loop("人气采样", 5.0, self._snapshot_all)),
            asyncio.create_task(self._loop("名单热加载", reload_iv, self._reload_rooms)),
            # cookie 保活：先立即执行一次（含启动时的登录态检查），之后周期运行
            asyncio.create_task(self._loop("cookie保活", keepalive_h * 3600,
                                           self._cookie_keepalive)),
            # v0.2 表情表拉取：启动时拉一次（新房间表情立即可用），之后周期刷新
            asyncio.create_task(self._loop("表情表拉取", emoticon_h * 3600,
                                           self._refresh_emotes)),
            # v0.9：整场录制请求扫描（web 写 recordings 表 → 2s 内捡起执行）
            asyncio.create_task(self._loop("录制请求扫描", 2.0, self._scan_recordings)),
            # v0.11：自动全程录制对账（auto_full 房间在播且无活跃录制 →
            # 插 source='auto' 请求行；幂等，覆盖开播/直播中开开关/重启三种场景）
            asyncio.create_task(self._loop(
                "自动全程对账", float(get_config()["recording"].get(
                    "auto_full_poll_sec", 30)), self._reconcile_auto_full)),
            # v0.11：手动切片处理（web 写 manual_clips pending 行 → 快切执行；
            # 每轮一条串行处理，快切秒级完成不构成瓶颈）
            asyncio.create_task(self._loop("手动切片处理", 2.0,
                                           self._scan_manual_clips)),
        ]

    def _upsert_emotes(self, room_id: int, emotes: list[dict]) -> None:
        """把某房间拉到的表情表批量 upsert 进 emotes 表（一个事务）。"""
        now = now_iso()
        with self.db._lock:  # noqa: SLF001
            self.db.conn.executemany(
                """INSERT INTO emotes (room_id, emoji_key, url, width, height, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (room_id, emoji_key) DO UPDATE SET
                       url = excluded.url, width = excluded.width,
                       height = excluded.height, updated_at = excluded.updated_at""",
                [(room_id, e["emoji_key"], e["url"], e["width"], e["height"], now)
                 for e in emotes])
            self.db.conn.commit()

    async def _refresh_emotes(self) -> None:
        """拉取所有监控房间的表情表写入 emotes 表，并刷新进程内缓存。

        房间专属表情包（如主播定制表情）可能在开播后才上架，因此周期性重拉；
        单房间失败只影响该房间的表情图片渲染，弹幕文本仍完整保留。
        """
        assert self.http_session is not None
        for room_id in list(self.clients):
            emotes = await fetch_emotes(self.http_session, room_id)
            if not emotes:
                continue
            self._upsert_emotes(room_id, emotes)
            print(f"[capture] 房间{room_id} 表情表已更新（{len(emotes)} 个）")
        self.caches.refresh()

    async def _cookie_keepalive(self) -> None:
        """cookie 保活：① 访问 B站首页触发 Set-Cookie 刷新短时效凭据
        （bili_ticket 等，aiohttp jar 自动接收）；② 检查登录态（nav.isLogin）；
        ③ 把刷新后的 cookie 持久化到 data/cookies.json，重启后自动延续。

        SESSDATA 本身有效期长（月级），真正的失效多为账号在别处登录/风控
        下线——isLogin=false 时打告警日志提醒更换，采集不中断（退回匿名态）。
        """
        assert self.http_session is not None
        try:
            async with self.http_session.get(
                    KEEPALIVE_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                await resp.read()
            async with self.http_session.get(
                    NAV_API, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                nav = await resp.json(content_type=None)
            ok = bool((nav.get("data") or {}).get("isLogin"))
            n = persist_runtime_cookies(self.http_session)
            if ok:
                print(f"[capture] cookie 保活完成：登录态正常（mid="
                      f"{nav['data'].get('mid')}），已持久化 {n} 个 cookie")
            else:
                print("[capture] [警告] cookie 已失效（isLogin=false）：弹幕将退回"
                      "匿名打码态，请尽快更新 config.toml 的 cookie 后重启服务")
        except Exception as e:
            print(f"[capture] cookie 保活异常（下轮重试）: {e!r}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        for c in self.clients.values():
            await c.stop()
        if self.http_session:
            await self.http_session.close()

    async def _loop(self, name: str, interval: float, fn) -> None:
        """通用周期循环：执行 fn（异步或同步）→ 睡 interval → 循环。"""
        while True:
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[capture] {name}异常: {e!r}")
            await asyncio.sleep(interval)

    async def _poll_all(self) -> None:
        """对所有房间并发查一次直播状态。"""
        if self.clients:
            await asyncio.gather(*(c.poll_live_status() for c in self.clients.values()))

    def _snapshot_all(self) -> None:
        for c in self.clients.values():
            # 单房间隔离：任何一个房间的快照抛错都不得中断本轮循环，
            # 否则排在迭代顺序后面的房间会连带停写人气快照
            try:
                c.maybe_snapshot()
            except Exception as e:
                print(f"[capture] 房间{c.room_id} 快照异常: {e!r}")

    async def _reload_rooms(self) -> None:
        """热加载房间名单：新增的启动采集，移除/禁用的停止并清理。
        顺带刷新运行时缓存（v0.2：感知管理页编辑的盲盒价值映射）。"""
        rows = self.db.query("SELECT room_id FROM rooms WHERE enabled = 1")
        wanted = {r["room_id"] for r in rows}
        current = set(self.clients)

        for room_id in wanted - current:
            print(f"[capture] 开始监控房间 {room_id}")
            c = RoomClient(self, room_id)
            self.clients[room_id] = c
            c.start()
            # 新房间的表情表立即拉一次（不等下一个周期），失败静默
            if self.http_session is not None:
                emotes = await fetch_emotes(self.http_session, room_id)
                if emotes:
                    self._upsert_emotes(room_id, emotes)
                    print(f"[capture] 房间{room_id} 表情表已加载（{len(emotes)} 个）")
        for room_id in current - wanted:
            print(f"[capture] 停止监控房间 {room_id}")
            c = self.clients.pop(room_id)
            await c.stop()
        self.caches.refresh()

    # ------------------------------------------------------------------
    # v0.9：整场录制请求扫描（web ↔ capture 的指令通道）
    # ------------------------------------------------------------------
    async def _recover_recordings(self) -> None:
        """启动恢复：上次运行遗留的 recording/finalizing 整场行收尾。

        已录分段 concat 成 mp4 保存（interrupted），不自动续录——按钮页
        重新点击即可开始新录制；这是"下播自动停止保存"在进程重启下的
        对应语义：不静默丢失已录内容。
        """
        rows = self.db.query(
            "SELECT id, room_id FROM recordings WHERE status IN "
            "('recording', 'finalizing', 'rotate_requested')")
        for r in rows:
            await finalize_recording_row(self.db, r["id"], r["room_id"],
                                         "interrupted", "capture 重启收尾")
            print(f"[capture] 恢复收尾整场 rec #{r['id']}（房间{r['room_id']}）")

    async def _scan_recordings(self) -> None:
        """2s 扫描 recordings 表：requested → 启动录制；stop_requested → 停止。

        开播校验在 capture 侧完成（复用登录会话现查一次房间状态），
        未开播的请求行置 failed + 原因，按钮页轮询可见。并发达到上限时
        requested 行保持原状等下一轮（先到先得）。
        """
        rcfg = get_config()["recording"]
        if not rcfg.get("enabled", True):
            return
        # ---- 启动请求 ----
        rows = self.db.query(
            "SELECT id, room_id, qn, source FROM recordings "
            "WHERE status = 'requested' ORDER BY id")
        if rows:
            active = sum(1 for c in self.clients.values()
                         if c.recorder.full_rec_id is not None)
            for r in rows:
                c = self.clients.get(r["room_id"])
                if c is None:
                    self.db.execute(
                        "UPDATE recordings SET status = 'failed', reason = ?, "
                        "ended_at = ? WHERE id = ?",
                        ("房间未在监控名单（enabled=1）", now_iso(), r["id"]))
                    continue
                if active >= int(rcfg.get("max_concurrent", 5)):
                    break  # 并发满：保持 requested，下轮再试
                # 同房间防重（第二道防线，兜 web 端竞态漏网的重复请求行）：
                # 该房间 Recorder 已在录，或库里已有另一条活跃行 → 本行作废
                if (c.recorder.full_rec_id is not None
                        or self.db.query(
                            "SELECT 1 FROM recordings WHERE room_id = ? "
                            "AND id != ? AND status IN ('recording', 'finalizing') "
                            "LIMIT 1", (r["room_id"], r["id"]))):
                    self.db.execute(
                        "UPDATE recordings SET status = 'failed', reason = ?, "
                        "ended_at = ? WHERE id = ?",
                        ("该房间已有进行中的录制", now_iso(), r["id"]))
                    continue
                info = await fetch_room_info(self.http_session, r["room_id"])
                if info is None or info.get("live_status") != 1:
                    self.db.execute(
                        "UPDATE recordings SET status = 'failed', reason = ?, "
                        "ended_at = ? WHERE id = ?",
                        ("当前未开播", now_iso(), r["id"]))
                    continue
                # v0.11：qn 用行内值（插入时已按来源定档：web 手动=full_qn /
                # 自动对账=auto_full_qn），start_full 缺省回退 full_qn
                await c.recorder.start_full(
                    r["id"], int(r["qn"] or 0) or None)
                active += 1
        # ---- 停止请求 ----
        rows = self.db.query(
            "SELECT id, room_id FROM recordings WHERE status = 'stop_requested' ORDER BY id")
        for r in rows:
            c = self.clients.get(r["room_id"])
            if c is None:
                # 房间已被移除：直接按孤儿行收尾文件
                await finalize_recording_row(self.db, r["id"], r["room_id"], "finished")
            else:
                await c.recorder.request_stop_full(r["id"])
        # ---- v2.1 轮换请求（web 压力快路径下发：磁盘 <5GB 提前分段）----
        rows = self.db.query(
            "SELECT id, room_id FROM recordings WHERE status = 'rotate_requested' ORDER BY id")
        for r in rows:
            c = self.clients.get(r["room_id"])
            if c is not None and c.recorder.full_rec_id == r["id"]:
                await c.recorder.rotate_now("磁盘压力提前轮换")
            else:
                # 实例已不持有该段（重启/已收尾）→ 按孤儿行收尾
                await finalize_recording_row(self.db, r["id"], r["room_id"],
                                             "interrupted", "轮换请求时录制已不在进行")

    # ------------------------------------------------------------------
    # v0.11：自动全程录制对账 + 手动切片处理
    # ------------------------------------------------------------------
    async def _reconcile_auto_full(self) -> None:
        """auto_full 房间对账：在播且本房无任何活跃/排队录制行 → 插
        source='auto' 的 requested 行（qn=auto_full_qn），由 _scan_recordings
        按全局并发上限起录。

        幂等三条件缺一不可，天然覆盖三种场景：开播（正常轮询发现）、
        直播中打开开关（本循环下一轮发现）、capture 重启后恢复（重启时
        活跃行已被 _recover_recordings 收尾为 interrupted，本循环重新起录，
        中断缺口无法避免——与手动重启行为一致）。
        """
        rcfg = get_config()["recording"]
        if not rcfg.get("enabled", True):
            return
        rows = self.db.query(
            "SELECT room_id FROM rooms WHERE enabled = 1 AND auto_full = 1")
        for r in rows:
            rid = r["room_id"]
            c = self.clients.get(rid)
            if c is None:
                continue   # 尚未被热加载（下轮再看）
            # 本房已有活跃整场（manual 或 auto，含排队）→ 无事可做
            if (c.recorder.full_rec_id is not None
                    or self.db.query(
                        "SELECT 1 FROM recordings WHERE room_id = ? AND status IN "
                        "('requested', 'recording', 'stop_requested', 'finalizing') "
                        "LIMIT 1", (rid,))):
                continue
            # v2.3.1：连续起流失败退避（延迟直播等持续拉不到流的场景
            # 降频重试，防 failed 行每 30s 刷一条），见 _auto_full_hold
            if _auto_full_hold(
                    self.db.query(
                        "SELECT status, ended_at FROM recordings "
                        "WHERE room_id = ? AND source = 'auto' "
                        "ORDER BY id DESC LIMIT 2", (rid,)),
                    float(rcfg.get("auto_full_retry_backoff_sec", 300)),
                    dt.datetime.now()):
                continue
            info = await fetch_room_info(self.http_session, rid)
            if info is None or info.get("live_status") != 1:
                continue
            self.db.execute(
                "INSERT INTO recordings (room_id, status, qn, created_at, "
                "started_at, source) VALUES (?, 'requested', ?, ?, ?, 'auto')",
                (rid, int(rcfg.get("auto_full_qn", 150)), now_iso(), now_iso()))
            print(f"[capture] 房间{rid} 在播且开启自动全程录制 → 已排队自动录制"
                  f"（qn={rcfg.get('auto_full_qn', 150)}，开播头约缺 35s）")

    async def _scan_manual_clips(self) -> None:
        """每轮捡一条 pending 切片执行（快切秒级；串行避免并发 ffmpeg
        抢 2 核 CPU 与 600M cgroup 内存）。"""
        if not get_config()["recording"].get("enabled", True):
            return
        rows = self.db.query(
            "SELECT id FROM manual_clips WHERE status = 'pending' ORDER BY id LIMIT 1")
        if rows:
            await run_manual_clip(self.db, rows[0]["id"])
