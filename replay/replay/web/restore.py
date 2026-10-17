# -*- coding: utf-8 -*-
"""录播原片云端回灌模块（replay v2.0.1：切片动线「本地已移除 → 一键拉回」）。

背景：打点切片/直播截取的快切读**本机原片**（capture 侧源文件缺失直接
failed），而录播原片按留存策略（v2.2 起零留存——录制+low 转码+双上云
完毕即释本地）后切片入口即失效。本模块把已上云的原件按 recordings 行
拉回本机，让释放后的场次重新可切：

- ``start(row)``：登记任务并起 daemon 线程，**内网 endpoint** 顺序 Range
  分段拉取（实测 ~70MiB/s，10GiB 原片约 2~3 分钟），写入
  ``<file>.restore-part``，完成后字节核对 + 原子改名 + mtime 重置为回灌
  时刻，并写 ``recordings.restored_at`` 标记；
- **回灌留存窗口独立计时（v2.2.1，用户 2026-10-13 定稿）**：释放分支对
  restored_at 非空的行按 ``[oss].restore_keep_hours``（默认 24h）评估，
  与 full_keep_hours 互不干扰——零留存（=0）不再误杀刚回灌的文件；
- **只拉原件**（row.oss_key），不拉低清副本——云端 low 已存在（oss_low_key），
  转码循环对「low 已上云」的录播跳过本地重转码，回灌后不会产生多余的
  本地 low；
- ``status(rec_id)``：任务快照（status/got/total/error），供前端进度条
  轮询。任务表是进程内会话态（同 upload.JOBS 口径）：重启丢内存态，
  半截 ``.restore-part`` 由下一轮触发整档重来（内网快，不做断点续传）。

线程安全：任务表 dict 的键值赋值在 GIL 下原子，快照用 dict(t) 浅拷贝；
同一 rec_id 同时只允许一个任务（running 时重复触发返回现快照）。

本地开发：[oss].enabled=false 时入口端点直接 503，本模块不会被调用；
测试通过重绑本模块的 RECORDINGS_ROOT / OSS 与替换 _run_download 驱动
状态机。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from ..config import get_config
from ..db import connect, now_iso
from ..capture.recording import RECORDINGS_ROOT   # 模块级常量：测试重绑同名属性即可隔离
OSS = get_config()["oss"]

CHUNK = 16 << 20            # 单段 Range 请求大小（16MiB：内网下请求开销可忽略）
PART_RETRY = 3              # 单段失败重试次数（瞬时网络抖动）

# 任务表：rec_id -> {status: running|done|failed, got, total, started_at,
# finished_at, error, file_path}（file_path 供前端核对目标）
TASKS: dict[int, dict[str, Any]] = {}


def status(rec_id: int) -> dict | None:
    """任务快照；无任务返回 None。"""
    t = TASKS.get(rec_id)
    return dict(t) if t else None


def start(row: dict) -> dict:
    """登记回灌任务并启动下载线程，返回任务快照。

    row：recordings 行（至少 id / file_path / oss_key）。
    - 本机已有成片 → 直接登记 done（幂等，前端立即收到完成态），
      同时补写 restored_at 标记（文件可能在零留存下待释放，点回灌
      即应获得 24h 窗口）；
    - running 中重复触发 → 返回现快照（不重复起线程）；
    - failed / 旧 done 后再触发 → 重新起线程整档拉取。
    """
    rid = int(row["id"])
    dst = RECORDINGS_ROOT / row["file_path"]
    if dst.exists():
        size = dst.stat().st_size
        _mark_restored(rid)
        TASKS[rid] = {"status": "done", "got": size, "total": size,
                      "started_at": time.time(),
                      "finished_at": time.time(), "error": "",
                      "file_path": row["file_path"]}
        return dict(TASKS[rid])
    cur = TASKS.get(rid)
    if cur and cur["status"] == "running":
        return dict(cur)
    task: dict[str, Any] = {"status": "running", "got": 0, "total": 0,
                            "started_at": time.time(), "finished_at": 0.0,
                            "error": "", "file_path": row["file_path"]}
    TASKS[rid] = task
    threading.Thread(target=_run, args=(dict(row), task), daemon=True,
                     name=f"oss-restore-{rid}").start()
    return dict(task)


# ---------------------------------------------------------------------------
# 下载执行（daemon 线程内；任何异常 → failed + 清理半截 part）
# ---------------------------------------------------------------------------
def _run(row: dict, task: dict) -> None:
    try:
        _finalize(row, _run_download(row, task))
        task["status"] = "done"
        task["finished_at"] = time.time()
    except Exception as e:   # noqa: BLE001 —— 线程兜底：任何失败都落任务表
        task["status"] = "failed"
        task["finished_at"] = time.time()
        task["error"] = str(e) or e.__class__.__name__
        # 半截 .restore-part 不留（重试是整档重来，留着只占盘）
        try:
            (RECORDINGS_ROOT
             / (row["file_path"] + ".restore-part")).unlink(missing_ok=True)
        except OSError:
            pass


def _client():
    """内网 endpoint OSS 客户端（endpoint 取 bili config 的内网地址，ECS
    同地域免流量费；构造同 app._oss_delete 但常驻复用——回灌期间连续
    Range 请求）。失败抛异常由 _run 落 failed。"""
    import alibabacloud_oss_v2 as oss
    o = OSS
    c = oss.config.load_default()
    c.region = o.get("region", "cn-hangzhou")
    c.endpoint = o.get("endpoint") or f"oss-{c.region}.aliyuncs.com"
    c.credentials_provider = oss.credentials.StaticCredentialsProvider(
        access_key_id=o["access_key_id"],
        access_key_secret=o["access_key_secret"])
    return oss.Client(c), o["bucket"]


def _run_download(row: dict, task: dict) -> int:
    """分段拉取主体：head 定大小 → 顺序 Range 写 .restore-part。

    返回对象总字节数（供 _finalize 核对与改名）。每完成一段更新
    task['got']（进度端点即时可见）。
    bili v2.1：原片按行 oss_bucket 分派（10 天热窗口在 mid 桶、归档后
    回主桶；旧快照缺列/空值回退主桶）。"""
    client, main_b = _client()
    import alibabacloud_oss_v2 as oss
    key = row["oss_key"]
    bucket = (row.get("oss_bucket") or "") or main_b
    meta = client.head_object(oss.models.object_basic.HeadObjectRequest(
        bucket=bucket, key=key))
    total = meta.content_length
    task["total"] = total
    part = RECORDINGS_ROOT / (row["file_path"] + ".restore-part")
    part.parent.mkdir(parents=True, exist_ok=True)
    got = 0
    with open(part, "wb") as f:
        while got < total:
            end = min(got + CHUNK, total) - 1
            data = b""
            for attempt in range(PART_RETRY):
                try:
                    r = client.get_object(
                        oss.models.object_basic.GetObjectRequest(
                            bucket=bucket, key=key,
                            range_header=f"bytes={got}-{end}"))
                    data = r.body.read()
                    if data:
                        break
                except Exception:   # 单段瞬时失败：小睡重试
                    if attempt == PART_RETRY - 1:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            if not data:
                raise IOError("云端返回空数据段")
            f.write(data)
            got += len(data)
            task["got"] = got
    if got != total:
        raise IOError(f"字节核对不符：拉取 {got} / 对象 {total}")
    return total


def _finalize(row: dict, total: int) -> None:
    """原子改名 + mtime 重置 + 写 restored_at 标记（回灌留存窗口起点）。"""
    part = RECORDINGS_ROOT / (row["file_path"] + ".restore-part")
    dst = RECORDINGS_ROOT / row["file_path"]
    if part.stat().st_size != total:
        raise IOError(f"落盘大小不符：{part.stat().st_size} / {total}")
    os.replace(part, dst)
    now = time.time()
    os.utime(dst, (now, now))
    _mark_restored(int(row["id"]))


def _mark_restored(rec_id: int) -> None:
    """写回灌标记 restored_at=now（v2.2.1）：释放分支据此启用独立的
    restore_keep_hours 窗口，full_keep_hours=0（上云即释）不再误杀刚
    回灌的文件。库写失败只打日志不抛——mtime 已重置，最坏退化为按
    full_keep_hours 评估的旧语义。"""
    try:
        conn = connect()
        try:
            conn.execute("UPDATE recordings SET restored_at = ? WHERE id = ?",
                         (now_iso(), rec_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 —— 标记失败不阻断回灌本体
        print(f"[restore] 写 restored_at 失败（rec #{rec_id}）：{e}")
