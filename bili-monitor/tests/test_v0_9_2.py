# -*- coding: utf-8 -*-
"""v0.9.2 功能本地测试：录制注释（多人各注各的；v1.3.1 起署名主体
为管理员账户——监控码昵称会话已迁 replay 项目）。

覆盖：
1. auth 会话昵称扩展：5 段 Cookie 签发/解析（中文昵称、含分隔符昵称）、
   旧 4 段会话兼容（nickname 为空串）、篡改拒绝、admin/guest 仍为 4 段；
2. 监控码登录 API：缺昵称/空白昵称 400、超长 400、正常登录回显昵称；
3. 注释 CRUD 权限矩阵（TestClient，临时库 + 强制登录模式）：
   - 双管理员账户（admin/a2）署名隔离：各自添加、只改删自己的，
     改删对方的一律 403；
   - 旧 replay 会话（无昵称）：增改删一律 400（提示重新登录）；
   - admin：以用户名署名添加 ✓，改删 replay 用户的注释 403；
   - guest 写方法 403（中间件）、未登录 401；
   - replay 会话操作他人房间注释 API 403（中间件前缀隔离）、
     不存在的录制/注释 404、rec 与房间不匹配 404；
   - 内容校验：空白 400、超长（>500 字）400；
   - 列表接口每行带 notes（按时间正序、字段完整）；
   - 按钮页模板注入 me（昵称/用户名/空串三态）；
4. 级联删除：删 recordings 行后其注释同步消失（外键 ON DELETE CASCADE）；
5. db 迁移幂等：recording_notes 表二次 init_db 不炸。

运行：uv run python tests/test_v0_9_2.py
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import bili_monitor ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bili_monitor import db as dbm
from bili_monitor.db import init_db, now_iso
from bili_monitor.web import auth
from bili_monitor.web import app as app_mod

app = app_mod.app

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="bili_v092_test_"))
auth.CRED_FILE = TMP / "web_credentials.txt"   # 凭据文件指向临时目录

# ===========================================================================
# 1. auth 单元（v1.3.1：监控码昵称会话已迁 replay 项目，本站恒 4 段）
# ===========================================================================
check("会话-admin解析", auth.read_session(SimpleNamespace(cookies={
    "bili_auth": auth.sign_session("admin", "boss")})) ==
    {"role": "admin", "username": "boss"})
check("会话-篡改拒绝", auth.read_session(SimpleNamespace(cookies={
    "bili_auth": auth.sign_session("admin", "boss")[:-4] + "ffff"})) is None)
# v0.9.2 的 5 段昵称格式（replay 会话）不再被本站认可
_legacy5 = "9999999999|replay|654321|%E5%B0%8F%E6%98%8E|" + "0" * 64
check("会话-旧5段replay格式不再认可", auth.read_session(
    SimpleNamespace(cookies={"bili_auth": _legacy5})) is None)

# ===========================================================================
# 2. 临时库 + 种子数据（房间 654321 配监控码；两房各一条 finished 录制）
# ===========================================================================
dbm.DB_PATH = TMP / "test.db"
dbm.DATA_DIR = TMP
init_db()
conn = dbm.connect()
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (123456, '测试甲', 1, ?)", (now_iso(),))
conn.execute("INSERT INTO rooms (room_id, anchor_name, enabled, created_at) "
             "VALUES (654321, '测试乙', 1, ?)", (now_iso(),))
conn.execute("INSERT INTO admin_users VALUES ('admin', ?, ?)",
             (auth.hash_secret("admin-pass-123"), now_iso()))
conn.execute("INSERT INTO admin_users VALUES ('a2', ?, ?)",
             (auth.hash_secret("a2-pass-456"), now_iso()))
conn.execute("INSERT INTO app_secrets VALUES ('guest_code', ?)",
             (auth.hash_secret("guest-code-xyz"),))
conn.execute("UPDATE rooms SET replay_code = 'r654321-aaaaaaaaaa' WHERE room_id = 654321")
conn.commit()
conn.close()
# v2.2：recordings/recording_notes 表归 replay 项目（其 db.py 建表），
# 本测试不再种录制行；注释端点移除断言用固定 id 即可
rec_b = 1

# ===========================================================================
# 3. 登录 API：昵称校验
# ===========================================================================
app_mod.cfg["capture"]["admin_password"] = "legacy-token-pw"   # 强制登录模式
anon = TestClient(app)
# v1.3.1：监控码登录端点已移除（未登录一律 401）
check("登录-监控码端点已移除401", anon.post(
    "/api/replay/login", json={"room_id": 654321, "code": "x",
                               "nickname": "y"}, follow_redirects=False).status_code == 401)

# ===========================================================================
# 4. 注释 CRUD 面（v1.6 剥离：端点已删，等价行为由 replay 项目 tests 覆盖；
#    此处保留 middleware 拦截面与端点移除断言）
# ===========================================================================
guest = TestClient(app)
guest.post("/api/auth/login", json={"mode": "guest", "code": "guest-code-xyz"})
NOTES = f"/api/rooms/654321/recordings/{rec_b}/notes"
check("注释-未登录401", TestClient(app).post(
    NOTES, json={"content": "x"}).status_code == 401)
check("注释-guest写403", guest.post(NOTES, json={"content": "x"}).status_code == 403)
ming = TestClient(app)
ming.post("/api/auth/login", json={"mode": "admin", "username": "admin",
                                   "password": "admin-pass-123"})
check("注释-端点已移除", ming.post(
    NOTES, json={"content": "x"}).status_code in (404, 405))
check("旧按钮页-302新站", ming.get(
    "/654321/replay", follow_redirects=False).status_code == 302)

# ===========================================================================
# 5. 迁移幂等（v2.2：录制域级联检查归 replay 项目；此处仅验证监控表幂等）
# ===========================================================================
init_db()
init_db()
_c = dbm.connect()
check("迁移-二次init_db幂等", _c.execute(
    "SELECT count(*) FROM rooms").fetchone()[0] == 2)
_c.close()

# ===========================================================================
# 汇总
# ===========================================================================
print(f"\n共 {len(results)} 项，通过 {sum(results)}，失败 {len(results) - sum(results)}")
sys.exit(0 if all(results) else 1)
