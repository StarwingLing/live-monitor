# -*- coding: utf-8 -*-
"""切片一键投稿 B 站（replay v2.0.0：biliup-rs 子进程集成，2026-10-09 定稿）。

调研结论（2026-10-08）：
- B 站无官方公开投稿 API，社区事实标准 = biliup-rs（Rust 单二进制，走
  创作中心 web 协议；协议变更需跟版本——部署时**版本锁定并记录于
  deploy/README**，升级 = 重新 scp 二进制，无依赖涟漪）；
- 服务器出向固定带宽 3 Mbps → 50MB 切片 ≈ 2~3 分钟、100MB ≈ 4~5 分钟，
  **必须异步任务 + 状态轮询**，不做同步等待；
- 集成形态：``asyncio.create_subprocess_exec`` 子进程调用（无常驻新服务、
  无 Python 侧上传实现）；任务首期 = 进程内会话态任务表（dict + 轮询端点，
  不持久化——共享库建表须 bili 侧做，留作后续扩展）。

命令行口径（biliup-rs Guide 核对，2026-10-09；append 命令 2026-10-19 源码复核）：
- ``--user-cookie`` 是**全局参数**，须放在 ``upload`` / ``append`` 子命令之前；
- ``upload`` 子命令参数：``--title --desc --cover --tag --tid --copyright
  --source``（copyright：1 自制 / 2 转载，转载须填 source；tag 逗号分隔）；
- ``append`` 子命令（v2.3.0）：``append --vid <稿件 av 或 BV 号> <视频文件>``，
  向已有稿件追加分P——原稿元数据由 biliup 先取远端再合并不会被覆盖，
  **分P标题 = 上传文件名去扩展名**（唯一命名入口，见 run_job part 分支）。

内存要点（bili v1.4 OOM 教训）：biliup/ffmpeg 子进程默认计入本服务
cgroup——部署时 replay-web MemoryMax 150M → 300M（或子进程改
``systemd-run --scope`` 独立跑，二选一，见 deploy/README）。
"""
import asyncio
import base64
import binascii
import re
import shutil
import time
import uuid
from pathlib import Path

from ..config import ROOT_DIR, BILI
from . import bili_api

# 投稿常用分区（B 站一级分区 tid，前端下拉数据源；投稿账号无权限的分区
# 由 B 站侧报错，任务透传原因）
TIDS: dict[int, str] = {
    4: "游戏", 171: "电子竞技", 172: "手机游戏", 65: "网络游戏",
    3: "音乐", 129: "舞蹈", 5: "娱乐", 160: "生活", 36: "知识",
    188: "科技", 234: "运动", 1: "动画", 181: "影视",
}

TITLE_MAX = 80          # B 站标题上限（字符）
DESC_MAX = 2000         # 简介上限
TAG_MAX = 10            # 标签条数上限（逗号分隔传入）
TAG_LEN_MAX = 24        # 单个标签长度上限

TMP_DIR = ROOT_DIR / "data" / "tmp"      # 临时工作目录（OSS 回拉 / 封面）

# ---------------------------------------------------------------------------
# 任务注册表（进程内会话态：不持久化，重启即清；定期修剪防膨胀）
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}
JOB_TTL_SEC = 2 * 3600    # 完成态任务保留 2h 供前端轮询收尾
MAX_JOBS = 50             # 注册表上限（超限先删最旧完成态）

# v2.3.0 同账号串行上传：服务器出向 3Mbps，多路 biliup 并发会互相饿死
# （2026-10-19 实测三路 200MB 并发全部卡死在 upos 上传中途）。串行后单任务
# 独占上行，200MB ≈ 9~10 分钟；排队任务等锁期间状态仍为 uploading。
MAX_UPLOADS = 1
_upload_sem = asyncio.Semaphore(MAX_UPLOADS)

# 本地投稿器认领窗口（v2.3.0）：part 任务先进入 pending 等本地投稿器
# （家庭宽带，225MB ≈ 40s）认领；超时未认领由服务器兜底执行（3Mbps 慢路径）
WAIT_WORKER_SEC = float(BILI.get("submit_worker_wait_sec", 25))


def biliup_bin() -> str:
    """biliup 二进制路径（config [bili].biliup_bin；空 = 功能未部署）。"""
    return str(BILI.get("biliup_bin", "") or "").strip()


def cookies_path() -> Path:
    """投稿账号 cookies.json 路径（config [bili].biliup_cookies，相对项目根）。"""
    p = str(BILI.get("biliup_cookies", "") or "").strip()
    return ROOT_DIR / (p or "data/biliup-cookies.json")


def submit_enabled() -> bool:
    """投稿功能可用性：二进制与 cookies 文件均在位。"""
    return bool(biliup_bin() and cookies_path().exists())


def _prune_jobs() -> None:
    now = time.time()
    for jid in [j for j, v in JOBS.items()
                if v["status"] in ("success", "failed")
                and now - v.get("done_ts", 0) > JOB_TTL_SEC]:
        JOBS.pop(jid, None)
    while len(JOBS) > MAX_JOBS:
        done = [j for j, v in JOBS.items() if v["status"] != "uploading"]
        if not done:
            break
        JOBS.pop(min(done, key=lambda j: JOBS[j]["started_ts"]), None)


def create_job(room_id: int, clip_id: int, title: str,
               mode: str = "new", pending: bool = False) -> dict:
    """注册一个投稿任务，返回任务行。

    v2.3.0：mode = new（新视频投稿，可带合集）/ part（追加为现有稿件分P）；
    pending=True 时任务先挂起等本地投稿器认领（status='pending'），超时由
    server_fallback 兜底；season_note 记录「投稿成功但加入合集失败」的补充
    说明（fail-soft）。
    """
    _prune_jobs()
    job = {
        "id": uuid.uuid4().hex[:12], "room_id": room_id, "clip_id": clip_id,
        "title": title, "mode": mode, "season_note": "",
        "status": "pending" if pending else "uploading",
        "claimed_by": "", "error": "", "bv": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "started_ts": time.time(), "done_ts": 0,
    }
    JOBS[job["id"]] = job
    return job


def get_job(job_id: str, room_id: int, clip_id: int) -> dict | None:
    """取任务（校验房间/切片归属，防跨房轮询他人任务）。"""
    j = JOBS.get(job_id)
    if j and j["room_id"] == room_id and j["clip_id"] == clip_id:
        return j
    return None


def pending_part_jobs() -> list[dict]:
    """本地投稿器队列：pending 的 part 任务（认领窗口内）。"""
    return [j for j in JOBS.values()
            if j["status"] == "pending" and j.get("mode") == "part"]


def claim_job(job_id: str) -> dict | None:
    """本地投稿器认领：仅 pending 可认领，认领即转 uploading（防兜底竞态）。"""
    j = JOBS.get(job_id)
    if not j or j["status"] != "pending":
        return None
    j["status"] = "uploading"
    j["claimed_by"] = "worker"
    return j


def complete_job(job_id: str, result: dict) -> bool:
    """本地投稿器回报结果：仅其认领（claimed_by=worker）且未结束的任务可报。"""
    j = JOBS.get(job_id)
    if not j or j["claimed_by"] != "worker" or j["status"] != "uploading":
        return False
    ok = bool(result.get("ok"))
    bv = str(result.get("bv", "")).strip()
    if ok:
        j.update(status="success", bv=bv, done_ts=time.time())
        _record_submission(j, bv, j.get("title", ""))
        print(f"[upload] job {j['id']} clip {j['clip_id']} "
              f"本地投稿器回报成功 → {bv}", flush=True)
    else:
        err = str(result.get("error", ""))[:500] or "本地投稿器未提供原因"
        j.update(status="failed", error=err, done_ts=time.time())
        print(f"[upload] job {j['id']} clip {j['clip_id']} "
              f"本地投稿器回报失败：{err}", flush=True)
    return True


async def server_fallback(job: dict, form: dict, clip: dict,
                          recordings_root: Path, sign, delay: float) -> None:
    """本地投稿器认领窗口兜底：delay 秒内无人认领则由服务器自己跑（3Mbps
    慢路径）。已被认领或已结束则不动作（与 claim 同进程事件循环，无真竞态）。"""
    await asyncio.sleep(delay)
    if job["status"] != "pending":
        return
    job["status"] = "uploading"
    job["claimed_by"] = "server"
    print(f"[upload] job {job['id']} clip {job['clip_id']} "
          "无本地投稿器认领，服务器兜底上传", flush=True)
    await run_job(job, clip, form, recordings_root, sign)


# ---------------------------------------------------------------------------
# 选源与封面
# ---------------------------------------------------------------------------
def pick_local_source(clip: dict, recordings_root: Path) -> Path | None:
    """选源：本地 file_path 在 → 直接用；返回 None 表示只剩 OSS。"""
    if clip.get("file_path") and (recordings_root / clip["file_path"]).exists():
        return recordings_root / clip["file_path"]
    return None


async def oss_pull(key: str, bucket: str, dst: Path, sign) -> Path:
    """仅云端切片：签名 URL 回拉到 data/tmp/ 再传、完删（sign 为 app 侧
    注入的签名函数 (key, bucket) -> url）。服务器入方向 200 Mbps，OSS 公网
    回拉不受 3 Mbps 出向限制。"""
    import urllib.request
    dst.parent.mkdir(parents=True, exist_ok=True)
    url = await sign(key, bucket)
    def _dl() -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "replay-upload"})
        with urllib.request.urlopen(req, timeout=600) as r, dst.open("wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
    await asyncio.to_thread(_dl)
    return dst


def extract_cover(video: Path, duration_sec: float, dst: Path) -> bool:
    """ffmpeg 从切片中点抽帧为封面（nice + 单线程，小机友好）。失败返回
    False（前端回落占位图，投稿任务允许无封面——B 站侧自动截帧）。"""
    try:
        mid = max(0.0, (float(duration_sec or 0)) / 2)
        import os
        import subprocess
        # nice 仅 POSIX（服务器 Linux 降载用；本地 Windows 调试直接跑）
        cmd = (["nice", "-n", "10"] if os.name == "posix" else []) + [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{mid:.2f}", "-i", str(video), "-frames:v", "1",
            "-vf", "scale=640:-2", "-threads", "1", str(dst)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except (OSError, ValueError):
        return False


def save_cover_b64(b64: str, dst: Path) -> bool:
    """前端上传的自定义封面（data URL / 裸 base64，PNG/JPEG）。非法或超大
    （>8MB）返回 False。"""
    try:
        payload = b64.split(",", 2)[-1]      # 兼容 "data:image/png;base64,..."
        raw = base64.b64decode(payload, validate=False)
        if not raw or len(raw) > 8 * 1024 * 1024:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        return True
    except (binascii.Error, ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 子进程投稿
# ---------------------------------------------------------------------------
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


async def run_job(job: dict, clip: dict, form: dict,
                  recordings_root: Path, sign) -> None:
    """投稿任务主体（异步编排：选源 → [封面] → biliup 子进程 → 解析 BV 号）。

    v2.3.0 起按 form["mode"] 分两支：
    - ``new``（新视频）：封面（自定义优先，失败不阻断）→ upload 子命令 →
      成功后若选了合集：bv2av 本地换算 aid → 取稿件分P cid → episodes/add
      加入合集（**fail-soft**：失败只写 job["season_note"] 提示手动处理，
      不改变投稿成功态）；
    - ``part``（追加为现有稿件分P）：不动封面（原稿封面沿用）；**分P标题 =
      上传文件名 stem**（biliup 唯一命名入口），源文件统一复制/回拉到任务
      专属临时目录并命名为「清洗后的分P名.mp4」，保证表单所见即所得；
      append 子命令（原稿元数据由 biliup 先取远端再合并，不会被覆盖）。

    任何一步失败：任务标 failed 并透传可读原因（cookie 失效等由 biliup
    stderr 摘要呈现，提示重新上传 cookie）。临时文件/目录在 finally 统一清理。
    """
    mode = form.get("mode", "new")
    tmp_files: list[Path] = []
    tmp_dirs: list[Path] = []
    # 串行闸：同账号同时只跑一个 biliup 子进程（3Mbps 上行并发互相饿死）
    if _upload_sem.locked():
        print(f"[upload] job {job['id']} clip {job['clip_id']} 排队等待"
              "（同账号串行上传）", flush=True)
    await _upload_sem.acquire()
    try:
        if mode == "part":
            # ---- 分P模式：上传文件名必须就是分P名 ----
            part_stem = bili_api.sanitize_part_name(form.get("part_title", ""))
            if not part_stem:
                raise RuntimeError("分P名清洗后为空，无法命名上传文件")
            workdir = TMP_DIR / f"submit_{job['id']}"
            workdir.mkdir(parents=True, exist_ok=True)
            tmp_dirs.append(workdir)
            target_file = workdir / f"{part_stem}.mp4"
            local = pick_local_source(clip, recordings_root)
            if local is None:
                if not clip.get("oss_key"):
                    raise RuntimeError("切片既无本地文件也无云端副本，无法投稿")
                await oss_pull(clip["oss_key"], clip.get("oss_bucket") or "",
                               target_file, sign)
            else:
                # 本地盘复制（不走网络），换取文件名 = 分P名的确定性
                await asyncio.to_thread(shutil.copyfile, local, target_file)
            cmd = [biliup_bin(), "--user-cookie", str(cookies_path()),
                   "append", "--vid", str(form.get("target_vid", "")),
                   str(target_file)]
        else:
            # ---- 新视频模式（v2.0.0 原流程）----
            src = pick_local_source(clip, recordings_root)
            if src is None:
                if not clip.get("oss_key"):
                    raise RuntimeError("切片既无本地文件也无云端副本，无法投稿")
                src = TMP_DIR / f"submit_{job['id']}_{Path(clip['file_path'] or 'x.mp4').name}"
                tmp_files.append(src)
                await oss_pull(clip["oss_key"], clip.get("oss_bucket") or "",
                               src, sign)

            # 封面：自定义上传优先，否则中点抽帧；两者都失败不阻断投稿
            cover = ""
            cov_dst = TMP_DIR / f"cover_{job['id']}.jpg"
            tmp_files.append(cov_dst)
            if form.get("cover_b64"):
                if save_cover_b64(form["cover_b64"], cov_dst):
                    cover = str(cov_dst)
            if not cover and extract_cover(src, clip.get("duration_sec"), cov_dst):
                cover = str(cov_dst)

            cmd = [biliup_bin(), "--user-cookie", str(cookies_path()),
                   "upload", str(src),
                   "--title", form["title"],
                   "--desc", form.get("desc", ""),
                   "--tag", form.get("tags", ""),
                   "--tid", str(int(form.get("tid", 171))),
                   "--copyright", str(1 if form.get("source_type", "original")
                                      == "original" else 2)]
            if form.get("source_type") == "reprint" and form.get("source_url"):
                cmd += ["--source", form["source_url"]]
            if cover:
                cmd += ["--cover", cover]

        timeout = float(BILI.get("biliup_timeout_sec", 1800))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"投稿超时（上限 {int(timeout / 60)} 分钟），已终止")
        out = (out_b or b"").decode("utf-8", "replace")
        err = (err_b or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            why = _line_of_interest(err) or _line_of_interest(out) or "biliup 退出码非 0"
            if "cookie" in why.lower() or "登录" in why or "429" in why:
                why += "（投稿凭据可能失效，请管理员重新上传 cookies.json）"
            # v2.3.0：失败原因落日志（任务表进程内 2h 过期即失忆，此前
            # 排查只能靠前端弹条；biliup 输出尾部一并打印定位失败步骤）
            print(f"[upload] job {job['id']} clip {job['clip_id']} "
                  f"mode={mode} biliup 失败：{why[:300]}", flush=True)
            print(f"[upload] biliup 输出尾部：{(err + ' | ' + out)[-1500:]}",
                  flush=True)
            raise RuntimeError(why[:500])
        m = _BV_RE.search(out + " " + err)
        if mode == "part":
            # append 输出可能不带 BV 号：回退用传入的目标稿件号展示
            vid = str(form.get("target_vid", ""))
            bv = m.group(0) if m else (vid if _BV_RE.fullmatch(vid) else "")
            job.update(status="success", bv=bv, done_ts=time.time())
            print(f"[upload] job {job['id']} clip {job['clip_id']} "
                  f"part 成功 → {bv}", flush=True)
            _record_submission(job, bv, form.get("part_title", ""))
        else:
            if not m:
                raise RuntimeError("投稿已提交但未能从输出解析到 BV 号")
            job.update(status="success", bv=m.group(0), done_ts=time.time())
            print(f"[upload] job {job['id']} clip {job['clip_id']} "
                  f"new 成功 → {m.group(0)}", flush=True)
            _record_submission(job, m.group(0), form.get("title", ""),
                               job.get("season_note", ""))
            # 新视频 + 合集：加入合集（fail-soft，失败不推翻投稿成功）
            if form.get("season_id") and form.get("section_id"):
                try:
                    aid = await asyncio.to_thread(bili_api.resolve_aid,
                                                  job["bv"])
                    parts = await asyncio.to_thread(
                        bili_api.fetch_archive_parts, aid)
                    if not parts:
                        raise RuntimeError("未取到稿件分P（稿件可能仍在处理）")
                    await asyncio.to_thread(
                        bili_api.season_add_video, aid, parts[0]["cid"],
                        form.get("title", ""), int(form["section_id"]))
                except Exception as se:      # noqa: BLE001 —— 合集失败仅记录
                    job["season_note"] = (f"视频已投稿（{job['bv']}），但加入"
                                          f"合集失败：{se}")[:300]
    except Exception as e:              # noqa: BLE001 —— 任务失败原因全量透传
        job.update(status="failed",
                   error=str(e) or e.__class__.__name__, done_ts=time.time())
        print(f"[upload] job {job['id']} clip {job['clip_id']} "
              f"mode={mode} 失败：{e}", flush=True)
    finally:
        _upload_sem.release()
        for f in tmp_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)


async def renew_login() -> str:
    """跑 ``biliup renew`` 刷新投稿登录（管理页·账号管理按钮）。

    renew 用 cookies.json 里的 refresh_token 续期并原地更新文件；成功返回
    输出摘要（最后一条非空日志行），失败抛 RuntimeError 带可读原因。

    v2.4.0：renew 会整体重写 cookie_info 且只保留会话键，把 buvid3/buvid4/
    bili_ticket 等设备指纹全部剥掉（实测 renew 后 web 接口 nav -101、
    创作中心 61000）。跑前先把原文件读进内存，成功后按「指纹取旧、会话
    键与 token 取新」合并写回（bili_api.merge_renewed_cookies）。
    """
    old_doc = None
    p = cookies_path()
    if p.exists():
        try:
            import json
            old_doc = json.loads(p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError, AttributeError):
            old_doc = None
    proc = await asyncio.create_subprocess_exec(
        biliup_bin(), "--user-cookie", str(p), "renew",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), 120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError("renew 超时（120 秒），已终止")
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        why = _line_of_interest(err) or _line_of_interest(out) \
            or "biliup renew 退出码非 0"
        raise RuntimeError(why[:300])
    if old_doc is not None:
        try:
            import json
            new_doc = json.loads(p.read_text("utf-8"))
            merged = bili_api.merge_renewed_cookies(old_doc, new_doc)
            p.write_text(json.dumps(merged, ensure_ascii=False), "utf-8")
            print(f"[upload] renew 后已合并设备指纹"
                  f"（指纹 {sum(1 for c in merged['cookie_info']['cookies']
                        if c['name'] not in bili_api._SESSION_COOKIE_KEYS)} 键"
                  f" + 会话 {sum(1 for c in merged['cookie_info']['cookies']
                        if c['name'] in bili_api._SESSION_COOKIE_KEYS)} 键）",
                  flush=True)
        except (json.JSONDecodeError, OSError, KeyError, AttributeError) as e:
            print(f"[upload] renew 后指纹合并失败（保留 renew 原文件）：{e}",
                  flush=True)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else "登录信息已刷新"


def _record_submission(job: dict, bv: str, title: str,
                       season_note: str = "") -> None:
    """投稿成功结果落库（clip_submissions）：前端刷新/服务重启后「已投稿」
    徽标仍可显示——任务表是进程内 2h 会话态，落库才是持久真源。写库失败
    只打日志不影响任务态（老库无表等防御场景）。"""
    try:
        from ..db import connect
        conn = connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO clip_submissions "
                "(clip_id, bv, mode, title, submitted_at, season_note) "
                "VALUES (?,?,?,?,?,?)",
                (job["clip_id"], bv, job.get("mode", "new"), title,
                 time.strftime("%Y-%m-%dT%H:%M:%S"), season_note))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:              # noqa: BLE001 —— 落库失败不推翻投稿成功
        print(f"[upload] 投稿结果落库失败：{e}", flush=True)


def _line_of_interest(text: str) -> str:
    """从 biliup 输出里挑一行「人类可读」的失败原因（Error/失败/拒绝等）。"""
    for line in reversed((text or "").strip().splitlines()):
        s = line.strip()
        if s and any(k in s.lower() for k in
                     ("error", "fail", "失败", "拒绝", "登录", "cookie", "超时")):
            return s
    return ""
