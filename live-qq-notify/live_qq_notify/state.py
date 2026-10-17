# -*- coding: utf-8 -*-
"""房间开播状态持久化(data/state.json)。

目的:进程重启后不把"上一进程已通知过的开播"再发一遍(宁可漏发不可重发)。
结构:{"rooms": {room_id: {"status": 0|1, "since": ISO时间}}}
- status:归一后口径(0 未播 / 1 直播中;轮播归 0,见 bilibili.normalize)
- since :进入当前状态的时间(防抖窗口"开播后 N 秒内下播不发"的计时起点)
写入为原子替换(tmp + rename),崩溃最多丢一轮。
"""
import json
import os
import tempfile
from pathlib import Path


def load(path: Path) -> dict:
    if not path.exists():
        return {"rooms": {}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d.get("rooms"), dict):
            return d
    except (json.JSONDecodeError, OSError):
        pass  # 损坏则当作无状态:下轮 startup_grace 重建
    return {"rooms": {}}


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写:同目录临时文件 + rename,避免写一半崩溃留下残缺 JSON
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def get_room(state: dict, room_id: int) -> dict | None:
    r = state["rooms"].get(str(room_id))
    return r if isinstance(r, dict) else None


def set_room(state: dict, room_id: int, status: int, since: str) -> None:
    state["rooms"][str(room_id)] = {"status": status, "since": since}
