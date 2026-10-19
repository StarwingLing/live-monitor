# -*- coding: utf-8 -*-
"""下播直播报告图生成（v0.3.0 阶段一：生成模块）。

用途：下播文本通知发出后，为刚结束的这场直播生成一张报告图（主播信息 +
场次统计 + 弹幕排行 TOP8 + 本场词云），紧随文本发到同一批群。本模块
**独立于主循环**：主循环经子进程调用（见文末 CLI），避免 jieba/wordcloud
的词典与渲染内存驻留在通知器常驻进程里；同一 CLI 也是手动测试入口
（--session 直接指定场次出图，--room+--since 按通知器口径定位场次）。

数据面：只读共享 live.db（danmaku / live_sessions / rooms 表的 DDL 属
bili-monitor/replay 体系，本项目不改结构只查询）。

渲染面：Pillow 排版（深色卡片，视觉与 bili-monitor ThreeUI 暗色同族）+
wordcloud 词云 + jieba 分词。分词口径与 bili-monitor v2.3.4 web 词云板块
一致（表情弹幕还原短名 / 去 URL / 同字连刷折叠 / 停用词过滤），独立
实现、不跨项目 import。

词云内嵌表情包原图（2026-10-18 用户定稿方向）：弹幕里刷得最多的表情取
emots 字段的原图 URL 下载（hdslb 图床匿名可下），在词云画布上**预留矩形
孔位**（wordcloud 的 mask 掩码让文字绕开），原图按纵横比缩放贴入孔位
（不带次数角标，纯图）；表情对应短名从文字词频中剔除（图已代替词）。
原图抓取失败则退化为纯文字词云（该短名保留为文字词）。

中文字体随包分发（assets/fonts/NotoSansSC-*.otf，OFL 协议允许再分发），
服务器最小安装无 CJK 字体也能渲染。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 资源路径与主题
# ---------------------------------------------------------------------------
_ASSETS = Path(__file__).resolve().parent / "assets"
FONT_REGULAR = _ASSETS / "fonts" / "NotoSansSC-Regular.otf"
FONT_BOLD = _ASSETS / "fonts" / "NotoSansSC-Bold.otf"

# 头像/表情磁盘缓存目录（默认 <项目根>/data/avatar_cache，可用参数覆盖）
_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "avatar_cache"

# 画布与版式常量（宽 900，高度按内容自增）
W = 900
MARGIN = 36
RADIUS = 26          # 整卡圆角
PANEL_RADIUS = 18    # 内嵌面板圆角

# 主题色（RGB 元组；深色卡片）
BG_TOP = (13, 17, 32)        # 背景渐变顶
BG_BOTTOM = (22, 28, 50)     # 背景渐变底
PANEL = (19, 25, 45, 255)    # 面板底色（不透明）
PANEL_EDGE = (255, 255, 255, 22)   # 面板描边（低透明白）
INK = (232, 236, 244)        # 主文字
SUB = (154, 163, 184)        # 次级文字
ACCENT = (251, 114, 153)     # B站粉（主强调）
CYAN = (64, 183, 235)        # 次强调
GOLD = (245, 197, 24)        # 金（榜首）
VIOLET = (168, 132, 255)     # 紫（渐变副色）
BAR_TRACK = (255, 255, 255, 26)     # 排行柱轨道
DIVIDER = (255, 255, 255, 20)       # 分隔线

# 舰队徽标：privilege_type → (文案, 颜色)（0 无；1 总督 2 提督 3 舰长）
GUARDS = {1: ("总督", (245, 197, 24)), 2: ("提督", (251, 114, 153)),
          3: ("舰长", (96, 178, 236))}

# 词云规模：文字词频截断 + 入云表情原图个数（按表情出现次数取 TOP）
TOP_WORDS = 60
TOP_EMOTES = 6

# ---------------------------------------------------------------------------
# 分词口径（与 bili-monitor v2.3.4 web 词云一致；独立实现）
# ---------------------------------------------------------------------------
EMOTE_RE = re.compile(r"\[([^\[\]]+)\]")
URL_RE = re.compile(r"https?://\S+")
REPEAT_RE = re.compile(r"(.)\1{2,}")   # 同字符连刷 3+ 折成 2 个

# 直播间黑话（jieba 默认词典不认识会被切碎；与 web 词云同表）
CUSTOM_WORDS = [
    "哈基布", "镜花水月", "无限城", "结算动画", "打call", "人间烟火气",
    "大声密谋", "库洛", "鸣潮", "守岸人", "瓯雁城", "燎火之心",
]

STOPWORDS = frozenset("""
的 了 是 在 我 你 他 她 它 们 这 那 也 都 就 还 要 有 不 没 和 跟 给 被 把 让
吗 呢 吧 啊 呀 哦 嗯 哈 嘻 呜 呃 额 欸 咋 咯 嘛 呗 捂 艹 草 泪 水
一个 这个 那个 什么 怎么 为什么 没有 不是 还是 就是 觉得 感觉 直接
现在 已经 可以 应该 可能 知道 时候 真的 出来 起来 进去 回去 过来
上去 下去 一下 这样 那样 这里 那里 自己 我们 你们 他们 她们 它们
居然 竟然 然后 但是 所以 因为 如果 虽然 或者 以及 而且 等等 之类
这么 那么 多少 几个 有些 特别 非常 太 最 更 挺 蛮 略 稍 有点 一点
基本 完全 全部 整个 所有 各种 各个 每次 一直 总是 从来 马上 立刻
刚刚 刚才 其实 当然 肯定 一定 必须 不会 不能 不要 不用 不想 别去
别 东西 地方 样子 情况 问题 事情 开始 结束 有些 看到 听到
第一 第二 是不是 有没有 能不能 好不好 来了 看看 说说 想想
哈哈 嘻嘻 呜呜 啊啊 哦哦 嗯嗯 呵呵 233 www hhh awsl yyds xswl
还有 真是 这是 今天 昨天 明天
""".split())

TOKEN_OK_RE = re.compile(r"[\u4e00-\u9fffA-Za-z]")
TOKEN_BAD_RE = re.compile(r"^[\d\W_]+$")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class TopUser:
    """弹幕排行一行：用户 + 条数 + 舰队等级 + 头图字节（None=占位圆）。"""
    uid: int
    uname: str
    count: int
    guard: int = 0                # 0 无 1 总督 2 提督 3 舰长（取场内最高）
    avatar: bytes | None = None


@dataclass
class EmoteItem:
    """入云表情原图：短名 + 出现次数 + 原图字节与纵横比。

    img 为 None 表示原图不可用（离线/下载失败）——该表情退化为文字词，
    不在词云中占孔位。
    """
    name: str
    count: int
    url: str = ""
    img: bytes | None = None
    aspect: float = 1.0           # 原图宽/高（贴孔位时保持比例）


@dataclass
class SessionReport:
    """一场直播的全部报告素材（查询/图片抓取完成后，渲染是纯函数）。"""
    session_id: int
    room_id: int
    anchor: str = ""
    start: str = ""               # ISO
    end: str = ""                 # ISO（未收尾场次回退生成时刻）
    danmaku: int = 0
    speakers: int = 0
    peak_viewers: int = 0
    top: list[TopUser] = field(default_factory=list)
    freqs: dict[str, int] = field(default_factory=dict)
    emotes: list[EmoteItem] = field(default_factory=list)
    anchor_avatar: bytes | None = None   # 主播头图（None=占位圆）
    generated_at: str = ""


# ---------------------------------------------------------------------------
# 库查询（只读）
# ---------------------------------------------------------------------------
def find_session(conn: sqlite3.Connection, room_id: int,
                 since_iso: str, tol_minutes: int = 15) -> int | None:
    """按通知器口径定位"刚结束的这场"的 session_id（阶段二接线用）。

    通知器的开播起点 state.since 取自 B站接口 live_time，与 capture 落库
    的 live_sessions.start_time 存在时钟差/短闪断合并差，故按"覆盖该时刻"
    匹配：start_time ≤ since+容差 且（未收尾 或 end_time ≥ since−容差）；
    从最近的场次往回找，命中即返回；找不到返回 None（调用方 WARN 跳过）。
    """
    try:
        since = datetime.fromisoformat(since_iso)
    except ValueError:
        return None
    rows = conn.execute(
        "SELECT session_id, start_time, end_time FROM live_sessions "
        "WHERE room_id = ? ORDER BY session_id DESC LIMIT 5",
        (room_id,)).fetchall()
    for row in rows:
        try:
            st = datetime.fromisoformat(row["start_time"])
            et = (datetime.fromisoformat(row["end_time"])
                  if row["end_time"] else None)
        except ValueError:
            continue
        tol = tol_minutes * 60
        if (st - since).total_seconds() <= tol and (
                et is None or (et - since).total_seconds() >= -tol):
            return int(row["session_id"])
    return None


_jieba_ready = False


def build_freqs(texts: list[str]) -> dict[str, int]:
    """弹幕文本 → 词频 dict（TOP TOP_WORDS）。分词口径见模块 docstring。"""
    global _jieba_ready
    if not texts:
        return {}
    import jieba
    if not _jieba_ready:
        for w in CUSTOM_WORDS:
            jieba.add_word(w)
        _jieba_ready = True
    counter: Counter[str] = Counter()
    for raw in texts:
        # 预处理：表情还原短名 → 去 URL → 同字连刷折叠
        t = EMOTE_RE.sub(
            lambda m: (m.group(1).rsplit("_", 1)[-1]
                       if "_" in m.group(1) else m.group(1)),
            raw or "")
        t = URL_RE.sub(" ", t)
        t = REPEAT_RE.sub(r"\1\1", t).strip()
        if not t:
            continue
        counter.update(tok.strip() for tok in jieba.lcut(t) if _keep(tok))
    return dict(counter.most_common(TOP_WORDS))


def _keep(tok: str) -> bool:
    """词条过滤：长度≥2、含中文或字母、非纯数字符号、非停用词。"""
    tok = tok.strip()
    if len(tok) < 2:
        return False
    if not TOKEN_OK_RE.search(tok) or TOKEN_BAD_RE.match(tok):
        return False
    return tok.lower() not in STOPWORDS


def _emote_short(name: str) -> str:
    """表情文本 → 展示短名：去方括号后取「包名_短名」的短名段。

    emots 键两种形态：带括号「[鸣潮·…包名_坐好了]」（房间/频道表情）与
    不带括号纯文本「打call喵」（大表情）——分别处理。"""
    s = name.strip("[]")
    return s.rsplit("_", 1)[-1] if "_" in s else s


def load_report(conn: sqlite3.Connection, session_id: int, *,
                fetch_avatars: bool = True,
                avatar_cache: Path = _DEFAULT_CACHE,
                avatar_budget_s: float = 15.0) -> SessionReport:
    """从共享库汇聚一场直播的全部报告素材（含头像/表情原图抓取）。

    fetch_avatars=False 时头像与表情原图全部缺席（占位圆/纯文字词云，
    单测/离线）；图片抓取有总时长预算（默认 15s：主播+TOP8 头像 9 张 +
    表情原图 6 张），超预算的剩余项直接降级，不拖慢下播链路。
    """
    sess = conn.execute(
        "SELECT s.*, r.anchor_name, r.avatar_url FROM live_sessions s "
        "JOIN rooms r ON r.room_id = s.room_id WHERE s.session_id = ?",
        (session_id,)).fetchone()
    if sess is None:
        raise ValueError(f"session {session_id} 不存在")

    now = datetime.now().isoformat(timespec="seconds")
    rep = SessionReport(
        session_id=session_id, room_id=int(sess["room_id"]),
        anchor=(sess["anchor_name"] or "").strip(),
        start=sess["start_time"] or "",
        end=sess["end_time"] or now,   # 未收尾场次回退当前时刻（在播测试）
        peak_viewers=int(sess["peak_viewers"] or 0),
        generated_at=now)

    # ---- 场次统计：弹幕量/发言人数（danmaku 表实测口径，冗余列仅展示备胎）----
    row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT uid) FROM danmaku "
        "WHERE session_id = ?", (session_id,)).fetchone()
    rep.danmaku, rep.speakers = int(row[0]), int(row[1])

    # ---- 弹幕排行 TOP8 + 全量文本/表情（词云用）----
    rows = conn.execute(
        "SELECT uid, uname, COUNT(*) AS c, MAX(privilege_type) AS g "
        "FROM danmaku WHERE session_id = ? GROUP BY uid "
        "ORDER BY c DESC, uid LIMIT 8", (session_id,)).fetchall()
    rep.top = [TopUser(int(r["uid"]), r["uname"] or str(r["uid"]),
                       int(r["c"]), int(r["g"] or 0)) for r in rows]
    texts: list[str] = []
    emote_count: Counter[str] = Counter()
    emote_info: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT text, emots FROM danmaku WHERE session_id = ?",
            (session_id,)):
        texts.append(r["text"])
        raw = r["emots"]
        if not raw:
            continue
        try:                       # 个别行是抓包残缺 JSON，防御性跳过
            mapping = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for name, info in (mapping or {}).items():
            emote_count[name] += 1
            if isinstance(info, dict) and info.get("url"):
                emote_info.setdefault(name, info)
    rep.freqs = build_freqs(texts)
    rep.emotes = [
        EmoteItem(_emote_short(name), int(cnt),
                  url=str(emote_info.get(name, {}).get("url", "")))
        for name, cnt in emote_count.most_common(TOP_EMOTES)
        if emote_info.get(name, {}).get("url")]

    # ---- 头像与表情原图 ----
    if fetch_avatars:
        import time
        deadline = time.time() + avatar_budget_s
        anchor_url = (sess["avatar_url"] or "").strip()
        # 缓存键带房间号：主播头像按房隔离（曾因共用 "anchor" 键串台）
        rep.anchor_avatar = (_fetch_url_resource(
            anchor_url, f"a{rep.room_id}", avatar_cache) if anchor_url else None)
        for u in rep.top:
            if time.time() >= deadline:
                break
            u.avatar = _fetch_user_avatar(u.uid, avatar_cache)
        for e in rep.emotes:
            if time.time() >= deadline:
                break
            e.img = _fetch_emote(e.url, avatar_cache)

    # 图已代替词：成功取得原图的表情，其短名从文字词频中剔除
    for e in rep.emotes:
        if e.img is not None:
            rep.freqs.pop(e.name, None)

    return rep


# ---------------------------------------------------------------------------
# 图片抓取（全部匿名可下；磁盘缓存；失败返回 None → 各自降级）
# ---------------------------------------------------------------------------
_CARD_API = "https://api.bilibili.com/x/web-interface/card"


def _http_get(url: str, timeout: float = 6.0) -> bytes | None:
    """小工具 GET（带浏览器 UA；任何失败返回 None，不打断出图）。"""
    import httpx
    from .bilibili import HEADERS
    try:
        r = httpx.get(url, timeout=timeout, headers=HEADERS,
                      follow_redirects=True)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def _fetch_url_resource(url: str, key: str, cache_dir: Path) -> bytes | None:
    """URL 图直下 + 双文件磁盘缓存（<key>.url 记来源、<key>.img 记图）。

    缓存命中条件：来源 URL 一致且图文件非空——URL 变了（换头像等）自动
    重下，不会串台。"""
    url_file = cache_dir / f"{key}.url"
    img_file = cache_dir / f"{key}.img"
    try:
        if (img_file.exists() and img_file.stat().st_size > 100
                and url_file.exists()
                and url_file.read_text(encoding="utf-8").strip() == url):
            return img_file.read_bytes()
    except OSError:
        pass
    data = _http_get(url)
    if data and len(data) > 100:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            img_file.write_bytes(data)
            url_file.write_text(url, encoding="utf-8")
        except OSError:
            pass
        return data
    return None


def _fetch_user_avatar(uid: int, cache_dir: Path) -> bytes | None:
    """观众头像：名片接口查 face URL → 图缓存（键 u<uid>）。"""
    raw = _http_get(f"{_CARD_API}?mid={uid}")
    if not raw:
        return None
    try:
        face = str(json.loads(raw)["data"]["card"]["face"])
    except (ValueError, KeyError, TypeError):
        return None
    return _fetch_url_resource(face, f"u{uid}", cache_dir)


def _fetch_emote(url: str, cache_dir: Path) -> bytes | None:
    """表情原图直下（URL 恒定，键取 URL 摘要；键 e<md8>）。"""
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return _fetch_url_resource(url, f"e{digest}", cache_dir)


# ---------------------------------------------------------------------------
# 渲染（Pillow；输入 SessionReport，输出 PNG 路径）
# ---------------------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        path = FONT_BOLD if bold else FONT_REGULAR
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except ValueError:
        return iso or "--"


def _fmt_span(start_iso: str, end_iso: str) -> str:
    """直播时长（小时/分钟），解析失败返回空串。"""
    try:
        secs = (datetime.fromisoformat(end_iso)
                - datetime.fromisoformat(start_iso)).total_seconds()
    except ValueError:
        return ""
    m = max(0, round(secs / 60))
    if m < 1:
        return "不足 1 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"


def _display_name(name: str) -> str:
    """昵称展示清洗：去 BMP 外字符（emoji 等字体不含，画出会成豆腐块）。"""
    cleaned = "".join(ch for ch in name if ord(ch) <= 0xFFFF)
    cleaned = re.sub(r"[\uFE00-\uFE0F\u200D\u0000-\u001F]", "", cleaned)
    return cleaned.strip() or "?"


def _clip_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_w: int) -> str:
    """超宽文本截断加省略号（逐字符收缩，中文友好）。"""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    out = text
    while out and draw.textlength(out + ell, font=font) > max_w:
        out = out[:-1]
    return out + ell


def _circle_avatar(avatar: bytes | None, size: int, fallback_char: str,
                   seed: int, ring: tuple | None = None) -> Image.Image:
    """头像 → 圆形 RGBA 图；无头图时画"首字占位圆"（底色按种子散列取色）。

    ring 非空时描一圈 2px 边（榜首金色环/普通淡环）。3x 超采样抗锯齿。
    """
    palette = [ACCENT, CYAN, VIOLET, (94, 210, 168), (240, 148, 92)]
    bg = palette[seed % len(palette)]
    img = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size * 3 - 1, size * 3 - 1], fill=bg + (255,))
    ch = _display_name(fallback_char)[:1] or "?"
    f = ImageFont.truetype(str(FONT_BOLD), size * 3 // 2)   # 3x 画布上的字号
    d.text((size * 3 / 2, size * 3 / 2), ch, font=f, fill=(255, 255, 255, 240),
           anchor="mm")
    if avatar:
        try:
            av = Image.open(io.BytesIO(avatar)).convert("RGBA")
            # 头像常见 140/240 方图；非方形先居中裁方再缩放
            side = min(av.width, av.height)
            av = av.crop(((av.width - side) // 2, (av.height - side) // 2,
                          (av.width + side) // 2, (av.height + side) // 2))
            av = av.resize((size * 3, size * 3), Image.Resampling.LANCZOS)
            mask = Image.new("L", (size * 3, size * 3), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, size * 3 - 1, size * 3 - 1],
                                         fill=255)
            img.paste(av, (0, 0), mask)   # 圆内贴头图，圆外保留占位底
        except Exception:
            pass
    if ring:
        d.ellipse([1, 1, size * 3 - 2, size * 3 - 2], outline=ring + (255,),
                  width=4)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _rounded_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                   radius: int = PANEL_RADIUS) -> None:
    """内嵌面板：圆角底 + 1px 低透明白描边。"""
    draw.rounded_rectangle(box, radius=radius, fill=PANEL, outline=PANEL_EDGE,
                           width=1)


def _section_title(draw: ImageDraw.ImageDraw, x: int, y: int, title: str,
                   right: str = "") -> None:
    """面板标题：4px 强调色竖条 + 粗标题 + 右侧灰色小注。"""
    draw.rounded_rectangle([x, y + 4, x + 5, y + 30], radius=2,
                           fill=ACCENT + (255,))
    draw.text((x + 16, y + 17), title, font=_font(28, bold=True),
              fill=INK, anchor="lm")
    if right:
        draw.text((x + 16 + draw.textlength(title, font=_font(28, bold=True)) + 12,
                   y + 19), right, font=_font(21), fill=SUB, anchor="lm")


# ---- 词云：文字 + 表情原图混合 ------------------------------------------------
def _rects_apart(a: tuple, b: tuple, gap: int) -> bool:
    """两矩形是否相距至少 gap 像素（不重叠且留缝）。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return (ax1 + gap <= bx0 or bx1 + gap <= ax0
            or ay1 + gap <= by0 or by1 + gap <= ay0)


def _place_holes(rects: list[tuple[int, int]], canvas_w: int, canvas_h: int,
                 gap: int = 16, margin: int = 14,
                 seed: int = 42) -> list[tuple[int, int, int, int] | None]:
    """在词云画布内为表情原图找互不重叠的矩形孔位（确定性随机布点）。

    放不下时整组按 0.8 系数缩一档重试；缩到下限仍放不下则放弃该表情
    （对应位置返回 None，调用方跳过该图）。"""
    rng = random.Random(seed)
    placed: list[tuple[int, int, int, int] | None] = []
    for w, h in rects:
        while True:
            for _ in range(300):
                x = margin + int(rng.random() * max(1, canvas_w - w - 2 * margin))
                y = margin + int(rng.random() * max(1, canvas_h - h - 2 * margin))
                box = (x, y, x + w, y + h)
                if all(_rects_apart(box, p, gap) for p in placed if p):
                    placed.append(box)
                    break
            else:
                if w <= 48 or h <= 48:
                    placed.append(None)   # 放不下，放弃
                    break
                w, h = int(w * 0.8), int(h * 0.8)
                continue
            break
    return placed


def _render_cloud(freqs: dict[str, int], w: int, h: int,
                  emotes: list[EmoteItem] | None = None) -> Image.Image | None:
    """词频(+表情原图) → 词云 RGBA 图（透明底，贴在面板上）。

    表情原图混合方案：TOP 表情按次数定尺寸 → _place_holes 在画布上布孔
    → 孔位画进 wordcloud 的 mask（白=禁排文字），文字绕开 → 原图按纵横比
    缩放贴入孔位并加"×次数"角标。固定 random_state/布点种子保证同数据同图。
    """
    usable = [e for e in (emotes or []) if e.img is not None and e.count > 0]
    if not freqs and not usable:
        return None
    from wordcloud import WordCloud

    mask_arr = None
    holes: list[tuple | None] = []
    if usable:
        import numpy as np
        top_c = max(e.count for e in usable)
        rects = []
        for e in usable:
            # 显示高：132×(次数比^0.45)，夹在 60~132；宽按纵横比，夹 40~240
            dh = max(60, min(132, round(132 * (e.count / top_c) ** 0.45)))
            dw = max(40, min(240, round(dh * max(0.5, min(2.2, e.aspect)))))
            rects.append((dw, dh))
        holes = _place_holes(rects, w, h)
        mask_img = Image.new("RGB", (w, h), (0, 0, 0))
        md = ImageDraw.Draw(mask_img)
        for box in holes:
            if box:
                md.rectangle(box, fill=(255, 255, 255))   # 白=文字禁入
        mask_arr = np.asarray(mask_img)

    order = list(freqs)          # 名次 → 颜色（most_common 已按频次降序）
    colors = [ACCENT, CYAN, VIOLET, GOLD, (222, 228, 240)]
    cmap = {wd: colors[i % len(colors)] for i, wd in enumerate(order)}

    def color_func(word, **_kw):
        return cmap.get(word, (222, 228, 240))

    try:
        # 仅表情无词条时给一个空格占位词（wordcloud 要求词频非空；空格不可见）
        gen = freqs if freqs else {" ": 1}
        wc = WordCloud(
            font_path=str(FONT_REGULAR), width=w, height=h,
            background_color=None, mode="RGBA",
            prefer_horizontal=1.0, margin=10, max_words=len(gen),
            relative_scaling=0.5, min_font_size=17, random_state=42,
            color_func=color_func, mask=mask_arr,
        ).generate_from_frequencies(gen)
        out = wc.to_image()
    except Exception as e:
        print(f"[WARN] 词云渲染失败（词表 {len(freqs)} 词/表情 {len(usable)}）："
              f"{e!r}", flush=True)
        return None

    # ---- 表情原图贴孔（2026-10-18 用户评审:不加次数角标,纯图）----
    for e, box in zip(usable, holes):
        if not box:
            continue
        x0, y0, x1, y1 = box
        try:
            em = Image.open(io.BytesIO(e.img or b"")).convert("RGBA")
        except Exception:
            continue
        # 孔位内等比缩放居中（四周留 4px 呼吸边）
        bw, bh = x1 - x0 - 8, y1 - y0 - 8
        scale = min(bw / em.width, bh / em.height)
        em = em.resize((max(1, round(em.width * scale)),
                        max(1, round(em.height * scale))),
                       Image.Resampling.LANCZOS)
        out.paste(em, ((x0 + x1 - em.width) // 2, (y0 + y1 - em.height) // 2), em)
    return out


def render_report(rep: SessionReport, out_path: Path) -> Path:
    """渲染整卡（宽度 900，高度按内容自增）并落盘 PNG。"""
    anchor_avatar = rep.anchor_avatar

    # ---- 高度预算（先算后排版）----
    row_h = 62
    header_h = 128
    stats_h = 112
    rank_h = 66 + len(rep.top) * row_h + 14
    cloud_h = 66 + 400 + 14
    footer_h = 46
    H = (44 + header_h + 24 + stats_h + 22 + rank_h + 22 + cloud_h
         + footer_h + 30)

    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img, "RGBA")

    # 背景纵向渐变（逐行填色；900px 宽逐行画足够快）
    for y in range(H):
        t = y / max(1, H - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        draw.line([(0, y), (W, y)], fill=c)

    # ---- 头部：主播头像 + 名称 + 直播报告 + 时间段 ----
    y = 44
    av = _circle_avatar(anchor_avatar, 96, rep.anchor or "?", seed=rep.room_id,
                        ring=(120, 130, 160))
    # 头像较文字基线再下沉 8px(2026-10-18 用户定稿的视觉微调)
    img.paste(av, (MARGIN, y + 8), av)
    tx = MARGIN + 96 + 28
    draw.text((tx, y + 22), "直 播 报 告", font=_font(24, bold=True),
              fill=ACCENT, anchor="lm")
    draw.text((tx, y + 62), _clip_text(draw, _display_name(rep.anchor),
                                       _font(42, bold=True), W - tx - MARGIN),
              font=_font(42, bold=True), fill=INK, anchor="lm")
    span = _fmt_span(rep.start, rep.end)
    time_line = f"{_fmt_dt(rep.start)} — {_fmt_dt(rep.end)}"
    if span:
        time_line += f"   ·   {span}"
    draw.text((tx, y + 100), time_line, font=_font(24), fill=SUB, anchor="lm")
    y += header_h + 24

    # ---- 统计条（3 格均分；2026-10-18 用户定稿：移除本场收益）----
    _rounded_panel(draw, (MARGIN, y, W - MARGIN, y + stats_h))
    cells = [("弹幕", _fmt_int(rep.danmaku), INK),
             ("发言人数", _fmt_int(rep.speakers), INK),
             ("最高同接", _fmt_int(rep.peak_viewers), CYAN)]
    cw = (W - MARGIN * 2) // 3
    for i, (label, value, color) in enumerate(cells):
        cx = MARGIN + cw * i + cw // 2
        draw.text((cx, y + 34), label, font=_font(22), fill=SUB, anchor="mm")
        draw.text((cx, y + 74), value, font=_font(38, bold=True), fill=color,
                  anchor="mm")
        if i:   # 格间细分隔线
            lx = MARGIN + cw * i
            draw.line([(lx, y + 26), (lx, y + stats_h - 26)], fill=DIVIDER,
                      width=1)
    y += stats_h + 22

    # ---- 弹幕排行 TOP8 ----
    _rounded_panel(draw, (MARGIN, y, W - MARGIN, y + rank_h))
    _section_title(draw, MARGIN + 24, y + 18, "弹幕排行",
                   right=f"TOP {len(rep.top) or 8}")
    ry = y + 66
    top1 = rep.top[0].count if rep.top else 1
    # 列坐标（从左到右）：面板内边 24 → 名次徽章 30 → 头像 44 → 昵称区
    # 196（含舰队徽标空间，固定列宽防长昵称与柱条重叠）→ 柱条轨道 → 条数
    bar_x0 = MARGIN + 24 + 40 + 44 + 12 + 196 + 12
    bar_x1 = W - MARGIN - 24 - 64               # 右侧留条数
    track_w = max(60, bar_x1 - bar_x0)
    for i, u in enumerate(rep.top):
        cy = ry + row_h // 2
        # 名次徽章（前三金/银/铜，其余弱化）
        rank = i + 1
        badge_bg = {1: GOLD, 2: (176, 184, 198), 3: (206, 148, 102)}.get(
            rank, (46, 54, 82))
        badge_fg = (24, 28, 44) if rank <= 3 else SUB
        bx = MARGIN + 24
        draw.rounded_rectangle([bx, cy - 15, bx + 30, cy + 15], radius=8,
                               fill=badge_bg + (255,))
        draw.text((bx + 15, cy), str(rank), font=_font(22, bold=True),
                  fill=badge_fg, anchor="mm")
        # 头像（榜首描金环）
        avu = _circle_avatar(u.avatar, 44, u.uname or "?", seed=u.uid,
                             ring=GOLD if rank == 1 else (86, 96, 128))
        img.paste(avu, (bx + 40, cy - 22), avu)
        # 昵称 + 舰队徽标（昵称区固定 196px，含徽标时昵称缩让）
        nx = bx + 40 + 44 + 12
        guard = GUARDS.get(u.guard)
        name_max = 196 - (56 if guard else 0)
        shown = _clip_text(draw, _display_name(u.uname),
                           _font(25, bold=True), name_max)
        draw.text((nx, cy), shown, font=_font(25, bold=True), fill=INK,
                  anchor="lm")
        if guard:
            gtxt, gcol = guard
            gx = nx + draw.textlength(shown, font=_font(25, bold=True)) + 8
            draw.rounded_rectangle([gx, cy - 13, gx + 46, cy + 13], radius=7,
                                   fill=gcol + (38,))
            draw.text((gx + 23, cy), gtxt, font=_font(19, bold=True),
                      fill=gcol, anchor="mm")
        # 柱状条（相对榜首等比；榜首粉→紫渐变，其余青色系按名次降透明度）
        ratio = (u.count / top1) if top1 else 0
        bw = max(8, int(track_w * ratio))
        bh = 14
        by = cy - bh // 2
        draw.rounded_rectangle([bar_x0, by, bar_x0 + track_w, by + bh],
                               radius=bh // 2, fill=BAR_TRACK)
        if rank == 1:
            # 榜首：粉→紫横向渐变（单独小图逐像素填色，圆角蒙版贴回）
            bar = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            bd = ImageDraw.Draw(bar)
            for x in range(bw):
                t = x / max(1, bw - 1)
                c = tuple(int(a + (b - a) * t) for a, b in zip(ACCENT, VIOLET))
                bd.line([(x, 0), (x, bh)], fill=c)
            bm = Image.new("L", (bw, bh), 0)
            ImageDraw.Draw(bm).rounded_rectangle([0, 0, bw - 1, bh - 1],
                                                 radius=bh // 2, fill=255)
            img.paste(bar, (bar_x0, by), bm)
        else:
            alpha = max(120, 235 - rank * 18)
            draw.rounded_rectangle([bar_x0, by, bar_x0 + bw, by + bh],
                                   radius=bh // 2,
                                   fill=CYAN + (alpha,))
        # 条数
        draw.text((W - MARGIN - 24, cy), _fmt_int(u.count),
                  font=_font(26, bold=True),
                  fill=GOLD if rank == 1 else INK, anchor="rm")
        # 行分隔线（末行不画）
        if i < len(rep.top) - 1:
            draw.line([(MARGIN + 24, ry + row_h), (W - MARGIN - 24, ry + row_h)],
                      fill=DIVIDER, width=1)
        ry += row_h
    y += rank_h + 22

    # ---- 本场词云（文字 + 表情原图混合）----
    _rounded_panel(draw, (MARGIN, y, W - MARGIN, y + cloud_h))
    n_img = sum(1 for e in rep.emotes if e.img is not None)
    note = f"{len(rep.freqs)} 词" + (f" · {n_img} 表情" if n_img else "")
    _section_title(draw, MARGIN + 24, y + 18, "本场词云", right=note)
    # 词云画布比面板内宽再缩 16px（贴边词留呼吸边），居中贴入
    cloud_w = W - MARGIN * 2 - 24 * 2 - 16
    cloud = _render_cloud(rep.freqs, cloud_w, 400, rep.emotes)
    cy0 = y + 66
    if cloud is not None:
        if cloud.width > cloud_w:
            scale = cloud_w / cloud.width
            cloud = cloud.resize((int(cloud.width * scale),
                                  int(cloud.height * scale)),
                                 Image.Resampling.LANCZOS)
        img.paste(cloud, (MARGIN + 24 + 8, cy0), cloud)
    else:
        draw.text((W // 2, cy0 + 200), "本场无弹幕", font=_font(28),
                  fill=SUB, anchor="mm")
    y += cloud_h

    # ---- 页脚 ----
    draw.text((MARGIN, y + 20), f"live.bilibili.com/{rep.room_id}",
              font=_font(21), fill=SUB, anchor="lm")
    draw.text((W - MARGIN, y + 20),
              f"生成于 {datetime.now().strftime('%m-%d %H:%M')}",
              font=_font(21), fill=SUB, anchor="rm")

    # ---- 整卡圆角（超出圆角的部分填透明 → PNG 圆角边缘）----
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=RADIUS,
                                           fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# CLI：手动测试与主循环子进程共用入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成一场直播的下播报告图（测试/主循环子进程共用）")
    ap.add_argument("--db", required=True, help="共享 live.db 路径")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", type=int, help="直接指定 session_id（测试）")
    g.add_argument("--room", type=int, help="房间号（配合 --since 定位场次）")
    ap.add_argument("--since", help="开播时刻 ISO（--room 时的定位锚点）")
    ap.add_argument("--out", default="report.png", help="输出 PNG 路径")
    ap.add_argument("--no-avatar", action="store_true",
                    help="不抓头像/表情原图（全部占位；离线/单测）")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        if args.session:
            sid = args.session
        else:
            if not args.since:
                print("--room 需要 --since（通知器 state.since，ISO 时间）",
                      file=sys.stderr)
                return 2
            sid = find_session(conn, args.room, args.since)
            if sid is None:
                print(f"[WARN] 房间 {args.room} 未找到覆盖 {args.since} 的场次",
                      file=sys.stderr)
                return 2
        rep = load_report(conn, sid, fetch_avatars=not args.no_avatar)
    finally:
        conn.close()

    out = render_report(rep, Path(args.out))
    print(f"[INFO] 场次 {rep.session_id}（房间 {rep.room_id} {rep.anchor}）"
          f"报告图已生成: {out}")
    print(f"       弹幕 {rep.danmaku} / 发言 {rep.speakers} 人 / "
          f"最高同接 {rep.peak_viewers} / "
          f"TOP1 {rep.top[0].uname if rep.top else '-'} "
          f"{rep.top[0].count if rep.top else 0} 条 / "
          f"词云 {len(rep.freqs)} 词 + "
          f"{sum(1 for e in rep.emotes if e.img)} 表情")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
