# -*- coding: utf-8 -*-
"""主循环(同步实现:逻辑为纯顺序轮询,asyncio 无收益反而增复杂)。

每轮流程:
1. 读共享库:全局配置 + 启用房间(开关且在监控列表内)——页面改配置
   最迟一个轮询周期生效,无需重启;
2. 探活发送通道(按全局 channel 分派,v0.2.0 双通道):napcat=get_login_info
   并校验登录号;official=AccessToken 可获取(凭据有效性)。不通则本轮
   **只更新状态不发**(整轮熔断,避免逐房间各重试拖死循环);
3. 逐房间查 B 站接口:失败沿用上轮状态(不翻转,不会误判下播);
4. decide() 判定翻转 → 渲染模板 → 逐目标群发送(单条失败重试 1 次;
   两通道房间配置统一填 QQ 群号,official 发送前经 qq_group_map 换 openid,
   未绑定群 WARN 跳过;official 开播封面图为独立一条图片消息,失败仅 WARN
   不影响文本);
   下播翻转且全局 report_offline 开(v0.3.0)→ 文本全部发出后,子进程生成
   本场直播报告图并紧随发到同批群(生成/发送失败仅 WARN,不影响文本;
   生成耗时约 10~30s,会同步阻塞本轮轮询,下播事件低频可接受);
5. 状态落盘(原子写)。

运行:uv run python -m live_qq_notify.main
"""
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import bilibili, decide, onebot, official, state, store
from .config import db_path, load_config, state_path


def log(level: str, msg: str) -> None:
    """统一日志格式(stdout 由 journalctl 接管)。"""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}",
          flush=True)


# 官方通道客户端按机器人缓存(v0.4.0 多机器人):token 各自独立,凭据变化时重建
_official_clients: dict[int, official.OfficialClient] = {}


def _get_official(bot_id: int, g: dict) -> official.OfficialClient:
    """按机器人配置取/重建官方通道客户端(页面改凭据后下一轮生效)。"""
    app_id = str(g.get("official_app_id") or "")
    app_secret = str(g.get("official_app_secret") or "")
    oc = _official_clients.get(int(bot_id))
    if (oc is None or oc.app_id != app_id or oc.app_secret != app_secret):
        oc = official.OfficialClient(app_id, app_secret)
        _official_clients[int(bot_id)] = oc
    return oc


def run() -> None:
    cfg = load_config()
    interval = int(cfg["poll"]["interval"])
    timeout = float(cfg["poll"]["timeout"])
    api_base = str(cfg["bilibili"]["api_base"])
    spath = state_path()

    # 首启建表(DDL 属主;bili 侧锁步副本幂等,先后无妨)
    conn = store.connect(str(db_path(cfg)))
    store.ensure_tables(conn)

    st = state.load(spath)
    log("INFO", f"live-qq-notify 启动:库={db_path(cfg)} 轮询={interval}s "
                f"api={api_base} state={spath}")
    if api_base != "https://api.live.bilibili.com":
        log("WARNING", f"api_base 已覆盖为 {api_base}(测试仿直播页?),"
                       "期间真实开播状态被遮蔽,测试完须还原")

    fail_streak = 0  # B 站连续整轮失败计数(达阈值打 WARNING)
    db_file = str(db_path(cfg))
    with httpx.Client(timeout=timeout, headers=bilibili.HEADERS) as client:
        while True:
            started = time.time()
            try:
                _pass(conn, client, st, api_base, db_file, cfg)
                fail_streak = 0
            except Exception as e:  # 单轮异常不退出进程(systemd 兜底之外的自愈)
                fail_streak += 1
                log("ERROR", f"轮询异常({fail_streak} 连): {e!r}")
                if fail_streak == int(cfg["poll"]["backoff_rounds"]):
                    log("WARNING", f"B 站/库访问已连续失败 {fail_streak} 轮,"
                                   "请检查网络与共享库路径")
            state.save(spath, st)
            # 睡满剩余间隔(异常轮也保持节奏,避免快速空转)
            cost = time.time() - started
            time.sleep(max(1.0, interval - cost))


def _probe_bot(g: dict) -> tuple[bool, str]:
    """NapCat 探活一次:返回 (在线?, 登录账号);顺带校验预留 QQ 号。"""
    probe = httpx.Client(timeout=3.0)
    try:
        info = onebot.get_login_info(probe, g["onebot_url"], g["onebot_token"])
        account = str(info["user_id"])
        expect = str(g.get("qq_account") or "").strip()
        if expect and expect != account:
            log("WARNING", f"NapCat 登录账号 {account} 与配置预留 QQ 号 "
                           f"{expect} 不一致,请检查是否登错号")
        return True, account
    except onebot.OneBotError as e:
        log("ERROR", f"{e}")
        return False, ""
    finally:
        probe.close()


def _probe_channel(g: dict) -> tuple[bool, str]:
    """按机器人 channel 探活(v0.4.0 多机器人;official=token 可获取;napcat=登录在线)。"""
    if str(g.get("channel") or "napcat") == "official":
        try:
            appid = _get_official(g["id"], g).probe()
            return True, appid
        except official.OfficialError as e:
            log("ERROR", f"机器人 {g.get('name') or g['id']} 官方通道探活失败: {e}")
            return False, ""
    return _probe_bot(g)


def _wait_channel(g: dict, tries: int = 6, gap: float = 10.0) -> bool:
    """翻转瞬间的掉线窗口收窄(2026-10-15 用户定稿:不补发,错过的放弃):

    有通知要发而通道不可用时,就地重试探活至多 tries 次——超时放弃
    (不补发、不重发)。两通道等待策略不同:
    - napcat:每 10s 一次至多 6 次(约 60s,覆盖密码自动重登时长);
    - official:无"登录态重连"概念,token/网络抖动秒级恢复,2 次×5s 即可。"""
    if str(g.get("channel") or "napcat") == "official":
        tries, gap = 2, 5.0
    for i in range(tries):
        time.sleep(gap)
        ok, _ = _probe_channel(g)
        if ok:
            log("INFO", f"发送通道已在等待窗口内恢复(约 {(i + 1) * gap:.0f}s),继续发送")
            return True
    return False


def _pass(conn, client: httpx.Client, st: dict, api_base: str,
          db_file: str, cfg: dict) -> None:
    """单轮:读机器人档案与房间 → 按机器人分组逐组发送 → 更新状态(内存态)。

    v0.4.0 多机器人:房间按 qq_notify.bot_id 归组(NULL=机器人1),每组用
    各自机器人的通道凭据/群映射/通知文案发送;B 站状态查询与状态机仍是
    房间维度,与机器人无关。
    """
    bots = store.load_bots(conn)
    rooms = store.load_room_configs(conn)
    if not rooms:
        return
    bot_by_id = {int(b["id"]): b for b in bots}

    for bot_id, bot in bot_by_id.items():
        bot_rooms = {rid: rc for rid, rc in rooms.items()
                     if rc["bot_id"] == bot_id}
        if not bot_rooms:
            continue   # 该机器人名下无启用房间,通道无需探活

        # ---- 通道探活(整组熔断开关;错过不补发,翻转另有当场等待) ----
        bot_ok, _ = _probe_channel(bot)
        # official 通道:房间配置仍填 QQ 群号,发送前经该机器人的映射表换 openid
        is_official = str(bot.get("channel") or "napcat") == "official"
        group_map = store.load_group_map(conn, bot_id) if is_official else {}

        now_iso = datetime.now().isoformat(timespec="seconds")
        now_ts = time.time()

        for room_id, rc in bot_rooms.items():
            # ---- 查 B 站状态:失败沿用上轮状态(不翻转) ----
            try:
                cur = bilibili.fetch_room_status(client, room_id, api_base)
            except Exception as e:
                log("WARN", f"房间 {room_id} 状态查询失败,沿用上轮:"
                            f" {e}")
                continue

            prev = state.get_room(st, room_id)
            # v0.3.0:开关/防抖/报告图按"房间个人配置覆盖,回退机器人默认"生效
            rc_eff = decide.room_effective(rc, bot)
            raw = decide.decide(prev, cur["status"], now_iso, rc_eff)
            # 下播防抖(2026-10-14 语义修正):窗口内的下播视为闪断——不通知
            # 也**不翻转状态**(保持直播中),复播 1→1 无动作,整个闪断周期只发
            # 最初一次开播;真下播延后到窗口外的下一轮确认
            d = decide.apply_debounce(raw, prev, now_ts,
                                      rc_eff["debounce_offline"])
            if raw.action == "notify_off" and d.action == "none":
                try:
                    dur = int(now_ts - datetime.fromisoformat(
                        prev.get("since") or now_iso).timestamp())
                    log("INFO", f"房间 {room_id} 开播后 {dur}s 即下播,"
                                f"防抖窗口内视为闪断(状态保持直播中,静默)")
                except ValueError:
                    pass

            # 进入直播态(首见或 0→1 翻转):since 取接口的真实开播时间
            # (live_time),防抖窗口与下播时长都按真实开播计——否则中途才开启
            # 通知的房间会把"首次观察到"当开播起点,短场下播会被防抖误判为
            # 闪断而漏发(2026-10-14 链路排查时发现)
            if d.new_status == 1 and cur.get("live_time"):
                d = decide.Decision(d.action, d.new_status, cur["live_time"])

            # 主播名:库值优先(get_info 的 uname 在播时为空)
            uname = rc.get("anchor_name") or cur.get("uname") or ""

            # ---- 发送 ----
            if d.action != "none":
                send_ok = bot_ok
                if not send_ok:
                    # 掉线窗口收窄:翻转当场等待通道恢复;超时放弃
                    # (不补发,状态照常落库防重复判定)
                    send_ok = _wait_channel(bot)
                    if send_ok:
                        bot_ok = True   # 本组后续房间复用
                    else:
                        log("ERROR", f"房间 {room_id} {d.action} 通知放弃"
                                     "(发送通道掉线窗口未恢复,按约定不补发)")
                if send_ok:
                    tpl = decide.effective_template(
                        rc["template_on"] if d.action == "notify_on" else rc["template_off"],
                        bot["template_on"] if d.action == "notify_on" else bot["template_off"])
                    cover_url = ""   # official 通道下要补发的封面图(文本之外单发)
                    if d.action == "notify_on":
                        if is_official:
                            # 官方通道:富媒体不能内联文本,{cover} 渲染为空;
                            # 模板写了 {cover} 且接口有封面 URL 时,文本发完补一条图片
                            cover_url = cur.get("cover", "") if "{cover}" in tpl else ""
                            text = decide.render(tpl, uname, cur["title"], room_id,
                                                 cover="")
                        else:
                            text = decide.online_text(tpl, uname, cur["title"],
                                                      room_id, cur.get("cover", ""))
                    else:
                        # 下播通知:带上本次开播时长(state.since 起算,含闪断期)
                        live_secs = 0.0
                        if prev is not None and prev.get("since"):
                            try:
                                live_secs = max(0.0, now_ts - datetime.fromisoformat(
                                    prev["since"]).timestamp())
                            except ValueError:
                                pass
                        text = decide.offline_text(tpl, uname, cur["title"],
                                                   room_id, live_secs)
                    targets: list[str] = []
                    for gid in rc["groups"]:
                        if not is_official:
                            targets.append(str(gid))
                            continue
                        openid = group_map.get(gid)
                        if not openid:
                            log("WARN", f"房间 {room_id} 群 {gid} 未绑定 openid"
                                        "(管理页「群映射获取」),本轮跳过该群")
                            continue
                        targets.append(openid)
                    sent_texts = 0
                    for target in targets:
                        if _send(client, bot, target, text, room_id, d.action,
                                 cover_url):
                            sent_texts += 1
                            log("INFO", f"房间 {room_id} {'开播' if d.action == 'notify_on' else '下播'}"
                                        f"通知已发群 {target}: {text.splitlines()[0][:60]}")
                    # ---- 下播报告图(v0.3.0):文本全部发出后紧随其后,同批群各发 ----
                    if (d.action == "notify_off" and sent_texts
                            and rc_eff["report_offline"]):
                        _send_report_images(client, bot, cfg, room_id,
                                            (prev or {}).get("since", ""),
                                            targets, db_file)

            if prev is None or d.new_status != int(prev.get("status", -1)):
                state.set_room(st, room_id, d.new_status, d.new_since)


# ---------------------------------------------------------------------------
# 下播报告图(v0.3.0):生成走子进程(与手动测试 CLI 同入口)——jieba/wordcloud
# 的词典与渲染内存只活在子进程里,不驻留通知器常驻 RSS;同理主循环不 import
# report 模块(其顶层 import PIL),场次定位由子进程 --room+--since 自行完成。
# ---------------------------------------------------------------------------
def _generate_report_png(db_file: str, room_id: int, since_iso: str,
                         timeout_s: int = 180) -> Path | None:
    """子进程生成报告图,成功返回 PNG 路径;失败/超时/找不到场次返回 None。"""
    fd, tmp = tempfile.mkstemp(prefix=f"report-{room_id}-", suffix=".png")
    os.close(fd)
    out = Path(tmp)
    cmd = [sys.executable, "-m", "live_qq_notify.report",
           "--db", db_file, "--room", str(room_id), "--since", since_iso,
           "--out", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_s,
                           cwd=str(Path(__file__).resolve().parents[1]))
    except subprocess.TimeoutExpired:
        log("WARN", f"房间 {room_id} 报告图生成超时({timeout_s}s),放弃")
        out.unlink(missing_ok=True)
        return None
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        log("WARN", f"房间 {room_id} 报告图生成失败(rc={r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[-160:]}")
        out.unlink(missing_ok=True)
        return None
    return out


def _send_report_images(client: httpx.Client, g: dict, cfg: dict, room_id: int,
                        since_iso: str, targets: list[str],
                        db_file: str) -> None:
    """下播文本已发出后:生成报告图并逐群发送;图属附属品,任何失败仅 WARN。

    两通道发送方式(v0.3.0 定稿):
    - official:群富媒体只收公网 URL(2026-10-18 实测 multipart 被拒
      40093007)→ 报告图经 OSS 私有桶中转:上传→30 分钟签名 URL→
      send_group_media_url(与开播封面同通道)→ 发完即删对象;
    - napcat:CQ 图片码本地文件路径,协议端读盘代发。
    """
    if not since_iso:
        log("WARN", f"房间 {room_id} 跳过报告图(开播起点缺失)")
        return
    started = time.time()
    png = _generate_report_png(db_file, room_id, since_iso)
    if png is None:
        log("WARN", f"房间 {room_id} 报告图未发出(生成失败,文本通知不受影响)")
        return
    is_official = str(g.get("channel") or "napcat") == "official"
    oss_key = ""
    try:
        if is_official:
            from . import oss
            oss_key = oss.report_key(room_id)
            url = oss.upload_and_sign(cfg, png, oss_key)
            sent = sum(1 for t in targets
                       if _send_official_image(g, t, url, room_id))
            oss.delete(cfg, oss_key)
        else:
            sent = sum(1 for t in targets
                       if _send_napcat_image(client, g, t, str(png), room_id))
        log("INFO", f"房间 {room_id} 报告图已发 {sent}/{len(targets)} 群"
                    f"(生成+发送 {time.time() - started:.1f}s)")
    except Exception as e:   # OSS 未配置/中转失败等:图缺不影响文本
        log("WARN", f"房间 {room_id} 报告图未发出: {e}")
        if oss_key:
            from . import oss
            oss.delete(cfg, oss_key)
    finally:
        png.unlink(missing_ok=True)


def _send_official_image(g: dict, target: str, url: str, room_id: int) -> bool:
    """official:签名 URL 直传(已验证通道)+ 一次重试(失败前强制刷新 token)。"""
    for attempt in (1, 2):
        try:
            _get_official(g["id"], g).send_group_media_url(target, url)
            return True
        except official.OfficialError as e:
            if attempt == 1:
                try:
                    _get_official(g["id"], g).access_token(force=True)
                except official.OfficialError:
                    pass
                time.sleep(5)
                continue
            log("ERROR", f"房间 {room_id} 报告图发群 {target} 失败: {e}")
    return False


def _send_napcat_image(client: httpx.Client, g: dict, target: str,
                       png_path: str, room_id: int) -> bool:
    """napcat:CQ 图片码本地文件路径 + 一次重试。"""
    for attempt in (1, 2):
        try:
            onebot.send_group_msg(
                client, g["onebot_url"], g["onebot_token"], int(target),
                f"[CQ:image,file=file://{decide.cq_escape(png_path)}]")
            return True
        except onebot.OneBotError as e:
            if attempt == 1:
                time.sleep(5)
                continue
            log("ERROR", f"房间 {room_id} 报告图发群 {target} 失败: {e}")
    return False


def _send(client: httpx.Client, g: dict, target: str, text: str,
          room_id: int, action: str, cover_url: str = "") -> bool:
    """单条发送 + 一次重试(间隔 5s);返回是否成功。

    按全局 channel 分派(v0.2.0):
    - napcat:target=QQ 群号,onebot.send_group_msg;
    - official:target=group_openid,先发文本;带 cover_url 时补发一条封面图
      (失败仅 WARN,不影响文本结果);重试前强制刷新 token(过期自愈)。
    仍失败记 ERROR 放弃(宁漏发不重发刷屏)。"""
    is_official = str(g.get("channel") or "napcat") == "official"
    for attempt in (1, 2):
        try:
            if is_official:
                oc = _get_official(g["id"], g)
                oc.send_group_text(target, text)
                if cover_url:
                    try:
                        oc.send_group_media_url(target, cover_url)
                    except official.OfficialError as e:
                        log("WARN", f"房间 {room_id} 封面图发送失败"
                                    f"(文本通知已发出,仅图缺): {e}")
            else:
                onebot.send_group_msg(client, g["onebot_url"], g["onebot_token"],
                                      int(target), text)
            return True
        except (onebot.OneBotError, official.OfficialError) as e:
            if is_official and attempt == 1:
                # 官方通道:失败可能为 token 临期,重试前强制刷新
                try:
                    _get_official(g["id"], g).access_token(force=True)
                except official.OfficialError:
                    pass
            if attempt == 1:
                time.sleep(5)
                continue
            log("ERROR", f"房间 {room_id} {action} 通知发群 {target} 失败: {e}")
    return False


if __name__ == "__main__":
    run()
