# -*- coding: utf-8 -*-
"""store 层单测(临时库):建表幂等 / 全局默认 / 房间配置 JOIN 过滤与脏数据。

运行:uv run python tests/test_store.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import store  # noqa: E402

results = []


def check(name: str, ok: bool):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}")


tmp = Path(tempfile.mkdtemp(prefix="lqn_store_test_")) / "t.db"
conn = store.connect(str(tmp))

# 1. 建表 + 全局默认单行
store.ensure_tables(conn)
tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("两表已建", {"qq_notify", "qq_notify_global"} <= tables)
g = store.load_global(conn)
check("全局默认(账号空/双通知开/模板就位/防抖300)",
      g["qq_account"] == "" and g["notify_on"] and g["notify_off"]
      and g["debounce_offline"] == 300 and "{uname}" in g["template_on"])
check("全局默认(v0.2.0 官方通道字段:默认 napcat/凭据空)",
      g["channel"] == "napcat" and g["official_app_id"] == ""
      and g["official_app_secret"] == "")

# 2. 幂等:重复建表不报错、单行不重复
store.ensure_tables(conn)
check("重复 ensure 幂等", conn.execute(
    "SELECT count(*) FROM qq_notify_global").fetchone()[0] == 1)

# 3. rooms 最小子集 + 配置行
conn.executescript("""
CREATE TABLE IF NOT EXISTS rooms (
    room_id INTEGER PRIMARY KEY, short_id INTEGER NOT NULL DEFAULT 0,
    anchor_name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '');
INSERT OR REPLACE INTO rooms(room_id, enabled) VALUES
    (111, 1), (222, 1), (444, 0), (555, 1);
""")
conn.executemany(
    "INSERT OR REPLACE INTO qq_notify(room_id, enabled, group_ids, "
    "template_on) VALUES (?, ?, ?, ?)", [
        (111, 1, json.dumps([4242]), ""),
        (222, 1, json.dumps([4242, 8888]), "专属开播"),
        (333, 1, "不是JSON", ""),         # 脏 group_ids → 跳过
        (444, 1, json.dumps([4242]), ""),  # 房间已暂停监控 → JOIN 过滤
        (555, 1, json.dumps([4242]), ""),  # 正常(映射绑定与否属发送层判断)
    ])
# 群号→openid 映射(v0.2.0 官方通道;bili「群映射获取」写入)
conn.executemany(
    "INSERT OR REPLACE INTO qq_group_map(group_id, group_openid) VALUES (?, ?)",
    [(4242, "OID4242"), (8888, "OID8888")])
conn.commit()

cfgs = store.load_room_configs(conn)
check("启用且监控中:111/222/555(脏配置行 333 跳过)", set(cfgs) == {111, 222, 555})
check("群解析为整数列表", cfgs[222]["groups"] == [4242, 8888])
check("专属模板带出/空模板带出", cfgs[222]["template_on"] == "专属开播"
      and cfgs[111]["template_on"] == "")
check("群映射表读取(v0.2.0)",
      store.load_group_map(conn) == {4242: "OID4242", 8888: "OID8888"})

# 4. 通知关闭的房间不在轮询范围
conn.execute("UPDATE qq_notify SET enabled = 0 WHERE room_id = 111")
conn.commit()
check("开关关闭即退出轮询", set(store.load_room_configs(conn)) == {222, 555})

# 5. WAL/Row 工厂等连接属性
check("Row 工厂可用", isinstance(
    conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row))
conn.close()

# 6. 旧表迁移:手工建"无 webui 列"的旧版表 → ensure_tables 补列
old = store.connect(str(tmp.parent / "old.db"))
old.execute("""CREATE TABLE qq_notify_global (
    id INTEGER PRIMARY KEY CHECK (id = 1), qq_account TEXT NOT NULL DEFAULT '',
    onebot_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:3000',
    onebot_token TEXT NOT NULL DEFAULT '',
    notify_on INTEGER NOT NULL DEFAULT 1, notify_off INTEGER NOT NULL DEFAULT 1,
    template_on TEXT NOT NULL DEFAULT 'x', template_off TEXT NOT NULL DEFAULT 'x',
    debounce_offline INTEGER NOT NULL DEFAULT 300,
    updated_at TEXT NOT NULL DEFAULT '')""")
old.execute("INSERT INTO qq_notify_global (id) VALUES (1)")
old.commit()
store.ensure_tables(old)
cols = {r[1] for r in old.execute("PRAGMA table_info(qq_notify_global)")}
check("旧表迁移补 webui 两列", {"webui_url", "webui_token"} <= cols
      and old.execute("SELECT webui_url FROM qq_notify_global WHERE id=1")
      .fetchone()[0] == "http://127.0.0.1:6099")
check("旧表迁移补 notify_cover(列兼容保留,已停用)",
      "notify_cover" in cols
      and old.execute("SELECT notify_cover FROM qq_notify_global WHERE id=1")
      .fetchone()[0] == 1
      and "notify_cover" not in store.load_global(old))
check("旧表迁移补 v0.2.0 官方通道三列(默认 napcat/凭据空)",
      {"channel", "official_app_id", "official_app_secret"} <= cols
      and old.execute("SELECT channel FROM qq_notify_global WHERE id=1")
      .fetchone()[0] == "napcat"
      and old.execute("SELECT official_app_id FROM qq_notify_global WHERE id=1")
      .fetchone()[0] == "")
check("映射表随 ensure_tables 建立(空表读不报错)",
      store.load_group_map(old) == {})
check("旧表迁移幂等(重复执行不报错)", (store.ensure_tables(old), True)[1])
old.close()
print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
