"""YAMNet 模型加载与分块逐帧打分。

关键设计：

- **TFHub 缓存目录**：``TFHUB_CACHE_DIR`` 必须在 import tensorflow_hub 之前
  指向项目 ``models/`` 目录（本模块顶部完成），模型只下载一次、之后离线可用；
  网络不通时可手动下载模型压缩包解压到 ``models/`` 并把 config 的
  ``model.path`` 改为本地目录路径（``hub.load`` 兼容两种来源）。
- **懒加载 TF**：tensorflow 导入耗时数秒，模块顶层不 import，用到才进；
  CLI 的纯逻辑路径（如报告重渲染）不被拖慢。
- **进程内单例**：同一来源的模型只加载一次（批量分析/网页多任务复用）。
- **分块帧对齐推理**：
  - 16kHz 下帧窗口 = 15360 样本（0.96s）、步长 = 7680 样本（0.48s）；
  - 第 g 帧占用样本区间 ``[g*7680, g*7680+15360)``；
  - 每次从「下一待处理帧」的起点读一块（默认 60s），块内只取完整窗口的帧，
    块尾不足一窗的帧舍弃、由下一块覆盖——全程帧不重不漏；
  - 单块波形约 3.7MB，三小时音频也不会有内存压力。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from songdetector.config import DEFAULT_CONFIG, MODELS_DIR

# 必须先于 tensorflow_hub 的导入生效（故放模块顶部、且 tf 相关 import 全部懒加载）
os.environ.setdefault("TFHUB_CACHE_DIR", str(MODELS_DIR))

# YAMNet 帧几何（16kHz 采样率下的样本数）
WINDOW = 15360  # 0.96s 帧窗口
HOP = 7680      # 0.48s 帧步长
SAMPLE_RATE = 16000

# 进度回调签名：cb(done_frames: int, total_frames: int)
ProgressCB = Callable[[int, int], None]

#: 进程内模型单例缓存：{来源路径: 已加载模型}
_model_cache: dict[str, object] = {}


def load_model(path: str | None = None) -> object:
    """加载（或复用进程内已加载的）YAMNet SavedModel。

    :param path: TFHub URL 或本地目录；None 用 config 默认地址。
    :raises RuntimeError: 下载/加载失败时带排障指引（多为网络问题）。
    """
    source = path or DEFAULT_CONFIG["model"]["path"]
    if source in _model_cache:
        return _model_cache[source]
    try:
        import tensorflow as tf  # noqa: F401 — hub 依赖 tf 运行时
        import tensorflow_hub as hub
    except ImportError as exc:  # 环境异常（理论少见）：给出明确指引
        raise RuntimeError(f"tensorflow / tensorflow-hub 未安装或损坏：{exc}") from exc
    try:
        model = hub.load(source)
    except Exception as exc:
        raise RuntimeError(
            f"YAMNet 模型加载失败（{source}）：{exc}\n"
            f"常见原因是网络不通。可手动下载模型压缩包解压到 {MODELS_DIR} 下，"
            "再把 config.toml 的 model.path 改为该本地目录路径。"
        ) from exc
    _model_cache[source] = model
    return model


def class_map_csv_text(model: object) -> str:
    """读取模型自带的 class map CSV 文本（类名索引表，随模型离线可用）。"""
    raw_path = model.class_map_path().numpy().decode("utf-8")
    return Path(raw_path).read_text(encoding="utf-8")


def iter_frame_scores(
    model: object,
    reader: object,
    chunk_seconds: float = 60.0,
    progress_cb: ProgressCB | None = None,
    issues: list[str] | None = None,
) -> Iterator[tuple[int, np.ndarray]]:
    """分块逐帧打分（生成器）。

    :param model: ``load_model`` 返回的 YAMNet 模型。
    :param reader: 音频读取器，需提供 ``n_samples``（总样本数）与
        ``read(start, count) -> np.ndarray[float32, 单声道 16k]``（audio.py 实现）。
    :param chunk_seconds: 块长（秒），实际至少一个帧窗口。
    :param progress_cb: 每块完成后的进度回调 ``(已完成帧数, 总帧数)``。
    :param issues: 可选列表，收集运行中的非致命问题（如模型产出帧数异常）。
    :returns: 逐块 yield ``(起始全局帧号 g, 该块分数矩阵 [帧数, 521])``。
        调用方按 ``t[i] = (g+i)*0.48 + 0.48``（帧中心）换算时间。
    """
    import tensorflow as tf

    chunk_samples = max(int(chunk_seconds * SAMPLE_RATE), WINDOW + HOP)
    n_samples = reader.n_samples
    if n_samples < WINDOW:
        return  # 音频不足一帧窗口：无帧可产出

    total_frames = (n_samples - WINDOW) // HOP + 1
    cursor = 0  # 下一待处理帧的全局帧号
    warned_short = False
    while True:
        start = cursor * HOP
        wave = reader.read(start, chunk_samples)
        if len(wave) < WINDOW:
            break  # 剩余样本不足一整窗：结束
        scores, _embeddings, _spectrogram = model(
            tf.convert_to_tensor(wave, dtype=tf.float32)
        )
        scores = scores.numpy()
        # 期望帧数 = 完整窗口数；模型若多产出（如尾部补零帧）则截断，少了则告警
        n_expected = (len(wave) - WINDOW) // HOP + 1
        n = min(len(scores), n_expected)
        if len(scores) < n_expected and not warned_short:
            warned_short = True
            msg = (
                f"模型产出帧数({len(scores)})少于按窗口公式预期({n_expected})，"
                "已按实际产出数推进，时间轴可能有轻微缩短"
            )
            if issues is not None:
                issues.append(msg)
        if n <= 0:
            break
        yield cursor, scores[:n]
        cursor += n
        if progress_cb is not None:
            progress_cb(cursor, total_frames)
