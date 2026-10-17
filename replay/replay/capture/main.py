# -*- coding: utf-8 -*-
"""采集服务入口：`uv run python -m replay.capture.main`

启动顺序：建表 → 事件队列 → writer 协程 → RoomManager → 每日维护循环。
收到 SIGINT/SIGTERM 时优雅退出（冲刷队列、关闭场次、断开连接）。
"""
import asyncio
import signal

from ..config import get_config
from ..db import Db, init_db
from .manager import RoomManager
from .maintenance import hourly_recording_cleanup_loop, maintenance_loop
from .writer import Writer


async def main() -> None:
    init_db()  # 幂等建表
    db = Db()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)

    writer = Writer(db, queue,
                    flush_interval=float(get_config()["capture"]["flush_interval_sec"]))
    manager = RoomManager(db, queue)

    loop = asyncio.get_running_loop()
    stop_evt = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except NotImplementedError:  # Windows 本地开发无此 API，用 signal.signal 兜底
            signal.signal(sig, lambda *_: stop_evt.set())

    writer.start()
    await manager.start()
    maint = asyncio.create_task(maintenance_loop(db), name="maintenance")
    # v0.11：录制产物每小时水位 + 磁盘紧急清理（自动全程录制是长时任务，
    # 每日一次的清理不足以兜住双路连录的日内增长）
    hourly = asyncio.create_task(hourly_recording_cleanup_loop(db),
                                 name="hourly-rec-cleanup")
    print("[capture] 服务已启动")

    await stop_evt.wait()
    print("[capture] 正在退出……")
    maint.cancel()
    hourly.cancel()
    await manager.stop()
    await writer.stop()
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
