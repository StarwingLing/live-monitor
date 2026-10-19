"""分析编排：单文件 / 目录 全流程（probe → 抽音轨 → 推理 → 聚合 → 报告落盘）。

CLI（cli.py）与网页（webapp.py）共用 ``analyze_file`` / ``analyze_path``，
进度通过 ``progress_cb(stage, frac)`` 回调向上透传（CLI 打印百分比、网页更新进度条）。
"""

from __future__ import annotations

import hashlib
import pathlib
import tempfile
from typing import Callable, Union

import numpy as np

from songdetector import audio, model as model_mod
from songdetector import classes as classes_mod
from songdetector import report as report_mod
from songdetector import segments as segments_mod
from songdetector.config import RESULTS_DIR, load_config

#: 进度回调签名：(阶段描述, 0~1 完成度)
ProgressCB = Callable[[str, float], None]

#: 逐帧分数 JSON 精度（3 位小数：22k 帧 × 4 组约 270KB → 100KB 出头）
_SCORE_DECIMALS = 3


class AnalysisError(Exception):
    """分析失败（无音轨、音频过短、模型/ffmpeg 报错等），message 面向用户。"""


def analyze_file(
    path: pathlib.Path,
    cfg: dict | None = None,
    progress_cb: ProgressCB | None = None,
) -> tuple[dict, pathlib.Path, pathlib.Path]:
    """分析单个媒体文件，返回 ``(结果对象, json 路径, md 路径)``。

    :param cfg: 已加载配置（None 则用默认值）。
    :raises AnalysisError: 任一环节失败。
    """
    cfg = cfg or load_config()
    path = pathlib.Path(path)
    if not path.is_file():
        raise AnalysisError(f"文件不存在：{path}")

    def report_stage(stage: str, frac: float) -> None:
        if progress_cb is not None:
            progress_cb(stage, frac)

    # ---- 1) 探测：确认有音轨、拿总时长 ----
    report_stage("探测媒体", 0.02)
    try:
        info = audio.probe_media(path)
    except audio.AudioError as exc:
        raise AnalysisError(str(exc)) from exc
    if not info["has_audio"]:
        raise AnalysisError(f"文件没有音轨：{path.name}")
    if not info["duration"]:
        raise AnalysisError(f"无法确定媒体时长：{path.name}")

    # ---- 2) 抽音轨到临时目录（分析完随目录一起清理）----
    report_stage("抽取音轨", 0.06)
    with tempfile.TemporaryDirectory(prefix="songdetect_") as tmp:
        wav_path = pathlib.Path(tmp) / "audio16k.wav"
        try:
            audio.extract_wav(path, wav_path)
        except audio.AudioError as exc:
            raise AnalysisError(str(exc)) from exc

        # ---- 3) 加载模型（进程内单例，第二次起免加载）----
        report_stage("加载模型", 0.10)
        try:
            net = model_mod.load_model(cfg["model"]["path"])
        except RuntimeError as exc:
            raise AnalysisError(str(exc)) from exc

        # 类名表 + 分组（默认索引组已对照官方 CSV 核对，见 classes.py）
        csv_text = model_mod.class_map_csv_text(net)
        class_names = classes_mod.parse_class_map(csv_text)
        groups, class_warnings = classes_mod.build_groups(
            class_names, cfg.get("classes")
        )

        # ---- 4) 分块逐帧打分 + 组聚合 ----
        # 进度映射：推理占 0.10 ~ 0.92，之后聚合与写报告收尾
        issues: list[str] = []
        t_parts: list[np.ndarray] = []
        sg_parts, mu_parts, sp_parts = [], [], []
        sg_cls_parts, mu_cls_parts = [], []
        with audio.Wav16kReader(wav_path) as reader:
            def infer_progress(done: int, total: int) -> None:
                report_stage("逐帧打分", 0.10 + 0.82 * (done / max(total, 1)))

            for g, scores in model_mod.iter_frame_scores(
                net,
                reader,
                chunk_seconds=float(cfg["model"]["chunk_seconds"]),
                progress_cb=infer_progress,
                issues=issues,
            ):
                # 帧中心时间：第 k 帧全局帧号 g+i → (g+i+1)*0.48
                t_parts.append(
                    (np.arange(g, g + len(scores), dtype=np.float64) + 1.0) * 0.48
                )
                sg, sgc = classes_mod.aggregate_group(scores, groups["singing"])
                mu, muc = classes_mod.aggregate_group(scores, groups["music"])
                sp, _ = classes_mod.aggregate_group(scores, groups["speech"])
                sg_parts.append(sg)
                sg_cls_parts.append(sgc)
                mu_parts.append(mu)
                mu_cls_parts.append(muc)
                sp_parts.append(sp)

        if not t_parts:
            raise AnalysisError(f"音频过短（不足一个 0.96s 分析窗口）：{path.name}")

        times = np.concatenate(t_parts)
        singing = np.concatenate(sg_parts)
        music = np.concatenate(mu_parts)
        speech = np.concatenate(sp_parts)

        # ---- 5) 段落聚合 ----
        report_stage("聚合段落", 0.95)
        segments = segments_mod.build_segments(
            times,
            singing,
            np.concatenate(sg_cls_parts),
            music,
            np.concatenate(mu_cls_parts),
            speech,
            class_names,
            cfg,
        )

    # ---- 6) 组装结果并落盘 ----
    report_stage("写出报告", 0.98)
    frame_scores = None
    if cfg.get("save_frame_scores", True):
        frame_scores = {
            "t": [round(float(x), 2) for x in times],
            "singing": [round(float(x), _SCORE_DECIMALS) for x in singing],
            "music": [round(float(x), _SCORE_DECIMALS) for x in music],
            "speech": [round(float(x), _SCORE_DECIMALS) for x in speech],
        }
    result = report_mod.build_result(
        source_path=path,
        duration_sec=float(info["duration"]),
        model_path=str(cfg["model"]["path"]),
        class_map_sha1=hashlib.sha1(csv_text.encode("utf-8")).hexdigest()[:12],
        group_class_names={
            g: [class_names[i] for i in groups[g]] for g in groups
        },
        class_warnings=class_warnings + issues,
        config=cfg,
        frame_count=len(times),
        segments=segments,
        frame_scores=frame_scores,
    )
    out_dir = pathlib.Path(cfg.get("output", {}).get("dir") or RESULTS_DIR)
    json_path, md_path = report_mod.write_outputs(result, out_dir)
    report_stage("完成", 1.0)
    return result, json_path, md_path


def iter_media_files(
    directory: pathlib.Path, extensions: list[str]
) -> list[pathlib.Path]:
    """列出目录下（不递归）匹配扩展名的媒体文件，按文件名排序。"""
    exts = {e.lower().lstrip(".") for e in extensions}
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower().lstrip(".") in exts
    ]
    return sorted(files, key=lambda p: p.name.lower())


def analyze_path(
    path: Union[str, pathlib.Path],
    cfg: dict | None = None,
    progress_cb: ProgressCB | None = None,
) -> list[dict]:
    """分析文件或目录（目录 = 批量分析其中媒体文件）。

    :return: 逐文件结果列表，元素为
        ``{"path": ..., "ok": True, "result": ..., "json": ..., "md": ...}`` 或
        ``{"path": ..., "ok": False, "error": "原因"}``——单个文件失败不中断批量。
    """
    cfg = cfg or load_config()
    path = pathlib.Path(path)
    if path.is_dir():
        files = iter_media_files(path, cfg.get("media_extensions", []))
        if not files:
            return [{"path": path, "ok": False, "error": "目录中没有匹配的媒体文件"}]
    elif path.is_file():
        files = [path]
    else:
        return [{"path": path, "ok": False, "error": f"路径不存在：{path}"}]

    results: list[dict] = []
    total = len(files)
    for i, f in enumerate(files):
        base = i / total

        def wrap(stage: str, frac: float, _b=base, _i=i) -> None:
            # 目录批量时进度归一到整体：每文件占 1/total
            if progress_cb is not None:
                label = f"({ _i + 1}/{total}) {f.name} · {stage}"
                progress_cb(label, _b + frac / total)

        try:
            result, jp, mp = analyze_file(f, cfg, wrap)
            results.append(
                {"path": f, "ok": True, "result": result, "json": jp, "md": mp}
            )
        except AnalysisError as exc:
            results.append({"path": f, "ok": False, "error": str(exc)})
        except Exception as exc:  # 未预期异常也按单文件失败处理，不中断批量
            results.append({"path": f, "ok": False, "error": f"未预期错误：{exc!r}"})
    if progress_cb is not None:
        progress_cb("全部完成", 1.0)
    return results
