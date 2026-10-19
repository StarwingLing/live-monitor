# -*- coding: utf-8 -*-
"""状态机决策单测(decide/render/within_debounce 全分支,无 IO)。

运行:uv run python tests/test_state_machine.py
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import bilibili, decide  # noqa: E402

G = {"notify_on": True, "notify_off": True, "debounce_offline": 300,
     "template_on": "📺 {uname} 开播了:{title}\n{url}",
     "template_off": "🌙 {uname} 下播了"}

results = []


def check(name: str, ok: bool):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}")


# ---- get_info 解析(2026-10-14 修复:方法名须小写 get_info) ----
d = bilibili.parse_info({"live_status": 1, "title": "晚间歌回",
                         "keyframe": "https://x/kf.jpg",
                         "live_time": "2026-10-14 20:01:07", "uname": ""})
check("get_info 解析:在播+真实开播时间 ISO 化",
      d["status"] == 1 and d["live_time"] == "2026-10-14T20:01:07"
      and d["cover"] == "https://x/kf.jpg")
d = bilibili.parse_info({"live_status": 2, "live_time": "0000-00-00 00:00:00"})
check("get_info 解析:轮播归未播+无效时间置空",
      d["status"] == 0 and d["live_time"] == "")


# ---- 首见静默(重启/新启用不重发) ----
d = decide.decide(None, 1, "2026-10-13T12:00:00", G)
check("首见直播中→静默落库", d.action == "none" and d.new_status == 1)
d = decide.decide(None, 0, "2026-10-13T12:00:00", G)
check("首见未播→静默落库", d.action == "none" and d.new_status == 0)

# ---- 开播翻转 ----
prev = {"status": 0, "since": "2026-10-13T10:00:00"}
d = decide.decide(prev, 1, "2026-10-13T12:00:00", G)
check("未播→直播:发开播", d.action == "notify_on" and d.new_since == "2026-10-13T12:00:00")

# ---- 同状态无动作;since 保持(防抖窗口起点不被刷新) ----
prev_live = {"status": 1, "since": "2026-10-13T11:30:00"}
d = decide.decide(prev_live, 1, "2026-10-13T12:00:00", G)
check("持续直播:无动作且 since 不变", d.action == "none" and d.new_since == "2026-10-13T11:30:00")

# ---- 下播翻转(不在防抖窗口) ----
d = decide.decide(prev_live, 0, "2026-10-13T12:00:00", G)
check("直播→未播(过窗口):发下播", d.action == "notify_off")

# ---- 全局开关关闭 ----
g2 = {**G, "notify_on": False, "notify_off": False}
d = decide.decide(prev, 1, "2026-10-13T12:00:00", g2)
check("全局开播通知关闭:静默", d.action == "none")
d = decide.decide(prev_live, 0, "2026-10-13T12:00:00", g2)
check("全局下播通知关闭:静默", d.action == "none")

# ---- 轮播归一(B 站 live_status=2) ----
check("轮播(2)归一为未播", bilibili.normalize(2) == 0)
check("直播中(1)保持", bilibili.normalize(1) == 1)

# ---- 防抖窗口判定 ----
since = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
check("开播 60s 后下播:窗口内(300s)", decide.within_debounce(
    {"status": 1, "since": since}, time.time(), 300) is True)
since = (datetime.now() - timedelta(seconds=400)).isoformat(timespec="seconds")
check("开播 400s 后下播:窗口外", decide.within_debounce(
    {"status": 1, "since": since}, time.time(), 300) is False)
check("since 缺失:按窗口外", decide.within_debounce(
    {"status": 1}, time.time(), 300) is False)
check("防抖=0:恒窗口外", decide.within_debounce(
    {"status": 1, "since": since}, time.time(), 0) is False)

# ---- 模板渲染 ----
t = decide.render("{uname}|{title}|{url}|{nope}", "阿A", "晚间歌回", 12345)
check("渲染含未知占位符原样保留",
      t == "阿A|晚间歌回|https://live.bilibili.com/12345|{nope}")
check("专属模板优先", decide.effective_template("房间的", "全局的") == "房间的")
check("空专属回退全局", decide.effective_template("  ", "全局的") == "全局的")

# ---- 防抖语义修正(2026-10-14):窗口内下播不通知也不翻转状态 ----
since = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
prev_live = {"status": 1, "since": since}
raw = decide.decide(prev_live, 0, "2026-10-14T13:00:00", G)   # 60s 前开播→下播
d = decide.apply_debounce(raw, prev_live, time.time(), 300)
check("闪断:动作被拦且状态保持直播中",
      d.action == "none" and d.new_status == 1 and d.new_since == since)
# 复播:上一步状态保持 1 → 再查到直播中 → 1→1 无动作(不重发开播)
d2 = decide.decide({"status": 1, "since": since}, 1, "2026-10-14T13:00:10", G)
check("闪断后复播:无动作(不重发开播)", d2.action == "none")
# 窗口外下播:正常翻转+通知
since_old = (datetime.now() - timedelta(seconds=400)).isoformat(timespec="seconds")
raw = decide.decide({"status": 1, "since": since_old}, 0, "2026-10-14T13:00:00", G)
d = decide.apply_debounce(raw, {"status": 1, "since": since_old}, time.time(), 300)
check("窗口外下播:正常通知翻转", d.action == "notify_off" and d.new_status == 0)
# 防抖=0:立即确认下播
raw = decide.decide(prev_live, 0, "2026-10-14T13:00:00", G)
d = decide.apply_debounce(raw, prev_live, time.time(), 0)
check("防抖=0:立即确认下播", d.action == "notify_off")
# 全局下播通知关闭:防抖不参与,decide 已静默(apply 原样透传)
g3 = {**G, "notify_off": False}
raw = decide.decide(prev_live, 0, "2026-10-14T13:00:00", g3)
d = decide.apply_debounce(raw, prev_live, time.time(), 300)
check("全局下播关闭:静默透传(非防抖拦截)", d.action == "none" and d.new_status == 0)

# ---- 开播时长属性(2026-10-14 定稿:纯模板变量 {duration},写才有) ----
check("时长格式:分钟", decide.fmt_duration(5 * 60) == "5 分钟")
check("时长格式:小时+分钟", decide.fmt_duration(2 * 3600 + 3 * 60) == "2 小时 3 分钟")
check("时长格式:整小时", decide.fmt_duration(2 * 3600) == "2 小时")
check("时长格式:不足一分钟", decide.fmt_duration(30) == "不足 1 分钟")
t = decide.offline_text("🌙 {uname} 下播了\n⏱ 本次直播:{duration}", "阿A",
                        "晚间场", 123, 2 * 3600 + 3 * 60)
check("下播模板含 {duration} 渲染时长",
      t == "🌙 阿A 下播了\n⏱ 本次直播:2 小时 3 分钟")
t = decide.offline_text("🌙 {uname} 下播了", "阿A", "晚间场", 123, 5 * 60)
check("下播模板不含 {duration}:不带时长", t == "🌙 阿A 下播了")

# ---- 封面图属性(2026-10-14 定稿:纯模板变量 {cover},写才有) ----
check("CQ 转义(& 与方括号)",
      decide.cq_escape("a&b[1]c") == "a&amp;b&#91;1&#93;c")
cov = "https://i0.hdslb.com/bfs/live-key-frame/kf100.jpg?w=1&t=2"
t = decide.online_text("📺 {uname} 开播了:{title}\n{url}\n{cover}", "阿A",
                       "晚间场", 123, cov)
check("开播模板含 {cover} 渲染封面图(含 URL 转义)",
      t == "📺 阿A 开播了:晚间场\nhttps://live.bilibili.com/123\n"
           f"[CQ:image,file={decide.cq_escape(cov)}]")
t = decide.online_text("📺 {uname} 开播了:{title}", "阿A", "晚间场", 123, cov)
check("开播模板不含 {cover}:不带图", "[CQ:" not in t)
t = decide.online_text("📺 {uname} 开播了 {cover}", "阿A", "晚", 123, "")
check("模板含 {cover} 但无封面 URL:渲染为空", t == "📺 阿A 开播了 ")

# ---- room_effective:房间个人配置与全局合并(v0.3.0) ----
G = {"notify_on": True, "notify_off": True, "report_offline": False,
     "debounce_offline": 300}
check("合并-全 None 跟随全局", decide.room_effective(
    {"notify_on": None, "notify_off": None, "report_offline": None,
     "debounce_offline": None}, G)
    == {"notify_on": True, "notify_off": True, "report_offline": False,
        "debounce_offline": 300})
check("合并-覆盖值优先", decide.room_effective(
    {"notify_on": False, "notify_off": True, "report_offline": True,
     "debounce_offline": 60}, G)
    == {"notify_on": False, "notify_off": True, "report_offline": True,
        "debounce_offline": 60})
check("合并-混合(部分覆盖部分跟随)", decide.room_effective(
    {"notify_on": None, "notify_off": False, "report_offline": None,
     "debounce_offline": 0}, G)
    == {"notify_on": True, "notify_off": False, "report_offline": False,
        "debounce_offline": 0})
check("合并-全局缺键回退默认(防抖 300)", decide.room_effective(
    {"notify_on": None, "notify_off": None, "report_offline": None,
     "debounce_offline": None}, {})["debounce_offline"] == 300)

print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
