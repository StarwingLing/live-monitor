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

命令行口径（biliup-rs Guide 核对，2026-10-09）：
- ``--user-cookie`` 是**全局参数**，须放在 ``upload`` 子命令之前；
- ``upload`` 子命令参数：``--title --desc --cover --tag --tid --copyright
  --source``（copyright：1 自制 / 2 转载，转载须填 source；tag 逗号分隔）。

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


def create_job(room_id: int, clip_id: int, title: str) -> dict:
    """注册一个投稿任务（uploading 态），返回任务行。"""
    _prune_jobs()
    job = {
        "id": uuid.uuid4().hex[:12], "room_id": room_id, "clip_id": clip_id,
        "title": title, "status": "uploading", "error": "", "bv": "",
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
    """投稿任务主体（异步编排：选源 → 封面 → biliup 子进程 → 解析 BV 号）。

    任何一步失败：任务标 failed 并透传可读原因（cookie 失效等由 biliup
    stderr 摘要呈现，提示重新上传 cookie）。临时文件（回拉视频/封面）在
    finally 统一清理。
    """
    tmp_files: list[Path] = []
    try:
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
            raise RuntimeError(why[:500])
        m = _BV_RE.search(out + " " + err)
        if not m:
            raise RuntimeError("投稿已提交但未能从输出解析到 BV 号")
        job.update(status="success", bv=m.group(0), done_ts=time.time())
    except Exception as e:              # noqa: BLE001 —— 任务失败原因全量透传
        job.update(status="failed",
                   error=str(e) or e.__class__.__name__, done_ts=time.time())
    finally:
        for f in tmp_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def _line_of_interest(text: str) -> str:
    """从 biliup 输出里挑一行「人类可读」的失败原因（Error/失败/拒绝等）。"""
    for line in reversed((text or "").strip().splitlines()):
        s = line.strip()
        if s and any(k in s.lower() for k in
                     ("error", "fail", "失败", "拒绝", "登录", "cookie", "超时")):
            return s
    return ""
