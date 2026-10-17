# -*- coding: utf-8 -*-
"""整场录播 OSS → 服务器本地回灌工具（v2.0.1；v2.2.1 补桶路由与回灌标记）。

场景：录播原片按零留存策略（录制 + low 转码 + 双上云完毕即释本地）后，
需要把某场**已上云的原画质文件**放回服务器本地——如切片截取要按原片
无损快切、或临时本机回看/兜底。

用法（服务器 /opt/replay 内）：

    uv run python deploy/restore_recording_from_oss.py <recording_id> [--with-low]

    <recording_id>  recordings 表行 id（回放页 URL / 数据库可查）
    --with-low      同时回灌低清副本（<stem>/low.mp4）。推荐带上：否则扫描
                    线程（web 服务，≤10 分钟一轮）发现本地有原片而无 low
                    会重新单线程转码一份（2C 小机 ~1 小时 CPU）；low 在云端
                    已有现成对象，内网拉回只要几十秒。

行为与约定：
- 读该行的 oss_key / oss_low_key，按行级桶路由取源桶（v2.1 语义：原片
  10 天热窗口在 mid 桶、归档后回主桶，low 恒 mid；行值空回退主桶），
  走内网 endpoint 分片断点下载（免流量费）；
- 先落 <file>.restore-part，完成后原子改名——扫描线程的转码/释放分支都按
  DB 行的精确 file_path 判断，看不到临时名，不会误处理半截文件；
- 下载后校验本地字节数 == OSS content_length，不符则删残件报错；
- mtime 刷新为回灌时刻并写 recordings.restored_at 标记（v2.2.1）：释放
  分支对回灌文件按 [oss].restore_keep_hours（默认 24h）独立计时回收，
  与 full_keep_hours 互不干扰——**keep=0（上云即释）下也可直接回灌**，
  不会再出现「回灌文件活不过下一轮扫描」的旧限制；
- 目标文件已存在时跳过下载（幂等，可重跑续灌；断点续传由 checkpoint
  承担），但同样补写 restored_at 刷新 24h 窗口。

只用标准库 + alibabacloud_oss_v2（服务器 venv 自带）；本地开发机 [oss]
enabled=false，本脚本仅在服务器上运行。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # /opt/replay
DATA_DIR = ROOT / "data"
RECORDINGS_ROOT = DATA_DIR / "recordings"


def _restore(client, bucket: str, key: str, dst: Path) -> bool:
    """回灌单个对象：head 校验 → 分片断点下载到 .restore-part → 原子改名。"""
    import alibabacloud_oss_v2 as oss
    if dst.exists():
        print(f"  跳过（本地已存在）：{dst.relative_to(ROOT)}")
        return True
    meta = client.head_object(oss.models.object_basic.HeadObjectRequest(
        bucket=bucket, key=key))
    expect = meta.content_length
    part = dst.with_name(dst.name + ".restore-part")
    part.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    dl = oss.Downloader(client)
    dl.download_file(
        oss.models.object_basic.GetObjectRequest(bucket=bucket, key=key),
        filepath=str(part), part_size=8 << 20, parallel_num=2,
        enable_checkpoint=True, checkpoint_dir=str(DATA_DIR / "oss_checkpoints"))
    got = part.stat().st_size
    if got != expect:
        part.unlink(missing_ok=True)
        print(f"  ✗ 大小不符（OSS {expect} / 本地 {got}），已删残件：{key}")
        return False
    os.replace(part, dst)          # 原子改名：扫描线程只见完整文件
    now = time.time()
    os.utime(dst, (now, now))      # mtime=回灌时刻，留存窗口从现在起算
    print(f"  ✓ {key} → {dst.relative_to(ROOT)}"
          f"（{expect / 2**30:.2f} GiB，{time.time() - t0:.0f}s）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把已上云的整场录播原片（可选低清副本）回灌到服务器本地")
    ap.add_argument("rec_id", type=int, help="recordings 表行 id")
    ap.add_argument("--with-low", action="store_true",
                    help="同时回灌低清副本 low.mp4（省去扫描线程重转码）")
    args = ap.parse_args()

    cfg = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["oss"]
    if not (cfg.get("enabled") and cfg.get("bucket")
            and cfg.get("access_key_id") and cfg.get("access_key_secret")):
        print("config [oss] 未启用或缺 bucket/AK，中止（本工具仅限服务器运行）")
        return 2

    # 只读连接共享库（WAL 多进程并存，与 web/capture 互不阻塞）
    db = sqlite3.connect(f"file:{DATA_DIR / 'live.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    row = db.execute(
        "SELECT id, room_id, file_path, oss_key, oss_low_key, oss_bucket, "
        "oss_low_bucket, status, started_at, ended_at FROM recordings "
        "WHERE id = ?",
        (args.rec_id,)).fetchone()
    db.close()
    if row is None:
        print(f"recordings 表无 id={args.rec_id} 行")
        return 2
    if not row["oss_key"]:
        print(f"id={args.rec_id}（房间 {row['room_id']} {row['started_at']}）"
              "无 oss_key——未上云，云端无可回灌")
        return 2

    # 客户端和 web/app.py::_oss_client(internal=True) 同款：内网 endpoint 免流量费
    import alibabacloud_oss_v2 as oss
    c = oss.config.load_default()
    c.region = cfg.get("region", "cn-hangzhou")
    c.endpoint = cfg["endpoint"]
    c.credentials_provider = oss.credentials.StaticCredentialsProvider(
        access_key_id=cfg["access_key_id"],
        access_key_secret=cfg["access_key_secret"])
    client = oss.Client(c)

    print(f"回灌 recordings#{row['id']} 房间 {row['room_id']} "
          f"{row['started_at']} ~ {row['ended_at']}（{row['status']}）：")
    # v2.1 行级桶路由：原片 10 天热窗口在 mid、归档后回主桶；low 恒 mid；
    # 行值空回退主桶（与本仓 web 侧 oss-url/删除联动同口径）
    orig_bucket = row["oss_bucket"] or cfg["bucket"]
    low_bucket = (row["oss_low_bucket"] or cfg.get("clips_bucket")
                  or cfg["bucket"])
    ok = _restore(client, orig_bucket, row["oss_key"],
                  RECORDINGS_ROOT / row["file_path"])
    if args.with_low:
        if row["oss_low_key"]:
            ok = _restore(client, low_bucket, row["oss_low_key"],
                          RECORDINGS_ROOT / (row["file_path"][:-4]
                                             + "/low.mp4")) and ok
        else:
            print("  （该行无 oss_low_key——云端无低清副本，跳过）")
    if ok:
        _mark_restored(row["id"])
    print("完成" if ok else "有失败项，可重跑续灌（断点 checkpoint 已保留）")
    return 0 if ok else 1


def _mark_restored(rec_id: int) -> None:
    """写 recordings.restored_at 标记（v2.2.1）：释放分支对回灌文件按
    [oss].restore_keep_hours（默认 24h）独立计时回收。短连接写一行，
    失败只提示不影响已落盘的文件。"""
    import datetime
    try:
        w = sqlite3.connect(DATA_DIR / "live.db", timeout=5)
        try:
            w.execute(
                "UPDATE recordings SET restored_at = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(timespec="seconds"),
                 rec_id))
            w.commit()
        finally:
            w.close()
        print(f"  已写回灌标记（本地按 restore_keep_hours 窗口留存，默认 24h）")
    except sqlite3.Error as e:
        print(f"  ⚠ 写 restored_at 失败（不影响文件）：{e}")


if __name__ == "__main__":
    sys.exit(main())
