# -*- coding: utf-8 -*-
"""每日维护任务（凌晨 04:00 触发一次，服务启动时也先跑一遍）：

1. 短间隔场次合并（v0.2.1）：下播→再开播间隔不足阈值的历史场次对合并为一场；
2. 用户分层重算：按配置窗口（默认 30 天）从明细表聚合每个用户的
   弹幕数 / 礼物金额 / 互动自然日数，重算 user_stats.tier 与 interact_days；
3. 明细保留清理：danmaku / interactions 超过保留期的删除；
4. WAL checkpoint：把 WAL 日志合并回主库文件，控制磁盘占用。

注意：分层全量重算会覆盖实时增量写入的 danmaku_count / gift_value_yuan，
口径从"累计值"对齐为"窗口内值"（页面展示的就是近 N 天数据，符合需求）。
"""
import asyncio
import datetime as dt
import shutil
import time

from ..config import DATA_DIR, get_config
from ..db import Db, now_iso


def _days_ago(days: int) -> str:
    """N 天前这一刻的 ISO8601 字符串（保留期/统计窗口的统一边界计算）。"""
    t = dt.datetime.now() - dt.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(ts: str) -> dt.datetime:
    """ISO8601 秒级时间串 → datetime（本模块写入的时间格式统一）。"""
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")


def merge_short_gap_sessions(db: Db) -> int:
    """把"下播→再开播"间隔不足阈值（config: capture.session_merge_gap_sec，
    默认 600 秒）的相邻两场合并为一场，返回合并的对数（无合并返回 0）。

    v0.2.1 需求：主播短时间内下播又复播（网络波动/换设备等被误判拆场）时，
    两场数据应视为同一场。运行时侧由 _open_or_adopt_session 直接沿用刚结束
    的场次（不产生拆场）；本函数负责回溯合并历史数据与服务停机窗口期产生
    的短间隔对，在每日维护里反复执行（幂等）。

    合并规则（保留前一场，吸收后一场）：
    - 明细表（danmaku/gifts/interactions/popularity_snapshots）的 session_id
      全部改指前一场；
    - 前一场 end_time 取后一场的 end_time（后一场仍在直播则为 NULL，
      即合并后继续直播）；
    - danmaku_count / gift_value_yuan 取两场冗余计数之和（不按明细重算——
      明细有保留期，重算会让老场次计数缩水）；峰值取两场最大值；
    - 删除后一场的 live_sessions 行（明细已迁移，外键安全）。
    """
    gap = float(get_config()["capture"].get("session_merge_gap_sec", 600))
    merged = 0
    room_ids = [r["room_id"] for r in db.query(
        "SELECT DISTINCT room_id FROM live_sessions ORDER BY room_id")]
    for rid in room_ids:
        sessions = db.query(
            """SELECT session_id, start_time, end_time, peak_popularity, peak_viewers,
                      danmaku_count, gift_value_yuan
               FROM live_sessions WHERE room_id = ? ORDER BY start_time, session_id""", (rid,))
        i = 0
        while i + 1 < len(sessions):
            prev, cur = sessions[i], sessions[i + 1]
            # 前一场必须已结束（正在直播的场次后面不该有下一场，防御性跳过）
            if not prev["end_time"] or not cur["start_time"]:
                i += 1
                continue
            gap_sec = (_parse_iso(cur["start_time"]) - _parse_iso(prev["end_time"])).total_seconds()
            if gap_sec > gap:
                i += 1
                continue
            # ---- 合并 cur → prev ----
            with db._lock:  # noqa: SLF001  一对合并一个事务
                conn = db.conn
                for table in ("danmaku", "gifts", "interactions",
                              "popularity_snapshots"):
                    conn.execute(f"UPDATE {table} SET session_id = ? WHERE session_id = ?",
                                 (prev["session_id"], cur["session_id"]))
                # v0.5：场次标题一并迁移。主键 (session_id, title)——两场若用过
                # 同一标题，UPDATE 撞主键：OR IGNORE 丢弃后场该行（仅损失后场
                # 该标题的时间段信息，影响可忽略），再删净后场残留行
                conn.execute(
                    "UPDATE OR IGNORE session_titles SET session_id = ? WHERE session_id = ?",
                    (prev["session_id"], cur["session_id"]))
                conn.execute("DELETE FROM session_titles WHERE session_id = ?",
                             (cur["session_id"],))
                conn.execute(
                    """UPDATE live_sessions SET
                           end_time = ?,
                           danmaku_count = danmaku_count + ?,
                           gift_value_yuan = round(gift_value_yuan + ?, 2),
                           peak_popularity = MAX(peak_popularity, ?),
                           peak_viewers = MAX(peak_viewers, ?)
                       WHERE session_id = ?""",
                    (cur["end_time"],  # 后一场仍在直播（NULL）则合并后继续直播
                     cur["danmaku_count"], cur["gift_value_yuan"],
                     cur["peak_popularity"], cur["peak_viewers"], prev["session_id"]))
                conn.execute("DELETE FROM live_sessions WHERE session_id = ?",
                             (cur["session_id"],))
                conn.commit()
            merged += 1
            print(f"[maintenance] 房间{rid} 场次 #{cur['session_id']} 并入 "
                  f"#{prev['session_id']}（间隔 {gap_sec:.0f}s ≤ {gap:.0f}s）")
            # prev 保留在列表里继续尝试与下一场合并（连环短间隔全并成一场）；
            # 字段同步为合并后的值，供下一轮间隔/峰值计算
            sessions[i] = {**prev, "end_time": cur["end_time"],
                           "danmaku_count": prev["danmaku_count"] + cur["danmaku_count"],
                           "gift_value_yuan": round(prev["gift_value_yuan"] + cur["gift_value_yuan"], 2),
                           "peak_popularity": max(prev["peak_popularity"], cur["peak_popularity"]),
                           "peak_viewers": max(prev["peak_viewers"], cur["peak_viewers"])}
            del sessions[i + 1]
    return merged


def run_daily_maintenance(db: Db) -> None:
    """同步执行全部维护步骤（在协程里用 to_thread 调用）。"""
    merge_short_gap_sessions(db)
    _unmask_history(db)
    _recompute_user_stats(db)
    _cleanup_details(db)
    _cleanup_recordings(db)   # v0.9 引入；v2.2.3 收缩为桥接清扫+切片超时+
                              # 紧急清理（保留期/水位自动删行已随定稿移除）
    with db._lock:  # noqa: SLF001
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.conn.commit()


def _unmask_history(db: Db) -> None:
    """用 uname_map 回填历史打码弹幕的真实身份。

    早期（或映射未命中时）写入的打码弹幕以 -crc32 作伪 uid；映射表里
    出现该用户后（高能榜/礼物来源），把历史行改写为真实 uid + 完整昵称，
    随后 _recompute_user_stats 重算会把同一人的计数自然合并。
    """
    import zlib
    mappings = db.query("SELECT uid, uname FROM uname_map")
    for m in mappings:
        pseudo = -zlib.crc32(str(m["uid"]).encode())
        db.execute(
            "UPDATE danmaku SET uid = ?, uname = ? WHERE uid = ? AND uname LIKE '%***%'",
            (m["uid"], m["uname"], pseudo))


def _recompute_user_stats(db: Db) -> None:
    """窗口内全量重算用户画像（计数 + 互动天数 + 分层）。

    用 SQL 直接从明细聚合，覆盖 user_stats 表：
    - danmaku_count / gift_value_yuan：窗口内合计；
    - interact_days：窗口内"出现过的自然日"数（弹幕/礼物/互动任一都算出现）；
    - 分层规则（v0.5 二次调整，config [tier] 可调）：
        核心 = 舰长/提督/总督（user_stats.guard_level>0 历史最高，
               或窗口内 gifts 有 type='guard' 购买记录，双保险），
               或场均送礼 ≥ core_gift_per_session_yuan
               （窗口内送礼总额 ÷ 参与场次数，分母 max(场次,1)；
               总额含场外收入，与营收口径一致）
        活跃 = 任一单场弹幕数 ≥ active_min_danmaku（默认 10 条）
        普通 = 其余
    first_seen / last_seen / guard_level 保留原值（不随窗口滚动）。
    本函数为分层权威口径（每日 04:00 + 服务启动各跑一遍）；writer 实时侧
    以"舰队身份 / 单场送礼达标"近似跟进（场均≥10 必有单场≥10，实时偏宽松，
    次日重算收敛），历史场次回顾页则按"本场送礼"计算核心。
    """
    cfg = get_config()["tier"]
    window_start = _days_ago(cfg["window_days"])
    core_avg = float(cfg.get("core_gift_per_session_yuan", 10.0))
    active_min = int(cfg.get("active_min_danmaku", 10))

    # 近窗窗口内用户活跃明细的聚合（弹幕、礼物、互动 UNION 成出现记录，
    # 按用户+房间分组；自然日数用 date(ts) 去重统计）。
    # 排除 uid=0：早期（uid_crc32 兜底上线前）的历史弹幕明细全部归在 0 号
    # 用户名下，属脏数据，不参与分层。
    # s 子查询：每用户"单场弹幕数的最大值"（活跃按单场口径判）；
    # sess 子查询：每用户参与场次数（弹幕/礼物任一出现过的 distinct 场次，
    #             场均送礼的分母）；g_guard：窗口内是否买过舰（核心双保险）；
    # us.guard_level：监控期最后已知舰队等级（实时侧跟随最新非零观测，v2.0.0 起；重算不覆盖）。
    sql = f"""
    WITH activity AS (
        SELECT room_id, uid, uname, date(ts) AS day FROM danmaku
          WHERE ts >= '{window_start}' AND uid != 0
        UNION ALL
        SELECT room_id, uid, uname, date(ts) FROM gifts
          WHERE ts >= '{window_start}' AND uid != 0
        UNION ALL
        SELECT room_id, uid, uname, date(ts) FROM interactions
          WHERE ts >= '{window_start}' AND uid != 0
    ), agg AS (
        SELECT room_id, uid, max(uname) AS uname,
               count(DISTINCT day) AS days
        FROM activity GROUP BY room_id, uid
    )
    SELECT a.room_id, a.uid, a.uname, a.days,
           coalesce(d.cnt, 0) AS danmaku_cnt,
           coalesce(gv.val, 0.0) AS gift_val,
           coalesce(s.max_per_session, 0) AS max_per_session,
           coalesce(sess.sessions, 0) AS sessions,
           CASE WHEN coalesce(g.guard_cnt, 0) > 0 THEN 1
                ELSE coalesce(us.guard_level, 0) END AS is_guard
    FROM agg a
    LEFT JOIN (SELECT room_id, uid, count(*) cnt FROM danmaku
                WHERE ts >= '{window_start}' AND uid != 0 GROUP BY room_id, uid) d
           ON d.room_id = a.room_id AND d.uid = a.uid
    LEFT JOIN (SELECT room_id, uid, sum(value_yuan) val, count(*) AS guard_cnt FROM gifts
                WHERE ts >= '{window_start}' AND uid != 0 AND type = 'guard' GROUP BY room_id, uid) g
           ON g.room_id = a.room_id AND g.uid = a.uid
    LEFT JOIN (SELECT room_id, uid, sum(value_yuan) val FROM gifts
                WHERE ts >= '{window_start}' AND uid != 0 GROUP BY room_id, uid) gv
           ON gv.room_id = a.room_id AND gv.uid = a.uid
    LEFT JOIN (SELECT room_id, uid, max(cnt) AS max_per_session FROM
                (SELECT room_id, uid, session_id, count(*) AS cnt FROM danmaku
                 WHERE ts >= '{window_start}' AND uid != 0 AND session_id IS NOT NULL
                 GROUP BY room_id, uid, session_id)
               GROUP BY room_id, uid) s
           ON s.room_id = a.room_id AND s.uid = a.uid
    LEFT JOIN (SELECT room_id, uid, count(DISTINCT session_id) AS sessions FROM (
                SELECT room_id, uid, session_id FROM danmaku
                  WHERE ts >= '{window_start}' AND uid != 0 AND session_id IS NOT NULL
                UNION
                SELECT room_id, uid, session_id FROM gifts
                  WHERE ts >= '{window_start}' AND uid != 0 AND session_id IS NOT NULL)
               GROUP BY room_id, uid) sess
           ON sess.room_id = a.room_id AND sess.uid = a.uid
    LEFT JOIN user_stats us ON us.room_id = a.room_id AND us.uid = a.uid
    """
    rows = db.query(sql)
    now = now_iso()
    with db._lock:  # noqa: SLF001
        for r in rows:
            # 场均送礼：总额 ÷ 参与场次数（分母至少 1，纯场外收入视作一场）
            avg_gift = r["gift_val"] / max(r["sessions"], 1)
            if r["is_guard"] > 0 or avg_gift >= core_avg:
                tier = "核心"      # 舰队成员，或场均送礼达标
            elif r["max_per_session"] >= active_min:
                tier = "活跃"      # 单场弹幕达标
            else:
                tier = "普通"
            db.conn.execute(
                """INSERT INTO user_stats (uid, room_id, uname, first_seen, last_seen,
                                           danmaku_count, gift_value_yuan, interact_days, tier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (uid, room_id) DO UPDATE SET
                       uname = CASE WHEN excluded.uname != '' THEN excluded.uname ELSE uname END,
                       danmaku_count = excluded.danmaku_count,
                       gift_value_yuan = excluded.gift_value_yuan,
                       interact_days = excluded.interact_days,
                       tier = excluded.tier""",
                (r["uid"], r["room_id"], r["uname"] or "",
                 # 首次见到该用户时用当前时间，已存在则保留原 first_seen
                 now, now, r["danmaku_cnt"], round(r["gift_val"], 2), r["days"], tier),
            )
        db.conn.commit()


def _cleanup_details(db: Db) -> None:
    """按保留期删除过期明细（0 = 永久保留）。"""
    ret = get_config()["retention"]
    if ret["danmaku_days"] > 0:
        db.execute("DELETE FROM danmaku WHERE ts < ?",
                   (_days_ago(int(ret["danmaku_days"])),))
    if ret["interaction_days"] > 0:
        db.execute("DELETE FROM interactions WHERE ts < ?",
                   (_days_ago(int(ret["interaction_days"])),))


# ---------------------------------------------------------------------------
# v0.9：录制产物清理（整场成片 + 手动切片）
# ---------------------------------------------------------------------------
def _unlink_rec_file(root, rel_path: str) -> None:
    """删除一条录制产物文件（file_path 为相对 data/recordings/ 的路径；
    文件缺失/路径异常只跳过，不中断清理）。"""
    if not rel_path:
        return
    try:
        (root / rel_path).unlink()
    except OSError:
        pass


def _cleanup_recordings(db: Db) -> None:
    """录制产物清理（v2.2.3 按留存策略定稿收缩）：

    v2.2.2 定稿「全部视频云端永久保存、无任何自动清理」后，v0.9 时代的两条
    自动删行路径整体移除——保留期（full_retention_days：超期删文件+删行）与
    总量水位（max_total_full_gb：从最旧删行至达标），两者均不看 oss_key，会把
    已上云的行连同回放入口一并删掉。2026-10-15 事故根因即水位路径：其总量按
    DB file_size 累计（本地零留存释放后行值仍在，统计的是历史录制量而非磁盘
    占用），10-14 插回的 #26/#28/#29 在下一轮小时清理里被再次删除。对应配置
    键 full_retention_days / max_total_full_gb 已自默认段与示例配置移除。

    现存清理仅三样（均不删已上云的行）：
    1. v2.1 桥接过渡目录兜底清扫（轮换中断残留的 bridge_<旧id>/ 目录，
       超 1 天即无主废件）；
    2. 手动切片 processing 超 1 天标 failed（capture 崩溃残留；容量水位
       v1.2 定稿默认 0=禁用）；
    3. 磁盘紧急清理（剩余 < emergency_free_gb 最后防线；v2.0.1 口径——
       已上云只删本地文件保留 DB 行，未上云才删行）。
    """
    root = DATA_DIR / "recordings"

    # ---- v2.1：桥接过渡目录兜底清扫 ----
    for staging in root.glob("*/bridge_*"):
        try:
            if time.time() - staging.stat().st_mtime > 86400:
                shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass

    # ---- v0.11 手动切片（超时标记 + 容量水位默认 0=禁用）----
    cleanup_manual_clips(db)
    # ---- v0.11 磁盘紧急清理（最后防线，v2.0.1 已上云保留行口径）----
    emergency_disk_cleanup(db)


def cleanup_manual_clips(db: Db) -> None:
    """手动切片清理：① processing 超 1 天标 failed（capture 崩溃残留）；
    ② done 总量超 max_total_manual_clips_gb 从最旧删至达标——v1.2 用户
    定稿默认 0=禁用（切片独立于录播/高能存续：云端随 OSS 生命周期管理，
    本地文件由上云+留存机制释放，DB 行不再因容量被自动删；磁盘安全由
    emergency_disk_cleanup 最后防线兜底）。pending 行不动（capture 未运行
    时等待，不判死）。"""
    rcfg = get_config()["recording"]
    root = DATA_DIR / "recordings"
    db.execute("UPDATE manual_clips SET status = 'failed', reason = '处理超时' "
               "WHERE status = 'processing' AND created_at < ?", (_days_ago(1),))
    cap = float(rcfg.get("max_total_manual_clips_gb", 0.0)) * 1e9
    if cap <= 0:
        return   # 预警线取消：不做容量删除
    total = (db.query("SELECT coalesce(sum(file_size), 0) AS s FROM manual_clips "
                      "WHERE status = 'done'")[0]["s"]) or 0
    if total > cap:
        rows = db.query(
            "SELECT id, file_path, file_size FROM manual_clips "
            "WHERE status = 'done' ORDER BY created_at ASC, id ASC")
        for r in rows:
            if total <= cap:
                break
            _unlink_rec_file(root, r["file_path"])
            total -= int(r["file_size"] or 0)
            db.execute("DELETE FROM manual_clips WHERE id = ?", (r["id"],))


def emergency_disk_cleanup(db: Db) -> None:
    """磁盘紧急清理：剩余空间 < emergency_free_gb（默认 5GB）时无视保留期
    与水位，从最旧开始删 finished/interrupted 整场成片，仍不足再删最旧
    切片。防长时间双路连录把 40G 盘顶爆（每日/每小时水位只看累计大小，
    看不到"当前剩余多少"；本函数是最后防线）。进行中的录制分段不可删。

    v2.0.1 云时代口径：**已上云（oss_key 非空）的行只删本地文件、保留
    DB 行**——与 web 释放分支同口径，回放页条目与切片挂载不丢（in_local
    自动转 false、播放走 OSS 签名，原片可由 replay 回灌气泡随时拉回）；
    未上云的行维持 v0.11 原逻辑删文件 + 删行（本地即唯一副本，清行防
    僵尸条目）。触发 48h 留存叠两晚场次时（40G 盘 ~10G/场）紧急阀不再
    误删页面行。腾出空间按**实际删除字节数**就地累计（原片 + <stem>/
    low 目录一起收）；文件已不在盘的候选跳过（无可释放）；候选一次性
    预取逐个处理，避免 while 重查同一条已释放行死循环。"""
    rcfg = get_config()["recording"]
    root = DATA_DIR / "recordings"
    free_min = float(rcfg.get("emergency_free_gb", 5.0)) * 1e9
    try:
        free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        return
    if free >= free_min:
        return
    print(f"[maintenance] 磁盘剩余 {free / 1e9:.1f}GB 低于 "
          f"{free_min / 1e9:.0f}GB，启动紧急清理")

    def _local_bytes(rel: str) -> int:
        """该条产物实际占用的本地字节数（原片 + <stem>/ low 目录）。"""
        total = 0
        try:
            total += (root / rel).stat().st_size
        except OSError:
            pass
        if rel.endswith(".mp4"):
            stem = root / rel[:-4]
            if stem.is_dir():
                for f in stem.rglob("*"):
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total

    # ---- 第一梯队：整场成片（最旧优先）----
    rows = db.query(
        "SELECT id, file_path, file_size, oss_key FROM recordings "
        "WHERE status IN ('finished', 'interrupted') AND file_path != '' "
        "ORDER BY ended_at ASC")
    for r in rows:
        if free >= free_min:
            break
        freed = _local_bytes(r["file_path"])
        if freed == 0:
            continue          # 本地已释放/已删：无可腾空间，跳过
        _unlink_rec_file(root, r["file_path"])
        if r["file_path"].endswith(".mp4"):
            shutil.rmtree(root / r["file_path"][:-4], ignore_errors=True)
        free += freed
        if r["oss_key"]:
            # 已上云：仅释本地文件，页面行保留（v2.0.1 口径）
            print(f"[maintenance] 紧急清理：释已上云整场 rec #{r['id']} "
                  f"本地文件 ({freed / 1e9:.2f}GB，保留行)")
        else:
            db.execute("DELETE FROM recordings WHERE id = ?", (r["id"],))
            print(f"[maintenance] 紧急清理：删整场 rec #{r['id']} "
                  f"({freed / 1e9:.2f}GB，未上云清行)")
    # ---- 第二梯队：切片（仍不足时，同口径）----
    rows = db.query(
        "SELECT id, file_path, file_size, oss_key FROM manual_clips "
        "WHERE status = 'done' AND file_path != '' "
        "ORDER BY created_at ASC")
    for r in rows:
        if free >= free_min:
            break
        freed = _local_bytes(r["file_path"])
        if freed == 0:
            continue
        _unlink_rec_file(root, r["file_path"])
        if r["file_path"].endswith(".mp4"):
            shutil.rmtree(root / r["file_path"][:-4], ignore_errors=True)
        free += freed
        if r["oss_key"]:
            print(f"[maintenance] 紧急清理：释已上云切片 #{r['id']} "
                  f"本地文件 ({freed / 1e9:.2f}GB，保留行)")
        else:
            db.execute("DELETE FROM manual_clips WHERE id = ?", (r["id"],))
            print(f"[maintenance] 紧急清理：删切片 #{r['id']} "
                  f"({freed / 1e9:.2f}GB，未上云清行)")


async def maintenance_loop(db: Db) -> None:
    """常驻协程：每天 04:00 执行一次维护；启动时先立即执行一轮。"""
    async def _once():
        try:
            await asyncio.to_thread(run_daily_maintenance, db)
        except Exception as e:  # 维护失败不退出服务，等下一轮
            print(f"[maintenance] 执行失败: {e!r}")

    await _once()
    while True:
        # 计算距下一个 04:00 的秒数
        now = dt.datetime.now()
        next_run = (now + dt.timedelta(days=1)).replace(
            hour=4, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        await _once()


async def hourly_recording_cleanup_loop(db: Db) -> None:
    """v0.11 常驻协程：每小时执行一次录制产物清理（桥接过渡目录清扫 +
    切片超时标记 + 切片容量水位（默认 0=禁用）+ 磁盘紧急检查）。

    v2.2.3 起**不再有保留期/总量水位的自动删行**（留存策略定稿：云端永久
    保存、入口不自动消失）；磁盘安全由紧急清理最后防线兜底（已上云只删
    本地文件、保留 DB 行）。启动时不立即执行（04:00 维护启动即跑的那轮
    已含本清理）。
    """
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(_cleanup_recordings, db)
        except Exception as e:
            print(f"[maintenance] 每小时录制清理失败: {e!r}")
