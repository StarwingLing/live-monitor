# -*- coding: utf-8 -*-
"""replay v0.1 认证与权限测试：三角色会话 / 监控码 / SSO 桥接 / 中间件矩阵。

覆盖：
1. 会话单元：三角色签发/解析、clipper-guest 房间绑定、篡改拒绝；
2. 监控码：格式生成、resolve_code 按码定位房间、错码/畸形码拒绝；
3. SSO 桥接单元：bili admin → admin、bili 旧监控码会话(replay) → 对应
   房间 clipper、bili guest 不映射、无效 Cookie 不映射、密钥缺失静默关闭；
4. 登录 API（生产模式，强制登录门）：三角色登录 / 错误口令 / 错误码 /
   昵称缺失；登录成功种 Cookie 且回跳按码定位房间；
5. 中间件矩阵（生产模式）：
   - 匿名：页面 302 → /login、API 401、/manage 302；
   - guest：本房间页 200、本房间 GET API 200、写方法 403、跨房 API/页/文件
     403 或弹回、/manage 不可达；/api/auth/me 放行；
   - clipper：本房间读写 API 放行（start/stop/切片/注释）、跨房 403、
     录播删除/命名（路由内 admin 校验）403、视频文件本房间可读；
   - admin：全放行，/api/admin/rooms 含监控码字段；
   - 已登录 clipper/guest 访问 /login 弹回自己房间；
6. SSO 桥接端到端：仅持 bili admin Cookie 访问回放页 → 200 且响应自动
   种下 replay_auth；仅持 bili 旧监控码会话 → 映射为对应房间切片员；
7. 登出清除会话。

运行：uv run python tests/test_replay_auth.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

# ---- 先隔离（helpers 已做），再 import 应用 ----
from replay.web import app as app_mod
from replay.web.app import app
from replay import auth

from fastapi.testclient import TestClient

# v2.0.0 React SPA：页面路由发前端构建产物 shell，未构建（frontend/dist
# 不在，如新克隆未跑 npm）时返回 503——页面可达性断言放宽为 200|503；
# 角色语义（视图显隐）已由 /api/auth/me 与写 API 断言覆盖，原 Jinja 模板
# 内容断言（"点击按钮开始录制"等）随服务端渲染一并退场
PAGE_OK = (200, 503)

# ---- 生产模式：启用登录门 + 录像根指向临时目录 ----
app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"
# 存量缺陷修复（v2.3.0 顺带）：本地 config.toml 自 v2.2.5 起 recording
# .enabled=false，起录端点 400——测试须自带开关状态，不随本地配置漂移
app_mod.cfg["recording"]["enabled"] = True
app_mod.RECORDINGS_ROOT = TMP / "recordings"
app_mod.RECORDINGS_ROOT.mkdir(exist_ok=True)
# /recordings 静态挂载在 import 时绑定了真实路径，测试环境替换挂载实例
from fastapi.staticfiles import StaticFiles as _SF
for _route in app.router.routes:
    if getattr(_route, "path", "") == "/recordings":
        _route.app = _SF(directory=str(app_mod.RECORDINGS_ROOT))

# 建表 + 种子（rooms 101/202、admin1/password123、101 在播）
from replay.db import connect
_conn = connect()
H.init_tables(_conn)
_conn.close()

CLIENT_PW = "test-admin-pw"

# ===========================================================================
# 1. 会话单元
# ===========================================================================
class _Req:
    """最小 request 桩（read_session 只读 cookies）。"""
    def __init__(self, cookies):
        self.cookies = cookies


v = auth.sign_session("admin", "admin1")
s = auth.read_session(_Req({"replay_auth": v}))
check("admin 会话往返", s and s["role"] == "admin" and s["username"] == "admin1")

v = auth.sign_session("clipper", "101", nickname="小明")
s = auth.read_session(_Req({"replay_auth": v}))
check("clipper 会话绑定房间+昵称",
      s and s["role"] == "clipper" and s["room_id"] == 101 and s["nickname"] == "小明")

v = auth.sign_session("guest", "202")
s = auth.read_session(_Req({"replay_auth": v}))
check("guest 会话绑定房间", s and s["role"] == "guest" and s["room_id"] == 202)

tampered = v[:-4] + "0000"
check("篡改签名拒绝", auth.read_session(_Req({"replay_auth": tampered})) is None)
check("过期拒绝", auth.read_session(_Req({"replay_auth": auth.sign_session(
    "admin", "x", ttl=-10)})) is None)
check("非法角色拒绝", auth.read_session(_Req({"replay_auth": auth.sign_session(
    "root", "x")})) is None)

# ===========================================================================
# 2. 监控码 + 切片员共用密码
# ===========================================================================
code = auth.gen_replay_code(101)
check("监控码格式 r<房>-<10位>", code.startswith("r101-") and len(code) == 5 + 10)
check("resolve_code 命中", auth.resolve_code("r101-ABCDEF1234") == 101)
check("resolve_code 错尾", auth.resolve_code("r101-ABCDEF1234".replace("A", "Z")) is None)
check("resolve_code 畸形", auth.resolve_code("hello") is None)
check("resolve_code 未知房间", auth.resolve_code("r999-ABCDEF1234") is None)

# 切片员共用密码（v0.1.1）：未播种不通过、设置后恒时校验
check("切片员密码未播种拒绝", auth.verify_clipper_password("x") is False)
auth.set_clipper_password("clipper-pw-123")
check("切片员密码正确校验", auth.verify_clipper_password("clipper-pw-123") is True)
check("切片员密码错误拒绝", auth.verify_clipper_password("wrong") is False)
check("切片员密码空输入拒绝", auth.verify_clipper_password("") is False)

# ===========================================================================
# 3. SSO 桥接单元
# ===========================================================================
class _ReqB(_Req):
    pass


r = _ReqB({"bili_auth": H.sign_bili("admin", "admin1")})
ident = auth.read_bili_identity(r)
check("桥接：bili admin → admin",
      ident and ident["role"] == "admin" and ident["username"] == "admin1")

r = _ReqB({"bili_auth": H.sign_bili("replay", "202", nickname="阿乙")})
ident = auth.read_bili_identity(r)
check("桥接：旧监控码会话 → 对应房间 clipper",
      ident and ident["role"] == "clipper" and ident["room_id"] == 202
      and ident["nickname"] == "阿乙")

r = _ReqB({"bili_auth": H.sign_bili("guest")})
check("桥接：bili guest 不映射", auth.read_bili_identity(r) is None)
r = _ReqB({"bili_auth": "garbage"})
check("桥接：无效 Cookie 不映射", auth.read_bili_identity(r) is None)
r = _ReqB({})
check("桥接：无 Cookie", auth.read_bili_identity(r) is None)

# 桥接签发的本站会话可用
val = auth.bridge_session_value({"role": "clipper", "room_id": 202,
                                 "nickname": "阿乙"})
s = auth.read_session(_Req({"replay_auth": val}))
check("桥接值签发的 clipper 会话可用",
      s and s["role"] == "clipper" and s["room_id"] == 202)

# ===========================================================================
# 4/5. 登录 API 与中间件矩阵（TestClient）
# ===========================================================================
def fresh(cookies: dict | None = None) -> TestClient:
    c = TestClient(app)
    for k, val in (cookies or {}).items():
        c.cookies.set(k, val)
    return c


# ---- 匿名 ----
c = fresh()
r = c.get("/login", follow_redirects=False)
check("匿名登录页可达（SPA shell）", r.status_code in PAGE_OK)
r = c.get("/101/replay", follow_redirects=False)
check("匿名回放页 302 → /login",
      r.status_code == 302 and r.headers["location"].endswith("/login"))
r = c.get("/api/rooms/101/recordings", follow_redirects=False)
check("匿名 API 401", r.status_code == 401)
r = c.get("/manage", follow_redirects=False)
check("匿名 /manage 302 → /login",
      r.status_code == 302 and r.headers["location"].endswith("/login"))

# ---- 登录 API ----
c = fresh()
r = c.post("/api/auth/login", json={"mode": "guest", "code": "bad-code"})
check("错误监控码 401", r.status_code == 401)
r = c.post("/api/auth/login", json={"mode": "admin", "username": "admin1",
                                    "password": "wrong"})
check("管理员错口令 401", r.status_code == 401)
r = c.post("/api/auth/login", json={"mode": "admin", "username": "admin1",
                                    "password": "password123"})
check("管理员免监控码登录 → 管理页",
      r.status_code == 200 and r.json()["redirect"].endswith("/manage")
      and "replay_auth" in c.cookies)
r = c.post("/api/auth/login", json={"mode": "admin", "username": "admin1",
                                    "password": "password123",
                                    "code": "whatever-wrong"})
check("管理员提交监控码也被忽略（全局权限）",
      r.status_code == 200 and r.json()["redirect"].endswith("/manage"))
r = c.post("/api/auth/login", json={"mode": "clipper", "password": "clipper-pw-123",
                                    "nickname": "", "code": "r101-ABCDEF1234"})
check("切片员缺昵称 400", r.status_code == 400)
r = c.post("/api/auth/login", json={"mode": "clipper", "nickname": "歌切工",
                                    "code": "r101-ABCDEF1234"})
check("切片员缺密码 400", r.status_code == 400)
r = c.post("/api/auth/login", json={"mode": "clipper", "password": "wrong",
                                    "nickname": "歌切工", "code": "r101-ABCDEF1234"})
check("切片员错密码 401", r.status_code == 401)
r = c.post("/api/auth/login", json={"mode": "clipper", "password": "clipper-pw-123",
                                    "nickname": "歌切工", "code": "r101-ABCDEF1234"})
check("切片员登录成功（密码+昵称+码）",
      r.status_code == 200 and r.json()["role"] == "clipper")
r = c.post("/api/auth/login", json={"mode": "guest", "code": "r202-GHIJKL5678"})
check("游客登录成功（仅码）", r.status_code == 200 and r.json()["role"] == "guest")
me = c.get("/api/auth/me").json()
check("guest /api/auth/me", me["role"] == "guest" and me["room_id"] == 202)

# ---- guest 矩阵（202）----
g = fresh({"replay_auth": auth.sign_session("guest", "202")})
r = g.get("/202/replay", follow_redirects=False)
check("guest 本房间回放页可达", r.status_code in PAGE_OK)
check("guest 页面不含大按钮", "toggleRecord()" not in r.text.split("<script")[0]
      or 'id="recArea" style="display:none"' in r.text)
r = g.get("/101/replay", follow_redirects=False)
check("guest 跨房页面弹回自己房间",
      r.status_code == 302 and "/202/replay" in r.headers["location"])
r = g.get("/api/rooms/202/auto-recordings")
check("guest 本房间 GET API 200", r.status_code == 200)
r = g.post("/api/rooms/202/recordings/start")
check("guest 写 API 403（只读）",
      r.status_code == 403 and "只读" in r.json()["detail"])
r = g.get("/api/rooms/101/recordings", follow_redirects=False)
check("guest 跨房 API 403", r.status_code == 403)
r = g.get("/api/admin/rooms", follow_redirects=False)
check("guest 管理 API 403", r.status_code == 403)
r = g.get("/manage", follow_redirects=False)
check("guest 管理页弹回自己房间",
      r.status_code == 302 and "/202/replay" in r.headers["location"])
r = g.post("/api/auth/logout")
check("guest 登出", r.status_code == 200)

# ---- clipper 矩阵（101）----
cp = fresh({"replay_auth": auth.sign_session("clipper", "101", nickname="歌切工")})
r = cp.get("/101/replay", follow_redirects=False)
check("clipper 本房间页可达", r.status_code in PAGE_OK)
r = cp.post("/api/rooms/101/recordings/start")
check("clipper 起录 200", r.status_code == 200)
rid = r.json()["id"]
r = cp.post(f"/api/rooms/101/recordings/{rid}/stop")
check("clipper 停录 200（requested → 取消）", r.status_code == 200)
r = cp.get("/api/rooms/202/recordings", follow_redirects=False)
check("clipper 跨房 API 403", r.status_code == 403)
r = cp.delete("/api/rooms/101/recordings/1")
check("clipper 删录播 403（路由内仅 admin）", r.status_code == 403)
r = cp.patch("/api/rooms/101/recordings/1", json={"title": "x"})
check("clipper 改录播名 403（路由内仅 admin）", r.status_code == 403)
r = cp.get("/api/admin/rooms", follow_redirects=False)
check("clipper 管理 API 403", r.status_code == 403)
r = cp.get("/manage", follow_redirects=False)
check("clipper 管理页弹回", r.status_code == 302)
# 视频文件：本房间可读、跨房 403
(app_mod.RECORDINGS_ROOT / "101").mkdir(exist_ok=True)
(app_mod.RECORDINGS_ROOT / "101" / "full_1.mp4").write_bytes(b"0" * 2048)
(app_mod.RECORDINGS_ROOT / "202").mkdir(exist_ok=True)
(app_mod.RECORDINGS_ROOT / "202" / "full_9.mp4").write_bytes(b"0" * 2048)
r = cp.get("/recordings/101/full_1.mp4")
check("clipper 本房间视频可读", r.status_code == 200)
r = cp.get("/recordings/202/full_9.mp4", follow_redirects=False)
check("clipper 跨房视频 403", r.status_code == 403)
r = g.get("/recordings/101/full_1.mp4", follow_redirects=False)
check("guest 跨房视频 403", r.status_code == 403)
r = g.get("/recordings/202/full_9.mp4")
check("guest 本房间视频可读", r.status_code == 200)

# ---- 登录页语义（v0.1.2）：到达即退出所有角色，恒为选择身份表单 ----
# （注：服务端以 Max-Age=0 Set-Cookie 删除会话，真实浏览器即删；
#   httpx 测试端不认空值 Set-Cookie，故后续断言前手动模拟浏览器删 Cookie）
r = cp.get("/login", follow_redirects=False)
check("clipper 到达登录页不再弹回", r.status_code in PAGE_OK)
check("到达登录页清除会话 Cookie",
      "replay_auth=" in (r.headers.get("set-cookie") or ""))
cp.cookies.delete("replay_auth")   # 模拟浏览器执行 Max-Age=0 删除
r = cp.get("/api/rooms/101/recordings", follow_redirects=False)
check("清除后原切片员会话 API 401", r.status_code == 401)
a2 = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
r = a2.get("/login", follow_redirects=False)
check("admin 到达登录页同样清会话",
      r.status_code in PAGE_OK
      and "replay_auth=" in (r.headers.get("set-cookie") or ""))
a2.cookies.delete("replay_auth")
r = a2.get("/api/admin/rooms", follow_redirects=False)
check("清除后原管理员会话 API 401", r.status_code == 401)
# SSO 桥接身份到达登录页：同样清除且不补发新会话（抑制续登）
b3 = fresh({"bili_auth": H.sign_bili("admin", "admin1")})
r = b3.get("/login", follow_redirects=False)
check("桥接身份到达登录页被清除（不续登）",
      r.status_code in PAGE_OK
      and "replay_auth=" in (r.headers.get("set-cookie") or "")
      and "Max-Age=0" in (r.headers.get("set-cookie") or ""))

# ---- admin 矩阵 ----
a = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
r = a.get("/101/replay", follow_redirects=False)
check("admin 任意房间页可达", r.status_code in PAGE_OK)
r = a.get("/manage")
check("admin 管理页可达", r.status_code in PAGE_OK)
r = a.get("/admin", follow_redirects=False)
check("旧路径 /admin 302 → /manage（改名过渡跳转）",
      r.status_code == 302 and r.headers["location"].endswith("/manage"))
r = a.get("/api/admin/rooms")
check("admin 房间列表含监控码",
      r.status_code == 200 and
      any(x["replay_code"] == "r101-ABCDEF1234" for x in r.json()["rooms"]))
# 切片员密码明文回显（v0.1.2 与监控码同口径：管理页随时查看/复制）
auth.set_clipper_password("clipper-pw-123")   # 重设已知值供读取对照
r = a.get("/api/admin/clipper-password")
check("管理页明文读取切片员密码",
      r.status_code == 200 and r.json()["clipper_password"] == "clipper-pw-123")
r = fresh({"replay_auth": auth.sign_session("clipper", "101",
                                            nickname="歌切工")}) \
    .get("/api/admin/clipper-password")
check("切片员禁读密码明文", r.status_code == 403)
r = a.post("/api/rooms/202/auto-full", json={"enabled": True})
check("admin 开启自动全程录制", r.status_code == 200)
_conn = connect()
_row = _conn.execute("SELECT auto_full FROM rooms WHERE room_id=202").fetchone()
_conn.close()
check("auto_full 落库", _row[0] == 1)

# 切片员共用密码重置（v0.1.1）：新码一次性返回、旧码立即失效、非 admin 403
r = a.post("/api/admin/clipper-password/reset")
check("admin 重置切片员密码", r.status_code == 200 and bool(r.json()["clipper_password"]))
_newpw = r.json()["clipper_password"]
check("重置后旧密码失效", auth.verify_clipper_password("clipper-pw-123") is False)
check("重置后新密码生效", auth.verify_clipper_password(_newpw) is True)
r = fresh({"replay_auth": auth.sign_session("clipper", "101",
                                            nickname="歌切工")}) \
    .post("/api/admin/clipper-password/reset")
check("切片员禁重置切片员密码", r.status_code == 403)

# ---- SSO 桥接端到端 ----
b = fresh({"bili_auth": H.sign_bili("admin", "admin1")})
r = b.get("/101/replay", follow_redirects=False)
check("桥接：bili admin 直进回放页可达",
      r.status_code in PAGE_OK and "replay_auth" in b.cookies)
me = b.get("/api/auth/me").json()
check("桥接：续登身份为 admin", me["role"] == "admin" and me["username"] == "admin1")
b2 = fresh({"bili_auth": H.sign_bili("replay", "101", nickname="老切片")})
r = b2.get("/101/replay", follow_redirects=False)
check("桥接：旧监控码会话映射切片员进页", r.status_code in PAGE_OK)
me = b2.get("/api/auth/me").json()
check("桥接：映射为 clipper+昵称",
      me["role"] == "clipper" and me["room_id"] == 101 and me["nickname"] == "老切片")

# ---- oss-url：OSS 未启用 404（存量缺陷修复 v2.3.0 顺带：本地 config.toml
# 可能已开只读 OSS（联调用），测试须自带开关状态，不随本地配置漂移）----
app_mod.cfg["oss"]["enabled"] = False
r = a.get("/api/rooms/101/oss-url?key=recordings/101/x.mp4")
check("OSS 未启用 404", r.status_code == 404)

# ===========================================================================
# 6. 本地调试模式语义（v0.1.2 统一权限门）：无会话 → 注入匿名管理员；
#    有会话 → 按真实角色校验（身份切换可本地测试）
# ===========================================================================
app_mod.cfg["capture"]["admin_password"] = ""   # 临时切调试模式
d_anon = fresh()
r = d_anon.get("/101/replay", follow_redirects=False)
check("调试匿名回放页可达（管理员由 me 断言）",
      r.status_code in PAGE_OK)
me = d_anon.get("/api/auth/me").json()
check("调试匿名 me = admin (local)",
      me["role"] == "admin" and me["username"] == "(local)")
r = d_anon.post("/api/rooms/202/recordings/start")
check("调试匿名可写（管理员兜底）", r.status_code == 200)

d_guest = fresh({"replay_auth": auth.sign_session("guest", "202")})
r = d_guest.get("/202/replay", follow_redirects=False)
check("调试模式游客视图生效（只读由写 API 断言）",
      r.status_code in PAGE_OK)
r = d_guest.post("/api/rooms/202/recordings/start")
check("调试模式游客写 API 403", r.status_code == 403)
r = d_guest.get("/manage", follow_redirects=False)
check("调试模式游客管理页弹回", r.status_code == 302)

d_clip = fresh({"replay_auth": auth.sign_session("clipper", "101", nickname="本地切")})
r = d_clip.get("/api/rooms/202/recordings", follow_redirects=False)
check("调试模式切片员跨房 403", r.status_code == 403)
r = d_clip.get("/101/replay", follow_redirects=False)
check("调试模式切片员本房间可达（可写视图）",
      r.status_code in PAGE_OK)

app_mod.cfg["capture"]["admin_password"] = CLIENT_PW   # 还原生产模式

# ---- 旧哈希格式迁移（v0.1.2 切片员密码改明文存储）----
_conn = connect()
_conn.execute("UPDATE app_secrets SET value = 'pbkdf2_sha256$120000$ab$cd' "
              "WHERE key = 'clipper_password'")
_conn.commit()
_conn.close()
_mig = auth.seed_clipper_password()
check("旧哈希格式启动时自动迁移为明文",
      _mig is not None and auth.verify_clipper_password(_mig))

sys.exit(H.summary("test_replay_auth"))
