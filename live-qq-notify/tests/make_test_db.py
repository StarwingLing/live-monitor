# -*- coding: utf-8 -*-
"""生成本地联调/测试用共享库(data/test_live.db)。

包含 rooms 表最小子集(DDL 抄自 bili-monitor/db.py,列集足够通知器
JOIN 即可)与通知两表(store.SCHEMA 属主),并播种演示数据:
- 房间 111(enabled=1,通知开,群 [4242],用全局模板)
- 房间 222(enabled=1,通知开,群 [4242, 8888],专属开播模板)
- 房间 333(enabled=1,通知关)——配置行存在但未启用
- 房间 444(enabled=0 已暂停监控,通知行开着)——验证 JOIN 过滤
运行:uv run python tests/make_test_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ROOMS_DDL = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id     INTEGER PRIMARY KEY,
    short_id    INTEGER NOT NULL DEFAULT 0,
    anchor_name TEXT NOT NULL DEFAULT '',
    avatar_url  TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT ''
);
"""


def make(path: str) -> None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(str(p))
    conn.executescript(ROOMS_DDL)
    store.ensure_tables(conn)
    conn.executemany("INSERT OR REPLACE INTO rooms(room_id, anchor_name, enabled) "
                     "VALUES (?, ?, ?)", [
        (111, "测试主播甲", 1),
        (222, "测试主播乙", 1),
        (333, "测试主播丙", 1),
        (444, "已暂停主播", 0),
    ])
    conn.executemany("INSERT OR REPLACE INTO qq_notify"
                     "(room_id, enabled, group_ids, template_on, template_off) "
                     "VALUES (?, ?, ?, ?, ?)", [
        (111, 1, "[4242]", "", ""),
        (222, 1, "[4242, 8888]", "🔔 {uname} 来了:{title}", ""),
        (333, 0, "[]", "", ""),
        (444, 1, "[4242]", "", ""),
    ])
    conn.commit()
    conn.close()
    print(f"测试库已生成: {p}")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "data/test_live.db")
