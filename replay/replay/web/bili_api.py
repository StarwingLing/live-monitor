# -*- coding: utf-8 -*-
"""B 站创作中心 web API 客户端（replay v2.3.0：投稿合集 / 分P 目标数据源）。

为什么需要它：biliup-rs 覆盖「投新稿件」与「向已有稿件追加分P」两条上传
链路，但**没有任何合集（season）参数**——「检测账号上的合集、把新视频加
入合集」必须直接调 B 站创作中心 web API（member.bilibili.com，SESSDATA
Cookie 认证、写操作再带 bili_jct 作 csrf）。凭据与 upload.py 共用同一份
biliup cookies.json（`biliup login` 生成）——投稿走 APP 接口需要
token_info.access_token，纯网页 cookie 拼不出有效文件，所以凭据只能整份
由管理页「账号管理」粘贴/上传（v2.3.0 新增）。

接口与字段（bilibili-API-collect 镜像仓 docs/creativecenter/season.md、
videos.md，2026-10-19 核对；B 站侧结构可能变动，解析一律防御式——字段
缺失给空值/跳过而不是抛错，页面据此显示异常态）：
- 账号信息   GET api.bilibili.com/x/web-interface/nav（兼作凭据有效性检查）
- 合集列表   GET member…/x2/creative/web/seasons?pn&ps
             → data.seasons[].season{id,title} / .sections.sections[]（小节）
               / .part_episodes[]（合集内视频）
- 稿件列表   GET member…/x2/creative/web/archives/sp?pn&ps
             → data.arc_audits[].Archive{aid,bvid,title}
- 稿件分P    GET api…/x/web/archive/videos?aid=
             → data.videos[]{cid,index,title}（新投稿取 cid 也走它）
- 合集加视频 POST member…/x2/creative/web/season/section/episodes/add?csrf=
             JSON {section_id, episodes:[{aid,cid,title}]}

风控与负载口径：统一桌面 UA + Referer、8s 超时、错误规范化（-101 等登录
态错误给「投稿凭据可能失效」文案）；seasons/arcs 是抽屉打开即拉的数据，
加 60s 进程内 TTL 缓存，防多端同时开投稿抽屉打爆创作中心接口（凭据变更
时由 drop_cache() 显式清空）。

同步/异步边界：本模块全部同步（urllib），端点侧用 asyncio.to_thread 调
入（与 upload.oss_pull 同款做法），不在事件循环里做阻塞 IO。
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import ROOT_DIR, BILI

_MEMBER = "https://member.bilibili.com"
_API = "https://api.bilibili.com"
_TIMEOUT_SEC = 8
_CACHE_TTL_SEC = 60
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 分P名（=上传文件名）安全字符集：biliup append 的分P标题取自文件名
# stem，文件系统非法字符统一替换为下划线（Windows 保留名等极端场景由
# 前缀无关性兜底——分P名不含路径分隔符即安全）
_UNSAFE_FS = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


class BiliApiError(RuntimeError):
    """创作中心接口失败（msg 面向用户展示；auth_failed 标记凭据问题）。"""

    def __init__(self, msg: str, auth_failed: bool = False):
        super().__init__(msg)
        self.auth_failed = auth_failed


def cookies_path() -> Path:
    """biliup cookies.json 路径（与 upload.cookies_path 同一配置口径）。"""
    p = str(BILI.get("biliup_cookies", "") or "").strip()
    return ROOT_DIR / (p or "data/biliup-cookies.json")


def load_cookies() -> dict[str, str]:
    """解析 cookies.json → {name: value} 平铺（缺失/结构非法抛可读异常）。

    每次调用重读文件——管理页刚保存的新凭据立即生效，不做 mtime 缓存。
    """
    p = cookies_path()
    if not p.exists():
        raise BiliApiError("投稿 cookies.json 未配置（管理页·账号管理上传）",
                           auth_failed=True)
    try:
        data = json.loads(p.read_text("utf-8"))
        jar = data.get("cookie_info", {}).get("cookies", []) or []
        kv = {str(c.get("name", "")): str(c.get("value", ""))
              for c in jar if isinstance(c, dict) and c.get("name")}
    except (json.JSONDecodeError, AttributeError, OSError) as e:
        raise BiliApiError(f"cookies.json 解析失败：{e}", auth_failed=True)
    if "SESSDATA" not in kv:
        raise BiliApiError(
            "cookies.json 缺少 SESSDATA（须粘贴 biliup login 生成的完整文件）",
            auth_failed=True)
    return kv


def _cookie_header() -> str:
    kv = load_cookies()
    return "; ".join(f"{k}={v}" for k, v in kv.items())


def parse_credential_text(raw: str) -> dict:
    """鉴别并规范化投稿凭据文本（管理页·账号管理粘贴入口）。

    接受两种格式，返回 ``{"kind", "doc", "upload_ready"}``；不合法抛
    BiliApiError（文案面向管理页展示）：
    - **完整 biliup cookies.json**（`biliup login` 生成，`{` 开头）：原样
      规范化保留；带 token_info.access_token → upload_ready=True（新视频
      投稿的 APP 接口必需）；
    - **浏览器 Cookie 串**（`k=v; k2=v2` 形态）：解析出 SESSDATA/bili_jct
      等包成 LoginInfo 外壳——创作中心 web 接口（合集检测/加入、稿件列表）
      可用；**新视频投稿不可用**（APP 接口需 access_token）。

    注意：token_info 统一补成**结构完整**的形态（缺字段补空壳）——biliup
    的 Rust serde 要求 TokenInfo 全字段存在，`{}` 会在读文件时直接报
    "missing field `access_token`"；空壳（access_token=""）则能正常加载，
    仅 APP 接口返回 -400（即 upload_ready=False 的语义来源）。
    """
    s = (raw or "").strip()
    if not s:
        raise BiliApiError("内容为空")
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise BiliApiError(f"不是合法 JSON：{e}")
        if not isinstance(data, dict):
            raise BiliApiError("JSON 顶层须为对象")
        jar = (data.get("cookie_info") or {}).get("cookies")
        if not isinstance(jar, list) or not jar:
            raise BiliApiError("缺少 cookie_info.cookies 数组（请粘贴 biliup "
                               "login 生成的完整 cookies.json）")
        names = {str(c.get("name", "")) for c in jar if isinstance(c, dict)}
        if "SESSDATA" not in names:
            raise BiliApiError("cookies 缺少 SESSDATA 字段")
        token = data.get("token_info")
        ready = isinstance(token, dict) and bool(token.get("access_token"))
        data.setdefault("sso", [])
        _ensure_token_stub(data, names)
        return {"kind": "biliup", "doc": data, "upload_ready": ready}
    pairs: list[dict] = []
    for part in s.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            pairs.append({"name": k, "value": v})
    names = {p["name"] for p in pairs}
    if "SESSDATA" not in names or "bili_jct" not in names:
        raise BiliApiError("Cookie 串缺少 SESSDATA / bili_jct（需浏览器登录"
                           "后的完整 Cookie 串）")
    doc = {"cookie_info": {"cookies": pairs}, "sso": []}
    _ensure_token_stub(doc, names)
    return {"kind": "web", "doc": doc, "upload_ready": False}


def _ensure_token_stub(doc: dict, names: set[str]) -> None:
    """token_info 缺失/残缺时补 biliup serde 可解析的空壳（全字段齐全）。"""
    jar = (doc.get("cookie_info") or {}).get("cookies") or []
    mid = 0
    for c in jar:
        if isinstance(c, dict) and c.get("name") == "DedeUserID":
            try:
                mid = int(c.get("value", 0) or 0)
            except (TypeError, ValueError):
                mid = 0
            break
    stub = {"access_token": "", "expires_in": 0, "mid": mid, "refresh_token": ""}
    t = doc.get("token_info")
    if not isinstance(t, dict):
        doc["token_info"] = dict(stub)
    else:
        for k, v in stub.items():
            t.setdefault(k, v)


def has_access_token() -> bool:
    """当前凭据是否带 token_info.access_token（新视频投稿 APP 接口必需）。"""
    try:
        p = cookies_path()
        if not p.exists():
            return False
        data = json.loads(p.read_text("utf-8"))
        t = data.get("token_info")
        return isinstance(t, dict) and bool(t.get("access_token"))
    except (json.JSONDecodeError, OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# 网页 Cookie → 完整登录兑换（biliup「网页Cookie登录1」等价实现，v2.3.0）
# ---------------------------------------------------------------------------
TV_APPKEY = "4409e2ce8ffd12b8"
TV_APPSEC = "59b43e04ad6965f34319062b478f83dd"
_TV_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:38.0) Gecko/20100101 "
          "Firefox/38.0 Iceweasel/38.2.1 BiliApp")


def _tv_sign(form: dict) -> str:
    """md5(urlencoded(form) + appsec)——biliup sign() 同口径（键序=插入序）。"""
    q = urllib.parse.urlencode([(k, form[k]) for k in form])
    return hashlib.md5((q + TV_APPSEC).encode()).hexdigest()


def exchange_web_login(raw_cookie: str) -> dict:
    """网页 Cookie 串 → 完整 LoginInfo（含 token_info.access_token）。

    biliup-rs「网页Cookie登录1」的等价实现（credential.rs
    login_by_web_cookies，2026-10-19 源码核对 + 真实账号实测成功）：
    1. TV 登录接口取 auth_code（BiliTV appkey + md5 签名）；
    2. 网页会话（SESSDATA+bili_jct）h5/qrcode/confirm 自动确认（scanning_type=3，
       无需手机扫码）；-101 时重试一次（实测偶发瞬时态，重试即过）；
    3. 轮询 poll（86039=待确认）取完整 LoginInfo。
    成功返回 LoginInfo dict（调用方落盘）；失败抛 BiliApiError。
    """
    kv = {}
    for part in (raw_cookie or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            kv[k] = v
    sess, jct = kv.get("SESSDATA", ""), kv.get("bili_jct", "")
    if not sess or not jct:
        raise BiliApiError("Cookie 串缺少 SESSDATA / bili_jct，无法兑换")

    def _tv_post(url: str, form: dict) -> dict:
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"User-Agent": _TV_UA})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as e:
            raise BiliApiError(f"登录兑换网络失败：{e}")

    ts = int(time.time())
    form = {"appkey": TV_APPKEY, "local_id": "0", "ts": str(ts)}
    form["sign"] = _tv_sign(form)
    r = _tv_post("https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code",
                 form)
    auth_code = (r.get("data") or {}).get("auth_code", "")
    if r.get("code") != 0 or not auth_code:
        raise BiliApiError(f"登录兑换取 auth_code 失败：{r.get('message') or r.get('code')}")

    # 网页会话自动确认；-101（瞬时未登录态）重试一次
    confirm = None
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/confirm",
            data=urllib.parse.urlencode(
                {"auth_code": auth_code, "csrf": jct, "scanning_type": "3"}).encode(),
            headers={"User-Agent": _TV_UA,
                     "Cookie": f"SESSDATA={sess}; bili_jct={jct}"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                confirm = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as e:
            raise BiliApiError(f"登录确认网络失败：{e}")
        if confirm.get("code") == 0:
            break
        if attempt == 1:
            time.sleep(2)
    if not confirm or confirm.get("code") != 0:
        raise BiliApiError(
            f"登录确认被拒：{confirm and confirm.get('message')}")

    poll_form = {"appkey": TV_APPKEY, "auth_code": auth_code,
                 "local_id": "0", "ts": str(ts)}
    poll_form["sign"] = _tv_sign(poll_form)
    for _ in range(60):
        time.sleep(1)
        r = _tv_post("https://passport.bilibili.com/x/passport-tv-login/qrcode/poll",
                     poll_form)
        code = r.get("code")
        if code == 0:
            info = r.get("data") or {}
            if isinstance(info, dict) and info.get("cookie_info"):
                return info
            raise BiliApiError("兑换轮询返回非登录数据")
        if code == 86039:
            continue
        if code == 86090:
            raise BiliApiError("兑换二维码已过期（86090），请重试")
        raise BiliApiError(f"兑换轮询异常：{r.get('message') or code}")
    raise BiliApiError("兑换轮询超时（60s）")


def _http_json(url: str, *, data: bytes | None = None, csrf: str = "") -> dict:
    """请求一个 B 站 JSON 接口 → data 字典（统一头/超时/错误规范化）。

    data 非空即 POST（JSON 正文）；创作中心写接口的 csrf 走 URL 参数。
    code != 0 抛 BiliApiError：登录态类错误（-101/-111、登录/cookie 字样）
    归一成「凭据失效」文案并带 auth_failed 标记。
    """
    headers = {"User-Agent": _UA, "Referer": _MEMBER,
               "Cookie": _cookie_header()}
    if csrf:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}csrf={urllib.parse.quote(csrf)}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise BiliApiError(f"B 站接口 HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BiliApiError(f"B 站接口网络失败：{e}")
    except json.JSONDecodeError:
        raise BiliApiError("B 站接口返回非 JSON（可能被风控拦截）")
    code = payload.get("code", 0)
    if code != 0:
        msg = str(payload.get("message", "") or f"code={code}")
        if code in (-101, -111) or "登录" in msg or "cookie" in msg.lower():
            raise BiliApiError(
                f"投稿凭据可能失效，请管理员重新配置 cookies.json（{msg}）",
                auth_failed=True)
        raise BiliApiError(f"B 站接口拒绝：{msg}")
    d = payload.get("data")
    return d if isinstance(d, dict) else {}


def sanitize_part_name(name: str) -> str:
    """分P名 → 安全文件名 stem：非法字符替 '_'、去首尾空白/点、限 80 字。

    biliup append 的分P标题 = 上传文件名去扩展名，所以「表单里的分P名」
    与「临时文件名」必须经同一函数清洗，保证两端一致。
    """
    s = _UNSAFE_FS.sub("_", (name or "").strip()).strip(". ")
    return s[:80]


def resolve_aid(bvid: str) -> int:
    """BV 号 → av 号（属主数据源反查，不做本地算法换算）。

    背景：B 站 2023-11 更换 BV 混淆算法且新旧并存、社区流传的字母表常被
    抄错（本项目实测两套公开实现均对不上真实数据对），本地换算风险大于
    收益——改走属主接口：稿件列表（archives/sp，含审核中稿件）按 bvid
    精确匹配取 aid；查不到再退公开 view 接口。调用方在投稿成功后使用，
    此时稿件必然已在账号稿件列表顶部。
    """
    bvid = (bvid or "").strip()
    if not _BV_RE.match(bvid):
        raise BiliApiError(f"BV 号格式不合法：{bvid!r}")
    for arc in fetch_arcs(refresh=True):
        if arc.get("bvid") == bvid:
            return arc["aid"]
    d = _http_json(f"{_API}/x/web-interface/view?bvid={bvid}")
    aid = int(d.get("aid", 0) or 0)
    if aid <= 0:
        raise BiliApiError(f"未能反查 {bvid} 的 av 号（稿件可能仍在处理）")
    return aid


# ---------------------------------------------------------------------------
# 60s TTL 缓存（seasons / arcs；凭据保存/清除/renew 时 drop_cache 清空）
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}


def drop_cache() -> None:
    _cache.clear()


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    return None


# ---------------------------------------------------------------------------
# 只读查询
# ---------------------------------------------------------------------------
def fetch_account_info() -> dict:
    """账号信息（nav）：{ok, uname, mid, face}——ok=False 即凭据已失效。"""
    d = _http_json(f"{_API}/x/web-interface/nav")
    return {"ok": bool(d.get("isLogin")),
            "uname": str(d.get("uname", "")),
            "mid": int(d.get("mid", 0) or 0),
            "face": str(d.get("face", ""))}


def fetch_seasons(refresh: bool = False) -> list[dict]:
    """账号合集列表（新→旧）→ [{season_id,title,ep_total,
    sections:[{id,title,ep_count}]}]。

    合集本身在 B 站创作中心手工创建（需人工审核），本站只检测与选择；
    小节（sections）绝大多数个人合集只有「正片」一节，多节时前端出二级
    下拉。分页兜底上限 5 页防异常 total 死循环。
    """
    if not refresh and _cached("seasons") is not None:
        return _cached("seasons")
    out: list[dict] = []
    pn = 1
    while pn <= 5:
        d = _http_json(f"{_MEMBER}/x2/creative/web/seasons"
                       f"?pn={pn}&ps=16&order=mtime&sort=desc")
        for row in d.get("seasons", []) or []:
            if not isinstance(row, dict):
                continue
            s = row.get("season") or {}
            secs = []
            for sec in ((row.get("sections") or {}).get("sections", [])
                        or []):
                if isinstance(sec, dict) and sec.get("id"):
                    secs.append({"id": int(sec["id"]),
                                 "title": str(sec.get("title", "")),
                                 "ep_count": int(sec.get("epCount", 0) or 0)})
            eps = [e for e in (row.get("part_episodes", []) or [])
                   if isinstance(e, dict) and e.get("aid")]
            ep_total = len(eps) or sum(x["ep_count"] for x in secs)
            if s.get("id"):
                out.append({"season_id": int(s["id"]),
                            "title": str(s.get("title", "")),
                            "ep_total": ep_total, "sections": secs})
        total = int(d.get("total", 0) or 0)
        if pn * 16 >= total:
            break
        pn += 1
    _cache["seasons"] = (time.time(), out)
    return out


def _filter_existing_arcs(arcs: list[dict]) -> list[dict]:
    """剔除公开接口已不存在（-404）的稿件。

    v2.3.0 实测：创作中心稿件列表（archives/sp）会残留已删除的稿件
    （公开 view 接口已 -404「啥都木有」，列表内 state 仍为 0）——这些幽灵
    项不能再作为分P目标出现在下拉里。逐条经公开 view 接口核验：仅明确
    -404 剔除；网络/解析失败放行（fail-open，不误删真实稿件）；条目过多
    （>60）时跳过核验（防打爆接口；60s TTL 缓存摊销常规开销）。
    """
    if not arcs or len(arcs) > 60:
        return arcs
    import concurrent.futures

    def exists(a: dict) -> bool:
        bvid = (a.get("bvid") or "").strip()
        if not bvid:
            return True     # 无 BV 号无从核验，放行
        req = urllib.request.Request(
            f"{_API}/x/web-interface/view?bvid={urllib.parse.quote(bvid)}",
            headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError, json.JSONDecodeError):
            return True     # 网络/解析问题：放行不误删
        return d.get("code") != -404

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        flags = list(ex.map(exists, arcs))
    return [a for a, ok in zip(arcs, flags) if ok]


def fetch_arcs(limit: int = 100, refresh: bool = False) -> list[dict]:
    """账号稿件列表（新→旧）→ [{aid,bvid,title}]（追加分P的目标选择用）。

    archives/sp 每页 ps=30，循环取满 limit 或翻尽即停（上限 5 页防死循环）。
    该接口部分字段为空值（bilibili-API-collect 备注口径），只取
    aid/bvid/title 三个可靠字段；结果经存在性核验（见 _filter_existing_arcs）。
    """
    if not refresh and _cached("arcs") is not None:
        return _cached("arcs")
    out: list[dict] = []
    pn = 1
    while len(out) < limit and pn <= 5:
        d = _http_json(f"{_MEMBER}/x2/creative/web/archives/sp?pn={pn}&ps=30")
        rows = d.get("arc_audits", []) or []
        if not rows:
            break
        for row in rows:
            a = (row.get("Archive") or row.get("archive") or {})
            if isinstance(a, dict) and a.get("aid"):
                out.append({"aid": int(a["aid"]),
                            "bvid": str(a.get("bvid", "")),
                            "title": str(a.get("title", ""))})
        count = int((d.get("page") or {}).get("count", 0) or 0)
        if pn * 30 >= count:
            break
        pn += 1
    out = _filter_existing_arcs(out)
    _cache["arcs"] = (time.time(), out)
    return out


def fetch_archive_parts(aid: int) -> list[dict]:
    """稿件分P列表 → [{cid,index,title}]（选中目标视频后查重 + 新投稿取 cid）。

    端点在 **member 域**（2026-10-19 实测：api.bilibili.com 同路径 404）。
    """
    d = _http_json(f"{_MEMBER}/x/web/archive/videos?aid={int(aid)}")
    out = []
    for v in d.get("videos", []) or []:
        if isinstance(v, dict) and v.get("cid"):
            out.append({"cid": int(v["cid"]),
                        "index": int(v.get("index", 0) or 0),
                        "title": str(v.get("title", ""))})
    return out


# ---------------------------------------------------------------------------
# 写操作（合集加入）
# ---------------------------------------------------------------------------
def season_add_video(aid: int, cid: int, title: str, section_id: int) -> None:
    """把视频加入合集小节（episodes/add，csrf 必带）。失败抛 BiliApiError。

    供新视频投稿成功后调用（fail-soft：调用方捕获后写 job 提示，不影响
    投稿成功态）。
    """
    csrf = load_cookies().get("bili_jct", "")
    body = json.dumps({"section_id": int(section_id),
                       "episodes": [{"aid": int(aid), "cid": int(cid),
                                     "title": title, "charging_pay": False}]})
    _http_json(f"{_MEMBER}/x2/creative/web/season/section/episodes/add",
               data=body.encode("utf-8"), csrf=csrf)


# ---------------------------------------------------------------------------
# v2.4.0 扫码登录（投稿账号管理页「扫码登录」：web 二维码 generate/poll）
# ---------------------------------------------------------------------------
# 调研来源（2026-10-19 context7 核对 bilibili-API-collect
# docs/login/login_action/QR.md）：二维码 180s 有效；poll 返回 data.code
# 86101=未扫 / 86090=已扫未确认 / 86038=失效 / 0=成功；成功时响应
# Set-Cookie 直接下发 SESSDATA/bili_jct/DedeUserID/DedeUserID__ckMd5/sid
# 且 data.refresh_token 可续期。网页密码登录因需极验 geetest 人机验证 +
# RSA 加密密码无法服务端自动化，B 站网页端亦已停用，不实现。
_QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 会话键（扫码/续期都会整体替换）；其余键（buvid3/buvid4/bili_ticket/
# fingerprint 等）为设备指纹，一旦丢失 B 站 web 接口立即 nav -101 /
# 创作中心 61000（2026-10-19 renew 剥指纹实测），须跨登录流程保留
_SESSION_COOKIE_KEYS = {"SESSDATA", "bili_jct", "DedeUserID",
                        "DedeUserID__ckMd5", "sid"}

# biliup serde 的 Cookie 项全字段（缺字段会报 missing field）
_COOKIE_FIELDS = ("name", "value", "http_only", "secure", "expires")


def _fingerprint_cookie_dict() -> dict[str, str]:
    """设备指纹 cookie 集：现有 biliup cookies.json 的非会话键打底，再并入
    config [capture].cookie 里的指纹键（采集账号的 buvid 系列长期有效，
    即使其 SESSDATA 已失效）。扫码/续期后必须把指纹与新的会话键合并写回，
    否则登录态无法通过 B 站 web 侧校验。"""
    out: dict[str, str] = {}
    try:
        p = cookies_path()
        if p.exists():
            data = json.loads(p.read_text("utf-8"))
            for c in ((data.get("cookie_info") or {}).get("cookies") or []):
                if isinstance(c, dict) and c.get("name") \
                        and c["name"] not in _SESSION_COOKIE_KEYS:
                    out[str(c["name"])] = str(c.get("value", ""))
    except (json.JSONDecodeError, OSError, AttributeError):
        pass
    try:
        from ..config import get_config
        raw = str(get_config()["capture"].get("cookie", "") or "")
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k not in _SESSION_COOKIE_KEYS and k not in out:
                out[k] = v.strip()
    except Exception:                       # noqa: BLE001 —— 指纹属增强项
        pass
    return out


def _qr_get(url: str) -> tuple[dict, dict[str, str]]:
    """GET 一个扫码登录接口 → (json, 响应 Set-Cookie 字典)。"""
    headers = {"User-Agent": _UA,
               "Referer": "https://www.bilibili.com/"}
    fp = _fingerprint_cookie_dict()
    if fp:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in fp.items())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise BiliApiError(f"扫码登录接口 HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BiliApiError(f"扫码登录网络失败：{e}")
    except json.JSONDecodeError:
        raise BiliApiError("扫码登录接口返回非 JSON（可能被风控拦截）")
    set_cookies: dict[str, str] = {}
    for hdr in r.headers.get_all("Set-Cookie") or []:
        if "=" in hdr:
            k, v = hdr.split("=", 1)
            set_cookies[k.strip()] = v.split(";", 1)[0].strip()
    return body, set_cookies


def qr_login_start() -> tuple[str, str]:
    """申请扫码登录二维码，返回 (qrcode_key, url)。url 交给前端渲染成
    二维码（内容仅对 B 站客户端有意义，密钥 180s 后失效）。"""
    body, _ = _qr_get(_QR_GENERATE_API)
    data = body.get("data") or {}
    key, url = str(data.get("qrcode_key", "")), str(data.get("url", ""))
    if body.get("code") != 0 or not key or not url:
        raise BiliApiError(
            f"申请二维码失败：{body.get('message') or body.get('code')}")
    return key, url


def qr_login_poll(qrcode_key: str) -> dict:
    """轮询一次扫码状态，返回 {"state": waiting|scanned|expired|confirmed,
    "cookies", "refresh_token"}。confirmed 时 cookies 为响应 Set-Cookie
    里的会话键（SESSDATA/bili_jct/...），refresh_token 取自 data。"""
    body, set_cookies = _qr_get(f"{_QR_POLL_API}?qrcode_key={qrcode_key}")
    if body.get("code") != 0:
        raise BiliApiError(
            f"扫码轮询失败：{body.get('message') or body.get('code')}")
    data = body.get("data") or {}
    st = int(data.get("code", -1))
    if st == 0:
        return {"state": "confirmed",
                "cookies": set_cookies,
                "refresh_token": str(data.get("refresh_token", ""))}
    if st == 86090:
        return {"state": "scanned", "cookies": {}, "refresh_token": ""}
    if st == 86038:
        return {"state": "expired", "cookies": {}, "refresh_token": ""}
    return {"state": "waiting", "cookies": {}, "refresh_token": ""}


def build_submit_cookies_doc(cookies: dict[str, str],
                             refresh_token: str = "") -> dict:
    """扫码确认后的会话键 + 设备指纹 → biliup LoginInfo 文档（落盘格式）。

    cookies 应含 qr_login_poll 返回的会话键（调用方先与
    _fingerprint_cookie_dict() 合并）；token_info 空壳（access_token 可
    后续经 biliup renew 补齐——网页 Cookie 登录口径一致，新视频投稿前
    提示 renew）。
    """
    jar = [{"name": k, "value": v, "http_only": 1 if k in
            ("SESSDATA", "DedeUserID", "DedeUserID__ckMd5") else 0,
            "secure": 0, "expires": 0} for k, v in cookies.items()]
    mid = 0
    for c in jar:
        if c["name"] == "DedeUserID":
            try:
                mid = int(c["value"] or 0)
            except (TypeError, ValueError):
                mid = 0
    return {
        "cookie_info": {"cookies": jar,
                        "domains": [".bilibili.com"]},
        "token_info": {"access_token": "", "expires_in": 0, "mid": mid,
                       "refresh_token": refresh_token},
        "sso": ["https://passport.bilibili.com/api/v2/sso"],
        "platform": None,
    }


def merge_renewed_cookies(old_doc: dict, new_doc: dict) -> dict:
    """biliup renew 后的凭据合并（纯函数，可单测）。

    renew 会整体重写 cookie_info 且只保留会话键，把 buvid3/buvid4/
    bili_ticket 等设备指纹全部剥掉——实测 renew 后 web 接口立即 nav -101、
    创作中心 61000（2026-10-19 服务器实况）。合并口径：**new 里有的键
    一律以 new 为准**（含 renew 顺手刷新的指纹键），new 缺的指纹键从
    old 补齐；token_info/sso/platform 取 new（None 值回退 old）。
    """
    old_jar = ((old_doc.get("cookie_info") or {}).get("cookies")) or []
    new_jar = ((new_doc.get("cookie_info") or {}).get("cookies")) or []
    new_names = {c.get("name") for c in new_jar
                 if isinstance(c, dict) and c.get("name")}
    fp = [dict(c) for c in old_jar
          if isinstance(c, dict) and c.get("name")
          and c.get("name") not in new_names]
    out = dict(old_doc)
    out["cookie_info"] = {"cookies": fp + [dict(c) for c in new_jar],
                          "domains": ((new_doc.get("cookie_info") or {})
                                      .get("domains")
                                      or ((old_doc.get("cookie_info") or {})
                                          .get("domains")
                                          or [".bilibili.com"]))}
    for key in ("token_info", "sso", "platform"):
        if key in new_doc and new_doc[key] is not None:
            out[key] = new_doc[key]
    return out
