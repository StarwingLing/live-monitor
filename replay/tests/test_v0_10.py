# -*- coding: utf-8 -*-
"""v0.10 外挂弹幕时间轴核心测试（v2.2 迁入 replay 后精简版）。

保留覆盖：build_timeline 纯函数（单/双 run 映射、边界、无效名跳过）——
墙钟→视频内偏移的映射是整场录播外挂弹幕对齐的唯一信息源（replay 侧
recordings.timeline 落库与 /danmaku 接口消费；接口行为由 test_replay_api
覆盖）。随 v2.2 架构改版移除：缓冲保留期公式与双轨出片（高能功能整体
下线，buffer 子系统随之退役）。

运行：uv run python tests/test_v0_10.py
"""
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay import db as dbm
from replay.db import init_db

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    return ok


TMP = Path(tempfile.mkdtemp(prefix="rp_v010_test_"))
dbm.DB_PATH = TMP / "test.db"
dbm.DATA_DIR = TMP
init_db()

import replay.capture.recording as rec_mod
# RECORDINGS_ROOT 在 import 时绑定真实 data/，测试指向临时目录
rec_mod.RECORDINGS_ROOT = TMP / "recordings"


# ===========================================================================
# 1. build_timeline 纯函数
# ===========================================================================
def _seg(y, mo, d, h, mi, s) -> Path:
    return Path(f"seg_{y:04d}{mo:02d}{d:02d}T{h:02d}{mi:02d}{s:02d}.ts")


with mock.patch.object(rec_mod.Recorder, "_ffprobe_duration", return_value=10.0):
    # 单 run（连续分段，间隔 = segment_sec）
    tl = rec_mod.build_timeline([_seg(2026, 10, 4, 21, 39, 0),
                                 _seg(2026, 10, 4, 21, 39, 10),
                                 _seg(2026, 10, 4, 21, 39, 20)], segment_sec=10)
    check("timeline-单run结构", len(tl["runs"]) == 1 and tl["approx"] is False)
    r = tl["runs"][0]
    check("timeline-单run映射", r == {"wall": "2026-10-04T21:39:00", "off": 0.0,
                                      "dur": 30.0}, str(r))
    # 双 run（35s 缺口 = 断流重启；off 累计前 run 时长）
    tl2 = rec_mod.build_timeline([_seg(2026, 10, 4, 21, 39, 0),
                                  _seg(2026, 10, 4, 21, 39, 10),
                                  _seg(2026, 10, 4, 21, 39, 20),
                                  _seg(2026, 10, 4, 21, 39, 55)], segment_sec=10)
    check("timeline-双run切分", len(tl2["runs"]) == 2, str(tl2))
    r1, r2 = tl2["runs"]
    check("timeline-双run边界", r1["dur"] == 30.0 and r2["off"] == 30.0
          and r2["wall"] == "2026-10-04T21:39:55", f"{r1} {r2}")
    # 边界：segment_sec×1.5 整（15s 间隔=正常，不切）vs +1s（切）
    tl3 = rec_mod.build_timeline([_seg(2026, 10, 4, 21, 0, 0),
                                  _seg(2026, 10, 4, 21, 0, 15)], segment_sec=10)
    check("timeline-1.5倍内不切", len(tl3["runs"]) == 1)
    # 空列表 / 无效名
    check("timeline-空列表", rec_mod.build_timeline([])["runs"] == [])
    check("timeline-无效名跳过", rec_mod.build_timeline(
        [Path("garbage.ts"), _seg(2026, 10, 4, 21, 0, 0)])["runs"][0]["off"] == 0.0)


print(chr(10) + "==== test_v0_10（timeline 精简版）: " +
      ("全部通过" if all(results) else f"失败 {results.count(False)} 项") + " ====")
raise SystemExit(0 if all(results) else 1)
