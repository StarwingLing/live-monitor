# -*- coding: utf-8 -*-
"""批量写库协程：从事件队列取消息，聚合后一次性写 SQLite。

所有明细表的 INSERT 都走这里；同时维护两张"加速表"：
- live_sessions 的 danmaku_count / gift_value_yuan 冗余计数；
- user_stats 的实时增量（弹幕数/礼物金额/首末次出现）。

分层（tier）与互动天数不在此实时计算——由每日维护任务全量重算
（见 maintenance.py），实时侧只更新能 O(1) 增量的字段。
"""
import asyncio
import zlib

from ..config import get_config
from ..db import Db, now_iso

# 各类事件的 INSERT 语句（字段顺序与 collect 时构造的 tuple 一致）
_SQL_DANMAKU = (
    "INSERT INTO danmaku (session_id, room_id, uid, uname, medal_name, medal_level, "
    "medal_color, privilege_type, text, emots, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SQL_GIFT = (
    "INSERT INTO gifts (session_id, room_id, is_offline, uid, uname, type, name, blind_box, "
    "text, count, value_yuan, gift_img, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SQL_INTERACT = (
    "INSERT INTO interactions (session_id, room_id, uid, uname, type, detail, ts) "
    "VALUES (?, ?, ?, ?, ?, '', ?)"
)
# user_stats UPSERT：不存在则插入（首末次出现=今天），存在则累加计数、
# 刷新 last_seen、uname 与粉丝牌（换牌跟随）；舰队等级**跟随最新非零
# 观测**——舰级编码为数字越小等级越高（1总督/2提督/3舰长），历史最高
# （MAX）会把等级锁死在数字最大（最低档）上，舰长(3)升提督(2)永不生效
# （v2.0.0 修复）；改为跟随弹幕/礼物消息携带的当前 privilege_type 与
# 上舰 toast 的购买舰级，用户下一次发言或升舰瞬间即更新。非舰队消息
# （0）不覆盖，保留最后已知等级（B站无到期掉舰推送）。
# v0.2：tier 实时跟进，避免当日新用户/新付费者要等凌晨 04:00 维护重算才显示
# 正确分层（分层权威口径仍由 maintenance 每日全量重算，规则保持一致）。
# v0.5 二次调整（与 maintenance 同口径的实时近似，配置只在启动读一次）：
#   核心 = 舰队成员（guard_level 历史最高 >0，含本条消息），
#          或当条消息所属场次内送礼累计 ≥ core_gift_per_session_yuan
#          ——鸽笼原理：场均 ≥10 必有单场 ≥10，实时偏宽松，次日重算收敛；
#   活跃 = 当条消息所属场次内弹幕数 ≥ active_min_danmaku（单场口径；
#          同批明细在 user_rows 之前已先插入，子查询可数到本批）。
# 子查询套一层 LIMIT 是性能关键：内层最多取阈值行就停，索引命中后单次
# 计数开销 O(阈值) 而非 O(单场总量)。
_TIER_CFG = get_config()["tier"]
_CORE_GIFT = float(_TIER_CFG.get("core_gift_per_session_yuan", 10.0))
_ACTIVE_MIN = int(_TIER_CFG.get("active_min_danmaku", 10))
_SQL_USER_UPSERT = f"""
INSERT INTO user_stats (uid, room_id, uname, medal_name, medal_level, medal_color,
                        guard_level, first_seen, last_seen, danmaku_count, gift_value_yuan, tier)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (uid, room_id) DO UPDATE SET
    uname = excluded.uname,
    medal_name = CASE WHEN excluded.medal_name != '' THEN excluded.medal_name ELSE medal_name END,
    medal_level = CASE WHEN excluded.medal_name != '' THEN excluded.medal_level ELSE medal_level END,
    medal_color = CASE WHEN excluded.medal_color != 0 THEN excluded.medal_color ELSE medal_color END,
    guard_level = CASE WHEN excluded.guard_level > 0 THEN excluded.guard_level
                       ELSE guard_level END,
    last_seen = excluded.last_seen,
    danmaku_count = danmaku_count + excluded.danmaku_count,
    gift_value_yuan = gift_value_yuan + excluded.gift_value_yuan,
    tier = CASE
        WHEN MAX(guard_level, excluded.guard_level) > 0 THEN '核心'
        WHEN (SELECT coalesce(sum(value_yuan), 0) FROM (SELECT value_yuan FROM gifts g
                                    WHERE g.uid = excluded.uid AND g.session_id = ?
                                    LIMIT 10000)) >= {_CORE_GIFT} THEN '核心'
        WHEN (SELECT count(*) FROM (SELECT 1 FROM danmaku d
                                    WHERE d.uid = excluded.uid AND d.session_id = ?
                                    LIMIT {_ACTIVE_MIN})) >= {_ACTIVE_MIN}
             AND tier != '核心' THEN '活跃'
        ELSE tier END
"""
# 场次冗余计数（弹幕）
_SQL_SESSION_DANMAKU = (
    "UPDATE live_sessions SET danmaku_count = danmaku_count + ? WHERE session_id = ?"
)
# 场次冗余合计（收入：礼物+SC+舰长；仅直播中产生的计入场次）
_SQL_SESSION_GIFT = (
    "UPDATE live_sessions SET gift_value_yuan = gift_value_yuan + ? WHERE session_id = ?"
)


# uname_map UPSERT：uid → 最新完整昵称（高能榜/礼物等未打码消息更新）
_SQL_UNAME_UPSERT = """
INSERT INTO uname_map (uid, uname, updated_at) VALUES (?, ?, ?)
ON CONFLICT(uid) DO UPDATE SET uname = excluded.uname, updated_at = excluded.updated_at
"""


class Writer:
    """事件队列 → SQLite 的唯一写入者。"""

    def __init__(self, db: Db, queue: asyncio.Queue, flush_interval: float = 1.0):
        self.db = db
        self.queue = queue
        self.flush_interval = flush_interval
        self._task: asyncio.Task | None = None
        # crc32(uid) → (uid, 完整昵称)：用于把打码弹幕（uid_crc32 伪 uid）
        # 还原成真实 uid + 全名。启动时从 uname_map 全量加载，之后增量维护。
        self._crc_map: dict[int, tuple[int, str]] = {}
        for r in db.query("SELECT uid, uname FROM uname_map"):
            self._crc_map[zlib.crc32(str(r["uid"]).encode())] = (r["uid"], r["uname"])

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="db-writer")

    async def stop(self) -> None:
        """停止前把队列里剩余的事件全部落库，避免丢尾部数据。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._flush()  # 最后再冲一次

    async def _run(self) -> None:
        """主循环：等到第一条事件（或超时）后，连同积压事件一起批量落库。"""
        while True:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
                self._flush(first)
            except asyncio.TimeoutError:
                self._flush()  # 没有新事件也定时冲刷，保证时延上限
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # 批量落库：一次事务写完所有明细 + 计数
    # first：触发本次 flush 的那条事件（已出队，须并入处理，防止丢失）
    # ------------------------------------------------------------------
    def _flush(self, first: tuple[str, dict] | None = None) -> None:
        # 取走当前队列中的全部事件（writer 是唯一消费者，直接 drain）
        events: list[tuple[str, dict]] = []
        if first is not None:
            events.append(first)
        while not self.queue.empty():
            events.append(self.queue.get_nowait())

        danmaku_rows: list[tuple] = []
        gift_rows: list[tuple] = []
        interact_rows: list[tuple] = []
        user_rows: list[tuple] = []          # (uid, room_id, uname, first, last, 弹幕增量, 金额增量, tier, session_id)
        uname_rows: list[tuple] = []         # (uid, uname, updated_at) → uname_map
        session_danmaku: dict[int, int] = {} # session_id → 本次弹幕条数
        session_gift: dict[int, float] = {}  # session_id → 本次收入增量
        latest_popularity: dict[int, int] = {}  # room_id → 最新人气值（仅供内存参考）

        def remember_uname(uid: int, uname: str) -> None:
            """记录未打码的真实 uid→昵称到 uname_map，并同步 crc 反查表。"""
            if uid > 0 and uname and "***" not in uname:
                uname_rows.append((uid, uname, now_iso()))
                self._crc_map[zlib.crc32(str(uid).encode())] = (uid, uname)

        def track_user(uid: int, room_id: int, uname: str, ts: str,
                       danmaku: int, value: float,
                       medal_name: str = "", medal_level: int = 0,
                       medal_color: int = 0, guard_level: int = 0,
                       session_id: int | None = None) -> None:
            """user_stats 实时增量（含粉丝牌/舰队等级跟随）。uid=0（打码且
            无法识别）不进个人画像，否则所有打码送礼者会并成同一个"假用户"。
            v0.5 二次调整：新用户首行初始分层——舰队成员或单笔送礼 ≥ 阈值即
            核心（拆多笔时由 UPSERT 的"当条场次送礼累计"分支升级）；首条弹幕
            只记普通（单场不足阈值不算活跃）。
            session_id 作为 UPSERT 第 13/14 个参数仅用于核心/活跃分支的场内聚合。"""
            if uid > 0:
                tier = "核心" if (guard_level > 0 or value >= _CORE_GIFT) else "普通"
                # session_id 传两遍：核心（单场送礼）与活跃（单场弹幕）两个
                # 子查询各占一个参数位（SQLite 按占位符出现顺序绑定）
                user_rows.append((uid, room_id, uname, medal_name, medal_level,
                                  medal_color, guard_level, ts, ts, danmaku, value,
                                  tier, session_id, session_id))

        for etype, p in events:
            if etype == "popularity":
                # 人气值不落明细表（由 RoomClient 采样协程写快照）；
                # 保留分支仅为兼容旧事件格式
                latest_popularity[p["room_id"]] = p["popularity"]
                continue
            if etype == "rank_users":
                # 高能榜：批量维护 uid→完整昵称映射
                for u in p["users"]:
                    remember_uname(u["uid"], u["uname"])
                continue
            if etype == "danmaku":
                uid, uname = p["uid"], p["uname"]
                if uid < 0:
                    # 隐私打码弹幕（uid 为 -crc32 伪 uid）：凭 uname_map 反查
                    # 真实身份，能查到则还原成真实 uid + 完整昵称
                    real = self._crc_map.get(-uid)
                    if real is not None:
                        uid, uname = real
                else:
                    remember_uname(uid, uname)  # 未打码用户，顺手丰富映射表
                danmaku_rows.append((p["session_id"], p["room_id"], uid,
                                     uname, p.get("medal_name", ""),
                                     p.get("medal_level", 0), p.get("medal_color", 0),
                                     p.get("privilege_type", 0), p["text"],
                                     p.get("emots", ""), p["ts"]))
                track_user(uid, p["room_id"], uname, p["ts"], 1, 0.0,
                           p.get("medal_name", ""), p.get("medal_level", 0),
                           p.get("medal_color", 0), p.get("privilege_type", 0),
                           p["session_id"])
                if p["session_id"] is not None:
                    session_danmaku[p["session_id"]] = \
                        session_danmaku.get(p["session_id"], 0) + 1
            elif etype == "gift":
                remember_uname(p["uid"], p["uname"])
                offline = 1 if p["session_id"] is None else 0
                gift_rows.append((p["session_id"], p["room_id"], offline, p["uid"],
                                  p["uname"], p["type"], p["name"], p.get("blind_box", ""),
                                  p.get("text", ""), p["count"], p["value_yuan"],
                                  p.get("gift_img", ""), p["ts"]))
                track_user(p["uid"], p["room_id"], p["uname"], p["ts"], 0, p["value_yuan"],
                           p.get("medal_name", ""), p.get("medal_level", 0),
                           0, p.get("privilege_type", 0), p["session_id"])
                if p["session_id"] is not None:
                    session_gift[p["session_id"]] = \
                        session_gift.get(p["session_id"], 0.0) + p["value_yuan"]
            elif etype == "interact":
                remember_uname(p["uid"], p["uname"])
                interact_rows.append((p["session_id"], p["room_id"], p["uid"],
                                      p["uname"], p["type"], p["ts"]))

        conn = self.db.conn
        with self.db._lock:  # noqa: SLF001  整批一个事务，保证原子性
            try:
                if danmaku_rows:
                    conn.executemany(_SQL_DANMAKU, danmaku_rows)
                if gift_rows:
                    conn.executemany(_SQL_GIFT, gift_rows)
                if interact_rows:
                    conn.executemany(_SQL_INTERACT, interact_rows)
                if uname_rows:
                    conn.executemany(_SQL_UNAME_UPSERT, uname_rows)
                if user_rows:
                    conn.executemany(_SQL_USER_UPSERT, user_rows)
                for sid, cnt in session_danmaku.items():
                    conn.execute(_SQL_SESSION_DANMAKU, (cnt, sid))
                for sid, val in session_gift.items():
                    conn.execute(_SQL_SESSION_GIFT, (round(val, 2), sid))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
