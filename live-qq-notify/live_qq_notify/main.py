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
5. 状态落盘(原子写)。

运行:uv run python -m live_qq_notify.main
"""
import time
from datetime import datetime

import httpx

from . import bilibili, decide, onebot, official, state, store
from .config import db_path, load_config, state_path


def log(level: str, msg: str) -> None:
    """统一日志格式(stdout 由 journalctl 接管)。"""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}",
          flush=True)


# 官方通道客户端进程级单例:token 缓存跨轮复用;凭据(AppID/Secret)变化时重建
_official_client: official.OfficialClient | None = None


def _get_official(g: dict) -> official.OfficialClient:
    """按全局配置取/重建官方通道客户端(页面改凭据后下一轮生效)。"""
    global _official_client
    app_id = str(g.get("official_app_id") or "")
    app_secret = str(g.get("official_app_secret") or "")
    if (_official_client is None
            or _official_client.app_id != app_id
            or _official_client.app_secret != app_secret):
        _official_client = official.OfficialClient(app_id, app_secret)
    return _official_client


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
    with httpx.Client(timeout=timeout, headers=bilibili.HEADERS) as client:
        while True:
            started = time.time()
            try:
                _pass(conn, client, st, api_base)
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
    """按全局 channel 探活(v0.2.0):official=token 可获取;napcat=登录在线。"""
    if str(g.get("channel") or "napcat") == "official":
        try:
            appid = _get_official(g).probe()
            return True, appid
        except official.OfficialError as e:
            log("ERROR", f"官方通道探活失败: {e}")
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


def _pass(conn, client: httpx.Client, st: dict, api_base: str) -> None:
    """单轮:读配置 → 探活 → 逐房间比对通知 → 更新状态(内存态)。"""
    global_cfg = store.load_global(conn)
    rooms = store.load_room_configs(conn)
    if not rooms:
        return

    # ---- 通道探活(整轮熔断开关;错过不补发,翻转另有当场等待) ----
    bot_ok, _ = _probe_channel(global_cfg)
    # official 通道:房间配置仍填 QQ 群号,发送前经映射表换 openid(v0.2.0)
    is_official = str(global_cfg.get("channel") or "napcat") == "official"
    group_map = store.load_group_map(conn) if is_official else {}

    now_iso = datetime.now().isoformat(timespec="seconds")
    now_ts = time.time()

    for room_id, rc in rooms.items():
        # ---- 查 B 站状态:失败沿用上轮状态(不翻转) ----
        try:
            cur = bilibili.fetch_room_status(client, room_id, api_base)
        except Exception as e:
            log("WARN", f"房间 {room_id} 状态查询失败,沿用上轮:"
                        f" {e}")
            continue

        prev = state.get_room(st, room_id)
        raw = decide.decide(prev, cur["status"], now_iso, global_cfg)
        # 下播防抖(2026-10-14 语义修正):窗口内的下播视为闪断——不通知
        # 也**不翻转状态**(保持直播中),复播 1→1 无动作,整个闪断周期只发
        # 最初一次开播;真下播延后到窗口外的下一轮确认
        d = decide.apply_debounce(raw, prev, now_ts,
                                  int(global_cfg["debounce_offline"]))
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
                send_ok = _wait_channel(global_cfg)
                if send_ok:
                    bot_ok = True   # 本轮后续房间复用
                else:
                    log("ERROR", f"房间 {room_id} {d.action} 通知放弃"
                                 "(发送通道掉线窗口未恢复,按约定不补发)")
            if send_ok:
                tpl = decide.effective_template(
                    rc["template_on"] if d.action == "notify_on" else rc["template_off"],
                    global_cfg["template_on"] if d.action == "notify_on" else global_cfg["template_off"])
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
                                    "(管理页「开播账号管理→群映射获取」),本轮跳过该群")
                        continue
                    targets.append(openid)
                for target in targets:
                    if _send(client, global_cfg, target, text, room_id, d.action,
                             cover_url):
                        log("INFO", f"房间 {room_id} {'开播' if d.action == 'notify_on' else '下播'}"
                                    f"通知已发群 {target}: {text.splitlines()[0][:60]}")

        if prev is None or d.new_status != int(prev.get("status", -1)):
            state.set_room(st, room_id, d.new_status, d.new_since)


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
                oc = _get_official(g)
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
                    _get_official(g).access_token(force=True)
                except official.OfficialError:
                    pass
            if attempt == 1:
                time.sleep(5)
                continue
            log("ERROR", f"房间 {room_id} {action} 通知发群 {target} 失败: {e}")
    return False


if __name__ == "__main__":
    run()
