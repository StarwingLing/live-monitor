# -*- coding: utf-8 -*-
"""采集进程内的小型只读缓存（v0.2 新增）。

缓存两张低频变更、高频查询的表：
- blind_gift_prices：盲盒礼物价值映射（Web 管理页编辑，30s 内被热加载感知）；
- emotes：直播间表情表（GetEmoticons API 定期拉取）。

为什么需要缓存：blivedm 的消息回调运行在事件循环线程，逐条弹幕/礼物都
可能要查这两张表；直接走 SQLite 也很快，但一行弹幕可能含多个 [表情]
占位符，逐个 SELECT 会放大查询次数。全量加载进 dict 后查询是 O(1)。

刷新时机（都由 manager 驱动）：
- 名单热加载循环每 30s 调一次 refresh()（感知管理页改价）；
- 表情拉取循环写完 emotes 表后调一次 refresh()。
"""
import threading

from ..db import Db


class RuntimeCaches:
    """blind_gift_prices / emotes 两张表的全量内存镜像（线程安全只读）。"""

    def __init__(self, db: Db):
        self._db = db
        self._lock = threading.Lock()
        # gift_name -> price_yuan（元/个）
        self._blind_prices: dict[str, float] = {}
        # (room_id, emoji_key不带方括号) -> {url, width, height}
        self._emotes: dict[tuple[int, str], dict] = {}

    def refresh(self) -> None:
        """从 SQLite 全量重载两张表（表都很小：百级映射 + 千级表情）。"""
        prices = {r["gift_name"]: float(r["price_yuan"])
                  for r in self._db.query("SELECT gift_name, price_yuan FROM blind_gift_prices")}
        emotes = {(r["room_id"], r["emoji_key"]): {"url": r["url"], "width": r["width"],
                                                   "height": r["height"]}
                  for r in self._db.query("SELECT room_id, emoji_key, url, width, height FROM emotes")}
        with self._lock:
            self._blind_prices = prices
            self._emotes = emotes

    # ----------------------------------------------------------------------
    # 查询接口（handler 消息回调里同步调用，必须足够快）
    # ----------------------------------------------------------------------
    def blind_price(self, gift_name: str) -> float | None:
        """盲盒开出的礼物名（或盲盒名）→ 单价（元）。查不到返回 None。"""
        with self._lock:
            return self._blind_prices.get(gift_name)

    def emote(self, room_id: int, key: str) -> dict | None:
        """按房间查表情：key 为占位符内容（不含方括号，如 "大笑"）。
        返回 {url, width, height} 或 None。房间专属表查不到就不查全局——
        基础表情每房间的表情表里都有，无需全局回退。"""
        with self._lock:
            return self._emotes.get((room_id, key))
