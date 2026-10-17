# -*- coding: utf-8 -*-
"""FastAPI 应用：查询接口 + 房间名单管理 + 登录（管理员/游客双角色）+ 数据分析页。

- 只读接口直接查 SQLite（WAL 模式下与采集进程读写互不阻塞）；
- 登录体系（v0.5）：/login 页双模式登录（管理员账号 / 游客码）→ 30 天签名
  Cookie；middleware 全站校验（游客只读，写接口仅管理员）。capture.admin_password
  未配置时为本地调试模式，不强制登录（行为与历史版本一致）；
- 旧通道保留：X-Admin-Token（或请求体 token）等于 admin_password 时视为管理员，
  既有测试脚本无需改动。

启动：uv run uvicorn bili_monitor.web.app:app --host 127.0.0.1 --port 8001
      （部署时加 --root-path /bili-monitor，或用 config 里的 root_path）
"""
import asyncio
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from pydantic import BaseModel

from ..config import DATA_DIR, get_config
from ..db import connect, init_db, now_iso
from . import auth

cfg = get_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务启动钩子：首次运行播种初始登录凭据（幂等）+ 官方通道事件中枢。

    放在 lifespan 而非模块级：测试脚本 import 本模块（常指向临时库）时
    不触发，避免在临时库上生成凭据、把无效明文追加进凭据文件。
    v2.2：capture 采集与转码+上云+释放管线已整体迁 replay 项目（其 web
    lifespan 承载管线线程），本服务收敛为纯查询/管理面。
    v2.3.1：official 通道常驻 WebSocket 事件监听（群指令自动回复）；
    TestClient 不以 with 使用时不触发 lifespan，单测不受影响。"""
    auth.seed_web_credentials()
    ev_stop = asyncio.Event()
    ev_task = asyncio.create_task(_official_event_loop(ev_stop))
    yield
    ev_stop.set()
    ev_task.cancel()


app = FastAPI(
    title="bili-monitor",
    description="B站直播间数据监控系统 API",
    # 部署在 Nginx 路径前缀之后时（/bili-monitor/），root_path 让
    # 生成的 docs / OpenAPI 路径正确（已通过 context7 核对用法）
    root_path=cfg["server"]["root_path"],
    lifespan=lifespan,
)
# v0.8 性能优化：接口响应 gzip 压缩（level 6 折中）。弹幕/曲线类 JSON 压缩比
# 约 8~10 倍，公网传输时间大幅下降；Nginx 默认不为 application/json 开 gzip，
# 故在应用层做（本地与服务器行为一致）。仅压 ≥1KB 响应，避免小包反增开销。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


# ---------------------------------------------------------------------------
# 全站 middleware：登录门（v0.5 双角色）+ API 禁缓存
# ---------------------------------------------------------------------------
# 免登录路径（相对 root_path）：登录页 / 登录接口 / 前端构建产物
#（v1.6 React SPA：/assets/ 为 Vite 内容寻址文件名，免登录 + 可缓存）
_PUBLIC_PATHS = ("/login", "/api/auth/login")
_PUBLIC_PREFIXES = ("/assets/",)

import re as _re
# v1.6 剥离：本站不再有回放页——旧地址 /<房>/replay 一律 302 引导至独立
# replay 项目（其登录门接管；bili 管理员经 SSO 桥接无感续登）
_REPLAY_PAGE_RE = _re.compile(r"^/(\d+)/replay$")


def _rel_path(request: Request) -> str:
    """剥掉 root_path 前缀后的应用内路径（本地开发 rp 为空串，路径原样）。"""
    rp = cfg["server"]["root_path"]
    p = request.url.path
    if rp and p.startswith(rp):
        p = p[len(rp):]
    return p


@app.middleware("http")
async def auth_gate_and_no_cache(request: Request, call_next):
    """统一拦截：

    1. 登录校验——仅在 capture.admin_password 已配置（生产）时启用；
       未配置=本机调试模式，全站放行（与历史行为一致）。规则：
       - 未登录：页面 302 → /login（带 ?next= 回跳参数），API 返回 401；
         例外：旧 replay 按钮页 302 → 新回放站（replay 项目，其登录门接管）；
       - guest：replay 页 403；写方法 API 403（只读监控）；
       - admin：全放行（含旧 replay 页——过渡期仍可直访）。
    2. API 响应禁用浏览器缓存（监控数据实时变化，浏览器启发式缓存会让
       页面拿到补全前的旧数据；静态资源不受影响仍可缓存）。
    """
    def is_public(p: str) -> bool:
        return p in _PUBLIC_PATHS or any(p.startswith(x) for x in _PUBLIC_PREFIXES)

    if cfg["capture"]["admin_password"]:
        p = _rel_path(request)
        rp = cfg["server"]["root_path"]
        if is_public(p):
            response = await call_next(request)
        else:
            # ---- 受保护路径：按角色分派 ----
            session = auth.get_session(request)
            role = session["role"] if session else auth.get_role(request)  # 旧通道兜底
            # replay 旧地址（/123/replay）：任何角色一律 302 → 独立回放站
            m_page = _REPLAY_PAGE_RE.match(p)
            if m_page:
                base = cfg.get("web", {}).get("replay_base", "/replay")
                return RedirectResponse(f"{base}/{m_page.group(1)}/replay",
                                        status_code=302)
            if role is None:
                if p.startswith("/api"):
                    return JSONResponse({"detail": "未登录"}, status_code=401)
                from urllib.parse import quote
                return RedirectResponse(f"{rp}/login?next={quote(p)}", status_code=302)
            if (role != "admin" and p.startswith("/api")
                    and request.method in ("POST", "PUT", "DELETE", "PATCH")):
                # guest 的写方法
                return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
            response = await call_next(request)
    else:
        response = await call_next(request)
    # API 与页面响应一律禁缓存（监控数据实时变化 + 页面随版本更新，浏览器
    # 启发式缓存会让旧脚本滞留标签页）；/assets 内容寻址资源不设（文件名即版本）
    if not request.url.path.startswith(("/static", "/assets")):
        response.headers["Cache-Control"] = "no-store"
    return response


def q(sql: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        conn.row_factory = None
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def q1(sql: str, params: tuple = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def check_admin(request: Request, password: str | None = None) -> None:
    """写操作权限校验（v0.5）：需管理员角色登录会话，或旧通道
    （Header X-Admin-Token / 请求体 token）与 capture.admin_password 匹配。
    admin_password 未配置=本机调试模式，不校验（与历史行为一致）。"""
    expect = cfg["capture"]["admin_password"]
    if not expect:
        return
    if auth.get_role(request) == "admin":   # 已含 X-Admin-Token 匹配分支
        return
    token = request.headers.get("X-Admin-Token") or password
    if token == expect:                     # 兼容请求体/查询参数传 token 的旧调用
        return
    raise HTTPException(403, "需要管理员权限（请用管理员账号登录）")


# ---------------------------------------------------------------------------
# 登录 / 登出 / 身份（v0.5：管理员 + 游客双角色）
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    mode: str                      # admin / guest
    username: str | None = None    # admin 模式：用户名
    password: str | None = None    # admin 模式：口令
    code: str | None = None        # guest 模式：游客码


@app.post("/api/auth/login")
async def login(body: LoginIn):
    """登录：管理员（账号+密码）或游客（游客码）。成功签发 30 天 HttpOnly
    签名 Cookie；失败 401（不区分"账号不存在/密码错误"，防账号枚举）。"""
    if body.mode == "admin":
        row = q1("SELECT password_hash FROM admin_users WHERE username = ?",
                 (body.username or "",))
        if not row or not auth.verify_secret(body.password or "", row["password_hash"]):
            raise HTTPException(401, "账号或密码错误")
        value = auth.sign_session("admin", body.username)
    elif body.mode == "guest":
        row = q1("SELECT value FROM app_secrets WHERE key = 'guest_code'")
        if not row or not auth.verify_secret(body.code or "", row["value"]):
            raise HTTPException(401, "游客码错误")
        value = auth.sign_session("guest")
    else:
        raise HTTPException(400, "mode 必须为 admin 或 guest")
    resp = JSONResponse({"ok": True, "role": body.mode})
    resp.set_cookie(auth.COOKIE_NAME, value, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def logout():
    """登出：清除会话 Cookie。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    """当前会话身份（前端据此显隐写入口）。本地调试模式（admin_password
    未配置）一律视为管理员，保持本地开发全功能可用。"""
    if not cfg["capture"]["admin_password"]:
        return {"role": "admin", "username": "(local)"}
    session = auth.read_session(request)
    if session is not None:
        return session
    if auth.get_role(request) == "admin":   # X-Admin-Token 旧通道
        return {"role": "admin", "username": "(token)"}
    return {"role": None, "username": None}


# ---------------------------------------------------------------------------
# 管理员账户管理（不开放注册；仅管理员可操作，v0.5）
# ---------------------------------------------------------------------------
class AdminIn(BaseModel):
    username: str
    password: str
    token: str | None = None


@app.get("/api/admins")
async def list_admins(request: Request, token: str | None = None):
    """管理员账户列表。"""
    check_admin(request, token)
    return {"admins": q("SELECT username, created_at FROM admin_users ORDER BY created_at")}


@app.post("/api/admins")
async def add_admin(body: AdminIn, request: Request):
    """添加管理员账户（口令哈希入库，明文不落任何文件）。"""
    check_admin(request, body.token)
    name = body.username.strip()
    if not name or not set(name) <= auth.USERNAME_OK:
        raise HTTPException(400, "用户名仅限字母/数字/下划线/中划线")
    if len(body.password) < 8:
        raise HTTPException(400, "口令至少 8 位")
    if q1("SELECT username FROM admin_users WHERE username = ?", (name,)):
        raise HTTPException(409, "用户名已存在")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (name, auth.hash_secret(body.password), now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/admins/{username}")
async def del_admin(username: str, request: Request, token: str | None = None):
    """删除管理员：不允许删除当前登录账户或最后一个账户（避免把自己锁在门外）。"""
    check_admin(request, token)
    session = auth.read_session(request)
    if session and session.get("username") == username:
        raise HTTPException(400, "不能删除当前登录的账户")
    if q1("SELECT count(*) AS c FROM admin_users")["c"] <= 1:
        raise HTTPException(400, "必须至少保留一个管理员")
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM admin_users WHERE username = ?", (username,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "账户不存在")
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/admins/reset-guest-code")
async def reset_guest_code(request: Request, token: str | None = None):
    """重置游客码：新码明文仅在本次响应返回一次（库中只存哈希）。
    已登录游客的旧会话不受影响（签名自包含），重置只挡住之后的新登录。"""
    check_admin(request, token)
    code = auth.gen_password(12)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO app_secrets (key, value) VALUES ('guest_code', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (auth.hash_secret(code),))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "guest_code": code}


# ---------------------------------------------------------------------------
# 房间名单管理（页面增删，采集服务 30s 内热加载）
# ---------------------------------------------------------------------------
class RoomIn(BaseModel):
    room_id: int
    token: str | None = None


@app.post("/api/rooms")
async def add_room(body: RoomIn, request: Request):
    check_admin(request, body.token)
    rid = body.room_id
    if q1("SELECT room_id FROM rooms WHERE room_id = ?", (rid,)):
        raise HTTPException(409, "房间已在监控名单中")
    # 调 B站接口校验房间存在并取主播昵称/头像（v2.2 capture 迁出后改用本侧
    # bili_api 匿名会话：UA 防风控；加房间为低频操作，匿名接口可用性足够）
    from .bili_api import build_bili_session, fetch_anchor_info, fetch_room_info
    async with build_bili_session() as s:
        info = await fetch_room_info(s, rid)
        if info is None:
            raise HTTPException(404, "房间不存在或查询失败")
        anchor = await fetch_anchor_info(s, info["room_id"])
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO rooms (room_id, short_id, anchor_name, avatar_url, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (info["room_id"], info.get("short_id", 0),
             anchor.get("uname", ""), anchor.get("face", ""), now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "room_id": info["room_id"],
            "anchor_name": anchor.get("uname", ""), "avatar_url": anchor.get("face", "")}


@app.delete("/api/rooms/{room_id}")
async def remove_room(room_id: int, request: Request,
                      token: str | None = None, keep_history: bool = True):
    """移除监控。keep_history=true（默认）只停用保留历史；false 彻底删除名单行。"""
    check_admin(request, token)
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不在名单中")
    conn = connect()
    try:
        if keep_history:
            conn.execute("UPDATE rooms SET enabled = 0 WHERE room_id = ?", (room_id,))
        else:
            # 彻底移除：明细（场次/弹幕/礼物）按约定保留，仅删名单行；
            # live_sessions 等表外键引用 rooms，需对本连接关闭外键检查才能删
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "paused": keep_history}


@app.post("/api/rooms/{room_id}/enable")
async def enable_room(room_id: int, request: Request, token: str | None = None):
    """恢复已暂停的房间监控。"""
    check_admin(request, token)
    conn = connect()
    try:
        cur = conn.execute("UPDATE rooms SET enabled = 1 WHERE room_id = ?", (room_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "房间不在名单中")
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 主播描述与关键词标签（v0.5：总览页展示，仅管理员可修改）
# ---------------------------------------------------------------------------
class DescIn(BaseModel):
    description: str
    token: str | None = None


class TagIn(BaseModel):
    tag: str
    token: str | None = None


def _room_tags() -> dict[int, list[str]]:
    """全部房间标签按房间分组（房间数少，一次查询取回；按创建时间排序）。"""
    out: dict[int, list[str]] = {}
    for r in q("SELECT room_id, tag FROM room_tags ORDER BY created_at, tag"):
        out.setdefault(r["room_id"], []).append(r["tag"])
    return out


@app.patch("/api/rooms/{room_id}/description")
async def set_description(room_id: int, body: DescIn, request: Request):
    """设置主播描述（总览页原"房间号"行位置；去首尾空格，上限 100 字）。"""
    check_admin(request, body.token)
    desc = body.description.strip()[:100]
    conn = connect()
    try:
        cur = conn.execute("UPDATE rooms SET description = ? WHERE room_id = ?",
                           (desc, room_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "房间不在名单中")
    finally:
        conn.close()
    return {"ok": True, "description": desc}


@app.post("/api/rooms/{room_id}/tags")
async def add_tag(room_id: int, body: TagIn, request: Request):
    """添加一个关键词标签（1~12 字，每房间上限 8 个；重复静默忽略）。"""
    check_admin(request, body.token)
    tag = body.tag.strip()
    if not tag or len(tag) > 12:
        raise HTTPException(400, "标签须为 1~12 个字")
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不在名单中")
    if q1("SELECT count(*) AS c FROM room_tags WHERE room_id = ?", (room_id,))["c"] >= 8:
        raise HTTPException(400, "每房间最多 8 个标签")
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO room_tags (room_id, tag, created_at) "
                     "VALUES (?, ?, ?)", (room_id, tag, now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/rooms/{room_id}/tags/{tag}")
async def del_tag(room_id: int, tag: str, request: Request, token: str | None = None):
    """删除一个关键词标签。"""
    check_admin(request, token)
    conn = connect()
    try:
        conn.execute("DELETE FROM room_tags WHERE room_id = ? AND tag = ?", (room_id, tag))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# v2.2 收益/人气曲线（详情页原高能时刻板块位置的替代板块数据源）
# ---------------------------------------------------------------------------
def _parse_iso(s: str):
    """ISO8601 秒级时间串 → datetime（与采集侧写入格式一致）。"""
    import datetime as dt
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


@app.get("/api/rooms/{room_id}/sessions/{session_id}/curves")
async def session_curves(room_id: int, session_id: int):
    """场次收益曲线 + 人气曲线叠加数据（v2.2，替代已移除的高能时刻板块）。

    收益（元/分）：gifts 表（gift/sc/guard 全口径，value_yuan 采集时已按
    盲盒映射兜底折算）按分钟桶聚合；人气：anchor_snapshots.online（HTTP
    主播指标快照的加权人气，同总览页口径）——WS 侧 popularity_snapshots.
    popularity 上游已恒推 1（2026-10-12 核实全部历史快照均为 1），不可用。
    快照约 2 分钟一轮，分钟网格每格取时间最近的快照值（就近取，查询窗口
    向两侧放宽兜边界），某分钟无礼物时 rev 给 null（前端 connectNulls
    断点续画）。窗口为场次起止（在播场次取到当前分钟）各放宽 1 分钟兜
    边界取整误差；超长场次（>2000 分钟）退化为只输出有值分钟（此时人气
    不再就近铺满，仅保留快照所在分钟），避免响应膨胀。"""
    import time as _time
    import datetime as _dt2
    fmt = "%Y-%m-%dT%H:%M:%S"

    def _epoch_min(iso: str) -> int:
        return int(_time.mktime(_time.strptime(iso, fmt))) // 60

    sess = q1("SELECT session_id, start_time, end_time FROM live_sessions "
              "WHERE session_id = ? AND room_id = ?", (session_id, room_id))
    if not sess:
        raise HTTPException(404, "场次不存在")
    start = _parse_iso(sess["start_time"])
    end = _parse_iso(sess["end_time"]) or _dt2.datetime.now()
    if start is None or end <= start:
        raise HTTPException(404, "场次时间无法解析")
    m0 = _epoch_min(sess["start_time"]) - 1
    m1 = _epoch_min(end.strftime(fmt)) + 1
    rev: dict[int, float] = {}
    for g in q("SELECT ts, value_yuan FROM gifts "
               "WHERE session_id = ? AND value_yuan > 0", (session_id,)):
        m = _epoch_min(g["ts"])
        rev[m] = round(rev.get(m, 0.0) + float(g["value_yuan"]), 2)
    # 人气快照：网格外两侧各放宽约 2 分钟（一个采样周期），保证边界分钟
    # 的「最近快照」存在
    snaps = q("SELECT ts, online FROM anchor_snapshots "
              "WHERE room_id = ? AND online > 0 AND ts >= ? AND ts <= ? "
              "ORDER BY ts",
              (room_id, _dt2.datetime.fromtimestamp((m0 - 1) * 60).strftime(fmt),
               _dt2.datetime.fromtimestamp((m1 + 1) * 60).strftime(fmt)))
    s_ts = [int(_time.mktime(_time.strptime(s["ts"], fmt))) for s in snaps]
    s_val = [int(s["online"]) for s in snaps]
    pop: dict[int, int] = {}
    if m1 - m0 <= 2000:
        grid = list(range(m0, m1 + 1))
        # 就近取：网格升序 + 快照升序，双指针取每格中点最近的快照值
        j = 0
        for m in grid:
            target = m * 60 + 30
            while (j + 1 < len(s_ts)
                   and abs(s_ts[j + 1] - target) <= abs(s_ts[j] - target)):
                j += 1
            if s_ts:
                pop[m] = s_val[j]
    else:
        for k, t in enumerate(s_ts):
            pop[t // 60] = s_val[k]
        grid = sorted(set(rev) | set(pop))
    # 峰值只统计场次窗口内的快照（放宽带的窗外快照不计入）
    pk = q1("SELECT MAX(online) AS mx FROM anchor_snapshots "
            "WHERE room_id = ? AND online > 0 AND ts >= ? AND ts <= ?",
            (room_id, sess["start_time"], end.strftime(fmt)))
    points = [{
        "t": _dt2.datetime.fromtimestamp(m * 60).strftime(fmt),
        "rev": rev.get(m),
        "pop": pop.get(m),
    } for m in grid]
    return {"session_id": session_id,
            "total_rev": round(sum(rev.values()), 2),
            "peak_pop": int(pk["mx"] or 0),
            "points": points}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------
@app.get("/api/rooms")
async def list_rooms(request: Request):
    """房间列表 + 当前状态与最新场次摘要（前端总览页数据源）。
    v0.5：带出主播描述与标签（总览页"描述行/标签列"渲染用）。
    v1.3.1：监控码 replay_code 字段移除（功能已迁 replay 项目，其管理页
    经自有接口读取共享库）；v2.2：高能开关 record_enabled 字段随高能功能
    移除（回放侧消费，本站不再带出）。"""
    rows = q("""
        SELECT r.room_id, r.anchor_name, r.enabled, r.created_at, r.description,
               s.session_id AS cur_session, s.start_time AS live_start,
               (SELECT popularity FROM popularity_snapshots p
                 WHERE p.room_id = r.room_id ORDER BY p.ts DESC LIMIT 1) AS popularity
        FROM rooms r
        LEFT JOIN live_sessions s
          ON s.room_id = r.room_id AND s.end_time IS NULL
        ORDER BY r.room_id
    """)
    tags = _room_tags()
    for r in rows:
        r["tags"] = tags.get(r["room_id"], [])
    return rows


@app.get("/api/overview")
async def overview(days: int = Query(7, ge=1, le=90)):
    """总览：近 N 天各房间开播时长、弹幕量、收入对比。"""
    since = _days_ago(days)
    sessions = q("""
        SELECT room_id,
               count(*) AS session_count,
               sum(CASE WHEN end_time IS NOT NULL
                        THEN (julianday(end_time) - julianday(start_time)) * 24
                        ELSE 0 END) AS total_hours,
               sum(danmaku_count) AS danmaku_count,
               round(sum(gift_value_yuan), 2) AS gift_value
        FROM live_sessions WHERE start_time >= ? GROUP BY room_id
    """, (since,))
    offline = q("""
        SELECT room_id, round(sum(value_yuan), 2) AS offline_value, count(*) AS cnt
        FROM gifts WHERE is_offline = 1 AND ts >= ? GROUP BY room_id
    """, (since,))
    off_map = {r["room_id"]: r for r in offline}
    for s in sessions:
        o = off_map.pop(s["room_id"], None)
        s["offline_value"] = o["offline_value"] if o else 0.0
    # 没有场次但有场外收入的房间也补上
    for rid, o in off_map.items():
        sessions.append({"room_id": rid, "session_count": 0, "total_hours": 0,
                         "danmaku_count": 0, "gift_value": 0.0,
                         "offline_value": o["offline_value"]})
    return {"days": days, "since": since, "rooms": sessions}


# v0.5：场次标题聚合表达式——每场的全部标题按首次出现顺序拼接（CHAR(10) 分行），
# 前端"历史场次"列按行渲染为多个标题；用嵌套子查询排序，避免依赖
# SQLite 3.44+ 才支持的聚合函数内 ORDER BY 语法
_TITLES_EXPR = """
  (SELECT GROUP_CONCAT(title, CHAR(10)) FROM
     (SELECT title FROM session_titles st
      WHERE st.session_id = live_sessions.session_id
      ORDER BY st.first_seen)) AS titles
"""


@app.get("/api/rooms/{room_id}/sessions")
async def list_sessions(room_id: int, page: int = Query(1, ge=1), size: int = Query(20, le=100)):
    """场次列表（分页，倒序）。v0.5：每行带出该场直播标题（多标题换行分隔）。"""
    total = q1("SELECT count(*) AS c FROM live_sessions WHERE room_id = ?", (room_id,))["c"]
    rows = q(f"""
        SELECT session_id, start_time, end_time, peak_popularity, danmaku_count,
               round(gift_value_yuan, 2) AS gift_value,
               CASE WHEN end_time IS NULL THEN 1 ELSE 0 END AS ongoing,
               CASE WHEN end_time IS NOT NULL
                    THEN round((julianday(end_time) - julianday(start_time)) * 24, 2)
                    ELSE round((julianday('now','localtime') - julianday(start_time)) * 24, 2)
               END AS hours,
               {_TITLES_EXPR}
        FROM live_sessions WHERE room_id = ?
        ORDER BY start_time DESC LIMIT ? OFFSET ?
    """, (room_id, size, (page - 1) * size))
    return {"total": total, "page": page, "sessions": rows}


@app.get("/api/rooms/{room_id}/sessions/{session_id}")
async def session_detail(room_id: int, session_id: int,
                         gift_limit: int = 100, danmaku_limit: int = 300):
    """场次详情：基本信息 + 观看/人气曲线（按场次的起止时间取快照）+ 收入明细 + 弹幕。"""
    s = q1("""
        SELECT session_id, room_id, start_time, end_time, peak_popularity, peak_viewers,
               danmaku_count, round(gift_value_yuan, 2) AS gift_value
        FROM live_sessions WHERE session_id = ? AND room_id = ?
    """, (session_id, room_id))
    if not s:
        raise HTTPException(404, "场次不存在")
    end = s["end_time"] or now_iso()
    s["hours"] = round((q1("SELECT (julianday(?) - julianday(?)) * 24 AS h",
                           (end, s["start_time"]))["h"] or 0), 2)
    # 同接曲线：房间观众数（真实）+ 人气值（就近取主播指标快照，见 dashboard 同款注释）
    s["viewer_curve"] = q("""
        SELECT p.ts, p.viewers,
               (SELECT a.online FROM anchor_snapshots a
                 WHERE a.room_id = p.room_id AND a.ts <= p.ts
                 ORDER BY a.ts DESC LIMIT 1) AS online
        FROM popularity_snapshots p
        WHERE p.session_id = ? ORDER BY p.ts
    """, (session_id,))
    s["popularity_curve"] = q("""
        SELECT ts, popularity FROM popularity_snapshots
        WHERE session_id = ? ORDER BY ts
    """, (session_id,))
    s["gifts"] = q("""
        SELECT ts, uname, uid, type, name, text, count, value_yuan
        FROM gifts WHERE session_id = ? ORDER BY value_yuan DESC, ts LIMIT ?
    """, (session_id, gift_limit))
    s["danmaku"] = q("""
        SELECT ts, uid, uname, text FROM danmaku
        WHERE session_id = ? ORDER BY ts LIMIT ?
    """, (session_id, danmaku_limit))
    return s


@app.get("/api/rooms/{room_id}/users")
async def room_users(room_id: int, sort: str = "gift",
                     page: int = Query(1, ge=1), size: int = Query(50, le=200)):
    """用户分布：分层统计 + 用户榜（sort: gift/danmaku/days）。"""
    order = {"gift": "gift_value_yuan DESC", "danmaku": "danmaku_count DESC",
             "days": "interact_days DESC"}[sort]
    tiers = q("""
        SELECT tier, count(*) AS cnt FROM user_stats WHERE room_id = ? GROUP BY tier
    """, (room_id,))
    total = q1("SELECT count(*) AS c FROM user_stats WHERE room_id = ?", (room_id,))["c"]
    rows = q(f"""
        SELECT uid, uname, danmaku_count, round(gift_value_yuan, 2) AS gift_value,
               interact_days, tier, first_seen, last_seen
        FROM user_stats WHERE room_id = ? ORDER BY {order} LIMIT ? OFFSET ?
    """, (room_id, size, (page - 1) * size))
    return {"total": total, "tiers": tiers, "page": page, "users": rows}


# ---------------------------------------------------------------------------
# 数据分析页（v0.5）：主播维度概览聚合；图表接口预留后期版本接入
# ---------------------------------------------------------------------------
@app.get("/api/rooms/{room_id}/analysis/overview")
async def analysis_overview(room_id: int, days: int = Query(30, ge=1, le=365)):
    """主播数据分析概览（窗口 = 近 days 天）：场次数 / 总时长 / 总弹幕 / 总营收 /
    场均弹幕 / 场均营收 / 最高同接 / 活跃观众数——全部从现有表聚合。"""
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不存在")
    since = _days_ago(days)
    s = q1("""SELECT count(*) AS session_count,
                     round(coalesce(sum(CASE WHEN end_time IS NOT NULL
                          THEN (julianday(end_time) - julianday(start_time)) * 24
                          ELSE 0 END), 0), 1) AS total_hours,
                     coalesce(sum(danmaku_count), 0) AS danmaku_count
              FROM live_sessions WHERE room_id = ? AND start_time >= ?""", (room_id, since))
    gv = q1("SELECT round(coalesce(sum(value_yuan), 0), 2) AS v FROM gifts "
            "WHERE room_id = ? AND ts >= ? AND is_offline = 0", (room_id, since))
    off = q1("SELECT round(coalesce(sum(value_yuan), 0), 2) AS v FROM gifts "
             "WHERE room_id = ? AND ts >= ? AND is_offline = 1", (room_id, since))
    peak = q1("SELECT coalesce(max(peak_viewers), 0) AS v FROM live_sessions "
              "WHERE room_id = ? AND start_time >= ?", (room_id, since))
    active = q1("SELECT count(DISTINCT uid) AS c FROM danmaku "
                "WHERE room_id = ? AND ts >= ? AND uid != 0", (room_id, since))
    n = s["session_count"] or 0
    gift_total = round((gv["v"] or 0) + (off["v"] or 0), 2)
    return {
        "days": days, "since": since,
        "session_count": n,
        "total_hours": s["total_hours"] or 0,
        "danmaku_count": s["danmaku_count"] or 0,
        "gift_value": gift_total,
        "offline_value": off["v"] or 0,
        "danmaku_per_session": round(s["danmaku_count"] / n, 1) if n else 0,
        "gift_per_session": round(gift_total / n, 2) if n else 0,
        "peak_viewers": peak["v"] or 0,
        "active_users": active["c"] or 0,
    }


# ---------------------------------------------------------------------------
# 数据分析页（v0.6）：观众留存 / 直播活跃度 / 数据问答。
# 三个接口均为只读 SQL 聚合（口径详见 v0.6 设计方案），零新表：
# - "场次观众" = 场次内任一互动（弹幕/礼物/其他互动）的去重 uid，uid=0 排除；
# - 留存率 = |本场观众 ∩ 对照窗口观众并集| / |并集人数|（回访口径；对照窗口
#   = 本场开播前 ref_days 天，默认 3 天，v0.7 起可经参数切换）；
# - 活跃度 = 分钟"任一互动去重人数 ÷ 该分钟平均同接"的场均算术平均；
# - 统计窗口统一为"近 days 个自然日（含今天）"，按日聚合口径与图表 x 轴对齐。
# ---------------------------------------------------------------------------
def _analysis_room_or_404(room_id: int) -> None:
    """三个分析接口共用的房间存在性校验。"""
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不存在")


def _window_start(days: int) -> tuple["dt.date", str]:
    """统计窗口起点：今天往前第 days-1 个自然日的 00:00（含今天共 days 天）。

    返回 (起始日期对象, ISO 时间串)。用自然日边界而非 _days_ago 的滚动
    时刻，保证"窗口内场次"与"days_list 日期序列"完全对齐（首日不出现
    因起点落在当天中午而漏掉上午场次的问题）。"""
    import datetime as dt
    d0 = dt.date.today() - dt.timedelta(days=days - 1)
    return d0, d0.strftime("%Y-%m-%dT00:00:00")


def _day_list(days: int) -> list[str]:
    """窗口内每个自然日的日期串（旧 → 新，含今天），days_list 的骨架。"""
    import datetime as dt
    today = dt.date.today()
    return [(today - dt.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


# 一批场次的"观众集合"：弹幕 ∪ 礼物 ∪ 其他互动，uid=0（打码未还原脏数据）排除。
# GROUP_CONCAT(DISTINCT uid) 一次取回，Python 侧拆 set；无互动的场次不在返回里。
_SQL_SESSION_AUDIENCE = """
SELECT session_id, GROUP_CONCAT(DISTINCT uid) AS uids FROM (
    SELECT session_id, uid FROM danmaku WHERE session_id IN ({ph}) AND uid != 0
    UNION ALL
    SELECT session_id, uid FROM gifts WHERE session_id IN ({ph}) AND uid != 0
    UNION ALL
    SELECT session_id, uid FROM interactions WHERE session_id IN ({ph}) AND uid != 0
) GROUP BY session_id
"""


def _session_audience_sets(session_ids: list[int]) -> dict[int, set[int]]:
    if not session_ids:
        return {}
    ph = ",".join("?" * len(session_ids))
    out: dict[int, set[int]] = {}
    for r in q(_SQL_SESSION_AUDIENCE.format(ph=ph), tuple(session_ids) * 3):
        out[r["session_id"]] = {int(x) for x in r["uids"].split(",") if x}
    return out


# 留存对照窗口默认值（用户 2026-09-17 第三轮反馈指定"前三天"口径）：对照集合 =
# 本场开播前 N 天内所有已结束场次的观众并集；v0.7 起 N 可经 ref_days 参数切换
# （3/7/30/90，前端按钮默认跟随页面统计窗口天数），此处仅作接口缺省值。
RETENTION_REF_DAYS = 3


@app.get("/api/rooms/{room_id}/analysis/retention")
async def analysis_retention(room_id: int, days: int = Query(30, ge=1, le=365),
                             ref_days: int = Query(RETENTION_REF_DAYS, ge=1, le=365)):
    """观众留存：每场与前 ref_days 天（开播前 ref_days×24 小时）内所有已结束
    场次的**观众并集**的重合度。

    - 留存率 = |本场观众 ∩ 前 ref_days 天观众并集| / |并集人数|（回访口径）；
    - 对照集合允许包含统计窗口外的场次（窗口首场也可能有对照值）；
    - 开播前 ref_days 天内没有任何已结束场次 → retention=null（前端不画柱）；
    - days_list 覆盖窗口内每个自然日，无直播日 sessions 为空数组（该日留空）；
    - ref_days：对照窗口天数（v0.7 新增，缺省 3 保持 v0.6 口径；对照明细受
      弹幕保留期约束，ref_days 不宜超过保留期天数）。
    """
    import datetime as dt
    _analysis_room_or_404(room_id)
    _, since = _window_start(days)
    ref_since = (dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%S")
                 - dt.timedelta(days=ref_days)).strftime("%Y-%m-%dT%H:%M:%S")
    # 对照池：窗口起点再往前 ref_days 天起的全部已结束场次（窗口外的只作对照不输出）
    pool = q(f"""
        SELECT session_id, start_time, {_TITLES_EXPR}
        FROM live_sessions
        WHERE room_id = ? AND end_time IS NOT NULL AND start_time >= ?
        ORDER BY start_time, session_id
    """, (room_id, ref_since))
    sets_ = _session_audience_sets([r["session_id"] for r in pool])
    t_since = dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%S")
    t_pool = [dt.datetime.strptime(r["start_time"], "%Y-%m-%dT%H:%M:%S") for r in pool]
    window_sec = ref_days * 86400

    by_date: dict[str, list[dict]] = {}
    for i, r in enumerate(pool):
        t = t_pool[i]
        if t < t_since:
            continue  # 窗口外场次只作对照
        ref: set[int] = set()
        for j, p in enumerate(pool):   # pool 已按 start_time 升序；规模小线性扫即可
            if t_pool[j] < t and (t - t_pool[j]).total_seconds() <= window_sec:
                ref |= sets_.get(p["session_id"], set())
        cur = sets_.get(r["session_id"], set())
        overlap = len(cur & ref)
        by_date.setdefault(r["start_time"][:10], []).append({
            "session_id": r["session_id"],
            "start_time": r["start_time"],
            "titles": r["titles"] or "",
            "retention": round(overlap / len(ref), 4) if ref else None,
            "overlap": overlap,
            "prev_total": len(ref),   # 命名沿用：对照（前 ref_days 天并集）观众数
            "cur_total": len(cur),
        })
    return {"days": days, "ref_days": ref_days, "since": since,
            "days_list": [{"date": d, "sessions": by_date.get(d, [])}
                          for d in _day_list(days)]}


@app.get("/api/rooms/{room_id}/analysis/activity")
async def analysis_activity(room_id: int, days: int = Query(30, ge=1, le=365)):
    """直播活跃度：分钟"任一互动去重人数 ÷ 该分钟平均同接"的场次算术平均。

    - 只统计已结束场次；分钟桶 = substr(ts,1,16)（弹幕/礼物/互动三表 UNION）；
    - 无快照或 viewers=0 的分钟跳过（开播初期同接未推送，避免除零与虚高）；
    - 整场无有效分钟 → 该场 avg_activity=null，不参与日聚合；
    - 日聚合：avg=当日各场平均、max=当日最高一场（均无直播则为 null）。
    """
    _analysis_room_or_404(room_id)
    _, since = _window_start(days)
    sessions = q("""SELECT session_id, start_time FROM live_sessions
                    WHERE room_id = ? AND end_time IS NOT NULL AND start_time >= ?
                    ORDER BY start_time, session_id""", (room_id, since))
    sids = [s["session_id"] for s in sessions]
    avg_by_sid: dict[int, float | None] = {s["session_id"]: None for s in sessions}
    if sids:
        ph = ",".join("?" * len(sids))
        # 互动分钟桶：三表明细 UNION 后按 (场次, 分钟) 对 uid 去重计数
        inter = q(f"""
            SELECT session_id, m, count(DISTINCT uid) AS cnt FROM (
                SELECT session_id, substr(ts,1,16) AS m, uid FROM danmaku
                  WHERE session_id IN ({ph}) AND uid != 0
                UNION ALL
                SELECT session_id, substr(ts,1,16), uid FROM gifts
                  WHERE session_id IN ({ph}) AND uid != 0
                UNION ALL
                SELECT session_id, substr(ts,1,16), uid FROM interactions
                  WHERE session_id IN ({ph}) AND uid != 0
            ) GROUP BY 1, 2
        """, tuple(sids) * 3)
        inter_map = {(r["session_id"], r["m"]): r["cnt"] for r in inter}
        # 同接分钟桶（主轴）：viewers>0 的分钟才有分母
        ratios: dict[int, list[float]] = {}
        for r in q(f"""SELECT session_id, substr(ts,1,16) AS m, avg(viewers) AS v
                       FROM popularity_snapshots
                       WHERE session_id IN ({ph}) AND viewers > 0
                       GROUP BY 1, 2""", tuple(sids)):
            cnt = inter_map.get((r["session_id"], r["m"]), 0)
            ratios.setdefault(r["session_id"], []).append(cnt / r["v"])
        for sid, lst in ratios.items():
            avg_by_sid[sid] = round(sum(lst) / len(lst), 4)

    by_date: dict[str, list[float]] = {}
    for s in sessions:
        a = avg_by_sid.get(s["session_id"])
        if a is not None:
            by_date.setdefault(s["start_time"][:10], []).append(a)
    days_list = []
    for d in _day_list(days):
        lst = by_date.get(d)
        days_list.append({
            "date": d,
            "avg_activity": round(sum(lst) / len(lst), 4) if lst else None,
            "max_activity": max(lst) if lst else None,
            "sessions": [{"session_id": s["session_id"], "start_time": s["start_time"],
                          "avg_activity": avg_by_sid[s["session_id"]]}
                         for s in sessions if s["start_time"][:10] == d
                         and avg_by_sid[s["session_id"]] is not None],
        })
    return {"days": days, "since": since, "days_list": days_list}


@app.get("/api/rooms/{room_id}/analysis/qa")
async def analysis_qa(room_id: int, days: int = Query(30, ge=1, le=365)):
    """数据问答：以"问题 → 答案"展示窗口内关键统计（指标体系参考小葫芦等
    直播数据平台，全部从现有表聚合）。窗口内无数据的条目自动省略；
    group: revenue=营收与场次篇 / audience=观众与互动篇（前端两张半宽卡）。"""
    _analysis_room_or_404(room_id)
    _, since = _window_start(days)
    items: list[dict] = []

    def d_cn(ts: str) -> str:
        return f"{int(ts[5:7])}月{int(ts[8:10])}日"

    def first_title(t: str | None) -> str:
        return ((t or "").split("\n")[0]).strip()

    # ---- 营收与场次篇 ----
    r = q1("""SELECT date(ts) AS d, round(sum(value_yuan), 2) AS v FROM gifts
              WHERE room_id = ? AND ts >= ? GROUP BY date(ts)
              ORDER BY sum(value_yuan) DESC LIMIT 1""", (room_id, since))
    if r and r["v"]:
        items.append({"group": "revenue", "q": f"近 {days} 天哪天营收最高？",
                      "a": d_cn(r["d"]), "sub": f"当日营收 ¥{r['v']:,.2f}（含场外礼物）"})
    r = q1(f"""SELECT danmaku_count, start_time, {_TITLES_EXPR} FROM live_sessions
               WHERE room_id = ? AND start_time >= ? AND danmaku_count > 0
               ORDER BY danmaku_count DESC, session_id LIMIT 1""", (room_id, since))
    if r:
        t = first_title(r["titles"])
        items.append({"group": "revenue", "q": "哪场直播弹幕最多？",
                      "a": f"{d_cn(r['start_time'])}场 · {r['danmaku_count']:,} 条",
                      "sub": t or "（该场无标题记录）"})
    r = q1("""SELECT strftime('%H', start_time) AS h, count(*) AS c FROM live_sessions
              WHERE room_id = ? AND start_time >= ?
              GROUP BY h ORDER BY c DESC, CAST(h AS INTEGER) LIMIT 1""", (room_id, since))
    if r:
        items.append({"group": "revenue", "q": "最常几点开播？",
                      "a": f"{int(r['h'])} 点档（{r['c']} 场）",
                      "sub": "按场次开播时间的小时分布统计"})
    r = q1("""SELECT count(*) AS c, round(coalesce(sum((julianday(end_time)
                     - julianday(start_time)) * 24), 0), 1) AS h
              FROM live_sessions
              WHERE room_id = ? AND start_time >= ? AND end_time IS NOT NULL""",
           (room_id, since))
    if r and r["c"]:
        items.append({"group": "revenue", "q": "平均每场直播多长？",
                      "a": f"{round(r['h'] / r['c'], 1)} 小时",
                      "sub": f"已结束 {r['c']} 场，累计 {r['h']} 小时"})
    r = q1(f"""SELECT peak_viewers, start_time, {_TITLES_EXPR} FROM live_sessions
               WHERE room_id = ? AND start_time >= ? AND peak_viewers > 0
               ORDER BY peak_viewers DESC, session_id LIMIT 1""", (room_id, since))
    if r:
        t = first_title(r["titles"])
        items.append({"group": "revenue", "q": "同接最高的一场？",
                      "a": f"{r['peak_viewers']:,} 人",
                      "sub": d_cn(r["start_time"]) + "场" + (f" · {t}" if t else "")})
    r = q1("""SELECT round(gift_value_yuan, 2) AS v, start_time FROM live_sessions
              WHERE room_id = ? AND start_time >= ? AND gift_value_yuan > 0
              ORDER BY gift_value_yuan DESC, session_id LIMIT 1""", (room_id, since))
    if r:
        items.append({"group": "revenue", "q": "单场营收最高多少？",
                      "a": f"¥{r['v']:,.2f}",
                      "sub": d_cn(r["start_time"]) + "场（场内礼物+SC+舰长）"})
    r = q1("""SELECT coalesce(sum(danmaku_count), 0) AS dm,
                     sum(CASE WHEN end_time IS NOT NULL
                         THEN (julianday(end_time) - julianday(start_time)) * 24 END) AS h
              FROM live_sessions WHERE room_id = ? AND start_time >= ?""", (room_id, since))
    if r and r["dm"] and r["h"]:
        items.append({"group": "revenue", "q": "弹幕密度如何？",
                      "a": f"{round(r['dm'] / r['h'], 1)} 条/小时",
                      "sub": f"共 {r['dm']:,} 条弹幕 / {round(r['h'], 1)} 小时"})

    # ---- 观众与互动篇 ----
    # 活跃观众 = 窗口内任一互动（弹幕/礼物/其他互动）的去重 uid（与留存口径一致）
    active = q1("""SELECT count(DISTINCT uid) AS c FROM (
                     SELECT uid FROM danmaku WHERE room_id = ? AND ts >= ? AND uid != 0
                     UNION
                     SELECT uid FROM gifts WHERE room_id = ? AND ts >= ? AND uid != 0
                     UNION
                     SELECT uid FROM interactions WHERE room_id = ? AND ts >= ? AND uid != 0
                   )""", (room_id, since) * 3)["c"]
    pay = q1("SELECT count(DISTINCT uid) AS c FROM gifts "
             "WHERE room_id = ? AND ts >= ? AND uid != 0", (room_id, since))["c"]
    if pay and active:
        items.append({"group": "audience", "q": "多少观众送过礼？",
                      "a": f"{pay} 人 · 付费率 {pay / active * 100:.1f}%",
                      "sub": f"活跃观众 {active} 人中 {pay} 人送过礼"})
    gv = q1("SELECT round(coalesce(sum(value_yuan), 0), 2) AS v FROM gifts "
            "WHERE room_id = ? AND ts >= ?", (room_id, since))
    if pay and gv["v"]:
        items.append({"group": "audience", "q": "人均消费多少？",
                      "a": f"¥{gv['v'] / pay:,.2f}",
                      "sub": "口径：窗口总营收 ÷ 送礼人数"})
    # 粉丝/大航海：取窗口首（起点前最近一条，无则窗口内最早）与最新快照对比
    def _snap_delta(col: str) -> tuple[int, int, int] | None:
        now_row = q1(f"SELECT {col} AS v FROM anchor_snapshots WHERE room_id = ? "
                     "ORDER BY ts DESC LIMIT 1", (room_id,))
        then_row = q1(f"""SELECT {col} AS v FROM anchor_snapshots WHERE room_id = ? AND ts <= (
                            SELECT min(ts) FROM anchor_snapshots WHERE room_id = ? AND ts >= ?)
                          ORDER BY ts DESC LIMIT 1""", (room_id, room_id, since))
        if then_row is None:   # 起点前无快照（刚开始监控）：回退窗口内最早一条
            then_row = q1(f"SELECT {col} AS v FROM anchor_snapshots "
                          "WHERE room_id = ? AND ts >= ? ORDER BY ts LIMIT 1", (room_id, since))
        if now_row is None or then_row is None or now_row["v"] is None or then_row["v"] is None:
            return None
        return now_row["v"], then_row["v"], now_row["v"] - then_row["v"]

    d_ = _snap_delta("attention")
    if d_ is not None:
        items.append({"group": "audience", "q": "粉丝涨了多少？",
                      "a": f"{'+' if d_[2] >= 0 else ''}{d_[2]:,}",
                      "sub": f"窗口首末快照：{d_[1]:,} → {d_[0]:,}"})
    new_users = q1("SELECT count(*) AS c FROM user_stats "
                   "WHERE room_id = ? AND first_seen >= ? AND uid != 0",
                   (room_id, since))["c"]
    if active:
        items.append({"group": "audience", "q": "新观众占比多少？",
                      "a": f"{new_users / active * 100:.1f}%",
                      "sub": f"新观众 {new_users} 人 / 活跃观众 {active} 人"})
    d_ = _snap_delta("guards")
    if d_ is not None:
        items.append({"group": "audience", "q": "大航海人数变化？",
                      "a": f"{'+' if d_[2] >= 0 else ''}{d_[2]}",
                      "sub": f"窗口首末快照：{d_[1]} → {d_[0]} 人"})
    return {"days": days, "since": since, "items": items}


@app.get("/api/rooms/{room_id}/analysis/revenue")
async def analysis_revenue(room_id: int, days: int = Query(30, ge=1, le=365)):
    """营收构成（v2.0.1）：窗口内 gifts 按 uid 聚合的营收贡献榜——前 10 用户 +
    其余用户合并（others），供前端「径向双层饼图」（角度 ∝ 个人总营收，径向按
    舰队占比分内/外圈）。

    口径：
    - 舰队收益 guard = type='guard'（开通/续费舰长），礼物收益 gift = 其余
      （type='gift' 礼物 + type='sc' 醒目留言），二者合计 = 个人总营收；
    - is_offline（场外收入）计入，与概览「总营收」口径一致；
    - uid=0（打码未还原脏数据）排除，与用户榜/留存口径一致；
    - 窗口为自然日边界（_window_start），与其他分析接口对齐；
    - 窗口内总用户 ≤ 10 时 others=null（无剩余可合并）；
    - 并列总营收按 uid 升序决胜（前端配色按名次固定，排名须确定）。
    """
    _analysis_room_or_404(room_id)
    _, since = _window_start(days)
    rows = q("""SELECT uid, max(uname) AS uname,
                       round(coalesce(sum(CASE WHEN type = 'guard'
                            THEN value_yuan END), 0), 2) AS guard,
                       round(coalesce(sum(CASE WHEN type != 'guard'
                            THEN value_yuan END), 0), 2) AS gift
                FROM gifts
                WHERE room_id = ? AND ts >= ? AND uid != 0
                GROUP BY uid
                HAVING guard + gift > 0
                ORDER BY guard + gift DESC, uid""", (room_id, since))

    def _row(r: dict, total: float) -> dict:
        return {"uid": r["uid"], "uname": r["uname"] or f"uid{r['uid']}",
                "guard": r["guard"], "gift": r["gift"],
                "value": round(total, 2)}

    items = [_row(r, r["guard"] + r["gift"]) for r in rows[:10]]
    rest = rows[10:]
    others = None
    if rest:
        others = {"uid": 0, "uname": "其他用户",
                  "guard": round(sum(r["guard"] for r in rest), 2),
                  "gift": round(sum(r["gift"] for r in rest), 2),
                  "value": round(sum(r["guard"] + r["gift"] for r in rest), 2)}
    return {
        "days": days, "since": since,
        "total": {"guard": round(sum(r["guard"] for r in rows), 2),
                  "gift": round(sum(r["gift"] for r in rows), 2),
                  "value": round(sum(r["guard"] + r["gift"] for r in rows), 2)},
        "items": items,
        "others": others,
    }


# ---------------------------------------------------------------------------
# 占位首页（M5 实现正式前端后替换）
# ---------------------------------------------------------------------------
@app.get("/api/summary")
async def summary(days: int = Query(7, ge=1, le=90)):
    """首页列表：每房间一行的汇总数据（时间窗默认近 7 天）。

    对应布局图的列：主播 | 场次/最近开播 | 时长/粉丝增长 | 同接峰值/弹幕 |
    粉丝/大航海(监控期) | 营收(场内+场外) | 互动数
    """
    since = _days_ago(days)
    # v2.3：LEFT JOIN 通知配置——总览页「通知」列（开关/已配群数/专属模板
    # 标记）一次查询带出，避免 N+1（两通道房间配置统一填 QQ 群号，计数同源）
    rooms = q("""
        SELECT r.room_id, r.anchor_name, r.avatar_url, r.description, r.auto_full,
               n.enabled AS notify_enabled, n.group_ids AS notify_groups,
               (CASE WHEN COALESCE(n.template_on, '') != ''
                      OR COALESCE(n.template_off, '') != '' THEN 1 ELSE 0 END)
                   AS notify_custom
        FROM rooms r LEFT JOIN qq_notify n ON n.room_id = r.room_id
        WHERE r.enabled = 1 ORDER BY r.room_id
    """)
    tags_map = _room_tags()
    rows = []
    for r in rooms:
        rid = r["room_id"]
        s = q1("""
            SELECT count(*) AS cnt,
                   max(start_time) AS last_start,
                   round(sum(CASE WHEN end_time IS NOT NULL
                        THEN (julianday(end_time) - julianday(start_time)) * 24 ELSE 0 END), 1) AS hours
            FROM live_sessions WHERE room_id = ? AND start_time >= ?
        """, (rid, since)) or {"cnt": 0, "last_start": None, "hours": 0}
        dm = q1("SELECT count(*) AS c FROM danmaku WHERE room_id = ? AND ts >= ?",
                (rid, since))["c"]
        inter = q1("SELECT count(*) AS c FROM interactions WHERE room_id = ? AND ts >= ?",
                   (rid, since))["c"]
        gv = q1("SELECT round(sum(value_yuan), 2) AS v FROM gifts WHERE room_id = ? AND ts >= ? AND is_offline = 0",
                (rid, since))
        off = q1("SELECT round(sum(value_yuan), 2) AS v FROM gifts WHERE room_id = ? AND ts >= ? AND is_offline = 1",
                 (rid, since))
        peak = q1("SELECT max(popularity) AS p, max(viewers) AS v FROM popularity_snapshots WHERE room_id = ? AND ts >= ?",
                  (rid, since))
        watched_peak = q1("SELECT max(watched) AS w FROM anchor_snapshots WHERE room_id = ? AND ts >= ?",
                          (rid, since))
        att_now = q1("SELECT attention, watched, guards FROM anchor_snapshots WHERE room_id = ? ORDER BY ts DESC LIMIT 1",
                     (rid,))
        att_then = q1("""SELECT attention FROM anchor_snapshots WHERE room_id = ? AND ts <= (
                            SELECT min(ts) FROM anchor_snapshots WHERE room_id = ? AND ts >= ?)
                         ORDER BY ts DESC LIMIT 1""", (rid, rid, since))
        guard = q1("SELECT count(DISTINCT uid) AS c FROM gifts WHERE room_id = ? AND type = 'guard'",
                   (rid,))
        live = q1("""SELECT session_id, start_time FROM live_sessions
                     WHERE room_id = ? AND end_time IS NULL""", (rid,))
        # 大航海优先用接口真实值（guard_info.count），无快照时回退监控期购买人数
        guards_real = att_now["guards"] if att_now and att_now["guards"] else None
        # v2.3：通知列透出（group_ids 损坏按未配置处理，不让一行脏数据 500）
        try:
            n_groups = len(json.loads(r["notify_groups"])) if r["notify_groups"] else 0
        except (ValueError, TypeError):
            n_groups = 0
        rows.append({
            "room_id": rid,
            "anchor_name": r["anchor_name"] or str(rid),
            "avatar_url": r["avatar_url"] or "",
            "description": r["description"] or "",
            "tags": tags_map.get(rid, []),
            "auto_full": bool(r["auto_full"]),             # v0.11 自动全程录制开关
            "notify_enabled": bool(r["notify_enabled"]),   # v2.3 开播通知开关
            "notify_group_count": n_groups,                # v2.3 已配目标群数
            "notify_custom": bool(r["notify_custom"]),     # v2.3 是否有房间专属模板
            "session_count": s["cnt"],
            "last_start": s["last_start"],
            "hours": s["hours"] or 0,
            "fan_growth": ((att_now["attention"] if att_now else 0)
                           - (att_then["attention"] if att_then else 0)) if att_now else None,
            "attention": att_now["attention"] if att_now else None,
            "peak_popularity": peak["p"] or 0,
            "viewer_peak": peak["v"] or 0,
            "watched_peak": watched_peak["w"] or 0,
            "danmaku_count": dm,
            "guard_users": guards_real if guards_real else guard["c"],
            "guards_is_real": guards_real is not None,
            "gift_value": (gv["v"] or 0) + (off["v"] or 0),
            "offline_value": off["v"] or 0,
            "interact_count": inter,
            "live_now": bool(live),
            "live_start": live["start_time"] if live else None,
        })
    return {"days": days, "rooms": rows}


def _day_bounds(offset_days: int) -> tuple[str, str]:
    """今天偏移 offset_days 的那个自然日的 [起, 止) ISO 时间串（本地时区）。"""
    import datetime as dt
    day = (dt.datetime.now() + dt.timedelta(days=offset_days)).date()
    start = dt.datetime.combine(day, dt.time.min)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), (start + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")


# v0.8 弹幕分批加载参数：dashboard 首屏条数 / 客户端可加载总上限（v0.2 口径不变）
DM_FIRST_LIMIT = 800
DM_MAX = 15000


@app.get("/api/rooms/{room_id}/dashboard")
async def room_dashboard(room_id: int, session_id: int | None = None,
                         dm_since_id: int | None = None):
    """详情页聚合数据。带 ?session=N 时为"历史场次模式"：
    所有场次相关板块（曲线/观众构成/SC/礼物/弹幕流/指标卡）按该场次统计，
    页面标题由前端改为"X月X日X时直播场次详情"。

    v0.8 性能优化：弹幕不再全量下发（原一次 15000 条、响应数 MB）——
    首屏只返回最近 DM_FIRST_LIMIT 条 + 总数/游标，前端"加载更多"走
    /api/rooms/{id}/danmaku 翻页；轮询带 dm_since_id 只取新增弹幕。"""
    room = q1("SELECT * FROM rooms WHERE room_id = ?", (room_id,))
    if not room:
        raise HTTPException(404, "房间不存在")
    session_mode = session_id is not None

    def day_metric(start: str, end: str) -> dict:
        gift = q1("SELECT count(*) AS c, round(coalesce(sum(value_yuan),0), 2) AS v "
                  "FROM gifts WHERE room_id = ? AND ts >= ? AND ts < ?", (room_id, start, end))
        dm = q1("SELECT count(*) AS c FROM danmaku WHERE room_id = ? AND ts >= ? AND ts < ?",
                (room_id, start, end))
        peak = q1("SELECT coalesce(max(popularity), 0) AS p, coalesce(max(viewers), 0) AS v "
                  "FROM popularity_snapshots WHERE room_id = ? AND ts >= ? AND ts < ?",
                  (room_id, start, end))
        watched = q1("SELECT coalesce(max(watched), 0) AS w FROM anchor_snapshots "
                     "WHERE room_id = ? AND ts >= ? AND ts < ?", (room_id, start, end))
        return {"gift_rows": gift["c"], "gift_value": gift["v"],
                "danmaku": dm["c"], "peak_popularity": peak["p"],
                "peak_viewers": peak["v"], "watched_peak": watched["w"]}

    t0, t1 = _day_bounds(0)   # 今天
    y0, y1 = _day_bounds(-1)  # 昨天
    today, yesterday = day_metric(t0, t1), day_metric(y0, y1)

    # 场次模式：覆盖 live/指标卡 为指定场次的口径
    session_info = None
    if session_mode:
        session_info = q1(f"""SELECT session_id, start_time, end_time, peak_popularity,
                                     peak_viewers, danmaku_count, round(gift_value_yuan,2) AS gift_value,
                                     {_TITLES_EXPR}
                             FROM live_sessions WHERE session_id = ? AND room_id = ?""",
                          (session_id, room_id))
        if not session_info:
            raise HTTPException(404, "场次不存在")
        # v0.2：本场在该主播全部场次中的序号（副标题"第 N 场"用）
        session_info["seq"] = q1(
            """SELECT count(*) + 1 AS seq FROM live_sessions
               WHERE room_id = ? AND (start_time < ? OR (start_time = ? AND session_id < ?))""",
            (room_id, session_info["start_time"], session_info["start_time"], session_id))["seq"]
        g = q1("SELECT count(*) AS c, round(coalesce(sum(value_yuan),0),2) AS v "
               "FROM gifts WHERE session_id = ?", (session_id,))
        today = {"gift_rows": g["c"], "gift_value": g["v"],
                 "danmaku": session_info["danmaku_count"],
                 "peak_viewers": session_info["peak_viewers"], "watched_peak": 0}
        yesterday = {"gift_rows": 0, "gift_value": 0, "danmaku": 0,
                     "peak_viewers": 0, "watched_peak": 0}

    # v0.9 粉丝图表改按天聚合：每天取最后一个 attention 快照为当日粉丝量，
    # 相邻两日差值为当日增长（首日以 30 天窗口前最后一个快照为基准）。
    # 替代 v0.8 的等距降采样折线（30 点替代 720 点，形状语义更清晰：
    # 上图粉丝量折线 + 下图每日增长柱状，横轴同为天）。
    daily = q("""
        SELECT day, attention FROM (
          SELECT date(ts) AS day, attention,
                 ROW_NUMBER() OVER (PARTITION BY date(ts) ORDER BY ts DESC) AS rn
          FROM anchor_snapshots WHERE room_id = ? AND ts >= ?
        ) WHERE rn = 1 ORDER BY day
    """, (room_id, _days_ago(30)))
    base = q1("SELECT attention FROM anchor_snapshots WHERE room_id = ? AND ts < ? "
              "ORDER BY ts DESC LIMIT 1", (room_id, _days_ago(30)))
    prev = base["attention"] if base else None
    fan_daily = []
    for r in daily:
        total = r["attention"]
        growth = (total - prev) if prev is not None else None
        fan_daily.append({"day": r["day"][5:], "total": total, "growth": growth})
        prev = total
    live = q1("""SELECT session_id, start_time, danmaku_count, round(gift_value_yuan,2) AS gift_value,
                        peak_viewers
                 FROM live_sessions WHERE room_id = ? AND end_time IS NULL""", (room_id,))
    if live is not None:
        # v0.2：当前场次在本主播全部场次中的序号（曲线标题"第 N 场"用）
        live["seq"] = q1(
            """SELECT count(*) + 1 AS seq FROM live_sessions
               WHERE room_id = ? AND (start_time < ? OR (start_time = ? AND session_id < ?))""",
            (room_id, live["start_time"], live["start_time"], live["session_id"]))["seq"]
    if session_mode:
        live = session_info   # 曲线/主播卡按指定场次展示
    # 当前场次曲线：房间观众数（真实同接，WS 实时推送+采样）+ 人气值（加权）。
    # 人气值就近取主播指标快照的 online（B站新协议心跳人气恒为 1，不可直接用）
    _CURVE_SELECT = """
        SELECT p.ts, p.viewers,
               (SELECT a.online FROM anchor_snapshots a
                 WHERE a.room_id = p.room_id AND a.ts <= p.ts
                 ORDER BY a.ts DESC LIMIT 1) AS online
        FROM popularity_snapshots p
    """
    curve = []
    if live:
        curve = q(_CURVE_SELECT + " WHERE p.session_id = ? ORDER BY p.ts",
                  (live["session_id"],))
    else:
        curve = q(_CURVE_SELECT + " WHERE p.room_id = ? AND p.ts >= ? ORDER BY p.ts",
                  (room_id, _hours_ago(3)))
    # 所有弹幕（v0.2：原"最新弹幕"板块升级；v0.8 性能优化改分批加载）：
    # 首屏只取最近 DM_FIRST_LIMIT 条（倒序，最新在前），历史走
    # /api/rooms/{id}/danmaku 游标翻页（上限仍 DM_MAX 条），轮询带
    # dm_since_id 时只返回比游标新的行（正序，前端头插合并）。
    # 场次模式下只取该场次的弹幕；emots 供前端把 [表情] 占位符渲染成图片。
    if session_mode:
        _dm_scope, _dm_args = "session_id = ?", (session_id,)
    else:
        _dm_scope, _dm_args = "room_id = ?", (room_id,)
    if dm_since_id is not None:
        recent_dm = q(f"""SELECT id, ts, uname, medal_name, medal_level, medal_color,
                                 privilege_type, text, emots FROM danmaku
                          WHERE {_dm_scope} AND id > ?
                          ORDER BY id ASC LIMIT 2000""", (*_dm_args, dm_since_id))
    else:
        recent_dm = q(f"""SELECT id, ts, uname, medal_name, medal_level, medal_color,
                                 privilege_type, text, emots FROM danmaku
                          WHERE {_dm_scope}
                          ORDER BY id DESC LIMIT {DM_FIRST_LIMIT}""", _dm_args)
    _dm_stat = q1(f"SELECT count(*) AS c, coalesce(max(id), 0) AS m FROM danmaku "
                  f"WHERE {_dm_scope}", _dm_args)
    # v0.2：送礼记录并入"最新弹幕"流（前端按时间合并渲染）；场次模式下
    # 取该场次的礼物，普通模式取全部最近礼物。blind_box 带出盲盒来源标记
    if session_mode:
        recent_gifts = q("""SELECT ts, uname, type, name, blind_box, count, value_yuan FROM gifts
                            WHERE session_id = ? ORDER BY id DESC LIMIT 20""", (session_id,))
    else:
        recent_gifts = q("""SELECT ts, uname, type, name, blind_box, count, value_yuan FROM gifts
                            WHERE room_id = ? ORDER BY id DESC LIMIT 20""", (room_id,))
    tiers = q("SELECT tier, count(*) AS cnt FROM user_stats WHERE room_id = ? GROUP BY tier",
              (room_id,))
    # 用户榜：全量返回（送礼+弹幕用户，窗口内画像），前端本地排序+折叠滚动。
    # v0.5：场次模式只显示**该场次**发过弹幕或送过礼的用户（明细表按 uid 聚合，
    # 弹幕条数/礼物金额为本场口径）；粉丝牌/舰队从 user_stats 带出（实时侧已保证
    # 发过言的用户均有行，LEFT JOIN 仅为防御）。分层口径（用户要求）：本场
    # 送礼 ≥ core_gift_per_session_yuan 或舰队成员 → 核心，其余沿用窗口画像分层。
    _core_avg = float(cfg["tier"].get("core_gift_per_session_yuan", 10.0))
    if session_mode:
        top_users = q(f"""
            WITH per_user AS (
                SELECT uid, max(uname) AS uname,
                       sum(cnt) AS danmaku_count, round(sum(val), 2) AS gift_value
                FROM (
                    SELECT uid, uname, count(*) AS cnt, 0.0 AS val FROM danmaku
                      WHERE session_id = ? AND uid != 0 GROUP BY uid, uname
                    UNION ALL
                    SELECT uid, uname, 0, sum(value_yuan) FROM gifts
                      WHERE session_id = ? AND uid != 0 GROUP BY uid, uname
                ) GROUP BY uid
            )
            SELECT p.uid, p.uname,
                   coalesce(us.medal_name, '') AS medal_name,
                   coalesce(us.medal_level, 0) AS medal_level,
                   coalesce(us.medal_color, 0) AS medal_color,
                   coalesce(us.guard_level, 0) AS guard_level,
                   p.danmaku_count, p.gift_value,
                   CASE WHEN p.gift_value >= {_core_avg}
                          OR coalesce(us.guard_level, 0) > 0 THEN '核心'
                        ELSE coalesce(us.tier, '普通') END AS tier,
                   coalesce(us.last_seen, '') AS last_seen
            FROM per_user p
            LEFT JOIN user_stats us ON us.uid = p.uid AND us.room_id = ?
            ORDER BY p.gift_value DESC, p.danmaku_count DESC LIMIT 500""",
                      (session_id, session_id, room_id))
    else:
        top_users = q("""SELECT uid, uname, medal_name, medal_level, medal_color,
                                guard_level, danmaku_count,
                                round(gift_value_yuan,2) AS gift_value, tier, last_seen
                         FROM user_stats WHERE room_id = ?
                         ORDER BY gift_value_yuan DESC, danmaku_count DESC LIMIT 500""",
                      (room_id,))
    # v0.2：场次序号按"该主播自己的第 N 场"计（row_number 在 LIMIT 之前、
    # WHERE 之后计算，覆盖该房间全部场次），不再展示数据库全局 session_id；
    # 跳转用的 session_id 仍随行返回；v0.5 带出直播标题（多标题换行分隔）
    sessions = q(f"""SELECT session_id, row_number() OVER (ORDER BY start_time, session_id) AS seq,
                           start_time, end_time, peak_popularity, peak_viewers,
                           danmaku_count, round(gift_value_yuan,2) AS gift_value,
                           CASE WHEN end_time IS NOT NULL
                                THEN round((julianday(end_time)-julianday(start_time))*24, 2)
                           END AS hours,
                           {_TITLES_EXPR}
                    FROM live_sessions WHERE room_id = ?
                    ORDER BY start_time DESC LIMIT 15""",
                 (room_id,))
    guard = q1("SELECT count(DISTINCT uid) AS c FROM gifts WHERE room_id = ? AND type = 'guard'",
               (room_id,))
    snap_latest = q1("SELECT guards FROM anchor_snapshots WHERE room_id = ? ORDER BY ts DESC LIMIT 1",
                     (room_id,))
    guards_real = snap_latest["guards"] if snap_latest and snap_latest["guards"] else None

    # 观众构成柱状图（口径：本场直播——直播中取当前场次，未开播取最近一场）：
    # 平均同接=本场分钟采样观众数均值；活跃人员=本场发弹幕超过 3 条的去重用户；
    # 核心用户=本场付费过的人。三项同一场次窗口（明细表带 session_id 直接过滤，
    # 平均同接也按该场次的 popularity_snapshots 计算，未开播时与「上一场」口径
    # 一致而非默认视图的近 3 小时曲线）。
    if live:
        aud_sid, aud_label = live["session_id"], "本场"
        # 场次模式下已结束的场次按其 end_time 截止统计
        aud_end = (live.get("end_time") or now_iso()) if session_mode else now_iso()
    else:
        last = q1("SELECT session_id, end_time FROM live_sessions WHERE room_id = ? "
                  "ORDER BY start_time DESC LIMIT 1", (room_id,))
        aud_sid = last["session_id"] if last else None
        aud_label = f"上一场#{last['session_id']}" if last else "本场"
        aud_end = (last["end_time"] if last and last["end_time"] else now_iso())
    if aud_sid is not None:
        aud_start = q1("SELECT start_time FROM live_sessions WHERE session_id = ?",
                       (aud_sid,))["start_time"]
        _aud_avg = q1("SELECT coalesce(round(avg(viewers)), 0) AS v, count(*) AS n "
                      "FROM popularity_snapshots WHERE session_id = ?", (aud_sid,))
        audience = {
            "label": aud_label,
            "all_viewers": (q1("SELECT coalesce(max(watched), 0) AS v FROM anchor_snapshots "
                               "WHERE room_id = ? AND ts >= ? AND ts <= ?",
                               (room_id, aud_start, aud_end)) or {"v": 0})["v"],
            # 活跃人员：本场发弹幕超过 3 条（>3，即 ≥4 条）的去重用户
            # ∪ 送过礼的去重用户（不限金额大小，含 0 元礼物；两集合
            # UNION 天然去重）
            "active_users": q1(
                "SELECT count(*) AS c FROM ("
                "SELECT uid FROM danmaku WHERE session_id = ? AND uid != 0 "
                "GROUP BY uid HAVING count(*) > 3 "
                "UNION "
                "SELECT uid FROM gifts WHERE session_id = ? AND uid != 0)",
                (aud_sid, aud_sid))["c"],
            # 核心用户：可识别付费者去重；打码送礼者（uid=0）无法区分个人，
            # 只要有则近似 +1
            "paying_users": q1("SELECT count(DISTINCT uid) AS c FROM gifts "
                               "WHERE session_id = ? AND value_yuan > 0 AND uid > 0",
                               (aud_sid,))["c"] + q1(
                "SELECT EXISTS(SELECT 1 FROM gifts WHERE session_id = ? "
                "AND value_yuan > 0 AND uid = 0) AS e", (aud_sid,))["e"],
            # 平均同接（整场）与采样点数：观众构成悬浮占比的分母
            "avg_viewers": _aud_avg["v"] or 0,
            "viewer_samples": _aud_avg["n"] or 0,
        }
    else:
        audience = {"label": aud_label, "all_viewers": 0, "active_users": 0,
                    "paying_users": 0, "avg_viewers": 0, "viewer_samples": 0}

    # SC 列表与礼物聚合（与观众构成同场次口径：直播中=本场，未开播=最近一场）
    if aud_sid is not None:
        sc_list = q("""SELECT ts, uname, text, value_yuan FROM gifts
                       WHERE session_id = ? AND type = 'sc'
                       ORDER BY id DESC LIMIT 100""", (aud_sid,))
        sc_total = q1("SELECT count(*) AS c, round(coalesce(sum(value_yuan),0),2) AS v FROM gifts "
                      "WHERE session_id = ? AND type = 'sc'", (aud_sid,)) or {"c": 0, "v": 0}
        # 礼物列表：按名称聚合（不含 SC），两列展示；缩略图取该名称任一非空图。
        # v0.2：盲盒开出的礼物带 blind_box 标记（前端显示"盲盒"角标）
        # v0.3：额外带出 type 与 orders（购买笔数）——上舰记录的 name 已含月数
        # （如"总督x3月"，count 存的是月数），前端对 guard 类型改按"笔数"显示 ×N，
        # 避免出现"总督x3月 ×3"（×3 是月数不是件数）的歧义展示
        gift_agg = q("""SELECT name, type, sum(count) AS cnt, count(*) AS orders,
                               round(sum(value_yuan),2) AS value,
                               max(gift_img) AS img, max(blind_box) AS blind_box
                        FROM gifts WHERE session_id = ? AND type != 'sc'
                        GROUP BY name, type ORDER BY sum(value_yuan) DESC LIMIT 100""",
                     (aud_sid,))
        gift_total = q1("SELECT count(*) AS c, round(coalesce(sum(value_yuan),0),2) AS v FROM gifts "
                        "WHERE session_id = ? AND type != 'sc'", (aud_sid,)) or {"c": 0, "v": 0}
    else:
        sc_list, sc_total = [], {"c": 0, "v": 0}
        gift_agg, gift_total = [], {"c": 0, "v": 0}
    return {
        "room": dict(room),
        "today": today, "yesterday": yesterday,
        "attention": fan_daily[-1]["total"] if fan_daily else None,
        "fan_daily": fan_daily,
        # 场次模式：live_now 表示"该场次是否仍在直播"（而非房间当前状态）
        "live_now": bool(live) and (live.get("end_time") is None),
        "live": live,
        "session_mode": session_mode,
        "session_info": session_info,
        # viewer_curve: [{ts, viewers, online}]，viewers=房间观众（真实同接），online=人气（加权）
        "viewer_curve": curve,
        # 活跃用户叠加曲线：每分钟发弹幕的去重用户数（与 viewer_curve 同场次/同窗口、
        # 同 HH:MM 键，前端按分钟对齐叠加为柱状）
        "active_curve": (
            q("""SELECT substr(ts, 12, 5) AS minute, count(DISTINCT uid) AS cnt
                 FROM danmaku WHERE session_id = ?
                 GROUP BY minute ORDER BY minute""", (live["session_id"],))
            if live else
            q("""SELECT substr(ts, 12, 5) AS minute, count(DISTINCT uid) AS cnt
                 FROM danmaku WHERE room_id = ? AND ts >= ?
                 GROUP BY minute ORDER BY minute""", (room_id, _hours_ago(3)))
        ),
        "audience": audience,
        "sc_list": sc_list, "sc_total": sc_total,
        "gift_agg": gift_agg, "gift_total": gift_total,
        "guards_real": guards_real,
        "recent_danmaku": recent_dm,   # 首屏/翻页=最新在前（倒序）；dm_since_id 模式=正序增量
        # v0.8 分批加载配套：当前口径弹幕总数 + 最大 id 游标（前端增量轮询用）
        "danmaku_total": _dm_stat["c"],
        "dm_last_id": _dm_stat["m"],
        "recent_gifts": recent_gifts,
        "tiers": tiers,
        "top_users": top_users,
        "sessions": sessions,
        "guard_users": guards_real if guards_real else guard["c"],
    }


@app.get("/api/rooms/{room_id}/danmaku")
async def room_danmaku(room_id: int, session_id: int | None = None,
                       before_id: int | None = None,
                       limit: int = Query(2000, ge=1, le=DM_MAX)):
    """弹幕游标翻页（v0.8 性能优化，配套 dashboard 首屏分批）。

    详情页"加载更多"/搜索全量加载由此接口按需拉取：
    - session_id：场次口径；不传=房间全量（与 dashboard 弹幕流同口径）；
    - before_id：取比该 id 更早的一页（倒序，最新在前）；首页不传；
    - limit：每页条数（默认 2000，上限 15000）。
    """
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不存在")
    if session_id is not None:
        scope, args = "session_id = ?", (session_id,)
    else:
        scope, args = "room_id = ?", (room_id,)
    cond, cargs = scope, list(args)
    if before_id is not None:
        cond += " AND id < ?"
        cargs.append(before_id)
    rows = q(f"""SELECT id, ts, uname, medal_name, medal_level, medal_color,
                       privilege_type, text, emots FROM danmaku
                WHERE {cond} ORDER BY id DESC LIMIT ?""", (*cargs, limit))
    total = q1(f"SELECT count(*) AS c FROM danmaku WHERE {scope}", args)["c"]
    return {"rows": rows, "total": total,
            "oldest_id": rows[-1]["id"] if rows else None}


# ---------------------------------------------------------------------------
# 盲盒价值映射 API（v0.2；v0.5 删除管理页 UI，API 保留）
# 2026-09-16 数据核查：最近 10 场盲盒礼物 30 行 100% 获得明确价值（B 站消息
# 自带开出礼物单价，兜底链未构成依赖），管理页按用户要求删除；映射表仍是
# 采集端 price 缺失时的兜底数据源（capture/caches.py 热加载），故 API 保留。
# ---------------------------------------------------------------------------
class BlindPriceIn(BaseModel):
    gift_name: str
    price_yuan: float
    note: str = ""
    token: str | None = None


@app.get("/api/blind-box")
async def blind_box_prices():
    """盲盒价值映射表（v0.2.1：仅保留映射表；历史记录修正功能已按用户要求移除，
    0.1 期间未按开出礼物计价的旧盲盒记录保持按盒价口径，不再提供改写入口）。"""
    return {"prices": q("SELECT gift_name, price_yuan, note, updated_at FROM blind_gift_prices "
                       "ORDER BY updated_at DESC")}


@app.post("/api/blind-box")
async def upsert_blind_price(body: BlindPriceIn, request: Request):
    """新增/修改一条盲盒礼物价值映射（采集端 30s 内热加载生效）。"""
    check_admin(request, body.token)
    name = body.gift_name.strip()
    if not name:
        raise HTTPException(400, "礼物名不能为空")
    if body.price_yuan < 0:
        raise HTTPException(400, "价值不能为负")
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO blind_gift_prices (gift_name, price_yuan, note, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(gift_name) DO UPDATE SET
                   price_yuan = excluded.price_yuan, note = excluded.note,
                   updated_at = excluded.updated_at""",
            (name, body.price_yuan, body.note, now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/blind-box/{gift_name}")
async def delete_blind_price(gift_name: str, request: Request, token: str | None = None):
    """删除一条盲盒价值映射。"""
    check_admin(request, token)
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM blind_gift_prices WHERE gift_name = ?", (gift_name,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "映射不存在")
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 开播 QQ 群通知配置（v2.3：live-qq-notify 项目联动）
# ---------------------------------------------------------------------------
# 数据落共享库 qq_notify / qq_notify_global 两表（DDL 见 db.py，属主为
# live-qq-notify 的 store.py，此处锁步）；配置只入库、不经本服务转发——
# 通知器进程每轮（60s）热加载，页面改完最迟一个轮询周期生效。
# 房间级：总览页「通知」列开关 + 设置弹窗（群映射/房间专属文案）；
# 全局级：管理页「开播账号管理」板块（QQ 号预留/NapCat 连接/总开关/默认文案）。
# ---------------------------------------------------------------------------
import hashlib
import urllib.error as _urlerr
import urllib.request as _urlreq

# 官方 QQ 机器人 OpenAPI 基址（v2.3.1；测试经 monkeypatch 指向本地 mock）
_OFFICIAL_API = "https://api.bot.qq.com"


def _qq_global_row() -> dict:
    """读全局配置单行（init_db 已播种；此处再防御性补种一次）。

    command_replies（v2.3.1 群指令表）解析为 dict 透出：库中为空对象或
    脏 JSON 时回退代码默认表 QQ_COMMAND_REPLIES（=「清空并保存=恢复默认」
    的生效口径；事件中枢取值同此）。
    """
    row = q1("SELECT * FROM qq_notify_global WHERE id = 1")
    if row is None:
        conn = connect()
        try:
            conn.execute("INSERT OR IGNORE INTO qq_notify_global (id) VALUES (1)")
            conn.commit()
        finally:
            conn.close()
        row = q1("SELECT * FROM qq_notify_global WHERE id = 1")
    row["notify_on"] = bool(row["notify_on"])
    row["notify_off"] = bool(row["notify_off"])
    try:
        replies = json.loads(row.get("command_replies") or "{}")
        assert isinstance(replies, dict)
    except (ValueError, TypeError, AssertionError):
        replies = {}
    row["command_replies"] = replies if replies else dict(QQ_COMMAND_REPLIES)
    return row


class QQNotifyGlobalIn(BaseModel):
    """全局配置 PATCH 体（全字段可选，只改传入项）。
    notify_cover 已停用（2026-10-14：封面/时长均为纯模板变量）。
    v2.3.1：官方 QQ 机器人通道字段（channel/official_app_id/official_app_secret）
    与群指令自动回复表（command_replies，页面键值对编辑；空 dict=回退代码默认）。"""
    qq_account: str | None = None
    onebot_url: str | None = None
    onebot_token: str | None = None
    webui_url: str | None = None
    webui_token: str | None = None
    channel: str | None = None
    official_app_id: str | None = None
    official_app_secret: str | None = None
    command_replies: dict[str, str] | None = None
    notify_on: bool | None = None
    notify_off: bool | None = None
    template_on: str | None = None
    template_off: str | None = None
    debounce_offline: int | None = None
    token: str | None = None   # X-Admin-Token 旧通道兼容（check_admin 用）


@app.get("/api/qq-notify/global")
async def qq_notify_global_get(request: Request):
    """全局通知配置（含 NapCat token，仅管理员）。"""
    check_admin(request)
    return _qq_global_row()


@app.patch("/api/qq-notify/global")
async def qq_notify_global_patch(body: QQNotifyGlobalIn, request: Request):
    """更新全局通知配置（逐字段合并；空串=清空该项）。"""
    check_admin(request, body.token)
    if body.onebot_url is not None:
        u = body.onebot_url.strip()
        if u and not u.startswith(("http://", "https://")):
            raise HTTPException(400, "NapCat 地址须以 http:// 或 https:// 开头")
    if body.webui_url is not None:
        u = body.webui_url.strip()
        if u and not u.startswith(("http://", "https://")):
            raise HTTPException(400, "WebUI 地址须以 http:// 或 https:// 开头")
    if body.debounce_offline is not None and not 0 <= body.debounce_offline <= 86400:
        raise HTTPException(400, "防抖秒数须在 0~86400 之间")
    if body.channel is not None and body.channel not in ("napcat", "official"):
        raise HTTPException(400, "channel 只支持 napcat / official")
    if body.command_replies is not None:
        # 指令表校验：键值非空、键 ≤32、值 ≤500（群消息长度余量）、条目 ≤50
        cr = body.command_replies
        if len(cr) > 50:
            raise HTTPException(400, "指令最多 50 条")
        for k, v in cr.items():
            if not str(k).strip() or not str(v).strip():
                raise HTTPException(400, "指令与回复内容都不能为空")
            if len(k) > 32:
                raise HTTPException(400, f"指令过长（≤32 字符）：{k[:16]}…")
            if len(v) > 500:
                raise HTTPException(400, f"回复内容过长（≤500 字符）：{k[:16]}…")
    for field, limit in (("qq_account", 40), ("onebot_url", 200),
                         ("onebot_token", 200), ("webui_url", 200),
                         ("webui_token", 200), ("official_app_id", 64),
                         ("official_app_secret", 128), ("template_on", 200),
                         ("template_off", 200)):
        v = getattr(body, field)
        if v is not None and len(v) > limit:
            raise HTTPException(400, f"{field} 过长（≤{limit} 字符）")

    sets: list[str] = []
    vals: list = []
    if body.qq_account is not None:
        sets.append("qq_account = ?"); vals.append(body.qq_account.strip())
    if body.onebot_url is not None:
        sets.append("onebot_url = ?"); vals.append(body.onebot_url.strip())
    if body.onebot_token is not None:
        sets.append("onebot_token = ?"); vals.append(body.onebot_token.strip())
    if body.webui_url is not None:
        sets.append("webui_url = ?"); vals.append(body.webui_url.strip())
    if body.webui_token is not None:
        sets.append("webui_token = ?"); vals.append(body.webui_token.strip())
    if body.channel is not None:
        sets.append("channel = ?"); vals.append(body.channel)
    if body.official_app_id is not None:
        sets.append("official_app_id = ?"); vals.append(body.official_app_id.strip())
    if body.official_app_secret is not None:
        sets.append("official_app_secret = ?")
        vals.append(body.official_app_secret.strip())
    if body.command_replies is not None:
        sets.append("command_replies = ?")
        vals.append(json.dumps(
            {str(k).strip(): str(v).strip()
             for k, v in body.command_replies.items()}, ensure_ascii=False))
    if body.notify_on is not None:
        sets.append("notify_on = ?"); vals.append(1 if body.notify_on else 0)
    if body.notify_off is not None:
        sets.append("notify_off = ?"); vals.append(1 if body.notify_off else 0)
    if body.template_on is not None:
        sets.append("template_on = ?"); vals.append(body.template_on)
    if body.template_off is not None:
        sets.append("template_off = ?"); vals.append(body.template_off)
    if body.debounce_offline is not None:
        sets.append("debounce_offline = ?"); vals.append(int(body.debounce_offline))
    if sets:
        sets.append("updated_at = ?"); vals.append(now_iso())
        conn = connect()
        try:
            conn.execute(f"UPDATE qq_notify_global SET {', '.join(sets)} "
                         "WHERE id = 1", tuple(vals))
            conn.commit()
        finally:
            conn.close()
    return _qq_global_row()


@app.get("/api/qq-notify/status")
async def qq_notify_status(request: Request):
    """探测发送通道连通性（不抛错，结果进 body）。

    v2.3.1 双通道：official=官方 API getAppAccessToken 探测（凭据有效性，
    2.5s 超时）；napcat=get_login_info（原逻辑）。
    通知器未部署 / 协议端未启动时不可达属预期，前端以文案区分。"""
    check_admin(request)
    g = _qq_global_row()

    if str(g.get("channel") or "napcat") == "official":
        app_id = (g.get("official_app_id") or "").strip()
        secret = (g.get("official_app_secret") or "").strip()
        if not app_id or not secret:
            return {"online": False, "account": None, "channel": "official",
                    "message": "未配置官方机器人 AppID/AppSecret（管理页填写）"}
        try:
            req = _urlreq.Request(
                _OFFICIAL_API + "/app/getAppAccessToken",
                data=json.dumps({"appId": app_id,
                                 "clientSecret": secret}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with _urlreq.urlopen(req, timeout=2.5) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (_urlerr.URLError, TimeoutError, OSError, ValueError) as e:
            return {"online": False, "account": None, "channel": "official",
                    "message": f"官方接口不可达（{e.__class__.__name__}）"}
        if "access_token" in payload:
            return {"online": True, "account": app_id, "channel": "official",
                    "message": f"官方通道就绪（AppID {app_id}，AccessToken 有效）"}
        return {"online": False, "account": None, "channel": "official",
                "message": f"官方凭据无效：{str(payload)[:80]}"}

    url = (g["onebot_url"] or "").rstrip("/") + "/get_login_info"
    headers = ({"Authorization": f"Bearer {g['onebot_token']}"}
               if g["onebot_token"] else {})
    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if payload.get("status") == "ok" and isinstance(payload.get("data"), dict):
            acct = int(payload["data"].get("user_id") or 0)
            note = ""
            expect = (g["qq_account"] or "").strip()
            if expect and expect != str(acct):
                note = f"；与预留 QQ 号 {expect} 不一致，请检查是否登错号"
            return {"online": True, "account": acct, "channel": "napcat",
                    "message": f"NapCat 在线，登录账号 {acct}{note}"}
        return {"online": False, "account": None, "channel": "napcat",
                "message": f"NapCat 应答异常：{str(payload)[:80]}"}
    except (_urlerr.URLError, TimeoutError, OSError, ValueError) as e:
        return {"online": False, "account": None, "channel": "napcat",
                "message": f"NapCat 不可达（{e.__class__.__name__}）；"
                           "通知器/NapCat 未部署或未启动属预期"}


# ---- NapCat WebUI 客户端（扫码登录嵌入；API 已对 NapCat 源码核对）----
# 鉴权流（packages/napcat-webui-backend，2026-10-13 核对）：
#   POST {webui}/api/auth/login  body {"hash": sha256_hex(token + ".napcat")}
#   → {"Credential": base64(...)}；业务接口带 Authorization: Bearer <Credential>，
#   凭证 1 小时有效；响应统一 {code, message, data}，code==0 为成功
#   （业务错/鉴权失败同为 HTTP 200 + code!=0）。
_WEBUI_CRED: dict = {"cred": "", "at": 0.0}   # 进程内凭证缓存（50 分钟复用）


def _webui_login(base: str, token: str) -> str:
    """用 webui.json 的 token 登录 WebUI，缓存并返回 Credential。

    响应形状（服务器 NapCat 4.18.38 实测）：{code:0, message, data:{Credential}}；
    兼容顶层 Credential（handler 层原形，防上游再改包装）。"""
    h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    req = _urlreq.Request(
        base.rstrip("/") + "/api/auth/login",
        data=json.dumps({"hash": h}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _urlreq.urlopen(req, timeout=5.0) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    data = (body or {}).get("data") or {}
    cred = (body or {}).get("Credential") or data.get("Credential")
    if not cred:
        raise RuntimeError(f"WebUI 登录被拒（token 不符？）: {str(body)[:120]}")
    _WEBUI_CRED.update(cred=cred, at=time.time())
    return cred


def _webui_call(g: dict, path: str, _retry: bool = True) -> dict:
    """调 WebUI POST API（自动登录/复用凭证）；返回 data，失败抛 RuntimeError。"""
    base = (g.get("webui_url") or "").rstrip("/")
    token = g.get("webui_token") or ""
    if not base or not token:
        raise RuntimeError("未配置 WebUI 地址或 token（管理页「开播账号管理」）")
    if not _WEBUI_CRED["cred"] or time.time() - _WEBUI_CRED["at"] > 3000:
        _webui_login(base, token)
    req = _urlreq.Request(
        base + path, data=b"{}",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _WEBUI_CRED["cred"]},
        method="POST")
    try:
        with _urlreq.urlopen(req, timeout=5.0) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (_urlerr.URLError, OSError, ValueError):
        if _retry:  # 凭证可能因 NapCat 重启失效——重登一次再试
            _WEBUI_CRED["cred"] = ""
            return _webui_call(g, path, _retry=False)
        raise
    if (body or {}).get("code") != 0:
        # code!=0 含鉴权失败（"Unauthorized"）：同样重登一次
        if _retry and "Unauthorized" in str(body.get("message", "")):
            _WEBUI_CRED["cred"] = ""
            return _webui_call(g, path, _retry=False)
        raise RuntimeError(f"WebUI 应答异常: {str(body)[:120]}")
    return body.get("data") or {}


@app.get("/api/qq-notify/qrlogin")
async def qq_notify_qrlogin(request: Request, refresh: bool = Query(False)):
    """扫码登录聚合端点（前端约 2.5 秒轮询，v2.3 嵌入「开播账号管理」）。

    返回 {phase, qrcode, message}：
    - online：QQ 已登录（isLogin）；
    - qr：待扫码（qrcode=二维码内容串，前端渲染成二维码图）；
    - scanned：已扫码待手机确认（qrLoginAccepted）；
    - error：WebUI 不可达 / 未配置 / 业务错（message 说明）。
    流程：CheckLoginStatus（自带 qrcodeurl）→ 无码时 GetQQLoginQrcode 取新码。
    refresh=true（2026-10-14 二维码保鲜）：强制调 RefreshQRcode 换新码——
    QQ 扫码二维码有效期约 2 分钟，前端每 90 秒带 refresh 重取，防止展示
    的码过期（用户扫码提示"二维码已过期"的根因）。登录服务重启中
    RefreshQRcode 返回空 qrcodeurl，转 error 提示稍候。
    """
    check_admin(request)
    g = _qq_global_row()
    try:
        st = _webui_call(g, "/api/QQLogin/CheckLoginStatus")
        if st.get("isLogin"):
            return {"phase": "online", "qrcode": "", "message": "QQ 已登录"}
        qr = st.get("qrcodeurl") or ""
        if refresh:
            r = _webui_call(g, "/api/QQLogin/RefreshQRcode") or {}
            qr = r.get("qrcodeurl") or ""
            if not qr:
                return {"phase": "error", "qrcode": "",
                        "message": "登录服务重启中，稍候会自动出新码"}
        if not qr:
            qr = (_webui_call(g, "/api/QQLogin/GetQQLoginQrcode")
                  or {}).get("qrcode") or ""
        if not qr:
            return {"phase": "error", "qrcode": "",
                    "message": st.get("loginError") or "未取得二维码，稍后重试"}
        return {"phase": "scanned" if st.get("qrLoginAccepted") else "qr",
                "qrcode": qr, "message": ""}
    except (RuntimeError, OSError, ValueError, _urlerr.URLError, TimeoutError) as e:
        msg = str(e) if isinstance(e, RuntimeError) else \
            f"WebUI 不可达（{e.__class__.__name__}）；NapCat 未启动属预期"
        return {"phase": "error", "qrcode": "", "message": msg}


# ---------------------------------------------------------------------------
# 群映射获取（v2.3.1 官方通道）：输入 QQ 群号 → 自动捕获 openid 并绑定。
#
# 原理：openid 是"机器人×群"维度标识，官方 OpenAPI 不提供群号反查（群信息
# 接口需单独授权），**只能从机器人事件拿**——GROUP_ADD_ROBOT（机器人被拉进
# 群）或 GROUP_AT_MESSAGE_CREATE（群内 @机器人）都带 group_openid。因此
# 绑定会话为：输入群号 → 后端起临时 WebSocket 订阅群事件（最长 2 分钟）→
# 用户按提示在目标群里做一个动作（@机器人 或 拉机器人进群）→ 事件到达即
# 把 openid 与该群号写入 qq_group_map，此后房间配置填群号即可直达（通知器
# 发送前查表换算）。这是平台机制下"全自动"的极限：必须有一个群内动作让
# 机器人"看见"这个群。
# ---------------------------------------------------------------------------
_BIND_TIMEOUT = 120.0   # 绑定会话最长等待（秒）
# 进程内绑定会话状态（同一时刻仅一个会话；daemon 线程持有）
_BIND: dict = {"state": "idle", "group_id": 0, "openid": "", "msg": "",
               "alive": False}


def _official_fetch_token(app_id: str, secret: str) -> str:
    """官方凭据换 AccessToken（复用 _OFFICIAL_API 常量，可测试注入）。"""
    req = _urlreq.Request(
        _OFFICIAL_API + "/app/getAppAccessToken",
        data=json.dumps({"appId": app_id, "clientSecret": secret}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _urlreq.urlopen(req, timeout=5.0) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if "access_token" not in body:
        raise RuntimeError(f"官方凭据无效：{str(body)[:80]}")
    return str(body["access_token"])


def _official_gateway(token: str) -> str:
    """取 WebSocket 网关地址。"""
    req = _urlreq.Request(_OFFICIAL_API + "/gateway",
                          headers={"Authorization": f"QQBot {token}"})
    with _urlreq.urlopen(req, timeout=5.0) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if "url" not in body:
        raise RuntimeError(f"获取网关失败：{str(body)[:80]}")
    return str(body["url"])


def _bind_worker(group_id: int, app_id: str, secret: str) -> None:
    """绑定会话后台线程：临时 WebSocket 监听群事件，捕获即写库。"""
    import asyncio

    async def listen() -> str:
        import websockets

        token = _official_fetch_token(app_id, secret)
        ws_url = _official_gateway(token)
        async with websockets.connect(ws_url) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get("op") != 10:
                raise RuntimeError(f"网关首帧异常(op={hello.get('op')})")
            interval = hello["d"]["heartbeat_interval"] / 1000

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await ws.send(json.dumps({"op": 1, "d": None}))

            hb = asyncio.create_task(heartbeat())
            try:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token": f"QQBot {token}",
                    "intents": 1 << 25,   # GROUP_AND_C2C_EVENT
                    "shard": [0, 1], "properties": {}}}))
                deadline = asyncio.get_event_loop().time() + _BIND_TIMEOUT
                while True:
                    left = deadline - asyncio.get_event_loop().time()
                    if left <= 0:
                        return ""   # 超时
                    frame = json.loads(await asyncio.wait_for(ws.recv(), left))
                    if frame.get("op") != 0:
                        continue
                    if frame.get("t") in ("GROUP_ADD_ROBOT",
                                          "GROUP_AT_MESSAGE_CREATE"):
                        return str((frame.get("d") or {}).get("group_openid") or "")
            finally:
                hb.cancel()

    try:
        openid = asyncio.run(asyncio.wait_for(listen(), _BIND_TIMEOUT + 30))
        if not openid:
            _BIND.update(state="timeout", msg="等待超时（2 分钟内未捕获群事件），"
                          "请确认已在目标群 @机器人或拉机器人进群后重试")
            return
        conn = connect()
        try:
            conn.execute(
                """INSERT INTO qq_group_map (group_id, group_openid, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(group_id) DO UPDATE SET
                       group_openid = excluded.group_openid,
                       updated_at = excluded.updated_at""",
                (group_id, openid.upper(), now_iso()))
            conn.commit()
        finally:
            conn.close()
        _BIND.update(state="captured", openid=openid.upper(),
                     msg=f"群 {group_id} 映射已绑定")
    except Exception as e:   # 凭据错/网络断/网关异常——会话失败可重试
        _BIND.update(state="error", msg=f"绑定会话失败：{e}"[:160])
    finally:
        _BIND["alive"] = False


class QQNotifyBindIn(BaseModel):
    """绑定会话发起入参。"""
    group_id: int
    token: str | None = None


@app.post("/api/qq-notify/bind-group")
async def qq_notify_bind_group(body: QQNotifyBindIn, request: Request):
    """发起群映射绑定会话（official 专属；同时刻仅一个会话）。"""
    check_admin(request, body.token)
    if body.group_id <= 0:
        raise HTTPException(400, "群号须为正整数")
    g = _qq_global_row()
    if str(g.get("channel") or "napcat") != "official":
        raise HTTPException(400, "群映射绑定仅官方通道使用（「发送通道」切换）")
    app_id = (g.get("official_app_id") or "").strip()
    secret = (g.get("official_app_secret") or "").strip()
    if not app_id or not secret:
        raise HTTPException(400, "未配置官方机器人 AppID/AppSecret")
    if _BIND["alive"]:
        raise HTTPException(409, "已有绑定会话进行中，请先完成或等待其超时")
    _BIND.update(state="waiting", group_id=body.group_id, openid="", msg="",
                 alive=True)
    import threading
    threading.Thread(target=_bind_worker,
                     args=(body.group_id, app_id, secret),
                     daemon=True).start()
    return {"ok": True, "group_id": body.group_id,
            "timeout_sec": int(_BIND_TIMEOUT)}


@app.get("/api/qq-notify/bind-status")
async def qq_notify_bind_status(request: Request):
    """绑定会话状态（前端 2.5s 轮询）：idle/waiting/captured/timeout/error。"""
    check_admin(request)
    return {"state": _BIND["state"], "group_id": _BIND["group_id"],
            "openid": _BIND["openid"], "message": _BIND["msg"]}


@app.get("/api/qq-notify/group-map")
async def qq_notify_group_map(request: Request):
    """已绑定映射列表（群号 ↔ openid）。"""
    check_admin(request)
    rows = q("SELECT group_id, group_openid, updated_at FROM qq_group_map "
             "ORDER BY group_id")
    return {"items": [dict(r) for r in rows]}


@app.delete("/api/qq-notify/group-map/{group_id}")
async def qq_notify_group_map_delete(group_id: int, request: Request):
    """解绑一个群映射（仅管理员；房间配置里的该群号将失去换算，发送跳过）。"""
    check_admin(request)
    conn = connect()
    try:
        conn.execute("DELETE FROM qq_group_map WHERE group_id = ?", (group_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 群指令自动回复（v2.3.1，official 通道）：常驻事件监听
# ---------------------------------------------------------------------------
# 指令回复**出厂默认**（v2.3.1）：库中 command_replies 为空对象时生效
# （「清空并保存=恢复默认」）；页面键值对编辑的内容存库覆盖本表。
QQ_COMMAND_REPLIES: dict[str, str] = {
    "/连接": "✅ 开播通知服务在线（官方通道直连，无需扫码登录）",
}

# 简易频控：openid → 最近回复时间戳；每群每分钟最多 5 次（防刷指令，
# 被动回复平台侧另有 5 分钟/条约束，此处避免高频触发频控错误码）
_cmd_rates: dict[str, list[float]] = {}
_CMD_RATE_LIMIT = 5
_CMD_RATE_WINDOW = 60.0


def match_command_reply(content: str,
                        replies: dict[str, str] | None = None) -> str:
    """@消息正文匹配指令表：去掉 @机器人 前缀（<@!BOTID>）后精确匹配。

    replies 为空/None 时用出厂默认表；纯函数便于单测。
    """
    text = _re.sub(r"<@!\d+>", "", content or "").strip()
    return (replies if replies else QQ_COMMAND_REPLIES).get(text, "")


def _reply_group_text(group_openid: str, content: str, msg_id: str) -> None:
    """被动回复一条群消息（带 msg_id，5 分钟内有效；同步 urllib 跑线程池）。"""
    g = _qq_global_row()
    token = _official_fetch_token((g.get("official_app_id") or "").strip(),
                                  (g.get("official_app_secret") or "").strip())
    req = _urlreq.Request(
        _OFFICIAL_API + f"/v2/groups/{group_openid}/messages",
        data=json.dumps({"msg_type": 0, "content": content,
                         "msg_id": msg_id}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"QQBot {token}"}, method="POST")
    with _urlreq.urlopen(req, timeout=5.0) as resp:
        resp.read()


async def _official_event_loop(stop: asyncio.Event) -> None:
    """official 通道事件中枢（lifespan 常驻任务）。

    周期检查全局配置（official 且凭据齐）→ 建立 WebSocket 订阅群事件 →
    GROUP_AT_MESSAGE_CREATE 命中指令表即以 msg_id 被动回复；配置不满足
    （未切通道/凭据空）时静默等待，通道切回/凭据变更 30s 内自动跟随
    （连接每 30s 无事件即重查配置）。异常断开 30s 退避重连。
    绑定会话（bind-group）为独立临时连接，与本任务互不影响。
    """
    import websockets

    async def serve_once() -> None:
        g = _qq_global_row()
        token = _official_fetch_token((g.get("official_app_id") or "").strip(),
                                      (g.get("official_app_secret") or "").strip())
        ws_url = _official_gateway(token)
        async with websockets.connect(ws_url) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get("op") != 10:
                raise RuntimeError(f"网关首帧异常 op={hello.get('op')}")
            interval = hello["d"]["heartbeat_interval"] / 1000

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await ws.send(json.dumps({"op": 1, "d": None}))

            hb = asyncio.create_task(heartbeat())
            try:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token": f"QQBot {token}", "intents": 1 << 25,
                    "shard": [0, 1], "properties": {}}}))
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 30)
                    except asyncio.TimeoutError:
                        # 静默期重查配置：通道切回/凭据清空 → 断开等待
                        cur = _qq_global_row()
                        if (str(cur.get("channel") or "napcat") != "official"
                                or not (cur.get("official_app_id") or "").strip()):
                            return
                        continue
                    frame = json.loads(raw)
                    if frame.get("op") != 0:
                        continue
                    if frame.get("t") != "GROUP_AT_MESSAGE_CREATE":
                        continue   # 绑定捕获走独立会话，此处只管指令回复
                    d = frame.get("d") or {}
                    # 指令表每条事件读库（页面保存后即时生效，无需重启）
                    reply = match_command_reply(
                        str(d.get("content") or ""),
                        _qq_global_row().get("command_replies"))
                    if not reply:
                        continue
                    oid = str(d.get("group_openid") or "")
                    now = time.time()
                    recent = [x for x in _cmd_rates.get(oid, [])
                              if now - x < _CMD_RATE_WINDOW]
                    if len(recent) >= _CMD_RATE_LIMIT:
                        continue
                    recent.append(now)
                    _cmd_rates[oid] = recent
                    try:
                        await asyncio.to_thread(
                            _reply_group_text, oid, reply, str(d.get("id") or ""))
                    except Exception as e:
                        print(f"[qq-notify] 指令回复发送失败: {e}", flush=True)
            finally:
                hb.cancel()
                try:
                    await asyncio.gather(hb, return_exceptions=True)
                except Exception:
                    pass

    while not stop.is_set():
        try:
            g = _qq_global_row()
            if (str(g.get("channel") or "napcat") == "official"
                    and (g.get("official_app_id") or "").strip()
                    and (g.get("official_app_secret") or "").strip()):
                await serve_once()
        except Exception as e:
            print(f"[qq-notify] 事件监听断开，30s 后重试: {e}", flush=True)
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            pass


def _qq_room_payload(room_id: int) -> dict:
    """房间级配置 + 全局模板与通道（后两者给前端做占位提示/动态 label）。"""
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不存在")
    row = q1("SELECT * FROM qq_notify WHERE room_id = ?", (room_id,))
    try:
        groups = json.loads(row["group_ids"]) if row else []
    except (ValueError, TypeError):
        groups = []
    g = _qq_global_row()
    return {
        "room_id": room_id,
        "enabled": bool(row["enabled"]) if row else False,
        "group_ids": [int(x) for x in groups],
        "template_on": (row["template_on"] if row else "") or "",
        "template_off": (row["template_off"] if row else "") or "",
        "global_template_on": g["template_on"],
        "global_template_off": g["template_off"],
        "channel": str(g.get("channel") or "napcat"),  # v2.3.1 官方通道下提示群号需先绑定映射
    }


class QQNotifyRoomIn(BaseModel):
    """房间级配置 PATCH 体（全字段可选；群列表传全量覆盖）。
    两通道统一填 QQ 群号（official 发送前经 qq_group_map 换 openid）。"""
    enabled: bool | None = None
    group_ids: list[int] | None = None
    template_on: str | None = None
    template_off: str | None = None
    token: str | None = None


@app.get("/api/rooms/{room_id}/qq-notify")
async def qq_notify_room_get(room_id: int):
    """房间级通知配置（登录角色可读；开关/文案不含敏感项）。"""
    return _qq_room_payload(room_id)


@app.patch("/api/rooms/{room_id}/qq-notify")
async def qq_notify_room_patch(room_id: int, body: QQNotifyRoomIn, request: Request):
    """更新房间级通知配置（UPSERT 整行；仅管理员）。"""
    check_admin(request, body.token)
    if not q1("SELECT room_id FROM rooms WHERE room_id = ?", (room_id,)):
        raise HTTPException(404, "房间不存在")
    groups: list[int] | None = None
    if body.group_ids is not None:
        groups = sorted({int(g) for g in body.group_ids if int(g) > 0})
        if len(groups) > 20:
            raise HTTPException(400, "目标群最多 20 个")
    for field in ("template_on", "template_off"):
        v = getattr(body, field)
        if v is not None and len(v) > 200:
            raise HTTPException(400, f"{field} 过长（≤200 字符）")

    # 与库中现值合并（PATCH 语义：未传字段不动）
    cur = q1("SELECT * FROM qq_notify WHERE room_id = ?", (room_id,)) or {}
    enabled = (1 if body.enabled else 0) if body.enabled is not None \
        else int(cur.get("enabled", 0) or 0)
    group_ids_json = (json.dumps(groups) if groups is not None
                      else cur.get("group_ids", "[]") or "[]")
    tpl_on = body.template_on if body.template_on is not None \
        else (cur.get("template_on", "") or "")
    tpl_off = body.template_off if body.template_off is not None \
        else (cur.get("template_off", "") or "")

    conn = connect()
    try:
        conn.execute(
            """INSERT INTO qq_notify (room_id, enabled, group_ids,
                                      template_on, template_off, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(room_id) DO UPDATE SET
                   enabled = excluded.enabled, group_ids = excluded.group_ids,
                   template_on = excluded.template_on,
                   template_off = excluded.template_off,
                   updated_at = excluded.updated_at""",
            (room_id, enabled, group_ids_json, tpl_on, tpl_off, now_iso()))
        conn.commit()
    finally:
        conn.close()
    return _qq_room_payload(room_id)


# ---------------------------------------------------------------------------
# 页面路由（v1.6 React SPA，ThreeUI 适配）+ 静态资源
# ---------------------------------------------------------------------------
from fastapi.staticfiles import StaticFiles

# 前端构建产物（frontend/ 下 npm run build 生成；dist 不入 git，随 tar
# 部署——同 bus-manager v2.0.0 先例）。页面路由统一发 SPA shell，客户端
# 按 URL 渲染视图；登录门语义保留在 middleware，SPA 只消费 Cookie 与
# /api/auth/me。
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

@app.get("/")
async def index_page(request: Request):
    """总览页（SPA 总览视图：房间列表 + 窗口切换 + hero）。"""
    return spa_shell()


@app.get("/room/{room_id}")
async def room_page(request: Request, room_id: int):
    """房间详情页（SPA 详情视图）。"""
    return spa_shell()


@app.get("/session/{room_id}/{session_id}")
async def session_page(request: Request, room_id: int, session_id: int):
    """历史场次详情页：同一详情视图的场次模式。"""
    return spa_shell()


@app.get("/login")
async def login_page(request: Request):
    """登录页（SPA 登录视图：管理员账号 / 游客码双模式）。"""
    return spa_shell()


@app.get("/analysis")
async def analysis_page(request: Request):
    """数据分析页（SPA 分析视图）。"""
    return spa_shell()


@app.get("/manage")
async def manage_page(request: Request):
    """房间管理页（SPA 管理视图）。"""
    return spa_shell()


# ---------------------------------------------------------------------------
# v1.6 剥离：本站不再有回放页——旧地址 302 引导至独立 replay 项目
# ---------------------------------------------------------------------------
@app.get("/{room_id}/replay", include_in_schema=False)
async def replay_redirect(room_id: int):
    """旧回放地址兜底（生产 middleware 已前置拦截重定向；本路由兜本地
    调试模式直访）。replay 站登录门接管，bili 管理员经 SSO 桥接无感续登。"""
    base = cfg.get("web", {}).get("replay_base", "/replay")
    return RedirectResponse(f"{base}/{room_id}/replay", status_code=302)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_root_files(full_path: str):
    """兜底路由（注册顺序在最后，优先级最低）：① Vite public/ 直拷到 dist
    根的文件（favicon 等）直出（路径限制在 dist 内，防目录穿越）；② 其余
    未知页面路径发 SPA shell（前端路由自显 404/回正）；③ 未知 /api 路径
    保持 404 JSON 语义不被 shell 吞掉。"""
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


def _days_ago(days: int) -> str:
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _hours_ago(hours: int) -> str:
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


# 启动时确保表存在（web 进程独立于 capture 运行）。
# 初始登录凭据的播种改在 lifespan（服务真正启动时）执行，见文件头部。
init_db()
