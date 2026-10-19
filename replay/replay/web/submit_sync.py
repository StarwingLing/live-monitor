# -*- coding: utf-8 -*-
"""投稿记录长周期同步（replay v2.3.0）：存在性核验 + 同名分P回填。

两个子任务（sync_once 一轮做完，web lifespan 内 daemon 线程周期执行，
默认 6h；管理页·账号管理也可手动触发）：

1. **存在性核验**（clip_submissions.alive）：逐条核对投稿记录在 B 站是否
   仍存在——mode=new 查视频本体（view 接口 -404 → 失效）；mode=part 查
   目标稿件存在 + 分P列表里是否仍有同名分P（稿件被删或分P被移除 → 失效）。
   失效记录 alive=0：回放页「已投稿」胶囊不再显示。核验网络/接口异常时
   保持原值不动（fail-open，不误杀）。

2. **同名分P回填**：历史手动上传的分P没有投稿链路记录（如 v2.3.0 之前
   传的三个歌切分P）——按「录播（recording）关联」补录：同录播下已有
   part 投稿记录 → 其目标稿件的分P列表 → 与本录播无记录切片**同名匹配**
   → 补一条 clip_submissions（season_note 注明回填来源）。同名即同片的
   口径与投稿动线一致（分P标题 = 切片名 = 上传文件名）。

并发口径：view/parts 查询走 ThreadPool（≤8 并发），与 _filter_existing_arcs
同款；单轮全量行数很小（个人账号量级），6h 周期对接口无压力。
"""
import concurrent.futures
import time
import urllib.request

from ..config import BILI
from ..db import connect, now_iso
from . import bili_api

SYNC_INTERVAL_SEC = float(BILI.get("submit_sync_interval_sec", 6 * 3600))


def _view_exists(bvid: str) -> bool | None:
    """公开 view 接口核验视频存在性：True/False；None = 网络/解析异常
    （调用方保持原值不动作）。"""
    req = urllib.request.Request(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers={"User-Agent": bili_api._UA})
    try:
        import json
        with urllib.request.urlopen(req, timeout=bili_api._TIMEOUT_SEC) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                   # noqa: BLE001
        return None
    code = d.get("code")
    if code == 0:
        return True
    if code == -404:
        return False
    return None     # 其他码（风控等）：不确定，不动


def _normalize_vid(v: str) -> str:
    """clip_submissions.bv 列统一为 BV 号形态（worker 回报可能是 av 数字串）。"""
    v = (v or "").strip()
    if v.isdigit() and v != "0":
        # av 数字 → 反查 BV：稿件列表里按 aid 匹配
        try:
            aid = int(v)
            for a in bili_api.fetch_arcs():
                if a["aid"] == aid:
                    return a["bvid"] or v
        except bili_api.BiliApiError:
            pass
    return v


def _verify_all(conn) -> dict:
    """① 存在性核验：更新 alive/checked_at，返回统计。异常行不动。"""
    rows = conn.execute("SELECT clip_id, bv, mode, title FROM clip_submissions"
                        ).fetchall()
    verified = dead = 0

    def check(row) -> tuple[int, bool | None]:
        bv = _normalize_vid(row["bv"])
        if not bv.startswith("BV"):
            return row["clip_id"], None
        ok = _view_exists(bv)
        if ok is False:
            return row["clip_id"], False
        if ok is None:
            return row["clip_id"], None
        if row["mode"] == "part":
            # 稿件在 → 再核同名分P仍在（分P可能被删/改名）
            try:
                aid = bili_api.resolve_aid(bv)
                parts = bili_api.fetch_archive_parts(aid)
            except bili_api.BiliApiError:
                return row["clip_id"], None
            return row["clip_id"], any(p["title"] == row["title"]
                                       for p in parts)
        return row["clip_id"], True

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check, rows))
    now = now_iso()
    for clip_id, alive in results:
        if alive is None:
            continue            # 不确定：本轮跳过（checked_at 也不动）
        verified += 1
        if not alive:
            dead += 1
        conn.execute("UPDATE clip_submissions SET alive = ?, checked_at = ? "
                     "WHERE clip_id = ?",
                     (1 if alive else 0, now, clip_id))
    return {"verified": verified, "dead": dead}


def _backfill(conn) -> dict:
    """② 同名分P回填：录播关联内，无记录切片 ← 目标稿件分P同名匹配。"""
    # 无投稿记录的 done 切片（有名字才有匹配意义）
    missing = conn.execute(
        "SELECT c.id, c.title, c.recording_id FROM manual_clips c "
        "LEFT JOIN clip_submissions s ON s.clip_id = c.id "
        "WHERE s.clip_id IS NULL AND c.status = 'done' AND c.title != ''"
    ).fetchall()
    if not missing:
        return {"backfilled": 0}
    # 每个录播的候选目标稿件（同录播已有 part 记录的 bv）
    targets_by_rec: dict[int, list[str]] = {}
    for r in conn.execute(
            "SELECT DISTINCT s.bv, c.recording_id FROM clip_submissions s "
            "JOIN manual_clips c ON c.id = s.clip_id "
            "WHERE s.mode = 'part'"):
        try:
            targets_by_rec.setdefault(r["recording_id"], []).append(r["bv"])
        except (KeyError, TypeError):
            continue
    # 目标稿件 → 分P列表（跨录播复用，一轮内每稿件只拉一次）
    parts_cache: dict[str, list[str]] = {}
    backfilled = 0
    now = now_iso()
    for row in missing:
        cands = targets_by_rec.get(row["recording_id"], [])
        for vid in cands:
            bv = _normalize_vid(vid)
            if not bv.startswith("BV"):
                continue
            if bv not in parts_cache:
                try:
                    aid = bili_api.resolve_aid(bv)
                    parts_cache[bv] = [p["title"]
                                       for p in bili_api.fetch_archive_parts(aid)]
                except bili_api.BiliApiError:
                    parts_cache[bv] = []      # 拉不到：跳过该稿件本轮
            if row["title"] in parts_cache[bv]:
                conn.execute(
                    "INSERT OR REPLACE INTO clip_submissions "
                    "(clip_id, bv, mode, title, submitted_at, season_note, "
                    "alive, checked_at) VALUES (?,?,?,?,?,?,1,?)",
                    (row["id"], bv, "part", row["title"], now,
                     "回填：同录播稿件同名分P匹配", now))
                backfilled += 1
                break
    return {"backfilled": backfilled}


def sync_once() -> dict:
    """一轮同步（核验 + 回填）。任何子步骤异常只打日志返回部分结果——
    周期任务不因单轮失败终止。"""
    out: dict = {"verified": 0, "dead": 0, "backfilled": 0}
    conn = connect()
    try:
        try:
            out.update(_verify_all(conn))
        except Exception as e:                  # noqa: BLE001
            print(f"[submit-sync] 核验异常（本轮跳过）：{e}", flush=True)
        try:
            out.update(_backfill(conn))
        except Exception as e:                  # noqa: BLE001
            print(f"[submit-sync] 回填异常（本轮跳过）：{e}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print(f"[submit-sync] 一轮完成：核验 {out['verified']} 条"
          f"（失效 {out['dead']}）· 回填 {out['backfilled']} 条", flush=True)
    return out


def _worker() -> None:
    """daemon 线程体：启动即跑一轮，此后按 SYNC_INTERVAL_SEC 周期执行。"""
    while True:
        try:
            sync_once()
        except Exception:                       # noqa: BLE001 —— 兜底不杀线程
            pass
        time.sleep(SYNC_INTERVAL_SEC)


def start() -> None:
    """启动同步线程（replay-web lifespan 调用；投稿功能未部署时不启动）。"""
    from . import upload as upload_mod
    if not upload_mod.submit_enabled():
        return
    import threading
    threading.Thread(target=_worker, daemon=True,
                     name="submit-sync").start()
