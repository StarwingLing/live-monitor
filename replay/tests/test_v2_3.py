# -*- coding: utf-8 -*-
r"""replay v2.3.0 新增件测试：投稿合集 / 分P 支持 + 投稿账号管理。

覆盖：
1. bili_api.py 单测（HTTP 层全部 mock，不打真实 B 站）：
   - cookies 解析（缺失 / 非法 JSON / 缺 SESSDATA / 正常 + Cookie 串拼接）；
   - _http_json 错误规范化（-101 凭据失效文案、code!=0 拒绝、HTTP/网络失败）；
   - fetch_seasons / fetch_arcs / fetch_archive_parts 防御式解析与分页停止；
   - season_add_video 载荷（csrf 走 URL 参数、episodes 数组形态）；
   - resolve_aid（稿件列表命中 bvid / 未命中走 view 反查 / 格式非法）；
   - sanitize_part_name（非法字符、空、限长）；
2. 端点：/api/submit/targets（guest 403、clipper 200、上游失败 502、
   未部署 503）、/api/submit/arcs/{aid}/parts、
   /api/admin/submit/account|cookies|renew（admin 专属矩阵 + 凭据结构校验）；
3. 投稿端点 v2.3.0 分支：part 模式表单校验（目标必填、BV/av 形态、分P名
   限长）、new 模式合集须带小节；
4. run_job 双模式（fake exec）：append 命令拼装（--user-cookie 在前、
   --vid、临时文件名 = 分P名、成功 BV 回退目标号）、new+合集流程
   （resolve_aid → 取 cid → episodes/add；合集失败 fail-soft 只写
   season_note）；submit_status 透出 mode / season_note。

运行：uv run python tests/test_v2_3.py
"""
import asyncio
import json
import sys
import time
import types as _types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

import replay.web.bili_api as bili_api
import replay.web.submit_sync as submit_sync
from replay.web import app as app_mod
from replay.web import upload as upload_mod
from replay.web.app import app
from replay import auth

from fastapi.testclient import TestClient

# ---- 生产模式 + 录像根 / 投稿临时区指向临时目录（同 test_v2_0）----
app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"
app_mod.RECORDINGS_ROOT = TMP / "recordings"
app_mod.RECORDINGS_ROOT.mkdir(exist_ok=True)
upload_mod.TMP_DIR = TMP / "tmp_v23"
upload_mod.TMP_DIR.mkdir(exist_ok=True)
from fastapi.staticfiles import StaticFiles as _SF
for _route in app.router.routes:
    if getattr(_route, "path", "") == "/recordings":
        _route.app = _SF(directory=str(app_mod.RECORDINGS_ROOT))

from replay.db import connect
_conn = connect()
H.init_tables(_conn)
_conn.close()

REC = app_mod.RECORDINGS_ROOT


def fresh(cookies: dict | None = None) -> TestClient:
    c = TestClient(app)
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    return c


_ADMIN_C = fresh({"replay_auth": auth.sign_session("admin", "admin1")})
CLIPPER_101 = fresh({"replay_auth": auth.sign_session("clipper", "101",
                                                      nickname="歌切工")})
GUEST_101 = fresh({"replay_auth": auth.sign_session("guest", "101")})
cp = _types.SimpleNamespace(
    get=lambda url, headers=None, **kw: (headers or _ADMIN_C).get(url, **kw),
    post=lambda url, headers=None, **kw: (headers or _ADMIN_C).post(url, **kw),
    delete=lambda url, headers=None, **kw:
        (headers or _ADMIN_C).delete(url, **kw))


def seed_clip(room, rec_id, title="测试切片", file_path="", status="done"):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO manual_clips (recording_id, room_id, title, "
            "start_sec, end_sec, file_path, file_size, duration_sec, status, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, room, title, 0, 60, file_path, 40000 if file_path else 0,
             60, status, "tester", "2026-10-19T10:00:00"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


rid = 1
c_id = seed_clip(101, rid, title="青鸟第一幕",
                 file_path="101/clip_sub/clip.mp4")
clip_file = REC / "101" / "clip_sub" / "clip.mp4"
clip_file.parent.mkdir(parents=True, exist_ok=True)
clip_file.write_bytes(b"fake-mp4")

# ===========================================================================
# 1. bili_api 单测（HTTP 层 mock）
# ===========================================================================
# 1.1 sanitize_part_name
check("分P名清洗：非法字符替下划线 + 去首尾空白/点",
      bili_api.sanitize_part_name('  青/鸟*第?一"幕.. ') == "青_鸟_第_一_幕")
check("分P名清洗：限长 80", len(bili_api.sanitize_part_name("字" * 200)) == 80)

# 1.2 cookies 解析（指向临时文件，不动 data/）
CK = TMP / "biliup-cookies-v23.json"
app_mod.cfg["bili"]["biliup_cookies"] = str(CK)
GOOD_COOKIES = {
    "cookie_info": {"cookies": [
        {"name": "SESSDATA", "value": "sess-fake"},
        {"name": "bili_jct", "value": "csrf-fake"},
        {"name": "DedeUserID", "value": "10086"}]},
    "token_info": {"access_token": "atk-fake", "expires_in": 1,
                   "mid": 10086, "refresh_token": "rt"},
    "sso": [],
}
try:
    bili_api.load_cookies()
    check("cookies 未配置 → 明确异常", False)
except bili_api.BiliApiError as e:
    check("cookies 未配置 → 明确异常（含指引文案）",
          "未配置" in str(e) and e.auth_failed)
CK.write_text("{ 不是 json", "utf-8")
try:
    bili_api.load_cookies()
    check("cookies 非法 JSON → 异常", False)
except bili_api.BiliApiError as e:
    check("cookies 非法 JSON → 异常", "解析失败" in str(e))
CK.write_text(json.dumps({"cookie_info": {"cookies": [
    {"name": "DedeUserID", "value": "1"}]}, "token_info": {}}), "utf-8")
try:
    bili_api.load_cookies()
    check("cookies 缺 SESSDATA → 异常", False)
except bili_api.BiliApiError as e:
    check("cookies 缺 SESSDATA → 异常（提示完整文件）", "SESSDATA" in str(e))
CK.write_text(json.dumps(GOOD_COOKIES), "utf-8")
check("cookies 正常解析 + Cookie 串拼接",
      bili_api._cookie_header() ==
      "SESSDATA=sess-fake; bili_jct=csrf-fake; DedeUserID=10086")


# 1.3 _http_json 错误规范化（monkeypatch urlopen）
class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(payload, record: list | None = None):
    def _u(req, timeout=None):
        if isinstance(payload, Exception):
            raise payload
        if record is not None:
            record.append((req.full_url, dict(req.headers), req.data))
        return _FakeResp(payload)
    return _u


_orig_urlopen = bili_api.urllib.request.urlopen
bili_api.urllib.request.urlopen = _fake_urlopen(
    {"code": -101, "message": "账号未登录"})
try:
    bili_api._http_json("https://x/api")
    check("-101 → 凭据失效异常", False)
except bili_api.BiliApiError as e:
    check("-101 → 凭据失效异常（auth_failed + 文案）",
          e.auth_failed and "凭据" in str(e))
bili_api.urllib.request.urlopen = _fake_urlopen(
    {"code": 10071, "message": "稿件审核中不能编辑"})
try:
    bili_api._http_json("https://x/api")
    check("code!=0 → 拒绝异常", False)
except bili_api.BiliApiError as e:
    check("code!=0 → 拒绝异常（透传 message）",
          not e.auth_failed and "10071" not in str(e) and "审核中" in str(e))
bili_api.urllib.request.urlopen = _fake_urlopen(
    {"code": 0, "message": "0", "data": {"isLogin": True, "uname": "UP主",
                                         "mid": 10086, "face": "f.jpg"}})
check("nav 正常解析", bili_api.fetch_account_info() ==
      {"ok": True, "uname": "UP主", "mid": 10086, "face": "f.jpg"})
_req_seen: list = []
bili_api.urllib.request.urlopen = _fake_urlopen(
    {"code": 0, "message": "0", "data": {}}, record=_req_seen)
bili_api._http_json("https://member.bilibili.com/x2/creative/web/season/"
                    "section/episodes/add", data=b"{}", csrf="abc")
_u, _h, _d = _req_seen[-1]
check("写请求形态：csrf 进 URL 参数 + JSON 头 + Cookie 头",
      _u.endswith("episodes/add?csrf=abc")
      and _h.get("Content-type") == "application/json"
      and "SESSDATA=sess-fake" in _h.get("Cookie", ""))
bili_api.urllib.request.urlopen = _fake_urlopen(
    bili_api.urllib.error.URLError("net down"))
try:
    bili_api._http_json("https://x/api")
    check("网络失败 → 可读异常", False)
except bili_api.BiliApiError as e:
    check("网络失败 → 可读异常", "网络" in str(e))
bili_api.urllib.request.urlopen = _orig_urlopen

# 1.4 seasons / arcs / parts 解析（monkeypatch _http_json）
SEASONS_PAGE = {"seasons": [
    {"season": {"id": 777, "title": "直播歌集合集"},
     "sections": {"sections": [{"id": 555, "title": "正片", "epCount": 12}]},
     "part_episodes": [{"aid": 1}, {"aid": 2}]},
    {"season": {"id": 888, "title": "杂谈"}, "sections": {},
     "part_episodes": []}],
    "total": 2}
_orig_http = bili_api._http_json
bili_api._http_json = lambda url, **kw: SEASONS_PAGE
bili_api.drop_cache()
seasons = bili_api.fetch_seasons()
check("seasons 解析：标题/小节/集数",
      seasons[0]["season_id"] == 777 and seasons[0]["ep_total"] == 2
      and seasons[0]["sections"][0]["id"] == 555
      and seasons[0]["sections"][0]["ep_count"] == 12)
check("seasons 解析：空合集防御",
      seasons[1]["season_id"] == 888 and seasons[1]["sections"] == []
      and seasons[1]["ep_total"] == 0)
check("seasons 60s 缓存命中（同对象）",
      bili_api.fetch_seasons() is seasons)

ARCS_PAGE = {"arc_audits": [
    {"Archive": {"aid": 111, "bvid": "BV1AAAAAAAA1", "title": "10-18 直播"}},
    {"Archive": {"aid": 222, "bvid": "BV1BBBBBBBB2", "title": "10-19 直播"}}],
    "page": {"count": 2, "pn": 1, "ps": 30}}
# v2.3.0 起 fetch_arcs 逐条核验存在性（view 接口）——urlopen 一并 mock，
# 避免打真实网络：默认全部存在（code 0）
_orig_urlopen2 = bili_api.urllib.request.urlopen
bili_api.urllib.request.urlopen = _fake_urlopen({"code": 0, "data": {}})
bili_api._http_json = lambda url, **kw: ARCS_PAGE
bili_api.drop_cache()
arcs = bili_api.fetch_arcs()
check("arcs 解析：aid/bvid/title",
      arcs[0] == {"aid": 111, "bvid": "BV1AAAAAAAA1", "title": "10-18 直播"}
      and len(arcs) == 2)

PARTS_DATA = {"archive": {"aid": 111}, "videos": [
    {"cid": 9001, "index": 1, "title": "第一首歌"},
    {"cid": 9002, "index": 2, "title": "第二首歌"}]}
bili_api._http_json = lambda url, **kw: PARTS_DATA
parts = bili_api.fetch_archive_parts(111)
check("稿件分P解析：cid/index/title",
      parts[0] == {"cid": 9001, "index": 1, "title": "第一首歌"}
      and len(parts) == 2)

# 1.5 season_add_video 载荷
_seen: dict = {}


def _cap_http(url, *, data=None, csrf=""):
    _seen.update(url=url, data=data, csrf=csrf)
    return {}


bili_api._http_json = _cap_http
bili_api.season_add_video(111, 9001, "新视频", 555)
check("episodes/add：调用点带 csrf 参数 + 正确端点",
      _seen["url"].endswith(
          "/x2/creative/web/season/section/episodes/add")
      and _seen["csrf"] == "csrf-fake")
_payload = json.loads(_seen["data"].decode())
check("episodes/add：载荷形态（section_id + episodes 数组）",
      _payload["section_id"] == 555
      and _payload["episodes"] == [{"aid": 111, "cid": 9001,
                                    "title": "新视频", "charging_pay": False}])

# 1.6 resolve_aid：属主稿件列表命中 / view 反查 / 格式非法
bili_api.drop_cache()
bili_api._http_json = lambda url, **kw: ARCS_PAGE
check("resolve_aid：稿件列表 bvid 命中",
      bili_api.resolve_aid("BV1BBBBBBBB2") == 222)
bili_api._http_json = _cap_http  # 顺带记录 url
bili_api._http_json = lambda url, **kw: {"aid": 333}
check("resolve_aid：未命中走 view 反查",
      bili_api.resolve_aid("BV1CCCCCCCC3") == 333)
try:
    bili_api.resolve_aid("av12345")
    check("resolve_aid：格式非法拒绝", False)
except bili_api.BiliApiError:
    check("resolve_aid：格式非法拒绝", True)

# 1.7 存在性过滤：创作中心残留的已删除稿件（view -404）剔除，网络失败放行
GHOST_PAGE = {"arc_audits": [
    {"Archive": {"aid": 111, "bvid": "BV1AAAAAAAA1", "title": "真实稿"}},
    {"Archive": {"aid": 222, "bvid": "BV1GGGGGGGGG", "title": "测试投稿"}}],
    "page": {"count": 2, "pn": 1, "ps": 30}}
bili_api._http_json = lambda url, **kw: GHOST_PAGE
_route_404 = lambda req, timeout=None: (_FakeResp({"code": -404})
                                        if "BV1GGGGGGGGG" in req.full_url
                                        else _FakeResp({"code": 0}))
bili_api.urllib.request.urlopen = _route_404
bili_api.drop_cache()
arcs_f = bili_api.fetch_arcs()
check("存在性过滤：view -404 的幽灵稿件被剔除",
      len(arcs_f) == 1 and arcs_f[0]["aid"] == 111)
bili_api.urllib.request.urlopen = lambda req, timeout=None: (
    (_ for _ in ()).throw(bili_api.urllib.error.URLError("net down")))
bili_api.drop_cache()
arcs_n = bili_api.fetch_arcs()
check("存在性过滤：网络失败全放行（fail-open 不误删）", len(arcs_n) == 2)

bili_api._http_json = _orig_http
bili_api.urllib.request.urlopen = _orig_urlopen2

# ===========================================================================
# 2. 端点：targets / parts / 账号管理（B 站侧 mock）
# ===========================================================================
app_mod.cfg["bili"]["biliup_bin"] = ""

r = GUEST_101.get("/api/submit/targets")
check("targets：guest 403（middleware）", r.status_code == 403)
r = cp.get("/api/submit/targets")
check("targets：未部署 503", r.status_code == 503)

app_mod.cfg["bili"]["biliup_bin"] = "fake-biliup"
_orig_acc = bili_api.fetch_account_info
_orig_seas = bili_api.fetch_seasons
_orig_arcs = bili_api.fetch_arcs
bili_api.fetch_account_info = lambda: {"ok": True, "uname": "UP主",
                                       "mid": 10086, "face": "f.jpg"}
bili_api.fetch_seasons = lambda refresh=False: [{"season_id": 777,
                                                 "title": "歌集合集",
                                                 "ep_total": 2,
                                                 "sections": [{"id": 555,
                                                               "title": "正片",
                                                               "ep_count": 2}]}]
bili_api.fetch_arcs = lambda limit=100, refresh=False: [
    {"aid": 111, "bvid": "BV1AAAAAAAA1", "title": "10-18 直播"}]
r = CLIPPER_101.get("/api/submit/targets")
d = r.json()
check("targets：clipper 200 + 三段数据",
      r.status_code == 200 and d["account"]["uname"] == "UP主"
      and d["seasons"][0]["season_id"] == 777 and d["arcs"][0]["aid"] == 111)

bili_api.fetch_account_info = lambda: (_ for _ in ()).throw(
    bili_api.BiliApiError("投稿凭据可能失效，请管理员重新配置 cookies.json（x）",
                          True))
r = CLIPPER_101.get("/api/submit/targets")
check("targets：上游失败 502 透传原因",
      r.status_code == 502 and "凭据" in r.json()["detail"])

_orig_parts = bili_api.fetch_archive_parts
bili_api.fetch_account_info = _orig_acc
bili_api.fetch_archive_parts = lambda aid: [{"cid": 9001, "index": 1,
                                             "title": "第一首歌"}]
r = CLIPPER_101.get("/api/submit/arcs/111/parts")
check("parts：clipper 200", r.status_code == 200
      and r.json()["parts"][0]["cid"] == 9001)
r = CLIPPER_101.get("/api/submit/arcs/0/parts")
check("parts：非法 aid 400", r.status_code == 400)
r = GUEST_101.get("/api/submit/arcs/111/parts")
check("parts：guest 403", r.status_code == 403)

# ---- 账号管理端点（admin 专属）----
r = CLIPPER_101.get("/api/admin/submit/account")
check("账号状态：clipper 403（/api/admin 前缀门）", r.status_code == 403)
bili_api.fetch_account_info = lambda: {"ok": True, "uname": "UP主",
                                       "mid": 10086, "face": "f.jpg"}
r = cp.get("/api/admin/submit/account")
d = r.json()
check("账号状态：admin 200 + 不回传 cookie 内容",
      r.status_code == 200 and d["bin_configured"] is True
      and d["cookies_present"] is True and d["cookie_ok"] is True
      and d["account"]["uname"] == "UP主"
      and "SESSDATA" not in json.dumps(d))

r = cp.post("/api/admin/submit/cookies", json={"cookies_json": "{bad json"})
check("cookies 保存：非法 JSON 400", r.status_code == 400)
r = cp.post("/api/admin/submit/cookies", json={
    "cookies_json": json.dumps({"cookie_info": {"cookies": []}})})
check("cookies 保存：缺 cookies 数组 400", r.status_code == 400)
r = cp.post("/api/admin/submit/cookies", json={
    "cookies_json": json.dumps({"cookie_info": {"cookies": [
        {"name": "SESSDATA", "value": "x"},
        {"name": "bili_jct", "value": "y"}]}, "token_info": {}})})
d = r.json()
check("cookies 保存：biliup JSON 缺 access_token → 落盘但 upload_ready=False",
      r.status_code == 200 and d["saved"] is True and d["kind"] == "biliup"
      and d["upload_ready"] is False)
check("has_access_token：缺 token → False",
      bili_api.has_access_token() is False)
r = cp.post("/api/admin/submit/cookies", json={
    "cookies_json": json.dumps(GOOD_COOKIES)})
d = r.json()
check("cookies 保存：合法即落盘 + 即时验证 + upload_ready",
      r.status_code == 200 and d["saved"] is True and d["cookie_ok"] is True
      and d["upload_ready"] is True
      and json.loads(CK.read_text("utf-8"))["token_info"]
      == GOOD_COOKIES["token_info"])
check("has_access_token：完整 biliup 凭据 → True",
      bili_api.has_access_token() is True)
WEB_COOKIE = ("buvid3=xyz; SESSDATA=sess-fake; bili_jct=csrf-fake; "
              "DedeUserID=10086")
# 兑换函数统一 mock 起点（后续分支各自替换）：此用例走「兑换失败 + 无现有
# 好凭据」→ 回退网页格式落盘
_orig_exchange = bili_api.exchange_web_login
bili_api.exchange_web_login = lambda raw: (_ for _ in ()).throw(
    bili_api.BiliApiError("登录确认被拒：账号未登录"))
CK.write_text(json.dumps({"cookie_info": GOOD_COOKIES["cookie_info"],
                          "token_info": {}, "sso": []}), "utf-8")
r = cp.post("/api/admin/submit/cookies",
            json={"cookies_json": WEB_COOKIE})
d = r.json()
_doc = json.loads(CK.read_text("utf-8"))
check("cookies 保存：浏览器 Cookie 串 → web 形态落盘（token 空壳）",
      r.status_code == 200 and d["kind"] == "web"
      and d["upload_ready"] is False and d["exchanged"] is False
      and d["kept_existing"] is False
      and {c["name"] for c in _doc["cookie_info"]["cookies"]}
      == {"buvid3", "SESSDATA", "bili_jct", "DedeUserID"}
      and _doc["token_info"] == {"access_token": "", "expires_in": 0,
                                 "mid": 10086, "refresh_token": ""}
      and _doc["sso"] == [])

# ---- 网页 Cookie 保存时的自动登录兑换（成功 / 回退两分支）----
EXCHANGED = {"cookie_info": {"cookies": [
    {"name": "SESSDATA", "value": "sess-new"},
    {"name": "bili_jct", "value": "jct-new"}]},
    "token_info": {"access_token": "atk-real", "expires_in": 1,
                   "mid": 10086, "refresh_token": "rt-real"}, "sso": []}
bili_api.exchange_web_login = lambda raw: EXCHANGED
r = cp.post("/api/admin/submit/cookies",
            json={"cookies_json": WEB_COOKIE})
d = r.json()
_doc2 = json.loads(CK.read_text("utf-8"))
check("web 保存自动兑换：落盘完整凭据 + upload_ready=True",
      r.status_code == 200 and d["exchanged"] is True
      and d["upload_ready"] is True and d["cookie_ok"] is True
      and _doc2["token_info"]["access_token"] == "atk-real")
bili_api.exchange_web_login = lambda raw: (_ for _ in ()).throw(
    bili_api.BiliApiError("登录确认被拒：账号未登录"))
# 先清成无 token 的网页格式（首次配置场景——kept_existing 只保护已有好凭据）
CK.write_text(json.dumps({"cookie_info": GOOD_COOKIES["cookie_info"],
                          "token_info": {}, "sso": []}), "utf-8")
r = cp.post("/api/admin/submit/cookies",
            json={"cookies_json": WEB_COOKIE})
d = r.json()
check("web 保存兑换失败：无现有完整凭据时回退网页凭据 + 单独返回原因",
      r.status_code == 200 and d["exchanged"] is False
      and d["kept_existing"] is False
      and d["upload_ready"] is False and "被拒" in d["exchange_error"]
      and bili_api.has_access_token() is False)
bili_api.exchange_web_login = _orig_exchange
try:
    bili_api.parse_credential_text("随便一段不是凭据的文本")
    check("凭据鉴别：垃圾文本拒绝", False)
except bili_api.BiliApiError:
    check("凭据鉴别：垃圾文本拒绝", True)
r = cp.delete("/api/admin/submit/cookies")
check("cookies 清除：文件删除 + 状态回未部署",
      r.status_code == 200 and not CK.exists())
CK.write_text(json.dumps(GOOD_COOKIES), "utf-8")     # 恢复供后续投稿用例

# ---- 投稿 access_token 守卫（v2.3.0 服务器实证：两模式统一需完整凭据）----
CK.write_text(json.dumps({"cookie_info": GOOD_COOKIES["cookie_info"],
                          "token_info": {}, "sso": []}), "utf-8")
r = cp.post(f"/api/rooms/101/clips/{c_id}/submit",
            json={"mode": "new", "title": "有 token 才能投"},
            headers=CLIPPER_101)
check("投稿守卫：网页 cookie 新视频投稿 503（文案指引账号管理）",
      r.status_code == 503 and "access_token" in r.json()["detail"])
r = cp.post(f"/api/rooms/101/clips/{c_id}/submit",
            json={"mode": "part", "target_vid": "111",
                  "part_title": "守卫用例"}, headers=CLIPPER_101)
check("投稿守卫：part 模式同样 503（biliup 内部查稿件需 token，服务器实测 -400）",
      r.status_code == 503 and "access_token" in r.json()["detail"])
CK.write_text(json.dumps(GOOD_COOKIES), "utf-8")

# ---- 兑换失败不覆盖现有完整凭据（kept_existing 守卫，服务器事故修复）----
def _exchange_fail(raw):
    raise bili_api.BiliApiError("登录确认被拒：账号未登录")


bili_api.exchange_web_login = _exchange_fail
r = cp.post("/api/admin/submit/cookies",
            json={"cookies_json": WEB_COOKIE})
d = r.json()
_doc3 = json.loads(CK.read_text("utf-8"))
check("兑换失败 + 现有完整凭据 → 保留不动（kept_existing=True，saved=False）",
      r.status_code == 200 and d["kept_existing"] is True
      and d["saved"] is False and "被拒" in d["exchange_error"]
      and _doc3["token_info"]["access_token"] == GOOD_COOKIES["token_info"]["access_token"]
      and bili_api.has_access_token() is True)
bili_api.exchange_web_login = _orig_exchange

# ===========================================================================
# 3. 投稿端点 v2.3.0 分支（part 校验 / new 合集参数）
# ===========================================================================
for body, why in (
        ({"mode": "part"}, "part 缺目标稿件"),
        ({"mode": "part", "target_vid": "abc"}, "目标稿件号形态非法"),
        ({"mode": "part", "target_vid": "111", "part_title": "x" * 81},
         "分P名超长"),
        ({"mode": "new", "season_id": 777}, "选合集缺小节 400"),
        ({"mode": "banana"}, "mode 非法")):
    r = cp.post(f"/api/rooms/101/clips/{c_id}/submit", json=body,
                headers=CLIPPER_101)
    check(f"投稿表单校验：{why} → 400", r.status_code == 400)

conn = connect()
conn.execute("UPDATE manual_clips SET title='' WHERE id=?", (c_id,))
conn.commit()
conn.close()
r = cp.post(f"/api/rooms/101/clips/{c_id}/submit",
            json={"mode": "part", "target_vid": "111"},
            headers=CLIPPER_101)
check("投稿表单校验：分P名空且切片无名 → 400", r.status_code == 400)
conn = connect()
conn.execute("UPDATE manual_clips SET title='青鸟第一幕' WHERE id=?", (c_id,))
conn.commit()
conn.close()

# ===========================================================================
# 4. run_job 双模式（fake exec；同 test_v2_0 手法）
# ===========================================================================
CMD_SEEN: list[list[str]] = []


class _FakeProc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err


async def _fake_exec(*cmd, **kw):
    CMD_SEEN.append(list(cmd))
    return _FakeProc(0, out=b"ok BV1Xvn366Ef5\n")


async def _fake_exec_nobv(*cmd, **kw):
    CMD_SEEN.append(list(cmd))
    return _FakeProc(0, out=b"append done\n")


_orig_exec = asyncio.create_subprocess_exec


async def _run_one(form: dict, clip_id: int, exec_fake=None):
    asyncio.create_subprocess_exec = exec_fake or _fake_exec
    try:
        row = app_mod._load_clip(101, clip_id)
        job = upload_mod.create_job(101, clip_id, form.get("title", "")
                                    or form.get("part_title", ""),
                                    form.get("mode", "new"))

        async def _sign(key, bucket=""):
            raise AssertionError("本用例不应触发 OSS 回拉")

        await upload_mod.run_job(job, dict(row), form,
                                 app_mod.RECORDINGS_ROOT, _sign)
        return job, CMD_SEEN[-1]
    finally:
        asyncio.create_subprocess_exec = _orig_exec


# 4.1 part 模式：命令拼装 + 文件名 = 分P名
CMD_SEEN.clear()
job, cmd = asyncio.run(_run_one({
    "mode": "part", "target_vid": "BV1BBBBBBBB2",
    "part_title": '青鸟/第一"幕'}, c_id, _fake_exec))
check("part 模式：append 子命令 + --user-cookie 在其之前",
      cmd[cmd.index("--user-cookie") + 1] == str(upload_mod.cookies_path())
      and "append" in cmd
      and cmd.index("--user-cookie") < cmd.index("append"))
check("part 模式：--vid 传目标稿件号",
      cmd[cmd.index("--vid") + 1] == "BV1BBBBBBBB2")
upfile = Path(cmd[-1])
check("part 模式：上传文件名 = 清洗后的分P名（stem 一致）",
      upfile.name == "青鸟_第一_幕.mp4"
      and upfile.parent.parent == upload_mod.TMP_DIR)
check("part 模式：临时目录用后即清",
      not upfile.parent.exists())
check("part 模式：成功态 + BV 解析",
      job["status"] == "success" and job["bv"] == "BV1Xvn366Ef5"
      and job["mode"] == "part" and job["season_note"] == "")

# 4.2 part 模式：输出无 BV 时回退目标号
CMD_SEEN.clear()
job2, _ = asyncio.run(_run_one({
    "mode": "part", "target_vid": "BV1BBBBBBBB2",
    "part_title": "无输出BV"}, c_id, _fake_exec_nobv))
check("part 模式：无 BV 输出回退目标号",
      job2["status"] == "success" and job2["bv"] == "BV1BBBBBBBB2")

# 4.3 new 模式 + 合集成功路径：resolve_aid → parts → episodes/add
_calls: list[tuple] = []
bili_api.resolve_aid = lambda bvid: _calls.append(("aid", bvid)) or 333
bili_api.fetch_archive_parts = lambda aid: _calls.append(("parts", aid)) \
    or [{"cid": 9009, "index": 1, "title": "青鸟第一幕"}]
bili_api.season_add_video = (lambda aid, cid, title, sec:
                             _calls.append(("add", aid, cid, title, sec)))
CMD_SEEN.clear()
job3, cmd3 = asyncio.run(_run_one({
    "mode": "new", "title": "青鸟第一幕", "tid": 3,
    "source_type": "original", "season_id": 777, "section_id": 555}, c_id))
check("new+合集：投稿成功 + 合集三步调用链",
      job3["status"] == "success" and job3["season_note"] == ""
      and _calls == [("aid", "BV1Xvn366Ef5"), ("parts", 333),
                     ("add", 333, 9009, "青鸟第一幕", 555)])
check("new+合集：upload 命令不受合集参数影响（无 season 泄漏）",
      not any("season" in str(x) for x in cmd3) and "upload" in cmd3)

# 4.4 new 模式 + 合集失败：fail-soft（success + season_note）
def _boom(*a, **kw):
    raise bili_api.BiliApiError("B 站接口拒绝：合集不存在", False)


bili_api.resolve_aid = _boom
CMD_SEEN.clear()
job4, _ = asyncio.run(_run_one({
    "mode": "new", "title": "青鸟第一幕", "season_id": 777,
    "section_id": 555}, c_id))
check("new+合集失败：投稿仍成功 + season_note 记录原因",
      job4["status"] == "success" and "合集" in job4["season_note"]
      and "B 站接口拒绝" in job4["season_note"])

# 4.6 串行队列（v2.3.0）：同账号并发任务只允许一个 biliup 子进程在跑
_active = [0]
_peak = [0]


async def _serial_exec(*cmd, **kw):
    _active[0] += 1
    _peak[0] = max(_peak[0], _active[0])
    await asyncio.sleep(0.4)
    _active[0] -= 1
    return _FakeProc(0, out=b"ok BV1Xvn366Ef5\n")


async def _mk_serial_job(part_title: str):
    row = app_mod._load_clip(101, c_id)
    job = upload_mod.create_job(101, c_id, part_title, "part")
    form = {"mode": "part", "target_vid": "BV1BBBBBBBB2",
            "part_title": part_title}

    async def _sign(key, bucket=""):
        raise AssertionError("本用例不应触发 OSS 回拉")

    await upload_mod.run_job(job, dict(row), form,
                             app_mod.RECORDINGS_ROOT, _sign)
    return job


asyncio.create_subprocess_exec = _serial_exec
_coro_a = _mk_serial_job("串行A")
_coro_b = _mk_serial_job("串行B")


async def _run_both():
    return await asyncio.gather(_coro_a, _coro_b)


_ja, _jb = asyncio.run(_run_both())
asyncio.create_subprocess_exec = _orig_exec
check("串行队列：并发双任务 biliup 子进程峰值 = 1（3Mbps 上行并发互相饿死）",
      _peak[0] == 1 and _ja["status"] == "success"
      and _jb["status"] == "success")

# 4.7 投稿结果持久化：clip_submissions 落库 + auto_recordings 回传
conn = connect()
_row = conn.execute("SELECT bv, mode FROM clip_submissions "
                    "WHERE clip_id = ?", (c_id,)).fetchone()
conn.close()
check("投稿成功落库 clip_submissions（part → 解析到的 BV）",
      _row is not None and _row["bv"] == "BV1Xvn366Ef5"
      and _row["mode"] == "part")
r = CLIPPER_101.get("/api/rooms/101/auto-recordings")
_found = None
for _g in r.json().get("groups", []):
    for _gg in _g.get("groups", []):
        for _c in _gg.get("clips", []):
            if _c["id"] == c_id:
                _found = _c.get("submission")
check("auto_recordings 回传 submission（刷新后「已投稿」徽标持久显示）",
      r.status_code == 200 and _found is not None
      and _found["bv"] == "BV1Xvn366Ef5" and _found["mode"] == "part")

# 4.5 submit_status 透出 mode / season_note（端点层）
r = cp.post(f"/api/rooms/101/clips/{c_id}/submit",
            json={"mode": "part", "target_vid": "111",
                  "part_title": "端点建任务"}, headers=CLIPPER_101)
check("part 模式：端点建任务返回任务号",
      r.status_code == 200 and bool(r.json().get("job")))
jid = r.json().get("job", "")
r = CLIPPER_101.get(f"/api/rooms/101/clips/{c_id}/submit/{jid}")
d = r.json()
check("submit_status：透出 mode / season_note 字段",
      r.status_code == 200 and d.get("mode") == "part"
      and d.get("season_note") == "")

# ===========================================================================
# 5. 本地投稿器（v2.3.0）：pending 队列 / 认领 / 回报 / 服务器兜底
# ===========================================================================
# 认领窗口压短到 0.3s：测试即时验证 pending 态与兜底路径
upload_mod.WAIT_WORKER_SEC = 0.3

# 5.1 队列与认领回报（成功路径，认领先于兜底触发）
r = cp.post(f"/api/rooms/101/clips/{c_id}/submit",
            json={"mode": "part", "target_vid": "111",
                  "part_title": "投稿器用例"}, headers=CLIPPER_101)
jid_w = r.json().get("job")
check("part 提交 → 入 pending 队列", r.status_code == 200 and bool(jid_w))
r = CLIPPER_101.get(f"/api/rooms/101/clips/{c_id}/submit/{jid_w}")
check("任务初始状态 pending（等本地投稿器认领）",
      r.status_code == 200 and r.json()["status"] == "pending")
r = CLIPPER_101.get("/api/submit/queue")
qs = r.json().get("jobs", [])
check("queue 列出 pending 任务（含 part_title/target_vid）",
      r.status_code == 200
      and any(x["job"] == jid_w and x["part_title"] == "投稿器用例"
              and x["target_vid"] == "111" for x in qs))
r = GUEST_101.get("/api/submit/queue")
check("queue：guest 403（middleware）", r.status_code == 403)
r = CLIPPER_101.post(f"/api/submit/queue/{jid_w}/claim")
d = r.json()
check("claim 成功返回载荷（part_title/target_vid/oss_url 字段）",
      r.status_code == 200 and d["part_title"] == "投稿器用例"
      and d["target_vid"] == "111" and "oss_url" in d)
r = CLIPPER_101.post(f"/api/submit/queue/{jid_w}/claim")
check("重复 claim → 409", r.status_code == 409)
r = CLIPPER_101.post(f"/api/submit/queue/{jid_w}/result",
                     json={"ok": True, "bv": "BV1WORKER000"})
check("result 回报成功 → 200", r.status_code == 200)
r = CLIPPER_101.get(f"/api/rooms/101/clips/{c_id}/submit/{jid_w}")
d = r.json()
check("回报后任务 success + BV（前端轮询可见）",
      d["status"] == "success" and d["bv"] == "BV1WORKER000")
r = CLIPPER_101.post(f"/api/submit/queue/{jid_w}/result",
                     json={"ok": True, "bv": "BV1WORKER000"})
check("重复 result → 409", r.status_code == 409)
# 认领后被兜底跳过（0.3s 早已过，任务仍 success 未被改写）
time.sleep(0.5)
r = CLIPPER_101.get(f"/api/rooms/101/clips/{c_id}/submit/{jid_w}")
check("兜底不覆盖已认领任务", r.json()["status"] == "success")

# 5.2 兜底：无人认领窗口超时 → 服务器自己跑（直驱 server_fallback——
#    TestClient 不推进请求后 create_task 的后台任务，生产 uvicorn 下由
#    端点创建的兜底任务正常执行，此处确定性覆盖兜底逻辑本身）
job_f = upload_mod.create_job(101, c_id, "兜底用例", "part", pending=True)
job_f["target_vid"] = "111"
_form_f = {"mode": "part", "target_vid": "111", "part_title": "兜底用例"}
_row_f = app_mod._load_clip(101, c_id)


async def _sign_f(key, bucket=""):
    raise AssertionError("本用例不应触发 OSS 回拉")


asyncio.create_subprocess_exec = _fake_exec
asyncio.run(upload_mod.server_fallback(
    job_f, _form_f, dict(_row_f), app_mod.RECORDINGS_ROOT, _sign_f, 0.05))
asyncio.create_subprocess_exec = _orig_exec
check("无人认领 → 服务器兜底执行成功（3Mbps 慢路径）",
      job_f["status"] == "success" and job_f["bv"] == "BV1Xvn366Ef5")
job_c = upload_mod.create_job(101, c_id, "已认领", "part", pending=True)
job_c["target_vid"] = "111"
job_c["status"] = "uploading"
job_c["claimed_by"] = "worker"
asyncio.run(upload_mod.server_fallback(
    job_c, _form_f, dict(_row_f), app_mod.RECORDINGS_ROOT, _sign_f, 0.05))
check("兜底不覆盖已被本地投稿器认领的任务",
      job_c["status"] == "uploading" and job_c["claimed_by"] == "worker")
upload_mod.WAIT_WORKER_SEC = 25.0

# ---- 还原 monkeypatch，收尾 ----
bili_api.fetch_account_info = _orig_acc
bili_api.fetch_seasons = _orig_seas
bili_api.fetch_arcs = _orig_arcs
bili_api.fetch_archive_parts = _orig_parts
bili_api.drop_cache()
app_mod.cfg["bili"]["biliup_bin"] = ""

# ===========================================================================
# 6. 投稿记录长周期同步（v2.3.0）：存在性核验 + 同名分P回填
# ===========================================================================
from replay.db import connect as _connect


def _seed_sub(clip_id, bv, mode, title, rec_id=1, clip_title=""):
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO manual_clips (id, recording_id, room_id, "
        "title, start_sec, end_sec, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?, 'done', 't', '2026-10-19T10:00:00')",
        (clip_id, rec_id, 101, clip_title or title, 0, 60))
    conn.execute(
        "INSERT OR REPLACE INTO clip_submissions (clip_id, bv, mode, title, "
        "submitted_at) VALUES (?,?,?,?, '2026-10-19T10:00:00')",
        (clip_id, bv, mode, title))
    conn.commit()
    conn.close()


# 6.1 存在性核验：view -404 / 分P 消失 → alive=0；正常 → alive=1
_seed_sub(901, "BV1DEADBEEF1", "new", "被删视频")
_seed_sub(902, "BV1ALIVEBEEF2", "new", "健在视频")
_seed_sub(903, "BV1PARTBEEEF3", "part", "分P被删的歌")
_view_route = {
    "BV1DEADBEEF1": {"code": -404},
    "BV1ALIVEBEEF2": {"code": 0, "data": {}},
    "BV1PARTBEEEF3": {"code": 0, "data": {}},       # 稿件在，分P不在
}


class _ViewResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _view_urlopen(req, timeout=None):
    url = req.full_url
    for bv, payload in _view_route.items():
        if bv in url:
            return _ViewResp(payload)
    return _ViewResp({"code": 0, "data": {}})


_orig_view_open = bili_api.urllib.request.urlopen
bili_api.urllib.request.urlopen = _view_urlopen
bili_api.resolve_aid = lambda bvid: 777777
bili_api.fetch_archive_parts = lambda aid: [{"cid": 1, "index": 1,
                                             "title": "别的分P"}]
st = submit_sync.sync_once()
conn = _connect()
_rows = {r["clip_id"]: r for r in conn.execute(
    "SELECT clip_id, alive, checked_at FROM clip_submissions")}
conn.close()
check("核验：视频被删（view -404）→ alive=0",
      _rows[901]["alive"] == 0 and _rows[901]["checked_at"] != "")
check("核验：健在视频 → alive=1",
      _rows[902]["alive"] == 1)
check("核验：稿件在但同名分P消失 → alive=0",
      _rows[903]["alive"] == 0)
check("核验统计透出 verified/dead（含前序用例遗留记录）",
      st["verified"] >= 3 and st["dead"] >= 2)

# 6.2 同名分P回填：同录播已有 part 记录 → 目标稿件分P同名匹配补录
#（904 无记录切片，与 905 同录播 rec_id=2；905 已投稿到 BV1FILLBEEF4）
_seed_sub(905, "BV1FILLBEEF4", "part", "歌切《历史》", rec_id=2)
conn = _connect()
conn.execute(
    "INSERT INTO manual_clips (id, recording_id, room_id, title, start_sec, "
    "end_sec, status, created_by, created_at) VALUES (904, 2, 101, "
    "'歌切《无记录》', 0, 60, 'done', 't', '2026-10-19T10:00:00')")
conn.commit()
conn.close()
_view_route["BV1FILLBEEF4"] = {"code": 0, "data": {}}
bili_api.fetch_archive_parts = lambda aid: [
    {"cid": 1, "index": 1, "title": "歌切《历史》"},
    {"cid": 2, "index": 2, "title": "歌切《无记录》"}]
st2 = submit_sync.sync_once()
conn = _connect()
_bf = conn.execute("SELECT bv, mode, season_note, alive FROM "
                   "clip_submissions WHERE clip_id = 904").fetchone()
conn.close()
check("回填：同录播稿件同名分P → 补录 clip_submissions",
      _bf is not None and _bf["bv"] == "BV1FILLBEEF4"
      and _bf["mode"] == "part" and _bf["alive"] == 1
      and "回填" in _bf["season_note"])
check("回填统计透出 backfilled", st2["backfilled"] >= 1)
bili_api.urllib.request.urlopen = _orig_view_open

# 6.3 手动同步端点（admin 专属）
r = CLIPPER_101.post("/api/admin/submit/sync")
check("手动同步端点：clipper 403（/api/admin 前缀门）", r.status_code == 403)
app_mod.cfg["bili"]["biliup_bin"] = "fake-biliup"
_orig_parts2 = bili_api.fetch_archive_parts
bili_api.fetch_archive_parts = lambda aid: []
bili_api.urllib.request.urlopen = _view_urlopen
r = cp.post("/api/admin/submit/sync")
check("手动同步端点：admin 200 + 统计字段",
      r.status_code == 200 and "verified" in r.json()
      and "backfilled" in r.json())
bili_api.fetch_archive_parts = _orig_parts2
bili_api.urllib.request.urlopen = _orig_view_open
app_mod.cfg["bili"]["biliup_bin"] = ""

import sys as _sys
_sys.exit(H.summary("v2.3.0 投稿合集/分P + 账号管理"))
