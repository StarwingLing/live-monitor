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

# 4.5 房间级个人配置四键(v0.3.0):NULL=跟随全局,None 透出;覆盖值原样
conn.execute("""UPDATE qq_notify SET notify_on = 1, notify_off = 0,
                report_offline = 1, debounce_offline = 120 WHERE room_id = 222""")
conn.commit()
cfgs = store.load_room_configs(conn)
check("个人配置-未设置房间全部 None(跟随全局)",
      cfgs[555]["notify_on"] is None and cfgs[555]["notify_off"] is None
      and cfgs[555]["report_offline"] is None
      and cfgs[555]["debounce_offline"] is None)
check("个人配置-覆盖值三态透出(222 房间)",
      cfgs[222]["notify_on"] is True and cfgs[222]["notify_off"] is False
      and cfgs[222]["report_offline"] is True
      and cfgs[222]["debounce_offline"] == 120)

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
check("旧表迁移补 report_offline(默认 0 关)",
      "report_offline" in cols
      and old.execute("SELECT report_offline FROM qq_notify_global WHERE id=1")
      .fetchone()[0] == 0
      and store.load_global(old)["report_offline"] is False)
old.execute("UPDATE qq_notify_global SET report_offline = 1 WHERE id = 1")
old.commit()
check("report_offline 写读回环(bili 页面写/通知器读)",
      store.load_global(old)["report_offline"] is True)

# 6.5 旧表迁移补 v0.3.2 AI 智能回复列(bili 事件中枢消费;本进程不读)
# 含频控两列(2026-10-18 定稿从代码常量升级为页面配置项,默认 3 次/300s)
store.ensure_tables(old)
cols2 = {r[1] for r in old.execute("PRAGMA table_info(qq_notify_global)")}
check("旧表迁移补 AI 七列(默认关/空/频控 3 次 300s)",
      {"ai_reply_enabled", "ai_api_key", "ai_base_url", "ai_model",
       "ai_system_prompt", "ai_rate_limit", "ai_rate_window"} <= cols2
      and tuple(old.execute(
          "SELECT ai_reply_enabled, ai_api_key, ai_base_url, ai_model, "
          "ai_system_prompt, ai_rate_limit, ai_rate_window "
          "FROM qq_notify_global WHERE id=1").fetchone())
      == (0, "", "", "", "", 3, 300))
check("load_global 白名单不含 AI 字段(通知器进程不读,列仅 DDL 锁步)",
      not ({"ai_reply_enabled", "ai_api_key", "ai_rate_limit"}
           & set(store.load_global(old))))
old.close()

# 6.6 v0.4.0 多机器人:qq_bots 种子 / 群映射表重建迁移 / 房间 bot_id
# (主连接上文已关;同一文件重开新连接,数据仍在)
conn = store.connect(str(tmp))
bots = store.load_bots(conn)
check("qq_bots 种子:机器人1 承接旧全局默认(load_bots 白名单字段)",
      len(bots) == 1 and bots[0]["id"] == 1 and bots[0]["channel"] == "napcat"
      and bots[0]["notify_on"] is True and bots[0]["report_offline"] is False
      and "{uname}" in bots[0]["template_on"])
check("load_group_map 按机器人过滤(缺省=机器人1)",
      store.load_group_map(conn, 1) == {4242: "OID4242", 8888: "OID8888"}
      and store.load_group_map(conn, 2) == {})
cfgs = store.load_room_configs(conn)
check("房间配置带 bot_id(缺省 1)且个人配置三态不变",
      cfgs[222]["bot_id"] == 1 and cfgs[555]["bot_id"] == 1
      and cfgs[222]["notify_on"] is True)
# 旧版单主键群映射表 → 复合主键重建(存量行归机器人 1)
gdb = store.connect(str(tmp.parent / "gmap.db"))
gdb.execute("CREATE TABLE qq_group_map (group_id INTEGER PRIMARY KEY, "
            "group_openid TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT '', "
            "remark TEXT NOT NULL DEFAULT '')")
gdb.execute("INSERT INTO qq_group_map (group_id, group_openid) "
            "VALUES (4242, 'OID4242')")
gdb.commit()
store.ensure_tables(gdb)
check("群映射表重建迁移:复合主键+存量行归机器人1",
      {"bot_id", "group_id", "group_openid", "remark"}
      <= {r[1] for r in gdb.execute("PRAGMA table_info(qq_group_map)")}
      and [tuple(r) for r in gdb.execute(
          "SELECT bot_id, group_id, group_openid FROM qq_group_map")]
      == [(1, 4242, "OID4242")]
      and store.load_group_map(gdb, 1) == {4242: "OID4242"})
check("重建迁移幂等(重复 ensure 不再重建)",
      (store.ensure_tables(gdb), True)[1]
      and gdb.execute("SELECT count(*) FROM qq_group_map").fetchone()[0] == 1)
gdb.close()
print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
