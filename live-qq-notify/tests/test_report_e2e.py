# -*- coding: utf-8 -*-
"""下播报告图端到端联调(v0.3.0 阶段二:主循环接线后的本地装载验证)。

链路:mock NapCat(OneBot 3000)+ 仿 B站接口(恒未播)+ 临时库/临时 state
→ 启动真实通知器主进程 → 首轮即触发"下播翻转" → 断言 mock 收到:
①下播文本通知 ②紧随其后的 [CQ:image,file=file://...] 报告图消息,
且通知器日志含"报告图已发 1/1 群"。

主进程经环境变量指到临时资源(LQN_DB_PATH / LQN_API_BASE /
LQN_POLL_INTERVAL / LQN_STATE_PATH,见 config.load_config),不动真实
config.toml;报告图子进程会真实联网抓头像/表情原图(失败自动降级,不影响
断言)。运行:uv run python tests/test_report_e2e.py(需要本机 3000 端口空闲)。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ROOM = 9999
GROUP = 4242

results = []


def check(name: str, ok: bool, extra: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name} {extra}", flush=True)


class FakeBiliHandler(BaseHTTPRequestHandler):
    """仿 B站 get_info:任何房间一律未播(触发下播翻转)。"""

    def do_GET(self):
        body = json.dumps({"code": 0, "data": {
            "live_status": 0, "title": "测试场次", "uname": "",
            "keyframe": "", "user_cover": "", "cover": "",
            "live_time": "0000-00-00 00:00:00"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _wait_file(path: Path, needle: str, timeout_s: float) -> bool:
    """轮询等日志文件出现 needle。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def main() -> int:
    # 3000 被 mock NapCat 占用要求空闲
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 3000))
        except OSError:
            print("[失败] 本机 3000 端口被占用,mock NapCat 起不来(是否已有实例?)")
            return 1

    tmp = Path(tempfile.mkdtemp(prefix="lqn_e2e_"))
    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBiliHandler)
    fake_port = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    mock_log = tmp / "mock.log"
    main_log = tmp / "main.log"

    # ---- 临时库:房间/场次/弹幕 + 通知配置(走属主 DDL)----
    from live_qq_notify import store
    db = tmp / "live.db"
    conn = store.connect(str(db))
    store.ensure_tables(conn)
    conn.executescript("""
        CREATE TABLE rooms (room_id INTEGER PRIMARY KEY,
                            anchor_name TEXT NOT NULL DEFAULT '',
                            avatar_url TEXT NOT NULL DEFAULT '',
                            enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE live_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL, start_time TEXT NOT NULL,
            end_time TEXT, peak_viewers INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE danmaku (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER, room_id INTEGER NOT NULL,
            uid INTEGER NOT NULL, uname TEXT NOT NULL,
            privilege_type INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL, emots TEXT NOT NULL DEFAULT '');
    """)
    since = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO rooms VALUES (?, '端到端主播', '', 1)", (ROOM,))
    conn.execute(
        "INSERT INTO live_sessions (room_id, start_time, end_time, peak_viewers) "
        "VALUES (?, ?, ?, 66)",
        (ROOM,
         (datetime.fromisoformat(since) - timedelta(minutes=5)).isoformat(
             timespec="seconds"),
         (datetime.fromisoformat(since) + timedelta(minutes=90)).isoformat(
             timespec="seconds")))
    emots = ('{"[端到端_测试表情]": {"url": "https://i0.hdslb.com/bfs/live/'
             'e2589d086df0db8a7b5ca2b1273c02d31d4433d4.png", '
             '"width": 20, "height": 20}}')
    users = [(1, "用户甲", 8), (2, "用户乙", 6), (3, "用户丙", 4),
             (4, "用户丁", 3), (5, "用户戊", 3)]
    texts = ["好听", "打call", "[端到端_测试表情]", "来了来了", "哈哈哈哈哈哈"]
    n = 0
    for uid, uname, cnt in users:
        for i in range(cnt):
            t = texts[n % len(texts)]
            conn.execute(
                "INSERT INTO danmaku (session_id, room_id, uid, uname, "
                "privilege_type, text, emots) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (ROOM, uid, uname, 3 if uid == 2 else 0, t,
                 emots if t.startswith("[") else ""))
            n += 1
    conn.execute(
        "INSERT INTO qq_notify (room_id, enabled, group_ids) VALUES (?, 1, ?)",
        (ROOM, json.dumps([GROUP])))
    # v0.4.0 多机器人:通道/报告图开关在 qq_bots(旧全局表已冻结不读)
    conn.execute(
        "UPDATE qq_bots SET channel='napcat', report_offline=1, "
        "onebot_url='http://127.0.0.1:3000', onebot_token='' WHERE id=1")
    conn.commit()
    conn.close()

    # ---- 临时 state:该房间"直播中",首轮查询未播 → 下播翻转 ----
    (tmp / "state.json").write_text(
        json.dumps({"rooms": {str(ROOM): {"status": 1, "since": since}}}),
        encoding="utf-8")

    mock = None
    notifier = None
    try:
        env = {**os.environ,
               "LQN_DB_PATH": str(db),
               "LQN_API_BASE": f"http://127.0.0.1:{fake_port}",
               "LQN_POLL_INTERVAL": "2",
               "LQN_STATE_PATH": str(tmp / "state.json"),
               "PYTHONIOENCODING": "utf-8"}
        mock = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "mock_onebot.py")],
            stdout=mock_log.open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT, cwd=ROOT, env=env)
        time.sleep(1.5)   # 等 mock 双监听就绪
        notifier = subprocess.Popen(
            [sys.executable, "-m", "live_qq_notify.main"],
            stdout=main_log.open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT, cwd=ROOT, env=env)

        # 报告图子进程真实出图(联网抓头像,预算 15s)→ 总超时给足 120s
        ok_text = _wait_file(mock_log, "下播了", 60)
        check("下播文本通知已发 mock 群", ok_text)
        ok_img = _wait_file(mock_log, "[CQ:image,file=file://", 120)
        check("报告图消息紧随其后发出(CQ 本地图片)", ok_img)
        ok_log = _wait_file(main_log, "报告图已发 1/1 群", 15)
        check("通知器日志确认报告图 1/1 群", ok_log)
        if not (ok_text and ok_img and ok_log):
            tail = main_log.read_text(encoding="utf-8", errors="replace")[-1500:]
            print("---- 通知器日志尾 ----\n" + tail)
    finally:
        for p in (notifier, mock):
            if p is not None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} 项通过", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
