"""报告输出：分析结果对象 → JSON / Markdown 落盘。

JSON 供网页校对页消费（含逐帧分数），Markdown 供人直接阅读/复制时间戳；
两者同名成对：``<名>.analysis.json`` 与 ``<名>.analysis.md``。

``<名>`` 取「父目录名__文件主名」并在批量分析时保持唯一；
非法文件字符（Windows 保留字符等）替换为下划线。
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
from typing import Any

from songdetector import __version__

#: Windows 文件名非法字符 + 控制字符
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_stem(text: str) -> str:
    """清理输出文件主名中的非法字符（跨平台保守处理）。"""
    return _ILLEGAL_FS.sub("_", text).strip().rstrip(".")


def result_stem(path: pathlib.Path) -> str:
    """结果文件主名 = 源文件主名（清理非法字符）。

    replay 联动约定（2026-10-15）：replay 网页「下载low以进行识别」按钮
    落盘名 = ``<录播日期> <录播名> low.mp4``，本地识别结果随之以
    「录播日期+录播名」命名（如 ``2026-10-10 full_28 low.analysis.json``），
    在 replay 切片抽屉里按名对号。同名再分析即覆盖（同一场刷新结果）。
    """
    return sanitize_stem(path.stem)


def fmt_ts(seconds: float) -> str:
    """秒 → ``m:ss``（不足一小时）/ ``h:mm:ss``（满一小时），向下取整。

    例：59.5→"0:59"，61→"1:01"，3671→"1:01:11"。
    """
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def build_result(
    *,
    source_path: pathlib.Path,
    duration_sec: float,
    model_path: str,
    class_map_sha1: str,
    group_class_names: dict[str, list[str]],
    class_warnings: list[str],
    config: dict,
    frame_count: int,
    segments: dict[str, list[dict]],
    frame_scores: dict[str, list] | None,
) -> dict[str, Any]:
    """组装完整结果对象（即 JSON 的顶层结构）。

    段落数组在这里补上时间字符串字段（start_ts / end_ts / duration_ts），
    网页与 Markdown 用同一份格式化结果。
    """
    segs_out: dict[str, list[dict]] = {}
    for kind, segs in segments.items():
        rendered = []
        for seg in segs:
            item = dict(seg)
            item["kind"] = kind
            item["start_ts"] = fmt_ts(seg["start"])
            item["end_ts"] = fmt_ts(seg["end"])
            item["duration_ts"] = fmt_ts(seg["duration"])
            rendered.append(item)
        segs_out[kind] = rendered

    stat = source_path.stat()
    return {
        "schema_version": 1,
        # 输出文件主名（write_outputs 与网页按它定位产物文件）
        "stem": result_stem(source_path),
        "generator": {"name": "song-detector", "version": __version__},
        "analyzed_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "path": str(source_path),
            "file": source_path.name,
            "size_bytes": stat.st_size,
            "mtime": _dt.datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "duration_sec": duration_sec,
        },
        "model": {
            "path": model_path,
            "class_map_sha1": class_map_sha1,
            "class_warnings": class_warnings,
            "classes": group_class_names,
        },
        "config": config,
        "frames": {
            "count": frame_count,
            "hop_sec": 0.48,   # YAMNet 帧步长（16kHz 下 7680 样本）
            "window_sec": 0.96,  # 帧窗口长（15360 样本）
        },
        "segments": segs_out,
        "frame_scores": frame_scores,  # save_frame_scores=False 时为 None
    }


def _md_table(rows: list[dict]) -> list[str]:
    """段落列表 → Markdown 表格行（不含标题行）。

    唱歌尖峰占比（vocal_density）与说话占比（mean_speech）两列仅在段落
    携带该字段时渲染——音乐主导模式的歌曲段才有，用于人工判读
    「有人唱 / 纯伴奏」「是否说话贯穿」。
    """
    has_vocal = any("vocal_density" in r for r in rows)
    header = "| # | 起 | 止 | 时长 | 均分 |"
    sep = "|---|----|----|------|------|"
    if has_vocal:
        header += " 唱歌尖峰 | 说话 |"
        sep += "------|------|"
    header += " 峰值类 |"
    sep += "--------|"
    lines = [header, sep]
    for i, seg in enumerate(rows, start=1):
        row = (
            f"| {i} | {seg['start_ts']} | {seg['end_ts']} | {seg['duration_ts']} "
            f"| {seg['mean_score']:.2f} |"
        )
        if has_vocal:
            row += f" {seg['vocal_density']:.0%} | {seg['mean_speech']:.2f} |"
        row += f" {seg.get('peak_class', '')} |"
        lines.append(row)
    return lines


def render_markdown(result: dict) -> str:
    """结果对象 → 人读 Markdown 报告文本。"""
    src = result["source"]
    include_music = result["config"].get("output", {}).get("include_music", True)
    lines = [
        "# 唱歌/音乐段落分析报告",
        "",
        f"- 源文件：`{src['file']}`（{src['path']}）",
        f"- 文件时长：{fmt_ts(src['duration_sec'])}",
        f"- 分析时间：{result['analyzed_at']}（song-detector v{result['generator']['version']}）",
        f"- 关键参数：唱歌阈值 {result['config']['singing']['enter']}"
        f"/{result['config']['singing']['exit']}，"
        f"音乐阈值 {result['config']['music']['enter']}"
        f"/{result['config']['music']['exit']}，"
        f"合并间隔 {result['config']['merge_gap']}s，"
        f"最短段 {result['config']['min_duration']}s",
        "",
    ]
    singing = result["segments"]["singing"]
    lines.append(f"## 唱歌片段（{len(singing)}）")
    lines.append("")
    if singing:
        lines.extend(_md_table(singing))
    else:
        lines.append("未检出唱歌片段。")
    lines.append("")

    if include_music:
        music = result["segments"]["music"]
        lines.append(f"## 音乐/伴奏片段（{len(music)}）")
        lines.append("")
        if music:
            lines.extend(_md_table(music))
        else:
            lines.append("未检出音乐/伴奏片段。")
        lines.append("")
    else:
        lines.append("（音乐/伴奏段未包含：--no-music）")
        lines.append("")

    warnings = result["model"].get("class_warnings") or []
    if warnings:
        lines.append("## 分组警告")
        lines.append("")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")
    return "\n".join(lines)


def write_outputs(
    result: dict, out_dir: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    """把结果写成 ``<stem>.analysis.json`` 与 ``<stem>.analysis.md``。

    已存在则覆盖（重复分析即刷新）。返回两个产物路径。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = result["stem"]
    json_path = out_dir / f"{stem}.analysis.json"
    md_path = out_dir / f"{stem}.analysis.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path
