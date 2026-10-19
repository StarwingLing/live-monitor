# -*- coding: utf-8 -*-
"""v2.4.0 改动点的进程内测试：扫码登录 + renew 指纹合并。

背景：
- 投稿账号管理页新增「扫码登录」（web 二维码 generate/poll；密码登录因
  极验人机验证不可自动化、B 站网页端亦已停用而不实现，调研见 bili_api
  v2.4.0 注释）；
- 修复 biliup renew 剥指纹缺陷：renew 重写 cookie_info 只保留会话键，
  buvid3/buvid4/bili_ticket 等设备指纹全丢 → web 接口 nav -101 / 创作中心
  61000（2026-10-19 服务器实况）。合并口径：指纹取旧、会话键与 token 取新。

覆盖：
1. build_submit_cookies_doc：biliup serde 全字段、token_info 空壳 + mid、
   refresh_token 透传；
2. merge_renewed_cookies：指纹保留 + 会话键替换 + token 取新 + 同名不重复
   + 老文件缺 cookie_info 防御；
3. qr_login_poll 状态映射（HTTP 层 mock）：86101/86090/86038/0 成功带
   Set-Cookie 与 refresh_token / 顶层 code!=0 抛异常；
4. qr_login_start（mock）：成功返回 (key, url)；失败抛异常；
5. qrcode SVG 生成（纯 qrcode 库，免 PIL）；
6. 端点矩阵（TestClient）：start 200 含 svg data URI、guest 403；poll
   未知 key 404 / waiting 透传 / confirmed 落盘合并指纹并回账号信息。

运行：uv run python tests/test_v2_4.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers as H
from helpers import check, TMP

from replay import auth
import replay.web.app as app_mod
import replay.web.bili_api as bili_api
import replay.web.upload as upload_mod
from replay.web.app import app

app_mod.cfg["capture"]["admin_password"] = "test-admin-pw"

results: list[bool] = []


def checkx(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}"
          + (f"  {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# 0. 会话构造（与 test_v2_3 同款）
# ---------------------------------------------------------------------------
def fresh(cookies: dict | None = None):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    return c


_ADMIN_C = fresh({"replay_auth": auth.sign_session("admin", "admin1")})

# ---------------------------------------------------------------------------
# 1. build_submit_cookies_doc
# ---------------------------------------------------------------------------
def test_build_doc():
    doc = bili_api.build_submit_cookies_doc(
        {"SESSDATA": "s%2C123", "bili_jct": "jct", "DedeUserID": "374756",
         "DedeUserID__ckMd5": "md5", "sid": "sid1", "buvid3": "fp"},
        refresh_token="rt-1")
    jar = doc["cookie_info"]["cookies"]
    names = {c["name"] for c in jar}
    checkx("doc 会话+指纹键齐全",
           {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid",
            "buvid3"} <= names, f"实际 {names}")
    checkx("每项 serde 全字段", all(set(c) >= {"name", "value", "http_only",
                                            "secure", "expires"} for c in jar))
    checkx("token_info 空壳 + mid", doc["token_info"]["access_token"] == ""
           and doc["token_info"]["mid"] == 374756
           and doc["token_info"]["refresh_token"] == "rt-1",
           f"实际 {doc['token_info']}")
    checkx("domains/sso 齐备", ".bilibili.com" in doc["cookie_info"]["domains"]
           and bool(doc["sso"]))


# ---------------------------------------------------------------------------
# 2. merge_renewed_cookies
# ---------------------------------------------------------------------------
def test_merge_renewed():
    old = {"cookie_info": {"cookies": [
        {"name": "buvid3", "value": "fp3", "http_only": 0, "secure": 0,
         "expires": 0},
        {"name": "bili_ticket", "value": "tk-old", "http_only": 0, "secure": 0,
         "expires": 0},
        {"name": "SESSDATA", "value": "old-sess", "http_only": 1, "secure": 0,
         "expires": 0}]},
        "token_info": {"access_token": "old-at", "expires_in": 0, "mid": 1,
                       "refresh_token": "old-rt"}}
    new = {"cookie_info": {"cookies": [
        {"name": "SESSDATA", "value": "new-sess", "http_only": 1, "secure": 0,
         "expires": 0},
        {"name": "bili_jct", "value": "new-jct", "http_only": 0, "secure": 0,
         "expires": 0},
        {"name": "DedeUserID", "value": "9", "http_only": 1, "secure": 0,
         "expires": 0}]},
        "token_info": {"access_token": "new-at", "expires_in": 0, "mid": 9,
                       "refresh_token": "new-rt"},
        "sso": ["sso-new"], "platform": None}
    m = bili_api.merge_renewed_cookies(old, new)
    kv = {c["name"]: c["value"] for c in m["cookie_info"]["cookies"]}
    checkx("指纹保留（buvid3/bili_ticket）", kv.get("buvid3") == "fp3"
           and kv.get("bili_ticket") == "tk-old", f"实际 {kv}")
    checkx("会话键取新", kv.get("SESSDATA") == "new-sess"
           and kv.get("bili_jct") == "new-jct", f"实际 {kv}")
    checkx("token_info 取新", m["token_info"]["access_token"] == "new-at")
    checkx("sso 取新", m["sso"] == ["sso-new"])
    # 同名冲突不重复 + 老文件缺 cookie_info 防御
    new2 = {"cookie_info": {"cookies": [
        {"name": "buvid3", "value": "fp-new", "http_only": 0, "secure": 0,
         "expires": 0},
        {"name": "SESSDATA", "value": "s2", "http_only": 1, "secure": 0,
         "expires": 0}]},
        "token_info": {}}
    m2 = bili_api.merge_renewed_cookies(old, new2)
    kv2 = {c["name"]: c["value"] for c in m2["cookie_info"]["cookies"]}
    checkx("同名指纹不重复", sum(1 for c in m2["cookie_info"]["cookies"]
                             if c["name"] == "buvid3") == 1
           and kv2["buvid3"] == "fp-new", f"实际 {kv2}")
    m3 = bili_api.merge_renewed_cookies({"no_cookie_info": True}, new)
    checkx("老文件缺 cookie_info 防御",
           {c["name"] for c in m3["cookie_info"]["cookies"]} ==
           {"SESSDATA", "bili_jct", "DedeUserID"})


# ---------------------------------------------------------------------------
# 3. qr_login_poll 状态映射（mock 网络层）
# ---------------------------------------------------------------------------
def test_qr_poll_states():
    def fake_get(payload, set_cookies=None):
        def _g(url):
            return payload, set_cookies or {}
        return _g

    orig = bili_api._qr_get
    try:
        bili_api._qr_get = fake_get({"code": 0, "data": {"code": 86101}})
        checkx("86101 → waiting", bili_api.qr_login_poll("k")["state"] == "waiting")
        bili_api._qr_get = fake_get({"code": 0, "data": {"code": 86090}})
        checkx("86090 → scanned", bili_api.qr_login_poll("k")["state"] == "scanned")
        bili_api._qr_get = fake_get({"code": 0, "data": {"code": 86038}})
        checkx("86038 → expired", bili_api.qr_login_poll("k")["state"] == "expired")
        bili_api._qr_get = fake_get(
            {"code": 0, "data": {"code": 0, "refresh_token": "rt"}},
            {"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "8",
             "DedeUserID__ckMd5": "m", "sid": "i"})
        r = bili_api.qr_login_poll("k")
        checkx("0 → confirmed + cookies + refresh_token",
               r["state"] == "confirmed" and r["cookies"]["SESSDATA"] == "s"
               and r["refresh_token"] == "rt", f"实际 {r}")
        bili_api._qr_get = fake_get({"code": -101, "message": "x"})
        try:
            bili_api.qr_login_poll("k")
            checkx("顶层 code!=0 → 抛异常", False)
        except bili_api.BiliApiError:
            checkx("顶层 code!=0 → 抛异常", True)
    finally:
        bili_api._qr_get = orig


def test_qr_start():
    orig = bili_api._qr_get
    try:
        bili_api._qr_get = lambda url: ({"code": 0, "data": {
            "qrcode_key": "key123", "url": "https://passport.bilibili.com/scan"}},
            {})
        key, url = bili_api.qr_login_start()
        checkx("start 返回 (key, url)", key == "key123" and "scan" in url,
               f"实际 {(key, url)}")
        bili_api._qr_get = lambda url: ({"code": -400, "message": "bad"}, {})
        try:
            bili_api.qr_login_start()
            checkx("start 失败 → 抛异常", False)
        except bili_api.BiliApiError:
            checkx("start 失败 → 抛异常", True)
    finally:
        bili_api._qr_get = orig


# ---------------------------------------------------------------------------
# 4. qrcode SVG 生成（纯库，免 PIL）
# ---------------------------------------------------------------------------
def test_qr_svg():
    import io
    import qrcode
    import qrcode.image.svg
    img = qrcode.make("https://passport.bilibili.com/scan?x=1",
                      image_factory=qrcode.image.svg.SvgImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    checkx("SVG 生成含 svg 标记", b"<svg" in buf.getvalue())


# ---------------------------------------------------------------------------
# 5. 端点矩阵（TestClient + monkeypatch B 站侧）
# ---------------------------------------------------------------------------
CK = TMP / "biliup-qr-v24.json"
app_mod.cfg["bili"]["biliup_cookies"] = str(CK)


def test_endpoints():
    orig_start, orig_poll = bili_api.qr_login_start, bili_api.qr_login_poll
    orig_info, orig_drop = bili_api.fetch_account_info, bili_api.drop_cache
    orig_upload_ready = bili_api.has_access_token
    orig_renew, orig_bin = upload_mod.renew_login, upload_mod.biliup_bin
    orig_exchange = bili_api.exchange_web_login
    try:
        # ---- v2.4.0 配置清单字段暴露 ----
        r = _ADMIN_C.get("/api/admin/submit/account")
        d = r.json()
        checkx("account 暴露 bin_path/cookies_path/submit_enabled",
               r.status_code == 200 and "bin_path" in d and "cookies_path" in d
               and "submit_enabled" in d,
               f"实际 {sorted(d.keys())}")
        # guest → 401（admin 专属）
        r = fresh({}).post("/api/admin/submit/qr/start")
        checkx("匿名 start → 401", r.status_code == 401, f"实际 {r.status_code}")

        bili_api.qr_login_start = lambda: ("key9",
                                           "https://passport.bilibili.com/scan")
        r = _ADMIN_C.post("/api/admin/submit/qr/start")
        d = r.json()
        checkx("admin start → 200 含 svg/key",
               r.status_code == 200 and d.get("qrcode_key") == "key9"
               and str(d.get("svg", "")).startswith("data:image/svg+xml;base64,"),
               f"实际 {r.status_code}")

        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=nope")
        checkx("未知 key → 404", r.status_code == 404, f"实际 {r.status_code}")

        bili_api.qr_login_poll = lambda key: {"state": "waiting", "cookies": {},
                                              "refresh_token": ""}
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=key9")
        checkx("waiting 透传", r.status_code == 200
               and r.json()["state"] == "waiting")

        bili_api.qr_login_poll = lambda key: {
            "state": "confirmed",
            "cookies": {"SESSDATA": "s1", "bili_jct": "j1", "DedeUserID": "77",
                        "DedeUserID__ckMd5": "m1", "sid": "i1"},
            "refresh_token": "rt9"}
        bili_api.fetch_account_info = lambda: {"ok": True, "uname": "测试账号",
                                               "mid": 77, "face": ""}
        bili_api.has_access_token = lambda: False
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=key9")
        d = r.json()
        checkx("confirmed → 200 + 账号 + cookie 串",
               r.status_code == 200 and d.get("state") == "confirmed"
               and d.get("account", {}).get("uname") == "测试账号"
               and "SESSDATA=s1" in d.get("cookie_str", ""), f"实际 {d}")
        import json
        saved = json.loads(CK.read_text("utf-8"))
        names = {c["name"] for c in saved["cookie_info"]["cookies"]}
        checkx("落盘含会话键", {"SESSDATA", "bili_jct", "DedeUserID"} <= names,
               f"实际 {names}")
        checkx("落盘 refresh_token 透传",
               saved["token_info"]["refresh_token"] == "rt9")
        # 会话已消费 → 再次 poll 404
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=key9")
        checkx("confirm 后会话销毁 → 404", r.status_code == 404)

        # ---- v2.4.1：确认后自动 renew 补 access_token ----
        app_mod._QR_SESSIONS["keyA"] = time.time()
        calls: list[str] = []
        ready = {"v": False}

        async def _fake_renew():
            calls.append("renew")
            ready["v"] = True
            return "ok"

        orig_renew = upload_mod.renew_login
        orig_bin = upload_mod.biliup_bin
        upload_mod.renew_login = _fake_renew
        upload_mod.biliup_bin = lambda: "fake-bin"
        bili_api.has_access_token = lambda: ready["v"]
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=keyA")
        d = r.json()
        checkx("缺 access_token 时自动 renew 被调用", calls == ["renew"],
               f"实际 {calls}")
        checkx("renew 后 upload_ready 生效", d.get("upload_ready") is True,
               f"实际 {d.get('upload_ready')}")
        upload_mod.renew_login, upload_mod.biliup_bin = orig_renew, orig_bin

        # renew 失败：登录结果不被推翻，renew_note 透传
        app_mod._QR_SESSIONS["keyB"] = time.time()
        ready["v"] = False

        async def _fake_renew_fail():
            raise RuntimeError("网络失败")

        upload_mod.renew_login = _fake_renew_fail
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=keyB")
        d = r.json()
        checkx("renew 失败不推翻登录", d.get("state") == "confirmed"
               and d.get("account", {}).get("uname") == "测试账号",
               f"实际 {d.get('state')}")
        checkx("renew 失败透传 renew_note", "网络失败" in d.get("renew_note", ""),
               f"实际 {d.get('renew_note')}")

        # ---- v2.4.0 兜底：renew 换不出 access_token → TV 兑换补齐 ----
        app_mod._QR_SESSIONS["keyC"] = time.time()
        ready["v"] = False
        exchanges: list[str] = []
        orig_exchange = bili_api.exchange_web_login

        async def _fake_renew_no_token():
            return "ok"     # 退出 0 但不产出 access_token（扫码会话实测行为）

        def _fake_exchange(cookie_str: str):
            exchanges.append(cookie_str)
            ready["v"] = True    # 兑换产物含 access_token（模拟落盘后生效）
            return {"cookie_info": {"cookies": [
                {"name": "SESSDATA", "value": "sX", "http_only": 1,
                 "secure": 0, "expires": 0},
                {"name": "bili_jct", "value": "jX", "http_only": 0,
                 "secure": 0, "expires": 0}]},
                "token_info": {"access_token": "AT-9", "expires_in": 1,
                               "mid": 77, "refresh_token": "rtX"},
                "sso": [], "platform": None}

        upload_mod.renew_login = _fake_renew_no_token
        bili_api.exchange_web_login = _fake_exchange
        r = _ADMIN_C.get("/api/admin/submit/qr/poll?qrcode_key=keyC")
        d = r.json()
        checkx("renew 无 token 时触发 TV 兑换", len(exchanges) == 1,
               f"实际 {len(exchanges)}")
        checkx("兑换后 upload_ready 生效", d.get("upload_ready") is True,
               f"实际 {d.get('upload_ready')}")
        checkx("兑换结果落盘 access_token",
               json.loads(CK.read_text("utf-8"))["token_info"]["access_token"]
               == "AT-9")
        bili_api.exchange_web_login = orig_exchange
    finally:
        bili_api.qr_login_start, bili_api.qr_login_poll = orig_start, orig_poll
        bili_api.fetch_account_info, bili_api.drop_cache = orig_info, orig_drop
        bili_api.has_access_token = orig_upload_ready
        bili_api.exchange_web_login = orig_exchange
        upload_mod.renew_login = orig_renew
        upload_mod.biliup_bin = orig_bin
        app_mod._QR_SESSIONS.clear()


def main() -> int:
    test_build_doc()
    test_merge_renewed()
    test_qr_poll_states()
    test_qr_start()
    test_qr_svg()
    test_endpoints()
    total, passed = len(results), sum(results)
    print(f"\n==== test_v2_4: {passed}/{total} 通过 ====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
