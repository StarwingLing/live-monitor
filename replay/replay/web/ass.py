# -*- coding: utf-8 -*-
r"""ASS 弹幕字幕生成器（replay v2.0.0：下载携带同期弹幕）。

定位（2026-10-08 调研定稿）：服务端不做烧录（2 核小机算力/存储不划算），
本地拼接路线的第一期零自研形态——服务端只交付与视频**同名**的 ASS 字幕
文件，用户本地用播放器直载（PotPlayer/mpv/VLC/IINA 同名自动加载）或一条
ffmpeg 命令自行烧录。

设计要点（danmaku2ass 同惯例）：
- 固定逻辑画布 PlayResX/Y = 1920/1080 + ScaledBorderAndShadow: yes——视频
  分辨率不在 DB，libass/播放器按实际视频自动缩放固定画布；
- 直播弹幕无 mode 数据，全部滚动式（与网页版 canvas 叠加口径一致，不做
  顶部/底部固定弹幕），滚动全程约 10s：{\move(1920, y, -字宽估计, y)}；
- 样式两种：Scroll 白色滚动、Guard 按舰长等级配色——色值沿用前端
  danmaku.js 的约定色（1 总督金 / 2 提督粉 / 3 舰长蓝）；
- 车道分配：行高 = 字号×1.3，同车道防追尾判定（前一条尾部已移出足够
  距离，或新条不快于前条才可入道）；
- 文本清洗：剥 ``{}``（防 ASS 标签注入）、去换行、单条限长 100 字符；
  emots 表情弹幕 text 为空则整条丢弃；
- ASS 颜色为 ``&HAABBGGRR``（BGR 序，与 CSS 的 RGB 相反）。

纯函数模块：输入即 _danmaku_payload 产出的 {t, text, guard} items，
输出 ASS 文本，可独立单测（黄金样例断言 Script Info / 样式 / 关键
Dialogue 行 / 注入清洗）。
"""
import re

# 逻辑画布与滚动参数
PLAY_RES_X = 1920
PLAY_RES_Y = 1080
FONT_SIZE = 54                 # 1080p 画布下的弹幕字号（danmaku2ass 常用档）
LANE_H = round(FONT_SIZE * 1.3)     # 车道行高（同 danmaku.js 的 1.3 系数）
LANE_TOP = 24                  # 顶部留白（避开视频顶部 UI）
LANE_BOTTOM = round(PLAY_RES_Y * 0.85)   # 弹幕区下界（底部留 15% 给字幕/控件）
SCROLL_SEC = 10.0              # 单条滚动全程时长（秒）
LANE_MARGIN = 24               # 同车道前后两条的最小间隙（px）
TEXT_MAX = 100                 # 单条弹幕限长（字符）

# 舰长等级 → 弹幕颜色（与前端 danmaku.js BDM_GUARD_COLOR 一致；ASS 为 BGR 序）
GUARD_ASS_COLOR = {1: "&H003CB0FF", 2: "&H00A57AFF", 3: "&H00FFA861"}
# 1: #ffb03c 金（总督）→ 3cb0ff；2: #ff7aa5 粉（提督）→ a57aff；
# 3: #61a8ff 蓝（舰长）→ ffa861

# 文本清洗：剥 ASS 覆写标签 {...}（防注入）、去换行、压连续空白
_TAG_RE = re.compile(r"\{[^{}]*\}")
_NEWLINE_RE = re.compile(r"[\r\n\t]+")


def clean_text(text: str) -> str:
    """弹幕文本清洗：剥 ``{}`` 标签 / 换行制表，限长 TEXT_MAX；空文本返回 ''。"""
    t = _NEWLINE_RE.sub(" ", _TAG_RE.sub("", str(text or ""))).strip()
    return t[:TEXT_MAX]


def _text_width(text: str) -> int:
    """粗估文本渲染宽度（px）：CJK 全角按 1×字号、其余按 0.55×字号。"""
    units = sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in text)
    return max(1, round(units * FONT_SIZE))


def _fmt_time(sec: float) -> str:
    """秒 → ASS 时间格式 ``H:MM:SS.cc``（厘秒两位）。"""
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _to_bgr(css_hex: str) -> str:
    """``#RRGGBB`` → ASS ``&H00BBGGRR``（不透明）。"""
    v = css_hex.lstrip("#")
    r, g, b = v[0:2], v[2:4], v[4:6]
    return f"&H00{b}{g}{r}".upper()


_STYLE_FONT = "Microsoft YaHei"   # Windows/macOS 通用中文字体（libass 缺字体时回退）

_HEADER = f"""[Script Info]
; 由 replay 回放站生成（与同名视频配合使用：播放器同名自动加载）
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Guard1,{_STYLE_FONT},{FONT_SIZE},{GUARD_ASS_COLOR[1]},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,7,20,20,20,1
Style: Guard2,{_STYLE_FONT},{FONT_SIZE},{GUARD_ASS_COLOR[2]},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,7,20,20,20,1
Style: Guard3,{_STYLE_FONT},{FONT_SIZE},{GUARD_ASS_COLOR[3]},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,7,20,20,20,1
Style: Scroll,{_STYLE_FONT},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,7,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(items: list[dict]) -> str:
    """弹幕 items（[{t: 秒, text, guard}]）→ ASS 文本。

    车道分配（danmaku2ass 经典算法，逐条顺序处理，t 须升序或乱序均可——
    内部按 t 排序保证入屏顺序）：新条可入某车道当且仅当——
      ① 前一条已完整进入屏幕（前条尾缘 x0 > PlayResX 之前），且
      ② 追尾安全：新条更慢（v_new ≤ v0）时前条尾已让出足够距离；
         新条更快时，在前条完全滚出（尾缘 < 0）前追不上。
    全忙则丢弃该条（密度限流，与 canvas 版同口径）。
    """
    rows = []
    lane_last: list[dict | None] = [None] * max(
        1, (LANE_BOTTOM - LANE_TOP) // LANE_H)
    for it in sorted(items or [], key=lambda x: x.get("t") or 0):
        t = it.get("t") or 0
        text = clean_text(it.get("text") or "")
        if not text:
            continue    # 纯表情弹幕（text 为空）不生成 Dialogue
        guard = int(it.get("guard") or 0)
        style = f"Guard{guard}" if guard in (1, 2, 3) else "Scroll"
        w = _text_width(text)
        speed = (PLAY_RES_X + w) / SCROLL_SEC          # px/s，越宽越快
        for i, prev in enumerate(lane_last):
            if prev is None:
                lane = i
                break
            dt = t - prev["t"]
            if dt <= 0:
                continue    # 同刻已占：换道
            head0 = PLAY_RES_X - prev["speed"] * dt    # 前条头缘当前 x
            tail0 = head0 - prev["w"]                  # 前条尾缘当前 x
            if tail0 > PLAY_RES_X - w - LANE_MARGIN:
                continue    # ① 前条尚未让出入屏位
            if speed > prev["speed"]:
                # ② 新条更快：需在前条尾滚出屏幕（tail0→0）前追不上
                #    追上时刻解 head_new(t') == tail0(t')：
                #    (PLAY_RES_X - speed*(t'-t)) == (PLAY_RES_X - v0*(t'-t0) - w0)
                #    化简后若在 tail0 到达 0 之前发生则不安全
                tail_out_t = t - tail0 / prev["speed"] if prev["speed"] else t
                catch_dt = (prev["w"] + w + LANE_MARGIN) / (speed - prev["speed"])
                if t + catch_dt < tail_out_t:
                    continue
            lane = i
            break
        else:
            continue    # 全忙：限流丢弃
        y = LANE_TOP + lane * LANE_H + FONT_SIZE // 2
        lane_last[lane] = {"t": t, "w": w, "speed": speed}
        move = (f"{{\\move({PLAY_RES_X},{y},{-w},{y},"
                f"{round(t * 1000)},{round((t + SCROLL_SEC) * 1000)})}}")
        rows.append(f"Dialogue: 0,{_fmt_time(t)},{_fmt_time(t + SCROLL_SEC)},"
                    f"{style},,0,0,0,,{move}{text}")
    return _HEADER + "\n".join(rows) + ("\n" if rows else "")
