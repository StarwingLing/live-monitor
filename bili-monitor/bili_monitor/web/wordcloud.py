# -*- coding: utf-8 -*-
"""本场弹幕词云统计（v2.3.4：详情页场次模式「弹幕词云」板块数据源）。

口径经 tmp-wordcloud/ 临时预览页定稿（2026-10-16 用户验收）：
- 预处理：表情弹幕 `[包名_短名]` → 还原短名（表情刷屏与同义文字弹幕在
  词频上可合并观察）、URL 丢弃、同字符连刷折叠（哈×N→哈哈，N≥3 折成 2）；
- 分词：jieba 精确模式 + 自定义黑话词表（默认词典不认识会被切碎）；
- 过滤：长度 <2、纯数字/符号残片、停用词（虚词 + 弹幕噪声）；
- 输出：词频 TOP80（前端按 value^0.6 幂缩放字号）+ 表情 TOP12（带 B 站
  CDN 原图 URL，词云插件只能画文本，表情原图由前端瓦片展示）。

性能：jieba 词典首次加载约 0.8s/数十 MB 内存（进程内只加载一次）；
单场 4.8 万条弹幕分词约 2~4s，故带进程内缓存——以场次弹幕计数为版本
键（场次结束该值不再变，缓存永久命中；在播场次计数变化时自动重算）。
"""
import json
import re
import sqlite3
from collections import Counter

TOP_WORDS = 80   # 词频榜取词数（预览定稿口径）
TOP_EMOTES = 12  # 表情榜条数

# B站表情弹幕「[鸣潮·致予新世界静态表情包_坐好了]」/「[大笑]」
EMOTE_RE = re.compile(r"\[([^\[\]]+)\]")
EMOTE_SEP = "_"
URL_RE = re.compile(r"https?://\S+")
# 同字符连刷折叠：(.)\1{2,} → 连续 3+ 个同字折成 2 个
REPEAT_RE = re.compile(r"(.)\1{2,}")

# 自定义词表：直播间黑话（jieba 默认词典不认识会被切碎）。正式化方向是
# 按房间维护可编辑词表（管理页配置），当前先内置预览时验证过的词。
CUSTOM_WORDS = [
    "哈基布", "镜花水月", "无限城", "结算动画", "打call", "人间烟火气",
    "大声密谋", "库洛", "鸣潮", "守岸人", "瓯雁城", "燎火之心",
]

# 停用词：汉语高频虚词 + 弹幕场景噪声（预览两轮迭代定稿）
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

# 进程内缓存：session_id → (danmaku_count, payload)
_CACHE: dict[int, tuple[int, dict]] = {}

_jieba_ready = False


def _ensure_jieba():
    """延迟初始化 jieba（首次调用加载词典 + 自定义词，进程内一次）。"""
    global _jieba_ready
    if _jieba_ready:
        return
    import jieba
    for w in CUSTOM_WORDS:
        jieba.add_word(w)
    _jieba_ready = True


def _preprocess(text: str) -> str:
    """单条弹幕预处理：表情还原短名 → 去 URL → 同字连刷折叠。"""
    def emote_repl(m: re.Match) -> str:
        inner = m.group(1)
        return inner.rsplit(EMOTE_SEP, 1)[-1] if EMOTE_SEP in inner else inner

    text = EMOTE_RE.sub(emote_repl, text)
    text = URL_RE.sub(" ", text)
    return REPEAT_RE.sub(r"\1\1", text)


def _keep_token(tok: str) -> bool:
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
    return s.rsplit(EMOTE_SEP, 1)[-1] if EMOTE_SEP in s else s


def compute(conn: sqlite3.Connection, session_id: int) -> dict:
    """统计指定场次弹幕的词频 TOP 与表情 TOP（带进程内缓存）。

    conn 由调用方传入（app.py 的连接语义：用后即关，本函数只读）。
    """
    cur = conn.execute("SELECT COUNT(*) FROM danmaku WHERE session_id=?",
                       (session_id,))
    dm_count = cur.fetchone()[0]
    cached = _CACHE.get(session_id)
    if cached and cached[0] == dm_count:
        return cached[1]

    _ensure_jieba()
    import jieba

    rows = conn.execute(
        "SELECT text, emots FROM danmaku WHERE session_id=?", (session_id,)
    ).fetchall()

    # ---- 词频（预处理 → jieba.lcut → 过滤 → Counter）----
    counter: Counter[str] = Counter()
    for text, _ in rows:
        cleaned = _preprocess(text or "")
        if cleaned:
            counter.update(t for t in jieba.lcut(cleaned) if _keep_token(t))
    top_words = [{"name": w, "value": c}
                 for w, c in counter.most_common(TOP_WORDS)]

    # ---- 表情榜（emots 字段 {表情文本: {url,width,height}}；个别行是
    # 抓包残缺 JSON，防御性跳过）----
    emote_count: Counter[str] = Counter()
    emote_info: dict[str, dict] = {}
    for _, emots in rows:
        if not emots:
            continue
        try:
            mapping = json.loads(emots)
        except (ValueError, TypeError):
            continue
        for name, info in (mapping or {}).items():
            emote_count[name] += 1
            if isinstance(info, dict) and info.get("url"):
                emote_info.setdefault(name, info)
    top_emotes = []
    for name, cnt in emote_count.most_common(TOP_EMOTES):
        info = emote_info.get(name, {})
        top_emotes.append({
            "text": name,
            "short": _emote_short(name),
            "count": cnt,
            "url": info.get("url", ""),
        })

    payload = {
        "session_id": session_id,
        "danmaku_total": dm_count,
        "unique_tokens": len(counter),
        "words": top_words,
        "emotes": top_emotes,
    }
    # 缓存上限防长跑膨胀（场次 id 单调递增，简单按插入序淘汰最老条目）
    if len(_CACHE) >= 64:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[session_id] = (dm_count, payload)
    return payload
