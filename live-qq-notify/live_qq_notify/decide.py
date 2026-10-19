# -*- coding: utf-8 -*-
"""状态机决策(纯函数,无 IO,便于单测覆盖全部分支)。

输入为"上一轮已归一的状态 + 本轮查询结果 + 全局配置",输出本轮应执行的
动作;IO(查接口/发消息/落盘)全部在 main 循环里,规则集中在此。
"""
from dataclasses import dataclass


@dataclass
class Decision:
    action: str        # "none" | "notify_on" | "notify_off"
    new_status: int    # 本轮落库状态(0/1)
    new_since: str     # 本轮落库的状态进入时间(ISO)


class _SafeDict(dict):
    """模板 format 缺变量时保留占位符原样(如 {title}),不抛 KeyError。"""

    def __missing__(self, key):
        return "{" + key + "}"


def fmt_duration(seconds: float) -> str:
    """秒 → 人类可读时长(下播通知的"本次开播时长")。

    按分钟取整(轮询粒度 60s,更细无意义);不足 1 分钟按"不足 1 分钟"。
    """
    m = max(0, round(seconds / 60))
    if m < 1:
        return "不足 1 分钟"
    h, m = divmod(m, 60)
    if h < 1:
        return f"{m} 分钟"
    return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"


def render(template: str, uname: str, title: str, room_id: int,
           duration: str = "", cover: str = "") -> str:
    """渲染通知文案。可用变量:{uname} {title} {url}(url=直播间地址);
    {duration}=本次开播时长(下播文案变量);{cover}=封面图 CQ 码(开播
    文案变量,无封面时为空串)。"""
    return template.format_map(_SafeDict(
        uname=uname, title=title, url=f"https://live.bilibili.com/{room_id}",
        duration=duration, cover=cover))


def cq_escape(s: str) -> str:
    """CQ 码参数转义(OneBot 11 规范:& → &amp; 等,先转 & 再转方括号)。"""
    return (s.replace("&", "&amp;")
             .replace("[", "&#91;")
             .replace("]", "&#93;"))


def cq_image(url: str) -> str:
    """图片 URL → CQ 图片码(NapCat 收到后自行下载发送);空 URL 返回空串。"""
    return f"[CQ:image,file={cq_escape(url)}]" if url else ""


def online_text(template: str, uname: str, title: str, room_id: int,
                cover_url: str) -> str:
    """开播通知文本:渲染模板,{cover} 变量=封面图 CQ 码(2026-10-14 定稿:
    封面是纯模板变量,写在文案里才有——默认全局模板自带 `{cover}`,
    不想要图就从文案里删掉它;无封面 URL 时渲染为空行内容为空)。"""
    return render(template, uname, title, room_id, cover=cq_image(cover_url))


def offline_text(template: str, uname: str, title: str, room_id: int,
                 live_seconds: float) -> str:
    """下播通知文本:渲染模板,{duration} 变量=本次开播时长(2026-10-14 定稿:
    时长是纯模板变量,写在文案里才有——默认全局模板自带
    `⏱ 本次直播:{duration}`,不想要就从文案里删)。

    live_seconds = 本轮时间 - 进入直播状态时刻(state.since),含防抖
    窗口内的闪断期(闪断未翻转状态,以最初开播起算)。
    """
    return render(template, uname, title, room_id,
                  duration=fmt_duration(live_seconds))


def effective_template(room_tpl: str, global_tpl: str) -> str:
    """房间专属模板优先,空串回退全局模板。"""
    return room_tpl if room_tpl.strip() else global_tpl


def room_effective(rc: dict, global_cfg: dict) -> dict:
    """房间个人配置与全局合并(v0.3.0 机器人管理页主播卡片)。

    房间覆盖值(非 None)优先,否则回退全局;返回含
    notify_on / notify_off / report_offline(bool)与 debounce_offline(int)
    四键的 dict——decide/apply_debounce/报告图判定按房间取这一份生效值。
    文案模板的合并沿用 effective_template(空串=回退),不在此函数内。
    """
    return {
        "notify_on": (rc["notify_on"] if rc.get("notify_on") is not None
                      else bool(global_cfg.get("notify_on"))),
        "notify_off": (rc["notify_off"] if rc.get("notify_off") is not None
                       else bool(global_cfg.get("notify_off"))),
        "report_offline": (rc["report_offline"]
                           if rc.get("report_offline") is not None
                           else bool(global_cfg.get("report_offline"))),
        "debounce_offline": (rc["debounce_offline"]
                             if rc.get("debounce_offline") is not None
                             else int(global_cfg.get("debounce_offline", 300))),
    }


def decide(prev: dict | None, cur_status: int, now_iso: str,
           global_cfg: dict) -> Decision:
    """比对状态翻转,决定本轮动作。

    prev:state 里该房间的上一轮记录(None=进程首轮/新加入配置)
    cur_status:本轮 B 站接口归一状态(0/1;查询失败时调用方不进本函数)
    规则:
    - prev 为 None:首见只记录不发(固定行为——进程重启/房间新启用时
      无法得知此前是否已通知过,宁可漏发不可重发刷屏);
    - 未播→直播:发开播通知(全局 notify_on 关闭时静默);
    - 直播→未播:距开播不足 debounce_offline 秒时静默(防闪断刷屏),
      否则发下播通知(全局 notify_off 关闭时静默);
    - 同状态:无动作。
    """
    g = global_cfg
    if prev is None:
        return Decision("none", cur_status, now_iso)

    old = int(prev.get("status", 0))
    if cur_status == old:
        return Decision("none", cur_status, prev.get("since") or now_iso)

    if cur_status == 1:
        action = "notify_on" if g.get("notify_on") else "none"
        return Decision(action, 1, now_iso)

    # 下播:防抖窗口内静默,但状态照常落库(窗口起点=上次开播时间)
    action = "notify_off" if g.get("notify_off") else "none"
    return Decision(action, 0, now_iso)


def within_debounce(prev: dict, now_epoch: float, debounce_seconds: int) -> bool:
    """下播防抖判断:距"进入直播状态"的时间不足防抖窗口则视为闪断。

    prev["since"] 为 ISO 字符串;解析失败按"不在窗口"处理(宁可多发的
    分支只在时间信息缺失时出现,且仍受全局开关约束)。
    """
    if debounce_seconds <= 0 or not prev.get("since"):
        return False
    try:
        from datetime import datetime
        since = datetime.fromisoformat(prev["since"]).timestamp()
    except ValueError:
        return False
    return (now_epoch - since) < debounce_seconds


def apply_debounce(d: Decision, prev: dict | None, now_epoch: float,
                   debounce_seconds: int) -> Decision:
    """对下播判定施加防抖:窗口内的下播视为闪断——**既不通知也不翻转状态**。

    (2026-10-14 语义修正:此前只拦通知、状态照样翻转,导致"开播→闪断→
    复播"连发两条开播通知、零下播,防抖反而加重刷屏。修正后闪断期间
    状态保持直播中,复播 1→1 无动作,整个闪断周期只发最初一次开播;
    真下播则延后到窗口外的下一轮确认并通知。)

    返回替换后的 Decision:闪断时为保持 prev 状态的无动作;其余原样返回。
    """
    if (d.action == "notify_off" and prev is not None
            and within_debounce(prev, now_epoch, debounce_seconds)):
        return Decision("none", int(prev.get("status", 0)),
                        prev.get("since") or "")
    return d
