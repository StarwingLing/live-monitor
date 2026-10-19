# -*- coding: utf-8 -*-
"""本地投稿器（replay v2.3.0）：投稿上传的执行搬到家庭宽带机器。

服务器 3Mbps 出向是投稿上传的瓶颈（200MB ≈ 10 分钟，并发还会互相饿死）；
本机家庭宽带上同一文件实测 40 秒。分工：**服务器只调度与记账**——网页提交
的任务先进 pending 队列，本工具轮询认领后：从服务器拉取 biliup 凭据与
OSS 签名直链 → 本机下载切片 → 本机 biliup append 上传 → 回报结果（服务器
落 clip_submissions，回放页「已投稿」徽标照常显示）。认领窗口内无人认领
（本工具没开）时服务器会兜底自己传（慢路径），两端不冲突。

登录：复用回放站管理员账号（凭据每次轮询从服务器拉取，服务器侧 renew 后
本机自动同步，无需手工对齐）。

用法（在 replay 项目文件夹内）：
    首次：uv run python deploy/submit_worker.py --base http://<服务器IP>/replay \
            --user <管理员账号> --pass <密码>
    （首次登录成功即自动记忆账号到 data/worker-login.json，之后省略
    --user/--pass 直接运行；双击项目根目录的 submit_worker.bat 即可；
    biliup 二进制默认取项目内 data/biliup/biliup.exe。多任务自动本地
    排队逐个串行上传——认领与处理分离，处理期间新任务照常被认领。
    --once 轮询一轮并等队列处理完退出；--interval 3 轮询秒；
    --dry-run 联调不真传（每任务停 8s 模拟耗时）；--cookies-out 投稿
    凭据落盘路径，默认 data/biliup-worker-cookies.json）

安全：凭据仅在「本机 ↔ 自己的服务器」间流转，不经过任何第三方。
"""
import argparse
import base64
import getpass
import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay.web.bili_api import sanitize_part_name     # noqa: E402

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
DEFAULT_COOKIES = (Path(__file__).resolve().parent.parent
                   / "data" / "biliup-worker-cookies.json")
# 默认 biliup 位置（项目内标准路径）：--biliup 可不传——避免命令行里
# 反斜杠被 shell 转义（Git Bash 中 \b 变退格，路径被吃成
# data\biliup\biliup.exe → datiliuiliup.exe 的坑）
DEFAULT_BILIUP = (Path(__file__).resolve().parent.parent
                  / "data" / "biliup" / "biliup.exe")
LOGIN_FILE = (Path(__file__).resolve().parent.parent
              / "data" / "worker-login.json")


def _load_login() -> tuple[str, str] | None:
    """记忆的登录（base64 明码，仅本机私用文件；登录失败即清）。"""
    try:
        if LOGIN_FILE.exists():
            d = json.loads(LOGIN_FILE.read_text("utf-8"))
            return (base64.b64decode(d.get("u", "")).decode("utf-8"),
                    base64.b64decode(d.get("p", "")).decode("utf-8"))
    except Exception:                                   # noqa: BLE001
        LOGIN_FILE.unlink(missing_ok=True)
    return None


def _save_login(user: str, password: str) -> None:
    LOGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_FILE.write_text(json.dumps({
        "u": base64.b64encode(user.encode()).decode(),
        "p": base64.b64encode(password.encode()).decode()}), "utf-8")


def _resolve_path(p: Path) -> Path:
    """相对路径一律按「脚本所在项目根」解析为绝对路径——worker 可能在任意
    当前目录被启动（bat/计划任务/其他终端），相对路径依赖 cwd 会在执行
    biliup 时抛 [WinError 2] 系统找不到指定的文件。"""
    if p.is_absolute():
        return p
    return (Path(__file__).resolve().parent.parent / p).resolve()


class Worker:
    def __init__(self, base: str, user: str, password: str, biliup: str,
                 cookies_out: Path, dry_run: bool = False,
                 dry_delay: float = 0.0):
        self.base = base.rstrip("/")
        self.biliup = biliup
        self.cookies_out = cookies_out
        self.dry_run = dry_run
        self.dry_delay = dry_delay
        # 本地排队（v2.3.0）：认领（主线程轮询）与处理（独立线程串行）分离
        import queue as _queue
        self.q = _queue.Queue()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self._login(user, password)

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} \
            if body is not None else {}
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:                       # noqa: BLE001
                detail = {}
            return e.code, detail
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return -1, {"detail": f"网络失败：{e}"}

    def _login(self, user: str, password: str) -> None:
        st, d = self._req("POST", "/api/auth/login",
                          {"mode": "admin", "username": user,
                           "password": password})
        if st != 200:
            LOGIN_FILE.unlink(missing_ok=True)   # 记忆失效即清，下次重新输入
            raise SystemExit(
                f"登录失败（HTTP {st}）：{d.get('detail', d)}")
        print(f"已登录：{d.get('username', user)}")

    def _fetch_creds(self) -> None:
        st, d = self._req("GET", "/api/admin/submit/cookies-file")
        if st != 200:
            raise RuntimeError(
                f"取投稿凭据失败（HTTP {st}）：{d.get('detail', '')}")
        self.cookies_out.parent.mkdir(parents=True, exist_ok=True)
        self.cookies_out.write_text(d["cookies"], "utf-8")

    def _download(self, url: str, dst: Path) -> None:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=600) as r, \
                dst.open("wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  下载 {done / 1e6:.1f}/{total / 1e6:.1f} MB",
                          end="", flush=True)
            print()

    def _run_biliup(self, target_vid: str, video: Path):
        cmd = [self.biliup, "--user-cookie", str(self.cookies_out),
               "append", "--vid", target_vid, str(video)]
        if self.dry_run:
            print("  [dry-run] " + " ".join(cmd))
            return True, target_vid
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=1800)
        except FileNotFoundError:
            return False, (f"biliup 二进制找不到：{self.biliup}"
                           "（请确认 data/biliup/biliup.exe 存在）")
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        if p.returncode != 0:
            lines = [ln.strip() for ln in reversed((err + "\n" + out)
                                                   .strip().splitlines())
                     if ln.strip()]
            why = next((ln for ln in lines
                        if any(k in ln.lower() for k in
                               ("error", "fail", "失败", "拒绝", "登录",
                                "cookie", "超时"))),
                       lines[0] if lines else "biliup 退出码非 0")
            return False, why[:400]
        m = _BV_RE.search(out + " " + err)
        return True, m.group(0) if m else target_vid

    def _handle(self, payload: dict) -> None:
        """处理一个已认领的任务（处理线程串行调用；下载+上传+回报）。"""
        jid = payload["job"]
        part_title = payload.get("part_title") or \
            f"clip{payload.get('clip_id')}"
        work = self.cookies_out.parent / "worker-tmp" / jid
        work.mkdir(parents=True, exist_ok=True)
        dst = work / f"{sanitize_part_name(part_title)}.mp4"
        try:
            url = payload.get("oss_url") or ""
            if not url and not self.dry_run:
                raise RuntimeError("任务无 OSS 直链（切片未上云），无法本机下载")
            print(f"任务 {jid}：下载 {part_title}")
            if self.dry_run:
                print(f"  [dry-run] 跳过下载：{(url or '(调试环境无直链)')[:80]}")
                time.sleep(self.dry_delay)      # 模拟上传耗时（联调排队行为）
            else:
                self._download(url, dst)
            print(f"任务 {jid}：本机上传 "
                  f"（biliup append → {payload.get('target_vid')}）")
            ok, bv_or_err = self._run_biliup(
                payload.get("target_vid", ""), dst)
            self._req("POST", f"/api/submit/queue/{jid}/result",
                      {"ok": ok, "bv": bv_or_err if ok else "",
                       "error": "" if ok else bv_or_err})
            if ok:
                # v2.4.0：成功文案改为「<视频名>上传成功」+ 下行分隔线，
                # 与下载进度行形成清晰的段落边界
                print(f"{part_title}上传成功")
                print("-" * 40)
            else:
                print(f"任务 {jid}：失败 {bv_or_err}")
        except Exception as e:                      # noqa: BLE001
            print(f"任务 {jid} 异常：{e}")
            self._req("POST", f"/api/submit/queue/{jid}/result",
                      {"ok": False, "bv": "", "error": str(e)[:400]})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _process_loop(self) -> None:
        """处理线程体：从本地队列逐个取已认领任务串行执行（同账号上传
        本就要串行；排队期间任务已是 uploading 态，不受服务器兜底抢夺）。"""
        while True:
            payload = self.q.get()
            try:
                self._handle(payload)
            except Exception as e:                  # noqa: BLE001
                print(f"任务处理异常：{e}")
            finally:
                self.q.task_done()

    def poll_once(self) -> int:
        """轮询一轮：把队列里的 pending 任务**全部认领**入本地排队。
        认领与处理分离——处理耗时数分钟，若只在空闲时轮询，处理期间新
        提交的任务会错过 25s 认领窗口被服务器兜底抢走（即"第二个任务
        丢失"的根因）。返回本轮新认领数。"""
        self._fetch_creds()
        st, d = self._req("GET", "/api/submit/queue")
        if st != 200:
            raise RuntimeError(
                f"取队列失败（HTTP {st}）：{d.get('detail', '')}")
        claimed = 0
        for job in d.get("jobs", []):
            jid = job["job"]
            st2, payload = self._req("POST", f"/api/submit/queue/{jid}/claim")
            if st2 == 200:
                claimed += 1
                self.q.put(payload)
                print(f"已认领任务 {jid}（{payload.get('part_title')}）"
                      "，入本地队列")
            elif st2 == 409:
                pass        # 已被认领/兜底：跳过
            else:
                print(f"认领 {jid} 失败（HTTP {st2}）："
                      f"{payload.get('detail', '')}")
        if claimed:
            print(f"本轮认领 {claimed} 个，本地排队 {self.q.qsize()} 个待处理")
        return claimed


def main() -> None:
    ap = argparse.ArgumentParser(description="replay 本地投稿器")
    ap.add_argument("--base", default="http://127.0.0.1:8003",
                    help="回放站基址（服务器为 http(s)://<服务器IP>/replay）")
    ap.add_argument("--user", default="", help="管理员账号（首次必填，登录成功自动记忆）")
    ap.add_argument("--pass", dest="password", default="",
                    help="管理员密码（同 --user）")
    ap.add_argument("--biliup", default="",
                    help="本机 biliup 二进制路径（默认项目内 data/biliup/biliup.exe）")
    ap.add_argument("--cookies-out", default=str(DEFAULT_COOKIES))
    ap.add_argument("--interval", type=int, default=3, help="轮询间隔秒")
    ap.add_argument("--once", action="store_true",
                    help="轮询一轮并等本地队列处理完后退出")
    ap.add_argument("--dry-run", action="store_true",
                    help="跳过真实下载/上传（联调用；每任务停 8s 模拟耗时）")
    args = ap.parse_args()
    biliup = _resolve_path(Path(args.biliup or str(DEFAULT_BILIUP)))
    if not biliup.exists():
        print(f"[错误] biliup 二进制不存在：{biliup}")
        print("        （本地投稿需要 biliup.exe；见 deploy/README 第 8 章）")
        sys.exit(2)
    cookies_out = _resolve_path(Path(args.cookies_out))
    # 登录凭证：命令行 > 记忆文件 > 首次交互输入（并记忆）
    user, password = args.user, args.password
    if not user or not password:
        saved = _load_login()
        if saved:
            user, password = saved
            print("使用记忆的登录（data/worker-login.json）")
    if not user or not password:
        print("首次使用：请输入回放站管理员账号（仅本机保存记忆）")
        try:
            user = user or input("账号: ").strip()
            password = password or getpass.getpass("密码: ")
        except EOFError:
            print("[错误] 未收到输入（请直接双击 submit_worker.bat 运行，"
                  "或在命令行补 --user/--pass）")
            sys.exit(2)
    w = Worker(args.base, user, password, str(biliup), cookies_out,
               args.dry_run, dry_delay=8.0 if args.dry_run else 0.0)
    _save_login(user, password)
    import threading
    threading.Thread(target=w._process_loop, daemon=True,
                     name="worker-handle").start()
    print("投稿器已就绪（Ctrl+C 退出）；多任务自动排队，逐个本机上传")
    while True:
        try:
            w.poll_once()
        except KeyboardInterrupt:
            print("\n已退出")
            break
        except Exception as e:                      # noqa: BLE001
            print(f"轮询异常：{e}，10s 后重试")
            time.sleep(10)
            continue
        if args.once:
            w.q.join()          # 等本地队列全部处理完再退出
            print("本轮任务已全部处理完")
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
