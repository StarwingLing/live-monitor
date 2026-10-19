# -*- coding: utf-8 -*-
"""FastAPI 应用（replay 项目）：三角色登录 + 回放页 + 管理页。

与 bili-monitor/web/app.py 的分工（v2.2 架构改版后）：
- 本项目承载「采集 + 录制/切片执行 + 转码上云管线 + 回放」全动线：
  统一登录页（管理员/切片员/游客 + 监控码）、回放页 /<房间号>/replay、
  管理页 /manage（监控码 + 切片员密码 + 自动全程录制开关）、回放 API
  （录制列表/启停/删除/命名/注释、打点切片、直播截取、归档分组、外挂
  弹幕、OSS 签名直链）、capture 采集子包（replay.capture.main）与
  转码+上云+释放管线（web/pipeline.py，lifespan 内 daemon 线程）；
- bili-monitor 仅保留监控数据页（只读分析 + 房间/账户管理），与其共享
  本项目 data/ 下的同一 SQLite 与录像目录（见 replay/db.py；监控类表
  DDL 两侧锁步维护）。

角色模型（v0.1 定稿，v0.1.1 增补切片员密码，v0.1.2 管理员免监控码）：
- admin   管理员：账号密码 = bili-monitor 的 admin_users（两端一体）；
          全局权限、可管理所有房间，登录**无需监控码**，登录后进入管理页
          （监控码生成/重置、切片员密码重置、自动全程录制开关）；
- clipper 切片员：切片员共用密码（全员一个，仅作身份验证）+ 昵称（切片/
          注释署名）+ 监控码登录，绑定该房间；可起停录制、直播截取、
          打点切片、注释（能力集合 = 原 bili-monitor 监控码会话）；
- guest   游客：仅监控码登录，绑定该房间，只读（浏览归档/播放/查切片/
          看注释）；
- 监控码是切片员/游客的必填项（登录页公共输入框），按码进入对应主播的
  回放页——码是「入场凭证 + 房间导航」，实际能力由所选角色决定。

SSO 桥接：middleware 验签 bili-monitor 的会话 Cookie（读其密钥文件）——
bili 管理员点总览页「录制」跳转 /replay/<房>/replay 后自动续登为管理员；
旧监控码会话自动映射为对应房间切片员（存量用户零重登）。

启动：uv run uvicorn replay.web.app:app --host 127.0.0.1 --port 8003
      （部署时 config [server].root_path = /replay，Nginx 前缀反代）
"""
import asyncio
import datetime as _dt
import json
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from pydantic import BaseModel

from .. import auth
from ..config import DATA_DIR, get_config
from ..db import connect, init_db
from ..capture.recording import RECORDINGS_ROOT
from . import pipeline as pipeline_mod
from . import restore as restore_mod
from . import upload as upload_mod
from . import bili_api
from . import submit_sync
from .ass import build_ass

cfg = get_config()


def now_iso() -> str:
    """当前本地时间（服务器时区）ISO8601 秒级（与 bili 侧写入格式一致）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# OSS 客户端（只读用途：签名直链 + 删除联动；上传/转码/归档/释放管线
# 在本服务进程内的 web/pipeline.py，v2.2 起 capture 回灌也复用其内网客户端）
# ---------------------------------------------------------------------------
def oss_enabled() -> bool:
    """OSS 总开关（本项目 config [oss] 且 bucket/AK 齐备）。"""
    o = cfg["oss"]
    return bool(o.get("enabled") and o.get("bucket")
                and o.get("access_key_id") and o.get("access_key_secret"))


_OSS_PUB_CLIENT: list = []      # 懒加载缓存（[None] 表示初始化失败）


def _oss_public_client():
    """懒加载公网 endpoint 客户端（签名 URL 给浏览器，内网域名不可解析）。
    初始化失败返回 None（调用方 503，页面回落「云端不可用」占位）。"""
    if _OSS_PUB_CLIENT:
        return _OSS_PUB_CLIENT[0]
    o = cfg["oss"]
    try:
        import alibabacloud_oss_v2 as oss
        c = oss.config.load_default()
        c.region = o.get("region", "cn-hangzhou")
        c.endpoint = f"oss-{c.region}.aliyuncs.com"
        c.credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=o["access_key_id"],
            access_key_secret=o["access_key_secret"])
        _OSS_PUB_CLIENT.append(oss.Client(c))
    except Exception:
        _OSS_PUB_CLIENT.append(None)
    return _OSS_PUB_CLIENT[0]


def _oss_delete(key: str, bucket: str = "") -> None:
    """删除云端对象（尽力而为，失败静默——用户主动删除的联动清理）。
    每次新建内网客户端（删除是低频管理操作，不值得常驻连接）。
    bucket 空串 = 主桶；切片对象可能在其 oss_bucket 独立桶（v1.4 双
    bucket），须显式传入。"""
    o = cfg["oss"]
    if not oss_enabled():
        return
    try:
        import alibabacloud_oss_v2 as oss
        c = oss.config.load_default()
        c.region = o.get("region", "cn-hangzhou")
        c.endpoint = o.get("endpoint") or f"oss-{c.region}.aliyuncs.com"
        c.credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=o["access_key_id"],
            access_key_secret=o["access_key_secret"])
        client = oss.Client(c)
        client.delete_object(oss.models.object_basic.DeleteObjectRequest(
            bucket=bucket or o["bucket"], key=key))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 查询原语（共享库；表未建 → 503 引导先部署 bili-monitor）
# ---------------------------------------------------------------------------
def q(sql: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        conn.row_factory = None
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise HTTPException(503, "共享数据库未初始化：请先启动 "
                                     "replay-capture / replay-web（表结构由本侧创建）")
        raise
    finally:
        conn.close()


def q1(sql: str, params: tuple = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 会话解析（含 SSO 桥接结果的统一出口）与权限校验
# ---------------------------------------------------------------------------
def current_session(request: Request) -> dict | None:
    """当前会话：middleware 桥接注入的优先，否则解析本站 Cookie。

    返回 {role, username[, room_id, nickname]}；未登录 None。"""
    s = getattr(request.state, "session", None)
    if s is not None:
        return s
    return auth.read_session(request)


def check_admin(request: Request, password: str | None = None) -> None:
    """写操作权限校验：管理员会话，或旧通道（Header X-Admin-Token / 请求体
    token）与 bili config capture.admin_password 匹配（兼容运维脚本）。
    本地调试模式的匿名请求经 middleware 注入管理员会话后由此放行。"""
    s = current_session(request)
    if s is not None and s.get("role") == "admin":
        return
    expect = cfg["capture"]["admin_password"]
    token = request.headers.get("X-Admin-Token") or password
    if expect and token and token == expect:
        return
    raise HTTPException(403, "需要管理员权限（请用管理员账号登录）")


def note_author(request: Request) -> str | None:
    """当前会话在注释/切片体系里的署名。

    admin=用户名、clipper=登录昵称；返回 None 表示没有可署名身份
    （guest 只读本就写不了；旧会话未绑昵称 → 前端提示重新登录）。
    本地调试模式的匿名请求经 middleware 注入 username='(local)' 的
    管理员会话，署名即 '(local)'，保持本地全功能开发。"""
    s = current_session(request)
    if s is not None:
        if s["role"] == "clipper":
            return s.get("nickname") or None
        if s["role"] == "admin":
            return s.get("username") or "(admin)"
    token = request.headers.get("X-Admin-Token") or ""
    if token and token == cfg["capture"]["admin_password"]:
        return "(token)"
    return None


# ---------------------------------------------------------------------------
# 服务实例（lifespan：启动钩子）
# ---------------------------------------------------------------------------
def _ensure_replay_rooms() -> None:
    """v1.3.x 房间表分离：建 replay 自有名单表 replay_rooms。

    与共享 rooms（monitor/capture 的监控名单）互不影响：replay 的停用/
    删除/添加只写本表；监控码（replay_code）随表迁移为本表字段。表首次
    创建时从共享 rooms 全量播种一次（含监控码与停用态，切换瞬间零行为
    变化）；表已存在则不重播——之后两侧名单各自演进，重启不再对齐。"""
    conn = connect()
    try:
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='replay_rooms'").fetchone()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS replay_rooms (
                room_id     INTEGER PRIMARY KEY,
                short_id    INTEGER NOT NULL DEFAULT 0,
                anchor_name TEXT NOT NULL DEFAULT '',
                avatar_url  TEXT NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                replay_code TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT ''
            )
        """)
        if not existed:
            cur = conn.execute(
                "INSERT INTO replay_rooms (room_id, short_id, anchor_name, "
                "avatar_url, enabled, replay_code, created_at) "
                "SELECT room_id, short_id, anchor_name, avatar_url, enabled, "
                "replay_code, created_at FROM rooms")
            print(f"[replay] 房间表分离：首次创建 replay_rooms，"
                  f"自共享 rooms 播种 {cur.rowcount} 行（此后两表独立维护）")
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务启动钩子（v2.2 起本服务为数据属主）：建表（幂等 init_db）→
    确保录像目录存在 → 建 replay_rooms 自有名单表（首次从共享 rooms 播种）
    → 播种切片员共用密码（幂等）→ 启动转码+上云管线 daemon 线程
    （先睡满扫描周期再扫）。共享库不可用时打告警日志（接口层统一 503，
    不阻断启动）。"""
    RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
    upload_mod.TMP_DIR.mkdir(parents=True, exist_ok=True)   # 投稿回拉/封面临时区
    try:
        init_db()
        _ensure_replay_rooms()
        pw = auth.seed_clipper_password()
        if pw:
            print(f"[replay] 已生成切片员共用密码（明文见本项目 data/"
                  f"web_credentials.txt，请妥善保存）")
    except sqlite3.OperationalError as e:
        print(f"[replay] 警告：共享数据库不可用（{e}）——"
              "请检查 data/live.db 所在目录与权限")
    pipeline_mod.start()
    submit_sync.start()     # v2.3.0 投稿记录长周期同步（核验+回填；未部署时不启动）
    yield


app = FastAPI(
    title="replay",
    description="B站直播录播回放站 API",
    # 部署在 Nginx 路径前缀之后时（/replay/），root_path 让生成的路径正确
    root_path=cfg["server"]["root_path"],
    lifespan=lifespan,
)
# 接口响应 gzip 压缩（归档/弹幕类 JSON 压缩比高，公网传输大幅下降；
# 沿用 bili 侧 v0.8 方案：仅压 ≥1KB 响应）
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


# ---------------------------------------------------------------------------
# 全站 middleware：登录门（三角色 + SSO 桥接自动续登）+ API 禁缓存
# ---------------------------------------------------------------------------
# 免登录路径：登录页 / 登录接口 / 登出（清 Cookie 无需校验）/ 前端构建产物
#（v2.0.0 React SPA：/assets/ 为 Vite 内容寻址文件名，免登录 + 可缓存）
_PUBLIC_PATHS = ("/login", "/api/auth/login", "/api/auth/logout")
_PUBLIC_PREFIXES = ("/assets/",)

# 回放页路由（动态路径，正则匹配）：/<房间号>/replay
_PAGE_RE = re.compile(r"^/(\d+)/replay$")
# 管理页与管理 API（仅管理员）；页面路由 /manage 与 bili-monitor 管理页
# 同名（页面地址格式保持一致），API 前缀 /api/admin/* 为本项目内部命名空间
_ADMIN_PAGE_RE = re.compile(r"^/manage$")
# clipper/guest 可用的 API 前缀（均限本房间，房间号进捕获组）：
# /recordings*        录制列表 / 启停 / 删除 / 命名 / 注释 / 弹幕 / 切片创建 / 直播截取
# /auto-recordings    归档日期板块数据
# /oss-url            OSS 签名直链
# /clips/<id>（精确） 切片改名/删除与切片注释；v2.0.0 起切片弹幕（GET，
#                     /clips/<id>/danmaku）与投稿轮询（GET submit/<job>）
#                     同样精确放行（POST submit 属写方法，guest 仍被拦）
_API_ROOM_RE = re.compile(r"^/api/rooms/(\d+)/(recordings|auto-recordings|oss-url)(/|$)")
_API_CLIP_RE = re.compile(r"^/api/rooms/(\d+)/clips/\d+(/notes.*|/danmaku|/cover-preview|/submit(/\w+)?|/release-local)?$")
# v2.3.0 投稿目标数据（账号合集 / 稿件列表）：账号级而非房间级——clipper
# 放行（开投稿抽屉要用）、guest 一律 403（投稿账号信息不对游客暴露）
_API_SUBMIT_RE = re.compile(r"^/api/submit(/|$)")
# 房间绑定角色唯一可访问的视频文件前缀（自己房间的成片）
_FILE_RE = re.compile(r"^/recordings/(\d+)/")


def _rel_path(request: Request) -> str:
    """剥掉 root_path 前缀后的应用内路径（本地开发 root_path 为空串，原样）。"""
    rp = cfg["server"]["root_path"]
    p = request.url.path
    if rp and p.startswith(rp):
        p = p[len(rp):]
    return p


@app.middleware("http")
async def auth_gate_and_no_cache(request: Request, call_next):
    """统一拦截（生产与调试共用同一权限门）：

    0. 会话解析——本站 Cookie → SSO 桥接（无本站会话但携带有效
       bili-monitor 会话时映射身份并在响应上自动补发本站 Cookie：admin
       续登为 admin；旧监控码会话映射为对应房间 clipper）；
    0.5 本地调试兜底（admin_password 未配置）：**无任何会话**时注入匿名
       管理员（保持"打开就能用"的本地开发体验）；**有会话则按会话角色
       正常走门**——本地登录切片员/游客即可真实测试对应视图与权限；
    1. 免登录路径放行；**登录页特殊语义：到达即退出**——清除本站会话
       Cookie、抑制 SSO 桥接续登，所有用户在此页面均为"已退出所有角色、
       选择身份登录"状态（v0.1.2 定稿，不再弹回已登录用户）；
    2. 管理页 /manage 与 /api/admin/*：仅 admin（其余已登录角色 403 /
       弹回；未登录去登录页）；
    3. 回放页：未登录 → /login；房间绑定角色访问别的房间 → 弹回自己的；
    4. API / 录像文件：clipper 按白名单前缀（限本房间）读写；guest 同
       白名单但仅 GET/HEAD（只读）；跨房一律 403；
    5. 除 /static 外一律禁缓存（数据实时变化 + 页面内联 JS 随版本更新）。
    """
    pending_cookie: str | None = None

    def respond(resp):
        """出口统一：桥接补发会话 Cookie + 禁缓存头。"""
        if pending_cookie:
            resp.set_cookie(auth.COOKIE_NAME, pending_cookie,
                            max_age=auth.SESSION_TTL, httponly=True,
                            samesite="lax")
        if not request.url.path.startswith(("/static", "/assets")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- 会话解析：本站 Cookie → SSO 桥接 → 调试模式匿名管理员兜底 ----
    session = auth.read_session(request)
    if session is None:
        ident = auth.read_bili_identity(request)
        if ident is not None:
            if ident["role"] == "admin":
                session = {"role": "admin", "username": ident.get("username", "")}
            else:
                session = {"role": "clipper", "username": str(ident["room_id"]),
                           "room_id": ident["room_id"],
                           "nickname": ident.get("nickname", "")}
            request.state.session = session
            pending_cookie = auth.bridge_session_value(ident)
    if session is None and not cfg["capture"]["admin_password"]:
        # 本地调试模式且完全未登录：注入匿名管理员（不落 Cookie，仅本请求
        # 生效）；有会话（本站登录/桥接）时不注入，按真实角色校验
        session = {"role": "admin", "username": "(local)"}
        request.state.session = session

    def is_public(p: str) -> bool:
        return p in _PUBLIC_PATHS or any(p.startswith(x) for x in _PUBLIC_PREFIXES)

    rp = cfg["server"]["root_path"]
    p = _rel_path(request)
    if is_public(p):
        if p == "/login":
            # 登录页语义（v0.1.2 用户定稿）：到达本页即处于「已退出所有
            # 角色」的干净状态——清除本站会话 Cookie、抑制 SSO 桥接续登
            # （否则刚清掉又被 bili 会话补发），对三角色一致；页面恒为
            # 选择身份登录表单，不再弹回已登录用户
            session = None
            pending_cookie = None
            resp = await call_next(request)
            resp.delete_cookie(auth.COOKIE_NAME)
            return respond(resp)
        return respond(await call_next(request))

    m_page = _PAGE_RE.match(p)
    m_admin = _ADMIN_PAGE_RE.match(p) or p.startswith("/api/admin")
    role = session["role"] if session else None

    if m_admin:
        # ---- 管理页 / 管理 API：仅管理员 ----
        if role == "admin":
            response = await call_next(request)
        elif session is not None:
            # 切片员/游客：API 403；页面弹回自己的回放页
            if p.startswith("/api"):
                return respond(JSONResponse(
                    {"detail": "管理页仅管理员可访问"}, status_code=403))
            return respond(RedirectResponse(
                f"{rp}/{session['room_id']}/replay", status_code=302))
        elif p.startswith("/api"):
            return respond(JSONResponse({"detail": "未登录"}, status_code=401))
        else:
            return respond(RedirectResponse(f"{rp}/login", status_code=302))
    elif m_page:
        # ---- 回放页 ----
        rid = int(m_page.group(1))
        if session is None:
            return respond(RedirectResponse(f"{rp}/login", status_code=302))
        if role in ("clipper", "guest") and rid != session["room_id"]:
            return respond(RedirectResponse(
                f"{rp}/{session['room_id']}/replay", status_code=302))
        response = await call_next(request)
    elif session is None:
        # ---- 其余受保护路径：未登录 ----
        if p.startswith("/api"):
            return respond(JSONResponse({"detail": "未登录"}, status_code=401))
        return respond(RedirectResponse(f"{rp}/login", status_code=302))
    elif role in ("clipper", "guest"):
        # ---- 房间绑定角色：白名单前缀 + 房间隔离 ----
        own = session["room_id"]
        if p.startswith("/api"):
            if p == "/api/auth/me":
                response = await call_next(request)   # 身份查询对所有会话开放
            elif _API_SUBMIT_RE.match(p):
                if role == "guest":
                    # 游客只读也不放行：合集/稿件列表属投稿账号信息
                    return respond(JSONResponse(
                        {"detail": "游客无投稿权限"}, status_code=403))
                response = await call_next(request)
            else:
                m_api = _API_ROOM_RE.match(p) or _API_CLIP_RE.match(p)
                if not (m_api and int(m_api.group(1)) == own):
                    return respond(JSONResponse(
                        {"detail": "当前会话仅可访问自己房间的回放接口"},
                        status_code=403))
                if role == "guest" and request.method not in ("GET", "HEAD"):
                    return respond(JSONResponse(
                        {"detail": "游客仅可浏览（只读）"}, status_code=403))
                response = await call_next(request)
        else:
            m_file = _FILE_RE.match(p)
            if m_file:
                if int(m_file.group(1)) != own:
                    return respond(JSONResponse(
                        {"detail": "无权访问其他房间的录像"}, status_code=403))
                response = await call_next(request)
            else:
                return respond(RedirectResponse(
                    f"{rp}/{own}/replay", status_code=302))
    else:
        # ---- admin：全放行 ----
        response = await call_next(request)
    return respond(response)


# ---------------------------------------------------------------------------
# 登录 / 登出 / 身份（三角色统一登录页）
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    mode: str                       # admin / clipper / guest
    username: str | None = None     # admin 模式：用户名
    password: str | None = None     # admin 模式：口令；clipper 模式：切片员共用密码
    nickname: str | None = None     # clipper 模式：署名昵称
    code: str = ""                  # 切片员/游客必填：监控码（决定进入哪个房间）


@app.post("/api/auth/login")
async def login(body: LoginIn):
    """统一登录：三角色共用一个端点。

    - admin：账号密码（共享 bili-monitor 的 admin_users）即可——管理员是
      全局权限、可管理所有房间，**无需监控码**，登录后进入管理页；
    - clipper：切片员共用密码（全员一个，仅作身份验证）+ 昵称（切片/
      注释署名）+ 监控码；
    - guest：仅监控码（只读进入）。
    clipper/guest 的监控码必填并决定回跳房间（按码进入对应主播的回放页）；
    成功签发 30 天 HttpOnly 签名 Cookie 并返回回跳地址；码/密码错误 401
    （不区分"房间不存在/码错误"）。"""
    rp = cfg["server"]["root_path"]

    if body.mode == "admin":
        row = q1("SELECT password_hash FROM admin_users WHERE username = ?",
                 (body.username or "",))
        if not row or not auth.verify_secret(body.password or "",
                                             row["password_hash"]):
            raise HTTPException(401, "账号或密码错误")
        value = auth.sign_session("admin", body.username)
        resp = JSONResponse({"ok": True, "role": "admin",
                             "redirect": f"{rp}/manage"})
    elif body.mode in ("clipper", "guest"):
        # 房间绑定角色：监控码必填（入场凭证 + 房间导航）
        room_id = auth.resolve_code((body.code or "").strip())
        if room_id is None:
            raise HTTPException(401, "监控码错误或房间不存在")
        if body.mode == "clipper":
            # 切片员共用密码：仅作身份验证（全员一个），先于昵称校验——
            # 密码不对时不必关心署名
            if not (body.password or "").strip():
                raise HTTPException(400, "请填写切片员密码（由管理员提供）")
            if not auth.verify_clipper_password(body.password.strip()):
                raise HTTPException(401, "切片员密码错误")
            nick = (body.nickname or "").strip()
            if not nick:
                raise HTTPException(400, "请填写昵称（切片/注释的署名，登录后不可更改）")
            if len(nick) > auth.NICK_MAX:
                raise HTTPException(400, f"昵称最长 {auth.NICK_MAX} 个字符")
            value = auth.sign_session("clipper", str(room_id), nickname=nick)
        else:
            value = auth.sign_session("guest", str(room_id))
        resp = JSONResponse({"ok": True, "role": body.mode,
                             "room_id": room_id,
                             "redirect": f"{rp}/{room_id}/replay"})
    else:
        raise HTTPException(400, "mode 必须为 admin / clipper / guest")

    resp.set_cookie(auth.COOKIE_NAME, value, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def logout():
    """登出：清除本站会话 Cookie。

    注意（SSO 语义）：若浏览器仍持有有效的 bili-monitor 管理员会话，
    下次访问会经桥接自动续登——如需彻底退出请同时在 bili-monitor 总览页
    登出。切片员/游客不受影响（bili 侧无对应会话）。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    """当前会话身份（SPA 启动时拉取：角色显隐 + 署名）。本地调试模式的匿名
    请求经 middleware 注入管理员会话，返回 admin/(local)。

    v2.0.0：响应附带 me（注释/切片署名，与 note_author 同口径：admin=
    用户名、clipper=昵称、未绑昵称/游客为 null）——原 Jinja 模板注入的
    me/is_admin/can_write 改由此单一接口派生。"""
    s = current_session(request)
    if s is not None:
        return {**s, "me": note_author(request)}
    return {"role": None, "username": None, "me": None}


# ---------------------------------------------------------------------------
# 管理页 API（仅管理员；middleware 已拦，路由内 check_admin 双保险）
# ---------------------------------------------------------------------------
@app.get("/api/admin/rooms")
async def admin_rooms(request: Request):
    """管理页房间列表：replay 自有名单（replay_rooms）+ 在播状态 +
    自动全程录制开关（共享 rooms，capture 侧消费）+ 监控码。

    monitored = 该房间同时在 monitor 监控名单且监控中（enabled=1）——
    未监控的房间无法录制（capture 只为监控名单建房），前端据此降级
    自动录制开关并标注「未监控」。仅管理员端点（响应含明文监控码）；
    管理页每 30s 轮询刷新。"""
    check_admin(request)
    rows = q("""
        SELECT rr.room_id, rr.anchor_name, rr.avatar_url, rr.enabled,
               rr.replay_code,
               COALESCE(b.auto_full, 0) AS auto_full,
               CASE WHEN b.room_id IS NOT NULL AND b.enabled = 1
                    THEN 1 ELSE 0 END AS monitored,
               EXISTS(SELECT 1 FROM live_sessions s
                      WHERE s.room_id = rr.room_id AND s.end_time IS NULL) AS live
        FROM replay_rooms rr
        LEFT JOIN rooms b ON b.room_id = rr.room_id
        ORDER BY rr.room_id
    """)
    for r in rows:
        r["auto_full"] = bool(r["auto_full"])
        r["monitored"] = bool(r["monitored"])
        r["live"] = bool(r["live"])
    return {"rooms": rows}


class AutoFullToggleIn(BaseModel):
    enabled: bool


@app.post("/api/rooms/{room_id}/auto-full")
async def toggle_auto_full(room_id: int, body: AutoFullToggleIn,
                           request: Request):
    """自动全程录制开关（共享 rooms.auto_full，capture 侧消费）：管理页
    的开关（自 bili-monitor 总览页迁入）。capture（bili 侧）的 30s 对账
    循环感知：在播时打开 → 下一轮对账排队自动录制；关闭 → 不动进行中
    的录制，只是下次开播不再自动起录。开关只对 monitor 监控中的房间
    有意义（capture 只为监控名单建房）。"""
    check_admin(request)
    if not q1("SELECT room_id FROM rooms "
              "WHERE room_id = ? AND enabled = 1", (room_id,)):
        raise HTTPException(404, "房间未在 monitor 监控名单（或监控已停用），"
                                 "无自动全程录制")
    conn = connect()
    try:
        cur = conn.execute("UPDATE rooms SET auto_full = ? WHERE room_id = ?",
                           (1 if body.enabled else 0, room_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "房间不在监控名单中")
    finally:
        conn.close()
    return {"ok": True, "auto_full": body.enabled}


@app.post("/api/rooms/{room_id}/replay-code/reset")
async def reset_replay_code(room_id: int, request: Request):
    """生成/重置该房间的监控码：明文存 replay_rooms.replay_code（本站
    自有名单表，管理页可随时查看/复制），同时追加写 bili 侧凭据文件备份。
    旧码立即失效（已登录的会话不受影响——签名自包含）。"""
    check_admin(request)
    if not q1("SELECT room_id FROM replay_rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不在回放站名单中")
    code = auth.gen_replay_code(room_id)
    conn = connect()
    try:
        conn.execute("UPDATE replay_rooms SET replay_code = ? WHERE room_id = ?",
                     (code, room_id))
        conn.commit()
    finally:
        conn.close()
    auth.record_replay_code(room_id, code)
    return {"ok": True, "room_id": room_id, "replay_code": code}


# ---------------------------------------------------------------------------
# 房间名单增删（v2.0.0：项目独立后 replay 自管；v1.3.x 房间表分离后只写
# replay_rooms 自有表——停用/删除/添加均不影响 monitor（capture）的监控
# 名单 rooms。录制仍由 capture 执行，故未监控的房间无法起录，录制请求
# 端点会给出明确指引）
# ---------------------------------------------------------------------------
class RoomAddIn(BaseModel):
    room_id: int


# B站房间信息接口：getInfoByRoom 一次返回 room_info + anchor_info.base_info
# （匿名请求会被风控 -352 拦，带共享 cookie 即可过）；Room/get_info 与
# Master/info 为匿名可用的兜底端点（两步各取房间/主播）
_BILI_ROOM_API = ("https://api.live.bilibili.com/xlive/web-room/v1/"
                  "index/getInfoByRoom")
_BILI_ROOM_INFO_API = ("https://api.live.bilibili.com/room/v1/"
                       "Room/get_info")
_BILI_MASTER_API = ("https://api.live.bilibili.com/live_user/v1/"
                    "Master/info")
_BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _bili_json(url: str, cookies: dict | None = None) -> dict | None:
    """GET 一个 B站 JSON 接口；成功（code=0）返回 data 字典，失败 None。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _BILI_UA})
    if cookies:
        req.add_header("Cookie",
                       "; ".join(f"{k}={v}" for k, v in cookies.items()))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return data.get("data") if data.get("code") == 0 else None


def _load_bili_cookies() -> dict:
    """读本侧 capture 的运行时 cookie（data/cookies.json：buvid /
    bili_ticket 等保活刷新键，v2.2 起随数据目录归本项目）供 getInfoByRoom
    防风控；缺失/损坏返回空。"""
    try:
        p = DATA_DIR / "cookies.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def _fetch_bili_room_sync(room_id: int) -> dict | None:
    """拉取房间与主播信息（不依赖 bili 侧代码）。

    返回 {room_id(短号已解析的真实号), short_id, anchor_name, avatar_url}；
    房间不存在 / 接口异常返回 None。两级策略：
    ① getInfoByRoom（一次拿全，带共享 cookie 防风控）；
    ② 匿名兜底 Room/get_info（房间校验+短号解析）+ Master/info（主播）。"""
    d = _bili_json(f"{_BILI_ROOM_API}?room_id={room_id}",
                   _load_bili_cookies())
    if d:
        ri = d.get("room_info") or {}
        base = ((d.get("anchor_info") or {}).get("base_info")) or {}
        rid = ri.get("room_id")
        if rid:
            return {"room_id": rid, "short_id": ri.get("short_id") or 0,
                    "anchor_name": base.get("uname", ""),
                    "avatar_url": base.get("face", "")}
    d = _bili_json(f"{_BILI_ROOM_INFO_API}?room_id={room_id}")
    if not d:
        return None
    rid, uid = d.get("room_id"), d.get("uid")
    if not rid:
        return None
    uname = face = ""
    if uid:
        m = _bili_json(f"{_BILI_MASTER_API}?uid={uid}")
        if m:
            info = m.get("info") or {}
            uname, face = info.get("uname", ""), info.get("face", "")
    return {"room_id": rid, "short_id": d.get("short_id") or 0,
            "anchor_name": uname, "avatar_url": face}


async def _fetch_bili_room(room_id: int) -> dict | None:
    """异步包装（阻塞 IO 放线程池，避免卡事件循环）。"""
    return await asyncio.to_thread(_fetch_bili_room_sync, room_id)


@app.post("/api/admin/rooms")
async def admin_add_room(body: RoomAddIn, request: Request):
    """添加回放站房间：调 B站接口校验房间号并取主播昵称/头像（短号自动
    解析为真实房间号），写入 replay_rooms（enabled=1）并同步生成监控码
    ——添加即用，无需再点「生成监控码」。只写本站自有名单表：如需录制
    （自动/手动），还需在 bili-monitor 管理页把房间加入监控。"""
    check_admin(request)
    rid = body.room_id
    if rid <= 0:
        raise HTTPException(422, "房间号须为正整数")
    if q1("SELECT room_id FROM replay_rooms WHERE room_id = ?", (rid,)):
        raise HTTPException(409, "房间已在回放站名单中")
    info = await _fetch_bili_room(rid)
    if info is None:
        raise HTTPException(404, "房间不存在或查询失败（稍后重试）")
    if info["room_id"] != rid and \
            q1("SELECT room_id FROM replay_rooms WHERE room_id = ?",
               (info["room_id"],)):
        raise HTTPException(409, "房间已在回放站名单中（该短号对应房间已添加）")
    code = auth.gen_replay_code(info["room_id"])
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO replay_rooms (room_id, short_id, anchor_name, "
            "avatar_url, enabled, replay_code, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (info["room_id"], info["short_id"], info["anchor_name"],
             info["avatar_url"], code, now_iso()))
        conn.commit()
    finally:
        conn.close()
    auth.record_replay_code(info["room_id"], code)
    return {"ok": True, "room_id": info["room_id"],
            "anchor_name": info["anchor_name"],
            "avatar_url": info["avatar_url"], "replay_code": code}


@app.delete("/api/admin/rooms/{room_id}")
async def admin_remove_room(room_id: int, request: Request,
                            hard: bool = False):
    """从回放站名单移除房间（房间表分离后只动 replay_rooms，monitor 的
    监控名单 rooms 与采集不受任何影响）：

    - 默认：停用（replay_rooms.enabled=0）——回放站隐藏该房间的管理页
      名单与新增入口，历史录像/注释/监控码全保留，直链回放页照常可进；
    - hard=true：删除 replay_rooms 行（监控码与回放页入口随之失效；场次/
      弹幕/录像等明细数据按约定保留在库中，monitor 侧照旧监控）。
    """
    check_admin(request)
    if not q1("SELECT room_id FROM replay_rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不在回放站名单中")
    conn = connect()
    try:
        if hard:
            conn.execute("DELETE FROM replay_rooms WHERE room_id = ?",
                         (room_id,))
        else:
            conn.execute("UPDATE replay_rooms SET enabled = 0 WHERE room_id = ?",
                         (room_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "paused": not hard}


@app.post("/api/admin/rooms/{room_id}/enable")
async def admin_enable_room(room_id: int, request: Request):
    """恢复已停用的回放站房间（仅本站名单；monitor 侧监控不受影响）。"""
    check_admin(request)
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE replay_rooms SET enabled = 1 WHERE room_id = ?", (room_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "房间不在回放站名单中")
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/admin/clipper-password")
async def get_clipper_password(request: Request):
    """管理页明文显示切片员共用密码（与监控码同口径：管理页需随时查看/
    复制分发，存储即明文、恒时比对校验）。仅管理员。"""
    check_admin(request)
    return {"clipper_password": auth.get_clipper_password() or ""}


@app.post("/api/admin/clipper-password/reset")
async def reset_clipper_password(request: Request):
    """重置切片员共用密码（全员同一个，重置后旧密码立即失效——已登录的
    切片员会话不受影响，签名自包含）。新密码明文仅在本次响应返回一次
    （库中只存哈希），并追加写凭据文件备份。"""
    check_admin(request)
    pw = auth.gen_password(12)
    auth.set_clipper_password(pw)
    try:
        cred = DATA_DIR / "web_credentials.txt"
        cred.parent.mkdir(parents=True, exist_ok=True)
        with cred.open("a", encoding="utf-8") as f:
            f.write(f"切片员共用密码（replay 回放站，全员共用）: {pw}\n")
    except OSError:
        pass
    return {"ok": True, "clipper_password": pw}


# ---------------------------------------------------------------------------
# 整场录制（回放页数据源；启停经 recordings 表由 bili 侧 capture 执行）
# ---------------------------------------------------------------------------
# 录制行的"进行中"状态集合（存在任一即视为该房间有活跃录制）
_ACTIVE_REC_STATUSES = ("requested", "recording", "stop_requested",
                        "rotate_requested", "finalizing")   # rotate_requested: bili v2.1 压力快路径的瞬时轮换态


@app.get("/api/rooms/{room_id}/recordings")
async def list_recordings(room_id: int):
    """该房间的整场录制列表（新→旧）。进行中的行带实时体积（分段目录
    实测）；终态行带成片预览/下载 URL（/recordings 静态挂载路径）。
    每行带 notes（注释，按时间正序）与 title（管理员命名，空=未命名）。"""
    rows = q("SELECT * FROM recordings WHERE room_id = ? ORDER BY id DESC", (room_id,))
    # 注释一次查齐按录制分组挂载（每房 30 天内的行数与注释量都很小）
    notes_by_rec: dict[int, list[dict]] = {}
    for n in q("SELECT n.id, n.recording_id, n.author, n.content, "
               "n.created_at, n.updated_at FROM recording_notes n "
               "JOIN recordings r ON r.id = n.recording_id "
               "WHERE r.room_id = ? ORDER BY n.id", (room_id,)):
        notes_by_rec.setdefault(n.pop("recording_id"), []).append(n)
    out = []
    for r in rows:
        item = dict(r)
        item["notes"] = notes_by_rec.get(r["id"], [])
        if r["status"] in _ACTIVE_REC_STATUSES:
            seg_dir = RECORDINGS_ROOT / str(room_id) / f"full_{r['id']}"
            try:
                item["live_size"] = sum(f.stat().st_size
                                        for f in seg_dir.glob("seg_*.ts"))
            except OSError:
                item["live_size"] = 0
        if r["file_path"]:
            item["file_url"] = f"{cfg['server']['root_path']}/recordings/{r['file_path']}"
        out.append(item)
    return {"recordings": out}


@app.post("/api/rooms/{room_id}/recordings/start")
async def start_recording(room_id: int):
    """开始整场录制：插入 requested 行，capture（bili 侧）扫描循环 2s 内
    捡起执行（开播校验在 capture 侧完成，未开播的请求行会变 failed 并带
    原因，回放页轮询可见）。同一房间同时只允许一场录制。"""
    rcfg = cfg["recording"]
    if not rcfg.get("enabled", True):
        raise HTTPException(400, "录制功能未启用（bili config [recording].enabled）")
    row = q1("SELECT enabled FROM replay_rooms WHERE room_id = ?", (room_id,))
    if not row:
        raise HTTPException(404, "房间不在回放站名单中")
    if not row["enabled"]:
        raise HTTPException(400, "回放站房间已停用，请先在本站管理页启用")
    # 录制由 bili 侧 capture 执行：它只为监控名单（rooms.enabled=1）建房，
    # 未监控/监控停用的房间请求行会被 capture 直接置 failed——这里前置
    # 拦下并给出指引，避免无效请求行
    mrow = q1("SELECT enabled FROM rooms WHERE room_id = ?", (room_id,))
    if not mrow:
        raise HTTPException(400, "monitor 侧未监控该房间，无法录制"
                                 "（请在 bili-monitor 管理页添加监控）")
    if not mrow["enabled"]:
        raise HTTPException(400, "monitor 侧监控已停用，"
                                 "请先在 bili-monitor 管理页恢复监控")
    # 同房间防重（并发安全）：检查与插入合并为单条原子 INSERT——
    # SELECT ... WHERE NOT EXISTS，两个并发请求只有一个能插入成功
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO recordings (room_id, status, qn, created_at, started_at) "
            "SELECT ?, 'requested', ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM recordings WHERE room_id = ? AND status IN "
            "('requested', 'recording', 'stop_requested', 'finalizing'))",
            (room_id, int(rcfg.get("full_qn", 200)), now_iso(), now_iso(), room_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "该房间已有进行中的录制")
        rec_id = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "id": rec_id}


@app.post("/api/rooms/{room_id}/recordings/{rec_id}/stop")
async def stop_recording(room_id: int, rec_id: int):
    """停止整场录制并保存：recording → stop_requested（capture 停流并
    concat 成 mp4，期间回放页显示"正在保存"）；requested（还没被 capture
    捡起）直接取消。幂等：已结束的行直接返回 ok。"""
    row = q1("SELECT status FROM recordings WHERE id = ? AND room_id = ?",
             (rec_id, room_id))
    if not row:
        raise HTTPException(404, "录制记录不存在")
    if row["status"] == "recording":
        conn = connect()
        try:
            conn.execute("UPDATE recordings SET status = 'stop_requested' WHERE id = ?",
                         (rec_id,))
            conn.commit()
        finally:
            conn.close()
    elif row["status"] == "requested":
        conn = connect()
        try:
            conn.execute("UPDATE recordings SET status = 'failed', reason = ?, "
                         "ended_at = ? WHERE id = ?",
                         ("已取消（尚未开始）", now_iso(), rec_id))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


def _purge_clip_asset(clip: dict) -> None:
    """单条切片的物理清理（v2.0.2 从 delete_clip 抽出供删除录播级联复用）：
    云端对象（本体按行 oss_bucket 路由 + low 同桶）+ 本地文件 + low 目录。
    行的删除由调用方负责（事务口径不同）。"""
    bucket = clip.get("oss_bucket") or ""
    if oss_enabled() and clip["oss_key"]:
        _oss_delete(clip["oss_key"], bucket=bucket)
    if oss_enabled() and clip.get("oss_low_key"):
        _oss_delete(clip["oss_low_key"], bucket=bucket)
    if clip["file_path"]:
        try:
            (RECORDINGS_ROOT / clip["file_path"]).unlink()
        except OSError:
            pass
        if clip["file_path"].endswith(".mp4"):
            shutil.rmtree(RECORDINGS_ROOT / clip["file_path"][:-4],
                          ignore_errors=True)


@app.delete("/api/rooms/{room_id}/recordings/{rec_id}")
async def delete_recording(room_id: int, rec_id: int, request: Request):
    """删除一条已结束的整场录制（仅管理员，路由内收紧——切片员/游客在
    middleware 已被拦，这里防旧通道等其他旁路）。

    成片文件 + 遗留分段目录 + 低画质目录 + DB 行一并删除（recording_notes
    随外键级联）；**挂载切片一并删除**（v2.0.2「删除视频=所有相关视频一并
    删除」：切片行 + 云端对象 + 本地文件，manual_clip_notes 随外键级联；
    有 processing 中的切片时整体 409 稍候再删）；云端副本联动删除。
    进行中的行不可删（先停止再删）。"""
    check_admin(request)
    # v2.1 行级桶分派删除；旧快照缺列回退不带桶列（= 主桶口径）
    try:
        row = q1("SELECT status, file_path, oss_key, oss_low_key, oss_bucket, "
                 "oss_low_bucket FROM recordings "
                 "WHERE id = ? AND room_id = ?", (rec_id, room_id))
    except sqlite3.OperationalError:
        row = q1("SELECT status, file_path, oss_key, oss_low_key "
                 "FROM recordings WHERE id = ? AND room_id = ?",
                 (rec_id, room_id))
    if not row:
        raise HTTPException(404, "录制记录不存在")
    if row["status"] in _ACTIVE_REC_STATUSES:
        raise HTTPException(409, "录制进行中，请先停止再删除")
    clips = q("SELECT * FROM manual_clips WHERE recording_id = ?", (rec_id,))
    if any(c["status"] == "processing" for c in clips):
        raise HTTPException(409, "该录播有切片正在处理中，请稍候再删除")
    # 云端副本同步删除（原片按行 oss_bucket、low 按 oss_low_bucket——
    # bili v2.1 起原片 10 天热窗口在 mid 桶、归档后回主桶；未上云或
    # OSS 停用静默跳过；旧快照缺列回退主桶）
    try:
        rec_b = row["oss_bucket"] or ""
        low_b = row["oss_low_bucket"] or ""
    except (IndexError, KeyError):
        rec_b = low_b = ""
    if oss_enabled():
        if row["oss_key"]:
            _oss_delete(row["oss_key"], bucket=rec_b)
        if row["oss_low_key"]:
            _oss_delete(row["oss_low_key"], bucket=low_b)
    if row["file_path"]:
        try:
            (RECORDINGS_ROOT / row["file_path"]).unlink()
        except OSError:
            pass
    try:
        shutil.rmtree(RECORDINGS_ROOT / str(room_id) / f"full_{rec_id}",
                      ignore_errors=True)
        # 低画质副本目录（<成片stem>/low.mp4）一并清理
        if row["file_path"] and row["file_path"].endswith(".mp4"):
            shutil.rmtree(
                RECORDINGS_ROOT / str(room_id) / row["file_path"][:-4],
                ignore_errors=True)
    except OSError:
        pass
    for c in clips:
        _purge_clip_asset(c)
    conn = connect()
    try:
        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
        conn.execute("DELETE FROM manual_clips WHERE recording_id = ?",
                     (rec_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted_clips": len(clips)}


# 录播命名：标题列的管理入口（仅管理员；切片员/游客只读）
_REC_TITLE_MAX = 60   # 录播名字长度上限（字符）


class RecRenameIn(BaseModel):
    title: str


@app.patch("/api/rooms/{room_id}/recordings/{rec_id}")
async def rename_recording(room_id: int, rec_id: int, body: RecRenameIn,
                           request: Request):
    """给一条录播命名/改名（仅管理员）。名字展示在列表条目上、参与前端
    搜索过滤；空串=清除名字；进行中的行也可命名（纯元数据）。"""
    check_admin(request)
    if not q1("SELECT id FROM recordings WHERE id = ? AND room_id = ?",
              (rec_id, room_id)):
        raise HTTPException(404, "录制记录不存在")
    title = (body.title or "").strip()
    if len(title) > _REC_TITLE_MAX:
        raise HTTPException(400, f"名字最长 {_REC_TITLE_MAX} 个字符")
    conn = connect()
    try:
        conn.execute("UPDATE recordings SET title = ? WHERE id = ?",
                     (title, rec_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "title": title}


# ---------------------------------------------------------------------------
# 云端原片回灌（v2.0.1：切片动线配套——本地已释放的录播一键拉回原件）
# ---------------------------------------------------------------------------
@app.post("/api/rooms/{room_id}/recordings/{rec_id}/restore-oss")
async def restore_recording_oss(room_id: int, rec_id: int, request: Request):
    """触发云端原件回灌（clipper/admin；游客 POST 与跨房由 middleware 403）。

    切片快切读本机原片（capture 侧源缺失直接 failed），本端点把已上云的
    **原件**（oss_key；不含 low 副本）拉回本机：内网 endpoint 后台线程
    分段下载（实测 ~70MiB/s），完成后原子改名 + mtime 重置 → 留存窗口
    从回灌时刻重新起算，到期由 bili 释放分支照常回收。返回任务快照
    （running/done），进度轮询走同名 GET。幂等：已在本地 → done。"""
    if not oss_enabled():
        raise HTTPException(503, "OSS 未启用（读 bili 配置），无法回灌")
    # v2.1 行级桶（oss_bucket）；旧快照缺列回退不带桶列（= 主桶口径）
    try:
        row = q1("SELECT id, room_id, file_path, oss_key, oss_bucket, "
                 "file_size FROM recordings WHERE id = ? AND room_id = ?",
                 (rec_id, room_id))
    except sqlite3.OperationalError:
        row = q1("SELECT id, room_id, file_path, oss_key, file_size "
                 "FROM recordings WHERE id = ? AND room_id = ?",
                 (rec_id, room_id))
    if not row:
        raise HTTPException(404, "录制记录不存在")
    if not row["file_path"] or not row["file_path"].endswith(".mp4"):
        raise HTTPException(400, "该录播无成片文件")
    if (RECORDINGS_ROOT / row["file_path"]).exists():
        return restore_mod.status(rec_id) or {"status": "done"}
    if not row["oss_key"]:
        raise HTTPException(404, "该录播未上云，云端无原件可拉取")
    # 磁盘余量粗检（file_size 为上云时落库值，与对象大小一致），留 2G 余量
    need = int(row["file_size"] or 0)
    free = shutil.disk_usage(RECORDINGS_ROOT).free
    if need and free < need + (2 << 30):
        raise HTTPException(503, f"磁盘剩余不足（需约 {need >> 30}G、"
                                 f"现余 {free >> 30}G），回灌已拒绝")
    return restore_mod.start(row)


@app.get("/api/rooms/{room_id}/recordings/{rec_id}/restore-oss")
async def restore_recording_status(room_id: int, rec_id: int):
    """回灌任务状态（前端进度条 ~1s 轮询）。快照含 status/got/total/error。

    v2.2.4 修复：任务完成即从进程内任务表出表（_finalize），且 web 重启
    会丢内存态——此前前端进度条轮询只会拿到 404 永不更新。现补两级兜底：
    ① 任务缺失但文件已在盘 → 返回 done 快照（前端据此走「完成 → 3s
    自动关」）；② 无任务且无文件 → 仍 404（前端连续 404 超限转失败可重试）。"""
    try:
        row = q1("SELECT file_path, file_size FROM recordings "
                 "WHERE id = ? AND room_id = ?", (rec_id, room_id))
    except sqlite3.OperationalError:
        row = None
    if not row:
        raise HTTPException(404, "录制记录不存在")
    snap = restore_mod.status(rec_id)
    if snap is not None:
        return snap
    if row["file_path"] and (RECORDINGS_ROOT / row["file_path"]).exists():
        size = int(row["file_size"] or 0)
        return {"status": "done", "got": size, "total": size, "error": ""}
    raise HTTPException(404, "暂无回灌任务（未触发或服务已重启）")


# ---------------------------------------------------------------------------
# 删除双选项（v2.0.1 引入；v2.0.2 修正口径）：删除视频（真删除：行+本地
# 文件+云端对象，录播级联全部挂载切片）/ 删除服务器存档（仅已上云可用）
# ---------------------------------------------------------------------------
def _release_local_files(file_path: str) -> bool:
    """删除一条产物在服务器的本地文件（原片 + <stem>/ low 目录），返回
    是否实际删到东西。DB 行与云端对象一律不动（「删除服务器存档」的
    执行体，与 bili 释放分支/紧急清理同款文件口径）。"""
    existed = False
    f = RECORDINGS_ROOT / file_path
    if f.exists():
        try:
            f.unlink()
            existed = True
        except OSError:
            pass
    if file_path.endswith(".mp4"):
        stem = RECORDINGS_ROOT / file_path[:-4]
        if stem.is_dir():
            shutil.rmtree(stem, ignore_errors=True)
            existed = True
    return existed


@app.post("/api/rooms/{room_id}/recordings/{rec_id}/release-local")
async def release_recording_local(room_id: int, rec_id: int, request: Request):
    """删除服务器存档（录播）：立即删除本地原片与 low 目录——条目保留、
    播放转 OSS 签名、可随时由回灌气泡拉回。**仅已上云（oss_key 非空）
    可用**（未上云时本地是唯一副本，拒绝）。管理员。幂等。"""
    check_admin(request)
    row = q1("SELECT file_path, oss_key FROM recordings "
             "WHERE id = ? AND room_id = ?", (rec_id, room_id))
    if not row:
        raise HTTPException(404, "录制记录不存在")
    if not row["oss_key"]:
        raise HTTPException(400, "该录播未上云——本地文件是唯一副本，不可删除")
    if not row["file_path"]:
        return {"ok": True, "released": False}
    return {"ok": True, "released": _release_local_files(row["file_path"])}


@app.post("/api/rooms/{room_id}/clips/{clip_id}/release-local")
async def release_clip_local(room_id: int, clip_id: int, request: Request):
    """删除服务器存档（切片）：删除本地本体（不留存策略下通常已释）与
    low 预览目录；条目保留、播放/下载转 OSS。仅已上云可用。管理员。"""
    check_admin(request)
    clip = _load_clip(room_id, clip_id)
    if not clip["oss_key"]:
        raise HTTPException(400, "该切片未上云——本地文件是唯一副本，不可删除")
    if not clip["file_path"]:
        return {"ok": True, "released": False}
    return {"ok": True, "released": _release_local_files(clip["file_path"])}


# ---------------------------------------------------------------------------
# 视频外挂弹幕（时间轴映射；数据与算法自 bili-monitor v0.10 原样迁移）
# ---------------------------------------------------------------------------
# 响应只含 {t, text, guard} 三字段——不返回 uname/uid（回放站游客/切片员
# 也不接触用户身份信息，保持 bili 侧既有隐私边界）。
import json as _json


def _parse_iso(s: str):
    """ISO8601 秒级时间串 → datetime（与 capture 侧写入格式一致）。"""
    import datetime as dt
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def _load_timeline(timeline_json: str) -> dict | None:
    """解析成片 timeline 列的 JSON；空/损坏/无 runs 返回 None（走近似）。"""
    if not timeline_json:
        return None
    try:
        data = _json.loads(timeline_json)
    except ValueError:
        return None
    return data if data.get("runs") else None


def _map_wall_to_video(ts_iso: str, runs: list[dict] | None,
                       wall0: str, duration_cap: float | None) -> float | None:
    """弹幕墙钟时间 → 视频内秒；映射到视频未覆盖范围返回 None（丢弃）。

    runs 模式：命中 run → off + (ts − wall)；run 间缺口钳到后一 run 起点；
    近似模式（runs=None，存量行）：t = ts − wall0，超范围丢弃。"""
    ts = _parse_iso(ts_iso)
    if ts is None:
        return None
    if runs:
        for i, r in enumerate(runs):
            w = _parse_iso(r.get("wall", ""))
            if w is None:
                continue
            dur = float(r.get("dur") or 0)
            if ts < w:
                return float(r.get("off") or 0) if i > 0 else None
            if (ts - w).total_seconds() < dur:
                return float(r.get("off") or 0) + (ts - w).total_seconds()
        return None
    t0 = _parse_iso(wall0)
    if t0 is None:
        return None
    t = (ts - t0).total_seconds()
    if t < 0 or (duration_cap is not None and t > duration_cap):
        return None
    return t


def _danmaku_payload(room_id: int, timeline_json: str, wall0: str,
                     duration_cap: float | None) -> dict:
    """弹幕查询 + 时间轴映射的公共出口。查询窗两端各放宽 60s（边界取整
    兜底）；返回 {approx, count, items}，items 按 t 升序。"""
    import datetime as dt
    fmt = "%Y-%m-%dT%H:%M:%S"
    runs = (_load_timeline(timeline_json) or {}).get("runs")
    approx = runs is None
    if runs:
        q_start_dt = _parse_iso(runs[0]["wall"])
        q_end_dt = _parse_iso(runs[-1]["wall"]) + dt.timedelta(
            seconds=float(runs[-1].get("dur") or 0))
    else:
        q_start_dt = _parse_iso(wall0)
        q_end_dt = q_start_dt + dt.timedelta(seconds=(duration_cap or 8 * 3600))
    pad = dt.timedelta(seconds=60)
    rows = q("SELECT text, privilege_type, ts FROM danmaku "
             "WHERE room_id = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
             (room_id, (q_start_dt - pad).strftime(fmt),
              (q_end_dt + pad).strftime(fmt)))
    items = []
    for r in rows:
        t = _map_wall_to_video(r["ts"], runs, wall0, duration_cap)
        if t is None:
            continue
        items.append({"t": round(t, 1), "text": r["text"],
                      "guard": int(r["privilege_type"] or 0)})
    return {"approx": approx, "count": len(items), "items": items}


@app.get("/api/rooms/{room_id}/recordings/{rec_id}/danmaku")
async def recording_danmaku(room_id: int, rec_id: int,
                            format: str = Query("json"),
                            filename: str = Query("")):
    """整场录播的外挂弹幕数据（回放页预览叠加用）。无 timeline 的存量行
    走单 run 近似（approx=true，前端显示"时间轴近似"标记）。

    v2.0.0：``format=ass`` 返回同名 ASS 字幕附件（下载携带同期弹幕——
    filename 由前端传与视频一致的 basename（前端拼名规则
    ``room<房号>-<etype><id>``，视频 .mp4 / 弹幕 .ass），本地播放器同名
    自动加载）；缺省 json 完全向后兼容。"""
    rec = q1("SELECT file_path, started_at, duration_sec, timeline "
             "FROM recordings WHERE id = ? AND room_id = ?", (rec_id, room_id))
    if not rec or not rec["file_path"]:
        raise HTTPException(404, "录制记录不存在或没有成片")
    payload = _danmaku_payload(room_id, rec["timeline"], rec["started_at"],
                               float(rec["duration_sec"] or 0) or None)
    if format == "ass":
        return _ass_response(payload, filename,
                             f"room{room_id}-rec{rec_id}.ass")
    return payload


def _ass_response(payload: dict, filename: str, default_name: str) -> Response:
    """弹幕 payload → ASS 附件响应（文件名清洗，防路径注入）。"""
    safe = re.sub(r'[^\w.\-\u4e00-\u9fff]', "_", (filename or "").strip()) \
        or default_name
    if not safe.endswith(".ass"):
        safe += ".ass"
    return Response(build_ass(payload["items"]),
                    media_type="text/x-ssa; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


# ---------------------------------------------------------------------------
# 切片弹幕（v2.0.0：下载携带同期弹幕——切片窗口映射）
# ---------------------------------------------------------------------------
def _clip_danmaku_payload(room_id: int, clip: dict) -> dict:
    """切片窗口的弹幕数据：把源录播弹幕映射到 [0, 切片时长]。

    两种模式（锚点口径待部署后用真实切片实测校准，见版本笔记 v2.0.0）：
    - 直播切片（start_wall 非空）：t_clip = map_wall(弹幕 ts) −
      map_wall(start_wall)；源行无 timeline 或已成孤儿 → 近似
      t_clip = ts − start_wall（墙钟直减，断流间隙被忽略，approx=true）；
    - 成片偏移（start_sec）：t_clip = map_wall(弹幕 ts) − start_sec，
      需源录播行在（孤儿切片无锚点 → 404）。
    """
    dur = float(clip["duration_sec"] or 0)
    sw = clip["start_wall"] or ""

    if sw:
        rec = (q1("SELECT timeline, started_at, duration_sec FROM recordings "
                  "WHERE id = ?", (clip["recording_id"],))
               if clip["recording_id"] else None)
        runs = (_load_timeline(rec["timeline"]) or {}).get("runs") if rec else None
        if rec and runs:
            payload = _danmaku_payload(room_id, rec["timeline"],
                                       rec["started_at"],
                                       float(rec["duration_sec"] or 0) or None)
            off0 = _map_wall_to_video(sw, runs, rec["started_at"], None)
            if off0 is None:
                return {"approx": True, "count": 0, "items": []}
            items = [{"t": round(it["t"] - off0, 1), "text": it["text"],
                      "guard": it["guard"]}
                     for it in payload["items"]
                     if -0.05 <= it["t"] - off0 <= dur + 0.5]
            return {"approx": False, "count": len(items), "items": items}
        # 近似：源无 timeline 或孤儿——弹幕墙钟直减切片起点
        t0 = _parse_iso(sw)
        t_end = _parse_iso(clip["end_wall"] or "") or _parse_iso(now_iso())
        if t0 is None or t_end is None:
            raise HTTPException(404, "切片起点时间无效，无法生成弹幕")
        rows = q("SELECT text, privilege_type, ts FROM danmaku "
                 "WHERE room_id = ? AND ts >= ? AND ts <= ? ORDER BY ts, id",
                 (room_id, (t0 - _dt.timedelta(seconds=5))
                  .strftime("%Y-%m-%dT%H:%M:%S"),
                  t_end.strftime("%Y-%m-%dT%H:%M:%S")))
        items = []
        for r in rows:
            ts = _parse_iso(r["ts"])
            if ts is None:
                continue
            t = (ts - t0).total_seconds()
            if -0.05 <= t <= dur + 0.5:
                items.append({"t": round(t, 1), "text": r["text"],
                              "guard": int(r["privilege_type"] or 0)})
        return {"approx": True, "count": len(items), "items": items}

    # 成片偏移模式：必须有源录播行
    rec = (q1("SELECT timeline, started_at, duration_sec FROM recordings "
              "WHERE id = ?", (clip["recording_id"],))
           if clip["recording_id"] else None)
    if not rec:
        raise HTTPException(404, "源录播已清理（孤儿切片），无法生成切片弹幕")
    payload = _danmaku_payload(room_id, rec["timeline"], rec["started_at"],
                               float(rec["duration_sec"] or 0) or None)
    start = float(clip["start_sec"] or 0)
    items = [{"t": round(it["t"] - start, 1), "text": it["text"],
              "guard": it["guard"]}
             for it in payload["items"]
             if -0.05 <= it["t"] - start <= dur + 0.5]
    return {"approx": payload["approx"], "count": len(items), "items": items}


@app.get("/api/rooms/{room_id}/clips/{clip_id}/danmaku")
async def clip_danmaku(room_id: int, clip_id: int,
                       format: str = Query("json"), filename: str = Query("")):
    """切片的外挂弹幕数据（v2.0.0；窗口映射见 _clip_danmaku_payload）。
    format=ass 同录播接口口径（下载气泡双下载连发的 .ass 侧）。"""
    clip = _load_clip(room_id, clip_id)
    payload = _clip_danmaku_payload(room_id, clip)
    if format == "ass":
        return _ass_response(payload, filename,
                             f"room{room_id}-clip{clip_id}.ass")
    return payload


# ---------------------------------------------------------------------------
# 录制注释（署名即身份：clipper=昵称、admin=用户名；仅作者本人可改删）
# ---------------------------------------------------------------------------
_NOTE_CONTENT_MAX = 500   # 注释内容长度上限（字符）


class NoteIn(BaseModel):
    content: str


def _check_note_target(room_id: int, rec_id: int, note_id: int | None = None):
    """校验注释的挂载对象存在且属于该房间；编辑/删除时返回注释行。"""
    if note_id is None:
        if not q1("SELECT id FROM recordings WHERE id = ? AND room_id = ?",
                  (rec_id, room_id)):
            raise HTTPException(404, "录制记录不存在")
        return None
    note = q1("SELECT n.* FROM recording_notes n "
              "JOIN recordings r ON r.id = n.recording_id "
              "WHERE n.id = ? AND n.recording_id = ? AND r.room_id = ?",
              (note_id, rec_id, room_id))
    if not note:
        raise HTTPException(404, "注释不存在")
    return note


def _note_content_or_400(content: str) -> str:
    """注释内容校验：去首尾空白后非空、不超过长度上限。"""
    content = (content or "").strip()
    if not content:
        raise HTTPException(400, "注释内容不能为空")
    if len(content) > _NOTE_CONTENT_MAX:
        raise HTTPException(400, f"注释内容最长 {_NOTE_CONTENT_MAX} 个字符")
    return content


@app.post("/api/rooms/{room_id}/recordings/{rec_id}/notes")
async def add_note(room_id: int, rec_id: int, body: NoteIn, request: Request):
    """给一条录制添加注释（署名=当前会话身份，不可指定他人）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入"
                                "（登录时填写昵称）即可添加注释")
    _check_note_target(room_id, rec_id)
    content = _note_content_or_400(body.content)
    ts = now_iso()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO recording_notes (recording_id, author, content, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (rec_id, author, content, ts, ts))
        conn.commit()
        note_id = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "note": {"id": note_id, "recording_id": rec_id,
                                 "author": author, "content": content,
                                 "created_at": ts, "updated_at": ts}}


@app.patch("/api/rooms/{room_id}/recordings/{rec_id}/notes/{note_id}")
async def edit_note(room_id: int, rec_id: int, note_id: int,
                    body: NoteIn, request: Request):
    """修改自己的注释（别人的注释 403，管理员也不例外）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    note = _check_note_target(room_id, rec_id, note_id)
    if note["author"] != author:
        raise HTTPException(403, "只能修改自己的注释")
    content = _note_content_or_400(body.content)
    conn = connect()
    try:
        conn.execute("UPDATE recording_notes SET content = ?, updated_at = ? "
                     "WHERE id = ?", (content, now_iso(), note_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/rooms/{room_id}/recordings/{rec_id}/notes/{note_id}")
async def delete_note(room_id: int, rec_id: int, note_id: int,
                      request: Request):
    """删除自己的注释（别人的注释 403，管理员也不例外）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    note = _check_note_target(room_id, rec_id, note_id)
    if note["author"] != author:
        raise HTTPException(403, "只能删除自己的注释")
    conn = connect()
    try:
        conn.execute("DELETE FROM recording_notes WHERE id = ?", (note_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 录制归档分组（回放页日期板块唯一数据源；逻辑自 bili-monitor v0.11 迁移）
# ---------------------------------------------------------------------------
@app.get("/api/rooms/{room_id}/auto-recordings")
async def auto_recordings(room_id: int):
    """按日期倒序分组输出（两栏布局：左主录播/占位，右切片手风琴）：
    - kind='full'：主录播组——**同日全部整场行合并为一组**（v2.2.2 起；
      auto 段、bridge 轮换桥接片、进行中行、时长 ≥ standalone_clip_max_sec
      的手动行），recs 按开始时间新→旧排列，前端在板块内按时间顺序置顶
      多个视频。此前每行一组，2 小时轮换把一场直播拆成多段后同一天会被
      切成 N 张组卡（2026-10-14 用户定稿按日归并）；
    - kind='standalone'：独立片段组（同日短手动录制合并，如逐首录制的歌切；
      bridge 行不再落入此桶——桥接片有对应全段录播且需要携带 oss 键走
      云端播放链，clip_form 形态缺 oss_key/oss_low_key 会让本地已释放的
      桥接片在页面上不可播）；
    - kind='orphan'：孤儿切片组（源录制行已被清理，切片独立存续）。
    活跃行带 live_size，终态成片带 file_url；auto_full 开关与房间在播状态
    （live）回传；in_local/on_oss 回传（本机优先访问）。"""
    room = q1(
        "SELECT rr.anchor_name, COALESCE(b.auto_full, 0) AS auto_full "
        "FROM replay_rooms rr LEFT JOIN rooms b ON b.room_id = rr.room_id "
        "WHERE rr.room_id = ?", (room_id,))
    if not room:
        raise HTTPException(404, "房间不在回放站名单中")
    live = q1("SELECT 1 FROM live_sessions WHERE room_id = ? "
              "AND end_time IS NULL LIMIT 1", (room_id,))
    threshold = float(cfg["recording"].get("standalone_clip_max_sec", 1800))
    rows = q("SELECT id, status, title, started_at, ended_at, qn, file_path, "
             "file_size, duration_sec, source, oss_key, oss_low_key "
             "FROM recordings "
             "WHERE room_id = ? ORDER BY started_at DESC, id DESC", (room_id,))
    # 投稿结果（v2.3.0 持久真源）：按 clip_id 一次查齐，随切片行回传
    #（alive=0 的记录仍回传——前端可决定显示形态，当前口径隐藏胶囊）
    subs: dict[int, dict] = {}
    try:
        for s in q("SELECT clip_id, bv, mode, title, submitted_at, "
                   "season_note, alive FROM clip_submissions"):
            subs[s.pop("clip_id")] = s
    except sqlite3.OperationalError:
        pass    # 老库缺表防御（init_db 启动即建，正常不走到）
    # 录制注释 / 切片注释一次查齐分组挂载
    notes_by_rec: dict[int, list[dict]] = {}
    for n in q("SELECT n.id, n.recording_id, n.author, n.content, "
               "n.created_at, n.updated_at FROM recording_notes n "
               "JOIN recordings r ON r.id = n.recording_id "
               "WHERE r.room_id = ? ORDER BY n.id", (room_id,)):
        notes_by_rec.setdefault(n.pop("recording_id"), []).append(n)
    clips_by_rec: dict[int, list[dict]] = {}
    cnotes_by_clip: dict[int, list[dict]] = {}
    for n in q("SELECT n.id, n.clip_id, n.author, n.content, n.created_at, "
               "n.updated_at FROM manual_clip_notes n "
               "JOIN manual_clips c ON c.id = n.clip_id "
               "WHERE c.room_id = ? ORDER BY n.id", (room_id,)):
        cnotes_by_clip.setdefault(n.pop("clip_id"), []).append(n)
    for c in q("SELECT id, recording_id, title, start_sec, end_sec, start_wall, "
               "end_wall, file_path, file_size, duration_sec, status, reason, "
               "created_by, created_at, oss_key, oss_low_key FROM manual_clips "
               "WHERE room_id = ? ORDER BY id", (room_id,)):
        if c["status"] == "done" and c["file_path"]:
            c["file_url"] = f"{cfg['server']['root_path']}/recordings/{c['file_path']}"
        c["in_local"] = bool(c["file_path"]
                             and (RECORDINGS_ROOT / c["file_path"]).exists())
        c["on_oss"] = bool(c["oss_key"])
        c["etype"] = "clip"
        c["notes"] = cnotes_by_clip.get(c["id"], [])
        c["submission"] = subs.get(c["id"])
        clips_by_rec.setdefault(c.pop("recording_id"), []).append(c)
    # 孤儿切片：源录制行已被保留期/水位清理（切片自包含存续）
    orphans = q("SELECT c.id, c.recording_id, c.title, c.start_sec, c.end_sec, "
                "c.start_wall, c.end_wall, c.file_path, c.file_size, "
                "c.duration_sec, c.status, c.reason, c.created_by, c.created_at, "
                "c.oss_key, c.oss_low_key "
                "FROM manual_clips c LEFT JOIN recordings r "
                "ON r.id = c.recording_id "
                "WHERE c.room_id = ? AND r.id IS NULL ORDER BY c.id", (room_id,))
    for c in orphans:
        if c["status"] == "done" and c["file_path"]:
            c["file_url"] = f"{cfg['server']['root_path']}/recordings/{c['file_path']}"
        c["in_local"] = bool(c["file_path"]
                             and (RECORDINGS_ROOT / c["file_path"]).exists())
        c["on_oss"] = bool(c["oss_key"])
        c["etype"] = "clip"
        c["notes"] = cnotes_by_clip.get(c["id"], [])
        c["submission"] = subs.get(c["id"])

    groups: dict[str, list[dict]] = {}
    for r in rows:
        date = (r["started_at"] or "")[:10]
        if not date:
            continue   # 无 started_at 的 requested 行（秒级窗口，不值得展示）
        item = dict(r)
        if r["status"] in _ACTIVE_REC_STATUSES:
            seg_dir = RECORDINGS_ROOT / str(room_id) / f"full_{r['id']}"
            try:
                item["live_size"] = sum(f.stat().st_size
                                        for f in seg_dir.glob("seg_*.ts"))
            except OSError:
                item["live_size"] = 0
        if r["file_path"]:
            item["file_url"] = f"{cfg['server']['root_path']}/recordings/{r['file_path']}"
        item["in_local"] = bool(r["file_path"]
                                and (RECORDINGS_ROOT / r["file_path"]).exists())
        item["on_oss"] = bool(r["oss_key"])
        item["clips"] = clips_by_rec.get(r["id"], [])
        item["notes"] = notes_by_rec.get(r["id"], [])
        # 组归类：同日全部整场行（auto 段 / bridge 桥接 / 进行中 / 长手动）
        # 合并进同一个 full 组（recs 新→旧；行迭代天然 started_at DESC，
        # 桥接行 started_at=桥接窗口起点，按时间自然落在两段之间）；
        # 短手动行仍归 standalone 桶
        if (item["source"] in ("auto", "bridge")
                or item["status"] in _ACTIVE_REC_STATUSES
                or (item["duration_sec"] or 0) >= threshold):
            bucket = next((g for g in groups.setdefault(date, [])
                           if g["kind"] == "full"), None)
            if bucket is None:
                bucket = {"kind": "full", "recs": []}
                groups[date].append(bucket)
            bucket["recs"].append(item)
        else:
            clip_form = {
                "id": item["id"], "etype": "rec", "recording_id": item["id"],
                "title": item["title"], "start_sec": 0,
                "end_sec": item["duration_sec"], "start_wall": "", "end_wall": "",
                "file_path": item["file_path"], "file_size": item["file_size"],
                "duration_sec": item["duration_sec"],
                "status": ("done" if item["status"] == "finished"
                           else ("failed" if item["status"] == "failed"
                                 else "processing")),
                "reason": "", "created_by": "",
                "created_at": item["started_at"],
                "ended_at": item["ended_at"] or "",
                "file_url": item.get("file_url", ""),
                "in_local": item["in_local"],
                "on_oss": item["on_oss"],
                "notes": item["notes"],
            }
            bucket = next((g for g in groups.setdefault(date, [])
                           if g["kind"] == "standalone"), None)
            if bucket is None:
                bucket = {"kind": "standalone", "rec": None, "clips": []}
                groups[date].append(bucket)
            bucket["clips"].append(clip_form)
    for c in orphans:
        date = (c["created_at"] or "")[:10]
        if not date:
            continue
        bucket = next((g for g in groups.setdefault(date, [])
                       if g["kind"] == "orphan"), None)
        if bucket is None:
            bucket = {"kind": "orphan", "rec": None, "clips": []}
            groups[date].append(bucket)
        bucket["clips"].append(c)
    # v2.2.2 场次分隔标记：同日 full 组 recs 按时间正序扫描，相邻两行
    # 间隔超过 600 秒（与采集侧 merge_short_gap_sessions 短间隔场次合并
    # 阈值同口径）判定两场分界，给时间上较早的一行标 session_end（它是
    # 其场次的最后一段）。窗口用「上行 started_at + duration_sec」近似
    # 实际收播时刻——轮换旧段的 ended_at 是后台收尾完成时刻、晚于真实
    # 接缝不宜作窗；桥接片窗口横跨接缝、与前后行天然重叠，不会误判拆场。
    # 时间倒序列表里，带标行与其上一行（更晚一场）之间画分隔线；同日
    # 最早一行天然无标（其下没有更早场次），不会在列表底部多出线。
    for gs in groups.values():
        full = next((g for g in gs if g["kind"] == "full"), None)
        if not full or not full["recs"]:
            continue
        prev_it: dict | None = None
        prev_end: _dt.datetime | None = None
        for it in reversed(full["recs"]):   # recs 为新→旧，反转即正序
            try:
                st = _dt.datetime.fromisoformat(it["started_at"])
            except (TypeError, ValueError):
                continue
            if prev_end is not None and prev_it is not None \
                    and (st - prev_end).total_seconds() > 600:
                prev_it["session_end"] = True
            prev_it, prev_end = it, st + _dt.timedelta(
                seconds=it["duration_sec"] or 0)
    # 统一按日期倒序输出（主行路径天然倒序；纯孤儿场景曾正序，v1.2 修复口径）
    out = [{"date": d, "groups": gs} for d, gs in
           sorted(groups.items(), key=lambda kv: kv[0], reverse=True)]
    # v2.0.0：anchor_name 供 SPA 页头（原 Jinja 注入变量改由接口携带）
    return {"auto_full": bool(room["auto_full"]), "live": bool(live),
            "anchor_name": room["anchor_name"] or "", "groups": out}


# ---------------------------------------------------------------------------
# 打点切片 + 直播截取（web 写指令行，capture 2s 内执行）
# ---------------------------------------------------------------------------
_CLIP_TITLE_MAX = 60   # 切片名字长度上限（字符，与录播命名同款）


def _validate_clip_range(start: float, end: float,
                         duration: float) -> tuple[float, float]:
    """打点区间校验：0 ≤ start < end、时长 ≥ min_clip_sec、end 不超成片
    时长（+0.5s 容差，浮点/四舍五入兜底）。"""
    rcfg = cfg["recording"]
    if not (0 <= start < end):
        raise HTTPException(400, "起点必须不小于 0 且小于终点")
    if end - start < float(rcfg.get("min_clip_sec", 10)):
        raise HTTPException(400, f"切片时长至少 {int(rcfg.get('min_clip_sec', 10))} 秒")
    if duration and end > duration + 0.5:
        raise HTTPException(400, f"终点超出成片时长（{int(duration)} 秒）")
    return start, end


class ClipCreateIn(BaseModel):
    title: str = ""
    start_sec: float
    end_sec: float


@app.post("/api/rooms/{room_id}/recordings/{rec_id}/clips")
async def create_clip(room_id: int, rec_id: int, body: ClipCreateIn,
                      request: Request):
    """打点切片：校验成片与区间 → 插 manual_clips pending 行，capture
    扫描循环 2s 内捡起 ffmpeg 快切。权限：本房间切片员（署名=登录昵称）
    或管理员。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入"
                                "（登录时填写昵称）即可切片")
    rec = q1("SELECT status, duration_sec, file_path FROM recordings "
             "WHERE id = ? AND room_id = ?", (rec_id, room_id))
    if not rec:
        raise HTTPException(404, "录制记录不存在")
    if (rec["status"] not in ("finished", "interrupted")
            or not rec["file_path"]):
        raise HTTPException(400, "仅已保存的成片可以切片")
    start, end = _validate_clip_range(body.start_sec, body.end_sec,
                                      float(rec["duration_sec"] or 0))
    title = (body.title or "").strip()
    if len(title) > _CLIP_TITLE_MAX:
        raise HTTPException(400, f"切片名字最长 {_CLIP_TITLE_MAX} 个字符")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
            "end_sec, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (rec_id, room_id, title, start, end, author, now_iso()))
        conn.commit()
        clip_id = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "id": clip_id}


class LiveCutStartIn(BaseModel):
    title: str = ""


@app.post("/api/rooms/{room_id}/recordings/live-cut/start")
async def live_cut_start(room_id: int, body: LiveCutStartIn, request: Request):
    """开始截取当前直播片段：要求该房间有进行中的整场录制（通常为自动
    全程录制）——在其分段流上打开一个墙钟起点标记。同一房间同时只允许
    一个进行中的截取。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入"
                                "（登录时填写昵称）即可截取")
    if q1("SELECT 1 FROM manual_clips WHERE room_id = ? AND status = 'live' "
          "LIMIT 1", (room_id,)):
        raise HTTPException(409, "该房间已有进行中的直播片段截取")
    rec = q1("SELECT id FROM recordings WHERE room_id = ? AND status = "
             "'recording' ORDER BY id DESC LIMIT 1", (room_id,))
    if not rec:
        raise HTTPException(400, "当前没有进行中的整场录制（未开播或已收尾），"
                                "无法截取直播片段")
    title = (body.title or "").strip()
    if len(title) > _CLIP_TITLE_MAX:
        raise HTTPException(400, f"切片名字最长 {_CLIP_TITLE_MAX} 个字符")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clips (recording_id, room_id, title, start_sec, "
            "end_sec, start_wall, status, created_by, created_at) "
            "VALUES (?, ?, ?, 0, 0, ?, 'live', ?, ?)",
            (rec["id"], room_id, title, now_iso(), author, now_iso()))
        conn.commit()
        clip_id = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "id": clip_id}


@app.post("/api/rooms/{room_id}/recordings/live-cut/{clip_id}/stop")
async def live_cut_stop(room_id: int, clip_id: int, request: Request):
    """停止截取：封口为 pending（end_wall=当前时刻），capture 2s 内从
    分段快切成 mp4。片段过短（< min_clip_sec）直接标 failed。幂等。"""
    row = q1("SELECT status, start_wall FROM manual_clips WHERE id = ? "
             "AND room_id = ?", (clip_id, room_id))
    if not row:
        raise HTTPException(404, "截取记录不存在")
    if row["status"] != "live":
        return {"ok": True}
    import datetime as _dt
    t0 = _parse_iso(row["start_wall"])
    end = now_iso()
    elapsed = (_parse_iso(end) - t0).total_seconds() if t0 else 0
    min_len = float(cfg["recording"].get("min_clip_sec", 10))
    new_status, reason = ("pending", "") if elapsed >= min_len else \
                         ("failed", f"片段时长不足（{int(elapsed)} 秒 < {int(min_len)} 秒）")
    conn = connect()
    try:
        conn.execute(
            "UPDATE manual_clips SET status = ?, end_wall = ?, reason = ? "
            "WHERE id = ?", (new_status, end, reason, clip_id))
        conn.commit()
    finally:
        conn.close()
    if new_status == "failed":
        raise HTTPException(400, reason)
    return {"ok": True}


@app.post("/api/rooms/{room_id}/recordings/live-cut/{clip_id}/cancel")
async def live_cut_cancel(room_id: int, clip_id: int, request: Request):
    """取消截取（v2.0.2 修复）：**废弃**该截取——删除 live 行，条目立即
    消失且不产生任何文件（分段属于整场录制本身，无需清理；未封口所以
    云端/本地无任何对象）。与 stop（封口快切保存）语义严格区分；仅
    live 行可取消，已封口的走删除切片。幂等。"""
    row = q1("SELECT status FROM manual_clips WHERE id = ? AND room_id = ?",
             (clip_id, room_id))
    if not row:
        raise HTTPException(404, "截取记录不存在")
    if row["status"] != "live":
        raise HTTPException(409, "仅进行中的截取可取消（已封口的片段请用删除）")
    conn = connect()
    try:
        conn.execute("DELETE FROM manual_clips WHERE id = ?", (clip_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 切片注释 + 切片改名/删除（权限与 bili 侧同口径）
# ---------------------------------------------------------------------------
class ClipNoteIn(BaseModel):
    content: str


def _check_clip_note_target(room_id: int, clip_id: int, note_id: int | None = None):
    """校验切片注释挂载对象：切片行属本房间 +（编辑/删除时）注释行本身。"""
    if note_id is None:
        if not q1("SELECT id FROM manual_clips WHERE id = ? AND room_id = ?",
                  (clip_id, room_id)):
            raise HTTPException(404, "切片不存在")
        return None
    note = q1("SELECT n.* FROM manual_clip_notes n JOIN manual_clips c "
              "ON c.id = n.clip_id WHERE n.id = ? AND c.id = ? AND c.room_id = ?",
              (note_id, clip_id, room_id))
    if not note:
        raise HTTPException(404, "注释不存在")
    return note


@app.post("/api/rooms/{room_id}/clips/{clip_id}/notes")
async def add_clip_note(room_id: int, clip_id: int, body: ClipNoteIn,
                        request: Request):
    """给切片添加注释（与录制注释同款：署名=会话身份，仅本人可改删）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    _check_clip_note_target(room_id, clip_id)
    content = _note_content_or_400(body.content)
    ts = now_iso()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clip_notes (clip_id, author, content, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (clip_id, author, content, ts, ts))
        conn.commit()
        note_id = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "note": {"id": note_id, "author": author,
                                 "content": content}}


@app.patch("/api/rooms/{room_id}/clips/{clip_id}/notes/{note_id}")
async def edit_clip_note(room_id: int, clip_id: int, note_id: int,
                         body: ClipNoteIn, request: Request):
    """修改自己的切片注释（别人的 403，管理员也不例外）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    note = _check_clip_note_target(room_id, clip_id, note_id)
    if note["author"] != author:
        raise HTTPException(403, "只能修改自己的注释")
    content = _note_content_or_400(body.content)
    conn = connect()
    try:
        conn.execute("UPDATE manual_clip_notes SET content = ?, updated_at = ? "
                     "WHERE id = ?", (content, now_iso(), note_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/rooms/{room_id}/clips/{clip_id}/notes/{note_id}")
async def delete_clip_note(room_id: int, clip_id: int, note_id: int,
                           request: Request):
    """删除自己的切片注释（别人的 403，管理员也不例外）。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    note = _check_clip_note_target(room_id, clip_id, note_id)
    if note["author"] != author:
        raise HTTPException(403, "只能删除自己的注释")
    conn = connect()
    try:
        conn.execute("DELETE FROM manual_clip_notes WHERE id = ?", (note_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _load_clip(room_id: int, clip_id: int) -> dict:
    """加载一条本房间的切片行；不存在 404。"""
    row = q1("SELECT * FROM manual_clips WHERE id = ? AND room_id = ?",
             (clip_id, room_id))
    if not row:
        raise HTTPException(404, "切片不存在")
    return row


def _check_clip_editable(request: Request, clip: dict) -> None:
    """切片改/删权限：管理员，或署名本人（切片员昵称即身份）。与注释的
    差别：管理员可操作任何人的切片（文件产物管理员本就管理磁盘资产）。
    本地调试模式的匿名请求经 middleware 注入管理员会话后由此放行。"""
    s = current_session(request)
    if s is not None and s.get("role") == "admin":
        return
    token = request.headers.get("X-Admin-Token") or ""
    if token and token == cfg["capture"]["admin_password"]:
        return
    author = note_author(request)
    if author is None or clip["created_by"] != author:
        raise HTTPException(403, "只有切片创建者与管理员可以操作")


class ClipRenameIn(BaseModel):
    title: str


@app.patch("/api/rooms/{room_id}/clips/{clip_id}")
async def rename_clip(room_id: int, clip_id: int, body: ClipRenameIn,
                      request: Request):
    """切片改名：创建者本人或管理员。空串=清除名字（回到默认按起止时间
    展示）。"""
    clip = _load_clip(room_id, clip_id)
    _check_clip_editable(request, clip)
    title = (body.title or "").strip()
    if len(title) > _CLIP_TITLE_MAX:
        raise HTTPException(400, f"切片名字最长 {_CLIP_TITLE_MAX} 个字符")
    conn = connect()
    try:
        conn.execute("UPDATE manual_clips SET title = ? WHERE id = ?",
                     (title, clip_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "title": title}


@app.delete("/api/rooms/{room_id}/clips/{clip_id}")
async def delete_clip(room_id: int, clip_id: int, request: Request):
    """删除切片：创建者本人或管理员；文件 + 行一并删除。processing 中的
    行不可删（ffmpeg 正在写目标文件，稍候再删）。云端副本联动删除
    （v1.1：切片对象可能在其 oss_bucket 独立桶，空=主桶；v1.2：已上云
    low 副本同桶联动删 + 本地 low 目录一并清；v2.0.2 物理清理抽
    _purge_clip_asset 与删除录播级联共用）。"""
    clip = _load_clip(room_id, clip_id)
    if clip["status"] == "processing":
        raise HTTPException(409, "切片正在处理中，请稍候再删除")
    _check_clip_editable(request, clip)
    _purge_clip_asset(clip)
    conn = connect()
    try:
        conn.execute("DELETE FROM manual_clips WHERE id = ?", (clip_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# OSS 签名直链（私有 bucket，本机释放后的播放/下载通道；v1.2 迁移）
# ---------------------------------------------------------------------------
def _presign_oss(key: str, bucket: str = "", disposition: str = "") -> str:
    """签名 URL 生成助手（oss-url 端点与投稿回拉共用）。失败抛 502/503。"""
    if not oss_enabled():
        raise HTTPException(404, "OSS 未启用")
    client = _oss_public_client()
    if not client:
        raise HTTPException(503, "OSS 客户端不可用")
    import alibabacloud_oss_v2 as oss
    from datetime import timedelta
    exp = int(cfg["oss"].get("url_expiry_sec", 7200))
    kwargs = {"bucket": bucket or cfg["oss"]["bucket"], "key": key}
    if disposition:
        kwargs["response_content_disposition"] = disposition
    try:
        return client.presign(
            oss.models.object_basic.GetObjectRequest(**kwargs),
            expires=timedelta(seconds=exp)).url
    except Exception:
        raise HTTPException(502, "签名生成失败")


@app.get("/api/rooms/{room_id}/oss-url")
async def oss_url(room_id: int, request: Request, key: str,
                  as_: str = Query("play", alias="as"),
                  filename: str = ""):
    """OSS 私有对象签名 URL。安全：① 登录门内（clipper/guest 限本房间，
    middleware 白名单放行）；② key 必须落在本房间前缀下（recordings/
    <room_id>/），跨房 403；③ bucket 私有，一切访问走短时签名（默认 2h）。
    as=download 时附加 Content-Disposition 附件头（恒原画质文件名）。
    v1.1 双 bucket：key 命中 manual_clips.oss_key 时按该行 oss_bucket
    签名（切片只存独立高频桶，bili v1.4 起上云于 [oss].clips_bucket，
    存量已搬移），其余（录播原片/low）一律主桶。
    2026-10-11 修复：oss_low_key（切片低清）同样落切片行——切片与其
    low 都在独立桶，此前只匹配 oss_key 会让 low 错误签主桶 404。
    v2.1 全量 mid 热桶：录播原片/low 按行分派——原片 oss_bucket
    （10 天热窗口在 mid、归档后回主桶）、low 恒 mid（oss_low_bucket）；
    逐表查询均空回退主桶（旧快照缺列同样回退，不因 schema 未对齐打断
    播放）。v2.2 高能表已清除，he_clips 查询分支移除。"""
    if not oss_enabled():
        raise HTTPException(404, "OSS 未启用")
    prefix = f"recordings/{room_id}/"
    if not key.startswith(prefix) or ".." in key:
        raise HTTPException(403, "无权访问该对象")
    bucket = ""
    try:
        clip = q1("SELECT oss_bucket FROM manual_clips "
                  "WHERE oss_key = ? OR oss_low_key = ? LIMIT 1",
                  (key, key))
    except sqlite3.OperationalError:
        clip = None
    if clip and clip["oss_bucket"]:
        bucket = clip["oss_bucket"]
    else:
        try:
            rec = q1("SELECT oss_bucket, oss_low_bucket, oss_key, oss_low_key "
                     "FROM recordings WHERE oss_key = ? OR oss_low_key = ? "
                     "LIMIT 1", (key, key))
        except sqlite3.OperationalError:
            rec = None
        if rec:
            if key == rec["oss_low_key"] and rec["oss_low_bucket"]:
                bucket = rec["oss_low_bucket"]
            elif rec["oss_bucket"]:
                bucket = rec["oss_bucket"]
    bucket = bucket or (cfg["oss"].get("bucket") or "")
    if as_ == "download":
        safe_name = re.sub(r'[^\w.\-\u4e00-\u9fff]', '_',
                           filename or key.rsplit("/", 1)[-1]) or "video.mp4"
        url = _presign_oss(key, bucket,
                           f'attachment; filename="{safe_name}"')
    else:
        url = _presign_oss(key, bucket)
    return {"url": url, "expires_in": int(cfg["oss"].get("url_expiry_sec", 7200))}


# ---------------------------------------------------------------------------
# 切片一键投稿 B 站（v2.0.0：biliup-rs 子进程，详见 upload.py；
# v2.3.0：新视频可加入合集、可追加为现有稿件分P；目标数据与账号管理见
# bili_api.py 与下方 /api/submit/*、/api/admin/submit/* 端点）
# ---------------------------------------------------------------------------
class SubmitIn(BaseModel):
    mode: str = "new"           # new 新视频投稿 / part 追加为现有稿件分P
    title: str = ""             # 空 = 用切片名兜底（仅 new 模式）
    desc: str = ""
    tags: str = ""              # 逗号分隔
    tid: int = 171
    source_type: str = "original"   # original 自制 / reprint 转载
    source_url: str = ""        # 转载必填
    cover_b64: str = ""         # 自定义封面（data URL / 裸 base64；空 = 抽帧）
    season_id: int | None = None    # new：加入合集（None = 不加入）
    section_id: int | None = None   # new：合集小节（多小节必选，默认第一节）
    target_vid: str = ""        # part：目标稿件 av 号或 BV 号
    part_title: str = ""        # part：分P名（= 上传文件名；空 = 用切片名）


@app.post("/api/rooms/{room_id}/clips/{clip_id}/submit")
async def submit_clip(room_id: int, clip_id: int, body: SubmitIn,
                      request: Request):
    """提交投稿任务（clipper/admin 写操作，middleware 已拦 guest 的 POST）。
    3Mbps 上行 → 异步任务 + 前端轮询 submit/<job>；任务表进程内会话态。"""
    author = note_author(request)
    if author is None:
        raise HTTPException(400, "当前会话未绑定昵称，请退出登录后重新进入")
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署（biliup 二进制或 "
                                "cookies.json 缺失，见管理页·账号管理）")
    clip = _load_clip(room_id, clip_id)
    if clip["status"] != "done" or not (clip["file_path"] or clip["oss_key"]):
        raise HTTPException(400, "切片尚未完成或没有可投的视频文件")
    if body.mode not in ("new", "part"):
        raise HTTPException(400, "mode 必须为 new / part")
    # v2.3.0 服务器实测修正：分P追加同样需要 access_token——biliup append
    # 内部先查稿件（APP 接口），网页 cookie 缺 token 时返回 -400「请求错误」
    # 而非可读提示。两模式统一守卫：凭据须为完整 biliup 登录态
    if not bili_api.has_access_token():
        raise HTTPException(503, "投稿需要完整 biliup 凭据（当前凭据缺 "
                                "access_token；请在管理页·账号管理重新配置，"
                                "粘贴浏览器 Cookie 会自动兑换完整登录）")

    if body.mode == "part":
        # ---- 分P模式：只管目标稿件与分P名（原稿元数据不动）----
        vid = body.target_vid.strip()
        if not vid:
            raise HTTPException(400, "追加分P必须选择目标稿件")
        if not (vid.isdigit() or re.fullmatch(r"BV[0-9A-Za-z]{10}", vid)):
            raise HTTPException(400, "目标稿件号须为 av 数字或 BV 号")
        part_title = body.part_title.strip() or (clip["title"] or "").strip()
        if not part_title:
            raise HTTPException(400, "分P名为空（且切片无名可兜底）")
        if len(part_title) > 80:
            raise HTTPException(400, "分P名最长 80 字")
        if not bili_api.sanitize_part_name(part_title):
            raise HTTPException(400, "分P名清洗后无有效字符")
        form = {"mode": "part", "target_vid": vid, "part_title": part_title}
        # v2.3.0 本地投稿器：part 任务先 pending 等认领（家庭宽带快路径），
        # WAIT_WORKER_SEC 内无人认领由服务器兜底执行（3Mbps 慢路径）
        job = upload_mod.create_job(room_id, clip_id, part_title, "part",
                                    pending=True)
        job["target_vid"] = vid

        async def _sign_part(key: str, bucket: str = "") -> str:
            return _presign_oss(key, bucket or "")

        asyncio.create_task(upload_mod.server_fallback(
            job, form, dict(clip), RECORDINGS_ROOT, _sign_part,
            upload_mod.WAIT_WORKER_SEC))
    else:
        # ---- 新视频模式：v2.0.0 表单校验 + v2.3.0 合集参数 ----
        title = (body.title or clip["title"] or "").strip() or \
            f"切片 clip{clip_id}"
        if len(title) > upload_mod.TITLE_MAX:
            raise HTTPException(400, f"标题最长 {upload_mod.TITLE_MAX} 字")
        if len(body.desc) > upload_mod.DESC_MAX:
            raise HTTPException(400, f"简介最长 {upload_mod.DESC_MAX} 字")
        tags = [t.strip() for t in body.tags.split(",") if t.strip()]
        if len(tags) > upload_mod.TAG_MAX:
            raise HTTPException(400, f"标签最多 {upload_mod.TAG_MAX} 个")
        if any(len(t) > upload_mod.TAG_LEN_MAX for t in tags):
            raise HTTPException(400, f"单个标签最长 {upload_mod.TAG_LEN_MAX} 字")
        if body.source_type not in ("original", "reprint"):
            raise HTTPException(400, "source_type 必须为 original / reprint")
        if body.source_type == "reprint" and not body.source_url.strip():
            raise HTTPException(400, "转载投稿必须填写来源地址")
        if body.tid not in upload_mod.TIDS:
            raise HTTPException(400, "投稿分区不在支持列表内")
        if body.season_id and not body.section_id:
            raise HTTPException(400, "选择合集时须同时指定小节（多小节合集"
                                    "请在下拉中选择）")
        form = {"mode": "new", "title": title, "desc": body.desc,
                "tags": ",".join(tags), "tid": body.tid,
                "source_type": body.source_type,
                "source_url": body.source_url.strip(),
                "cover_b64": body.cover_b64,
                "season_id": body.season_id, "section_id": body.section_id}
        job = upload_mod.create_job(room_id, clip_id, title, "new")

        async def _sign(key: str, bucket: str = "") -> str:
            return _presign_oss(key, bucket or "")

        asyncio.create_task(upload_mod.run_job(job, dict(clip), form,
                                               RECORDINGS_ROOT, _sign))
    return {"ok": True, "job": job["id"]}


@app.get("/api/rooms/{room_id}/clips/{clip_id}/submit/{job_id}")
async def submit_status(room_id: int, clip_id: int, job_id: str):
    """投稿任务轮询：uploading / success（带 BV 号）/ failed（带原因）。

    v2.3.0：附 mode（new/part）与 season_note（投稿成功但加入合集失败的
    补充说明，前端据此提示手动处理）。"""
    job = upload_mod.get_job(job_id, room_id, clip_id)
    if not job:
        raise HTTPException(404, "投稿任务不存在（可能已过期或重启清空）")
    return {"job": job["id"], "status": job["status"], "bv": job["bv"],
            "error": job["error"], "started_at": job["started_at"],
            "mode": job.get("mode", "new"),
            "season_note": job.get("season_note", "")}


# ---------------------------------------------------------------------------
# 投稿目标数据（v2.3.0：抽屉打开时拉取——账号信息 + 合集 + 稿件列表）
# ---------------------------------------------------------------------------
@app.get("/api/submit/targets")
async def submit_targets():
    """账号合集与稿件列表（clipper/admin；guest 由 middleware 403）。

    数据来自 B 站创作中心接口（bili_api，60s 进程内缓存）；上游失败 502
    透传可读原因，前端在抽屉里显示并允许重试。"""
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署（biliup 二进制或 "
                                "cookies.json 缺失，见管理页·账号管理）")
    try:
        account, seasons, arcs = await asyncio.gather(
            asyncio.to_thread(bili_api.fetch_account_info),
            asyncio.to_thread(bili_api.fetch_seasons),
            asyncio.to_thread(bili_api.fetch_arcs))
    except bili_api.BiliApiError as e:
        raise HTTPException(502, str(e))
    return {"account": account, "seasons": seasons, "arcs": arcs}


@app.get("/api/submit/arcs/{aid}/parts")
async def submit_arc_parts(aid: int):
    """指定稿件的分P列表（追加分P时选中目标视频后查重用）。"""
    if aid <= 0:
        raise HTTPException(400, "稿件 av 号不合法")
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署")
    try:
        parts = await asyncio.to_thread(bili_api.fetch_archive_parts, aid)
    except bili_api.BiliApiError as e:
        raise HTTPException(502, str(e))
    return {"parts": parts}


# ---------------------------------------------------------------------------
# 本地投稿器（v2.3.0）：任务队列 / 认领 / 结果回报——上传执行搬到家庭宽带
# 机器，服务器只调度记账；认领窗口超时由服务器兜底（见 server_fallback）
# ---------------------------------------------------------------------------
@app.get("/api/submit/queue")
async def submit_queue():
    """pending 的 part 任务列表（本地投稿器轮询用；clipper/admin）。"""
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署")
    return {"jobs": [{"job": j["id"], "clip_id": j["clip_id"],
                      "room_id": j["room_id"], "mode": j.get("mode", "part"),
                      "part_title": j.get("title", ""),
                      "target_vid": j.get("target_vid", "")}
                     for j in upload_mod.pending_part_jobs()]}


@app.post("/api/submit/queue/{job_id}/claim")
async def submit_queue_claim(job_id: str):
    """本地投稿器认领：返回参数载荷与 OSS 签名直链（长有效期，服务端
    url_expiry_sec 配置）；任务已认领/已兜底 → 409。"""
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署")
    j = upload_mod.JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "任务不存在")
    if not upload_mod.claim_job(job_id):
        raise HTTPException(409, "任务已被认领或已过期")
    clip = _load_clip(j["room_id"], j["clip_id"])
    oss_url = ""
    if clip.get("oss_key") and oss_enabled():
        oss_url = _presign_oss(clip["oss_key"], clip.get("oss_bucket") or "")
    return {"job": j["id"], "clip_id": j["clip_id"], "room_id": j["room_id"],
            "mode": j.get("mode", "part"),
            "part_title": j.get("title", ""),
            "target_vid": j.get("target_vid", ""),
            "oss_key": clip.get("oss_key", ""), "oss_url": oss_url}


class SubmitResultIn(BaseModel):
    ok: bool = False
    bv: str = ""
    error: str = ""


@app.post("/api/submit/queue/{job_id}/result")
async def submit_queue_result(job_id: str, body: SubmitResultIn):
    """本地投稿器回报结果（仅认领方可在 uploading 态回报一次）。"""
    j = upload_mod.JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "任务不存在")
    if not upload_mod.complete_job(job_id, {"ok": body.ok, "bv": body.bv,
                                           "error": body.error}):
        raise HTTPException(409, "任务状态不允许回报（未认领或已结束）")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 投稿账号管理（v2.3.0：管理页·账号管理 tab；/api/admin 前缀 middleware
# 已限 admin。cookies 含 SESSDATA/access_token 等敏感凭据——只写不读，
# 状态端点仅回有效性与账号昵称/mid/头像，绝不回传 cookie 内容）
# ---------------------------------------------------------------------------
class SubmitCookiesIn(BaseModel):
    cookies_json: str = ""      # biliup login 生成的 cookies.json 全文


@app.get("/api/admin/submit/cookies-file")
async def submit_cookies_file():
    """本地投稿器取 biliup 凭据文件内容（admin 专属）。敏感凭据仅回传
    自己的投稿账号——投稿器每轮拉取，服务器侧 renew 后本机自动同步。"""
    p = upload_mod.cookies_path()
    if not p.exists():
        raise HTTPException(404, "凭据文件不存在（先在账号管理配置投稿账号）")
    return JSONResponse({"cookies": p.read_text("utf-8")})


@app.post("/api/admin/submit/sync")
async def submit_sync_run():
    """手动触发一轮投稿记录同步（存在性核验 + 同名分P回填；平时由
    daemon 线程每 6h 自动执行）。返回统计。"""
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署")
    try:
        st = await asyncio.to_thread(submit_sync.sync_once)
    except Exception as e:                      # noqa: BLE001
        raise HTTPException(502, f"同步失败：{e}")
    return {"ok": True, **st}


@app.get("/api/admin/submit/account")
async def submit_account_status():
    """投稿账号状态 + 投稿配置清单（v2.4.0 暴露逐项配置值与路径，前端
    按项展示状态徽标与未就绪说明）。

    upload_ready=False 表示网页 cookie（创作中心接口可用、新视频投稿的
    APP 接口缺 access_token 不可用）——前端账号页给出提示。
    """
    bin_path = upload_mod.biliup_bin()
    ck_path = upload_mod.cookies_path()
    st = {"bin_configured": bool(bin_path),
          "bin_path": bin_path,
          "cookies_path": str(ck_path),
          "cookies_present": ck_path.exists(),
          "submit_enabled": upload_mod.submit_enabled(),
          "upload_ready": bili_api.has_access_token(),
          "cookie_ok": None, "account": None, "error": ""}
    if st["cookies_present"]:
        try:
            info = await asyncio.to_thread(bili_api.fetch_account_info)
            st["cookie_ok"] = info["ok"]
            st["account"] = info
            if not info["ok"]:
                st["error"] = "cookies 已失效（nav 未登录态），请重新生成"
        except bili_api.BiliApiError as e:
            st["cookie_ok"] = False
            st["error"] = str(e)
    return st


@app.post("/api/admin/submit/cookies")
async def submit_cookies_set(body: SubmitCookiesIn):
    """保存投稿凭据：支持完整 biliup cookies.json **或**浏览器 Cookie 串。

    网页 Cookie 串保存时自动尝试「网页 Cookie 登录兑换」（bili_api
    .exchange_web_login，biliup「网页Cookie登录1」等价：TV auth_code +
    网页会话自动确认 + 轮询）换取完整凭据（含 access_token，新视频投稿
    必需）；兑换失败则按网页凭据保存并单独返回原因（合集/分P 查询不受
    影响）。落盘后立即 nav 验证；网络原因验证失败不回滚。"""
    try:
        cred = await asyncio.to_thread(bili_api.parse_credential_text,
                                       body.cookies_json or "")
    except bili_api.BiliApiError as e:
        raise HTTPException(400, str(e))
    exchanged = False
    exchange_error = ""
    kept_existing = False
    doc = cred["doc"]
    if cred["kind"] == "web":
        try:
            doc = await asyncio.to_thread(bili_api.exchange_web_login,
                                          body.cookies_json)
            exchanged = True
        except bili_api.BiliApiError as e:
            exchange_error = str(e)
            # 现有凭据仍是完整登录（含 access_token）时不覆盖：网页 Cookie
            # 兑换失败绝不能把好凭据降级成无 token 网页格式（2026-10-19
            # 服务器实况：renew 后重贴 cookie，兑换瞬时 -101 回退覆盖，
            # 导致 6 个分P任务全 -400）
            if bili_api.has_access_token():
                kept_existing = True
                doc = None
    if doc is not None:
        p = upload_mod.cookies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
    bili_api.drop_cache()
    try:
        info = await asyncio.to_thread(bili_api.fetch_account_info)
        return {"saved": doc is not None, "kind": cred["kind"],
                "exchanged": exchanged, "exchange_error": exchange_error,
                "kept_existing": kept_existing,
                "upload_ready": bili_api.has_access_token(),
                "cookie_ok": info["ok"], "account": info}
    except bili_api.BiliApiError as e:
        return {"saved": doc is not None, "kind": cred["kind"],
                "exchanged": exchanged, "exchange_error": exchange_error,
                "kept_existing": kept_existing,
                "upload_ready": bili_api.has_access_token(),
                "cookie_ok": False, "account": None, "error": str(e)}


@app.delete("/api/admin/submit/cookies")
async def submit_cookies_del():
    """清除投稿凭据（前端二次确认；清除后投稿功能回到未部署态 503）。"""
    p = upload_mod.cookies_path()
    if p.exists():
        p.unlink()
    bili_api.drop_cache()
    return {"ok": True}


@app.post("/api/admin/submit/renew")
async def submit_renew():
    """刷新投稿登录（biliup renew：refresh_token 续期并原地更新文件）。"""
    if not upload_mod.submit_enabled():
        raise HTTPException(503, "投稿功能未部署（二进制或 cookies 缺失）")
    try:
        msg = await upload_mod.renew_login()
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    bili_api.drop_cache()
    return {"ok": True, "message": msg}


# ---------------------------------------------------------------------------
# v2.4.0 扫码登录（账号管理页）：web 二维码 generate/poll，进程内会话。
# B 站侧二维码 180s 有效；confirm 后合并设备指纹落盘 cookies.json 并 nav
# 验证。密码登录不实现（极验 geetest 人机验证无法服务端自动化，B 站网页
# 端亦已停用密码登录，调研见 bili_api.py v2.4.0 注释）。
# ---------------------------------------------------------------------------
_QR_SESSIONS: dict[str, float] = {}
_QR_TTL_SEC = 300          # 本侧留存上限（长于 B 站 180s 有效期）
_QR_MAX = 20               # 并发二维码会话上限


def _qr_prune() -> None:
    """清理过期二维码会话；超上限先踢最旧的。"""
    now = time.time()
    for k in [k for k, ts in _QR_SESSIONS.items() if now - ts > _QR_TTL_SEC]:
        _QR_SESSIONS.pop(k, None)
    while len(_QR_SESSIONS) > _QR_MAX:
        _QR_SESSIONS.pop(min(_QR_SESSIONS, key=_QR_SESSIONS.get), None)


@app.post("/api/admin/submit/qr/start")
async def submit_qr_start():
    """申请扫码登录二维码：返回 qrcode_key + SVG data URI（前端 <img> 直渲）。"""
    import base64 as _b64
    import io
    key, url = await asyncio.to_thread(bili_api.qr_login_start)
    svg = ""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgImage,
                          box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        svg = _b64.b64encode(buf.getvalue()).decode()
    except Exception as e:              # noqa: BLE001 —— SVG 失败不阻断登录
        print(f"[qr] SVG 生成失败：{e}", flush=True)
    _qr_prune()
    _QR_SESSIONS[key] = time.time()
    return {"qrcode_key": key, "qr_url": url,
            "svg": f"data:image/svg+xml;base64,{svg}"}


@app.get("/api/admin/submit/qr/poll")
async def submit_qr_poll(qrcode_key: str):
    """轮询扫码状态；confirmed 时合并设备指纹落盘并 nav 验证。"""
    if qrcode_key not in _QR_SESSIONS:
        raise HTTPException(404, "二维码会话不存在或已过期")
    st = await asyncio.to_thread(bili_api.qr_login_poll, qrcode_key)
    if st["state"] != "confirmed":
        return {"state": st["state"]}
    if not st["cookies"].get("SESSDATA") or not st["cookies"].get("bili_jct"):
        _QR_SESSIONS.pop(qrcode_key, None)
        raise HTTPException(502, "扫码确认响应缺少会话 Cookie（可能被风控拦截）")
    merged = dict(bili_api._fingerprint_cookie_dict())
    merged.update(st["cookies"])
    doc = bili_api.build_submit_cookies_doc(merged, st["refresh_token"])
    p = upload_mod.cookies_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
    bili_api.drop_cache()
    _QR_SESSIONS.pop(qrcode_key, None)
    # v2.4.0：扫码不含 access_token——确认后自动补齐（新视频投稿必需）：
    # ① 先 renew（refresh_token 兑换 + 指纹合并修复）；网页扫码会话的
    #    refresh_token 换不出 app token（实测 B 站只回新 refresh_token、
    #    access_token 恒空）；② 仍缺则走网页会话 TV 兑换（biliup「网页
    #    Cookie登录1」等价路径，实测可换出 access_token）。任何一步失败
    #    不推翻登录结果，仅透传说明（前端据此提示受限）。
    upload_ready = bili_api.has_access_token()
    renew_note = ""
    cookie_str = "; ".join(f"{k}={v}" for k, v in merged.items())
    if not upload_ready and upload_mod.biliup_bin():
        try:
            await upload_mod.renew_login()
            bili_api.drop_cache()
            upload_ready = bili_api.has_access_token()
        except RuntimeError as e:
            renew_note = f"自动续期失败：{e}"[:150]
    if not upload_ready:
        try:
            doc2 = await asyncio.to_thread(bili_api.exchange_web_login,
                                           cookie_str)
            old_doc = json.loads(p.read_text("utf-8"))
            doc3 = bili_api.merge_renewed_cookies(old_doc, doc2)
            p.write_text(json.dumps(doc3, ensure_ascii=False), "utf-8")
            bili_api.drop_cache()
            upload_ready = bili_api.has_access_token()
            if not upload_ready:
                renew_note = "自动续期与兑换均未获得 access_token"
        except Exception as e:              # noqa: BLE001 —— 兜底失败仅提示
            renew_note = (renew_note + ("；" if renew_note else "")
                          + f"兑换失败：{e}")[:200]
    try:
        info = await asyncio.to_thread(bili_api.fetch_account_info)
    except bili_api.BiliApiError as e:
        return {"state": "confirmed", "account": None,
                "cookie_str": cookie_str, "upload_ready": upload_ready,
                "renew_note": renew_note, "error": str(e)}
    return {"state": "confirmed", "account": info, "cookie_str": cookie_str,
            "upload_ready": upload_ready, "renew_note": renew_note}


@app.get("/api/rooms/{room_id}/clips/{clip_id}/cover-preview")
async def cover_preview(room_id: int, clip_id: int):
    """默认封面预览：本地文件中点抽帧（磁盘缓存到 data/tmp/）。仅云端的
    切片不回拉整个视频抽帧（3Mbps 出向不划算），前端显示占位并引导上传
    自定义封面。"""
    clip = _load_clip(room_id, clip_id)
    src = upload_mod.pick_local_source(clip, RECORDINGS_ROOT)
    if src is None:
        raise HTTPException(404, "本机已释放的切片暂无法抽帧，可上传自定义封面")
    dst = upload_mod.TMP_DIR / f"cover_prev_{clip_id}.jpg"
    if not (dst.exists() and dst.stat().st_size > 0):
        ok = await asyncio.to_thread(upload_mod.extract_cover,
                                     src, clip.get("duration_sec"), dst)
        if not ok:
            raise HTTPException(503, "封面抽帧失败（ffmpeg 不可用？）")
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# 页面路由（v2.0.0 React SPA）+ 静态资源
# ---------------------------------------------------------------------------
from fastapi.staticfiles import StaticFiles

# 前端构建产物（frontend/ 下 npm run build 生成；dist 不入 git，随 tar
# 部署——同 bus-manager v2.0.0 先例）。页面路由统一发 SPA shell，客户端
# 按 URL 渲染视图；登录门 / SSO 桥接 / 「到达 /login 即退出」语义全部
# 保留在 middleware 与路由层，SPA 只消费 Cookie 与 /api/auth/me。
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_INDEX_HTML = FRONTEND_DIST / "index.html"
# 目录确保存在：Starlette StaticFiles 首个请求时会校验目录（check_dir=False
# 只豁免 import 期），未构建环境先建空目录，请求 404 而非 RuntimeError
(FRONTEND_DIST / "assets").mkdir(parents=True, exist_ok=True)


def spa_shell() -> Response:
    """SPA shell 响应；未构建（新克隆/换机未跑 npm）时 503 带自解释文案。"""
    if _INDEX_HTML.exists():
        return FileResponse(_INDEX_HTML, headers={"Cache-Control": "no-store"})
    return PlainTextResponse(
        "前端未构建：请在项目 frontend/ 目录执行 npm install && npm run build "
        "（产物 dist/ 随部署同步，不入 git）", status_code=503)


# Vite 内容寻址资源（/assets/index-<hash>.js）：check_dir=False 允许未构建
# 时启动（运行期 404，页面 shell 503 已引导）；middleware 对 /assets 放行
# 且不禁缓存（文件名即版本号）
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets"),
                                 check_dir=False), name="assets")

# 录像产物静态挂载（整场成片 + 切片；StaticFiles 原生支持 HTTP Range →
# 浏览器 <video> 可拖动播放）。目录即 bili-monitor 的 data/recordings/，
# 本服务不写视频只读播放；访问控制见 middleware（clipper/guest 限本房间前缀）。
RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_ROOT)),
          name="recordings")


@app.get("/login")
async def login_page(request: Request):
    """统一登录页（SPA 登录视图）：管理员 / 切片员 / 游客三角色 + 公共
    监控码输入框。「到达即退出」语义由 middleware 在本路由前完成（清本站
    会话 + 抑制 SSO 续登），SPA 无需处理。"""
    return spa_shell()


@app.get("/manage")
async def admin_page(request: Request):
    """管理页（/manage，路由名与 bili-monitor 管理页一致）：房间卡片
    （监控码 + 切片员密码 + 自动全程录制开关）。
    middleware 已限管理员（调试模式匿名请求被注入管理员会话）；路由内
    再校验一次防旁路。"""
    s = current_session(request)
    if s is None or s.get("role") != "admin":
        raise HTTPException(403, "管理页仅管理员可访问")
    return spa_shell()


@app.get("/admin", include_in_schema=False)
async def admin_page_legacy(request: Request):
    """旧路径跳转（/admin → /manage）：改名过渡便利，无任何站内/外部
    链接指向旧路径，后续版本可删。"""
    return RedirectResponse(f"{cfg['server']['root_path']}/manage",
                            status_code=302)


@app.get("/{room_id}/replay")
async def replay_page(request: Request, room_id: int):
    """回放页（/replay/<房间号>/replay，SPA 回放视图）。

    独立动线：不继承任何监控页面导航；页头按角色显隐「管理」入口与
    「退出」。角色/署名（role/me/is_admin/can_write 的派生源）由 SPA 启动
    时 GET /api/auth/me 获取；房间存在性校验仍在本路由（404 语义不变）。"""
    row = q1("SELECT anchor_name FROM replay_rooms WHERE room_id = ?",
             (room_id,))
    if not row:
        raise HTTPException(404, "房间不在回放站名单中")
    return spa_shell()


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_root_files(full_path: str):
    """兜底路由（注册顺序在最后，优先级最低）：① Vite public/ 直拷到 dist
    根的文件（favicon 等）直出（路径限制在 dist 内，防目录穿越）；② 其余
    未知路径发 SPA shell（前端路由自显 404/回正）；③ 未知 /api 路径保持
    404 JSON 语义不被 shell 吞掉。"""
    if full_path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    if full_path:
        f = (FRONTEND_DIST / full_path).resolve()
        try:
            f.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            raise HTTPException(404, "Not Found")
        if f.is_file():
            return FileResponse(f)
    return spa_shell()
