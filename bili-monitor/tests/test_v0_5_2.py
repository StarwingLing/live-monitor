# -*- coding: utf-8 -*-
"""v0.5 第二批功能本地测试：登录双角色 / 主播描述标签 / 场次标题 / 分层新口径。

覆盖：
1. auth 工具单元（PBKDF2 哈希、签名 Cookie 有效/过期/篡改）；
2. Web API（TestClient，临时库隔离）：登录/登出/me、middleware 角色拦截
   （未登录 401/302、游客写 403）、管理员账户管理、游客码重置、描述/标签、
   分析页概览、场次 titles 带出；
3. 场次标题合并迁移（merge_short_gap_sessions，幂等）；
4. 分层权威口径（maintenance._recompute_user_stats：≥1 元核心 / 单场 ≥10 条活跃）；
5. 分层实时口径（writer._flush：UPSERT 阈值 + 单场计数子查询，含跨批累计）。

运行：uv run python tests/test_v0_5_2.py
"""
import sys
import tempfile
import time
from pathlib import Path

# ---- 定位项目根：脚本位于 tests/ 子目录，先加 sys.path 再 import bili_monitor ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from bili_monitor import db as dbm
from bili_monitor.db import Db, init_db, now_iso
from bili_monitor.web import auth
from bili_monitor.web import app as app_mod   # app_mod.app = FastAPI 实例；cfg = 模块级配置

app = app_mod.app

results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# 1. auth 工具单元
# ---------------------------------------------------------------------------
class _Req:
    """auth.read_session 所需的最小 request 替身（只读 cookies）。"""
    def __init__(self, cookies):
        self.cookies = cookies


h = auth.hash_secret("s3cret-口令")
check("哈希/校验-正确", auth.verify_secret("s3cret-口令", h))
check("哈希/校验-错误", not auth.verify_secret("wrong", h))
check("哈希/校验-存储串损坏", not auth.verify_secret("x", "not-a-hash"))
check("哈希-随机盐（两次哈希不同）", auth.hash_secret("a") != auth.hash_secret("a"))

s = auth.sign_session("admin", "alice")
check("会话-有效", auth.read_session(_Req({"bili_auth": s})) == {"role": "admin", "username": "alice"})
check("会话-过期", auth.read_session(_Req({"bili_auth": auth.sign_session("guest", "", ttl=-10)})) is None)
tampered = s.rsplit("|", 1)[0] + "|deadbeef"
check("会话-签名篡改", auth.read_session(_Req({"bili_auth": tampered})) is None)
check("会话-无 Cookie", auth.read_session(_Req({})) is None)


# ---------------------------------------------------------------------------
# 2. Web API（临时库 + 强制登录模式）
# ---------------------------------------------------------------------------
tmpdir = tempfile.mkdtemp(prefix="bili_v05_test_")
TMP_DB = Path(tmpdir) / "web.db"
init_db(TMP_DB)
dbm.DB_PATH = TMP_DB          # app.py 的 connect()/q() 均读模块全局，切到临时库

_conn = dbm.connect(TMP_DB)
_conn.execute("INSERT OR IGNORE INTO rooms (room_id, anchor_name, enabled, created_at) "
              "VALUES (999001, '测试主播', 1, ?)", (now_iso(),))
cur = _conn.execute("INSERT INTO live_sessions (room_id, start_time, end_time) "
                    "VALUES (999001, ?, ?)", ("2026-09-15T20:00:00", "2026-09-15T22:00:00"))
_sid = cur.lastrowid
_conn.execute("INSERT INTO session_titles VALUES (?, '首播标题', '2026-09-15T20:00:10', '2026-09-15T21:00:00')", (_sid,))
_conn.execute("INSERT INTO session_titles VALUES (?, '改过的标题', '2026-09-15T21:00:10', '2026-09-15T22:00:00')", (_sid,))
_conn.execute("INSERT INTO admin_users VALUES ('admin', ?, ?)",
              (auth.hash_secret("admin-pass-123"), now_iso()))
_conn.execute("INSERT INTO app_secrets VALUES ('guest_code', ?)",
              (auth.hash_secret("guest-code-xyz"),))
_conn.commit()
_conn.close()

# 开启强制登录（生产形态）；测试结束恢复，避免影响其他脚本
app_mod.cfg["capture"]["admin_password"] = "legacy-token-pw"
anon = TestClient(app)
admin = TestClient(app)
guest = TestClient(app)

# --- 未登录：页面 302、API 401 ---
r = anon.get("/", follow_redirects=False)
# v0.9：302 的 location 带 ?next= 回跳参数（/login?next=%2F），改为前缀断言；
# v1.6：本地 config.toml 可配 root_path（构建产物联调），Location 会带上
# 该前缀（生产 Nginx 同形态），按实际前缀校验
_rp = app_mod.cfg["server"]["root_path"]
check("未登录-页面302", r.status_code == 302
      and r.headers["location"].startswith(_rp + "/login"))
check("未登录-API401", anon.get("/api/rooms").status_code == 401)
# v1.6 SPA：登录页发 shell（未构建 503，亦不算被拦截——非 302/401）；
# 旧 /static 已下线，免拦截白名单换 /assets（内容寻址资源，缺文件 404）
check("登录页免拦截", anon.get("/login").status_code in (200, 503))
check("前端资源免拦截", anon.get("/assets/none.js").status_code == 404)

# --- 登录成功/失败 ---
check("登录-admin成功", admin.post("/api/auth/login", json={
    "mode": "admin", "username": "admin", "password": "admin-pass-123"}).status_code == 200)
check("登录-admin密码错", anon.post("/api/auth/login", json={
    "mode": "admin", "username": "admin", "password": "nope"}).status_code == 401)
check("登录-guest成功", guest.post("/api/auth/login", json={
    "mode": "guest", "code": "guest-code-xyz"}).status_code == 200)
check("登录-guest码错", anon.post("/api/auth/login", json={
    "mode": "guest", "code": "bad"}).status_code == 401)
check("登录-模式非法", anon.post("/api/auth/login", json={"mode": "hacker"}).status_code == 400)

# --- 身份 ---
check("me-admin", admin.get("/api/auth/me").json()["role"] == "admin")
check("me-guest", guest.get("/api/auth/me").json()["role"] == "guest")
check("旧通道-X-Admin-Token读API",
      anon.get("/api/rooms", headers={"X-Admin-Token": "legacy-token-pw"}).status_code == 200)
check("旧通道-错误token读API",
      anon.get("/api/rooms", headers={"X-Admin-Token": "bad"}).status_code == 401)

# --- 游客只读：读 OK、写 403 ---
check("游客-读rooms", guest.get("/api/rooms").status_code == 200)
check("游客-写tag403", guest.post("/api/rooms/999001/tags",
      json={"tag": "唱歌"}).status_code == 403)
check("游客-改描述403", guest.patch("/api/rooms/999001/description",
      json={"description": "x"}).status_code == 403)

# --- 管理员：描述 / 标签 ---
r = admin.patch("/api/rooms/999001/description", json={"description": "  唱歌杂谈势  "})
check("描述-PATCH", r.status_code == 200 and r.json()["description"] == "唱歌杂谈势")
check("描述-房间不存在", admin.patch("/api/rooms/1/description",
      json={"description": "x"}).status_code == 404)
check("标签-添加", admin.post("/api/rooms/999001/tags", json={"tag": "歌势"}).status_code == 200)
check("标签-超12字400", admin.post("/api/rooms/999001/tags",
      json={"tag": "一" * 13}).status_code == 400)
check("标签-空400", admin.post("/api/rooms/999001/tags", json={"tag": "  "}).status_code == 400)
for i in range(8):    # 已有 1 个，再补 7 个到 8 个上限
    admin.post("/api/rooms/999001/tags", json={"tag": f"标签{i}"})
check("标签-超8个400", admin.post("/api/rooms/999001/tags", json={"tag": "多"}).status_code == 400)
rooms = admin.get("/api/rooms").json()
row = next(x for x in rooms if x["room_id"] == 999001)
check("标签-列表带出", set(row["tags"]) == {"歌势"} | {f"标签{i}" for i in range(7)}
      and len(row["tags"]) == 8, f"实际 {row['tags']}")
check("标签-删除", admin.delete("/api/rooms/999001/tags/" + "标签0").status_code == 200)
row = next(x for x in admin.get("/api/rooms").json() if x["room_id"] == 999001)
check("标签-删除后7个", len(row["tags"]) == 7 and "标签0" not in row["tags"])
summ = next(x for x in admin.get("/api/summary").json()["rooms"] if x["room_id"] == 999001)
check("描述-summary带出", summ["description"] == "唱歌杂谈势")

# --- 管理员账户管理 ---
check("管理员-添加", admin.post("/api/admins", json={
    "username": "alice", "password": "alice-pass-1"}).status_code == 200)
check("管理员-用户名非法400", admin.post("/api/admins", json={
    "username": "坏|名字", "password": "12345678"}).status_code == 400)
check("管理员-口令过短400", admin.post("/api/admins", json={
    "username": "bob", "password": "123"}).status_code == 400)
check("管理员-重复409", admin.post("/api/admins", json={
    "username": "alice", "password": "12345678"}).status_code == 409)
check("管理员-列表", {a["username"] for a in admin.get("/api/admins").json()["admins"]}
      >= {"admin", "alice"})
check("管理员-游客不可管理", guest.get("/api/admins").status_code == 403)
alice_client = TestClient(app)
alice_client.post("/api/auth/login", json={
    "mode": "admin", "username": "alice", "password": "alice-pass-1"})
check("管理员-新账户可登录", alice_client.get("/api/auth/me").json()["username"] == "alice")
check("管理员-不能删当前账户", alice_client.delete("/api/admins/alice").status_code == 400)
check("管理员-删别人", alice_client.delete("/api/admins/admin").status_code == 200)
check("管理员-不能删最后一个", alice_client.delete("/api/admins/alice").status_code == 400)
r = admin.post("/api/admins/reset-guest-code")
check("游客码-重置", r.status_code == 200 and len(r.json()["guest_code"]) == 12)
check("游客码-旧码失效", anon.post("/api/auth/login", json={
    "mode": "guest", "code": "guest-code-xyz"}).status_code == 401)
check("游客码-新码可用", anon.post("/api/auth/login", json={
    "mode": "guest", "code": r.json()["guest_code"]}).status_code == 200)
check("登出", admin.post("/api/auth/logout").status_code == 200
      and admin.get("/api/auth/me").status_code == 401)   # 登出后匿名访问即 401

# --- 场次标题与分析页（admin 已在上一节登出，先重新登录） ---
admin.post("/api/auth/login", json={
    "mode": "admin", "username": "alice", "password": "alice-pass-1"})
r = admin.get("/api/rooms/999001/sessions").json()["sessions"][0]
check("场次-titles按首见顺序", r["titles"] == "首播标题\n改过的标题", f"实际 {r.get('titles')}")
d = admin.get("/api/rooms/999001/dashboard").json()
check("dashboard-sessions带titles",
      d["sessions"][0]["titles"] == "首播标题\n改过的标题")
r = admin.get("/api/rooms/999001/analysis/overview?days=30").json()
check("分析页-概览字段", r["session_count"] == 1 and r["peak_viewers"] == 0
      and {"total_hours", "danmaku_count", "gift_value", "danmaku_per_session",
           "gift_per_session", "active_users"} <= set(r))
check("分析页-房间不存在404",
      admin.get("/api/rooms/1/analysis/overview").status_code == 404)

# 恢复：关闭强制登录（本地调试模式），后续脚本/人工浏览不受影响
app_mod.cfg["capture"]["admin_password"] = ""


print(chr(10) + "汇总:", "全部通过" if all(results) else f"存在失败（{results.count(False)} 项）")
raise SystemExit(0 if all(results) else 1)
