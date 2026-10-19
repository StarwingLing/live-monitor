"""配置加载：内置默认参数 + config.toml 深合并 + 调用方覆盖。

优先级（低 → 高）：``DEFAULT_CONFIG`` → ``config.toml`` → CLI / 调用方 overrides。
所有可调参数的含义见 config.toml.example 行内注释。
"""

from __future__ import annotations

import copy
import pathlib
import tomllib
from typing import Any

# 项目根目录 = songdetector/ 包的上一级（pyproject.toml 所在目录）
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
# 分析产物输出目录（gitignore，运行时自动创建）
RESULTS_DIR = PROJECT_ROOT / "results"
# TFHub 模型缓存目录（model.py 会把 TFHUB_CACHE_DIR 指向这里）
MODELS_DIR = PROJECT_ROOT / "models"

#: 全部参数的默认值。config.toml 只需写想改的键（深合并，缺省即用默认）。
DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        # YAMNet 模型来源：TFHub URL 或本地目录路径（hub.load 两者都接受；
        # 网络不通时手动下载解压后把这里改成 models/yamnet 之类的本地路径）
        "path": "https://tfhub.dev/google/yamnet/1",
        # 分块推理的块长（秒）。块越大单次调用越快，内存占用略增
        "chunk_seconds": 60,
    },
    # 唱歌段迟滞双阈值：>=enter 开段，开段中 <exit 关段
    "singing": {
        "enter": 0.30,
        "exit": 0.15,
        # 「唱歌尖峰」判定线：帧唱歌分 >= vocal_floor 计入尖峰占比统计
        # （歌曲段报告中的「尖峰占比」列，用于区分有人唱 / 纯伴奏）
        "vocal_floor": 0.15,
    },
    # **歌曲判定模式**（2026-10-15 用 full_28 真实录播标定）：
    # true（默认）= 唱歌片段按「音乐主导段」判定——music 过阈 + 说话抑制 +
    # 连续 >= min_duration。实测依据：带伴奏人声演唱在 YAMNet 里整体落在
    # Music 子树（music 均分 0.7~0.9），Singing 类只在强人声瞬间尖峰
    # （全片密度 ~1%，任何阈值下都凑不满连续 2 分钟），严格按 Singing 类
    # 判段恒为 0；false = 严格模式，只按 Singing 类迟滞段输出。
    "songs_from_music": True,
    # 歌曲段边界修形（full_28 五个参考点标定，2026-10-15）：
    "boundary": {
        # ① 干净伴奏碎片吸收：歌中间 music 分数凹陷几秒把段切断，重开的
        #   短碎片不足 min_duration 被丢 → 段尾缺漏。与保留段间隔 <= 此秒数、
        #   碎片内说话 < max_speech（聊天 BGM 说话普遍 ≥0.3，干净伴奏 ≈0.04）、
        #   且碎片 >= min_len 的，并回保留段。
        "fragment_absorb_gap": 6.0,
        "fragment_absorb_max_speech": 0.15,
        "fragment_absorb_min_len": 10.0,
        # ② 说话尾延：收尾「伴奏仍在走但主播说话」时 music 分数已塌（被说话
        #   主导），沿 2 秒滑窗说话均分 >= floor 的尾巴后移结束点，上限秒数；
        #   不越过下一段起点。
        "tail_speech_floor": 0.30,
        "tail_extend_sec": 8.0,
    },
    # 音乐段阈值（略高于唱歌，压低纯 BGM 误报）
    "music": {
        "enter": 0.40,
        "exit": 0.20,
        # BGM 抑制：music 段内 speech 平均分超过 speech_threshold 则判定
        # 「说话 + 背景音乐」并丢弃该段（电台类直播间 BGM 常开场景）
        "require_low_speech": True,
        "speech_threshold": 0.50,
    },
    # 同组相邻段间隔 <= merge_gap 秒时合并（吸收中间的低分间隙）
    "merge_gap": 3.0,
    # 合并后时长 < min_duration 秒的段落丢弃
    "min_duration": 10.0,
    # 结果 JSON 是否携带逐帧分数（网页时间轴需要；3h 视频约 270KB）
    "save_frame_scores": True,
    # 输出控制：是否在报告里包含音乐/伴奏段（--no-music 置否）
    "output": {"include_music": True},
    # 目录批量分析时识别的媒体扩展名（小写匹配）
    "media_extensions": [
        "mp4", "flv", "mkv", "mov", "ts", "m4a",
        "mp3", "wav", "flac", "ogg", "opus", "aac",
    ],
    # 类别分组覆盖：{"singing": [24,25,...], "music": [...], "speech": [...]}
    # None = 用 classes.py 里按官方 class map 核对过的默认索引组
    "classes": None,
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典：override 中的值覆盖 base，未涉及的键保留 base 原值。

    嵌套 dict 深合并；其他类型（标量/列表）整体替换。
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    config_path: str | pathlib.Path | None = None,
    overrides: dict | None = None,
) -> dict:
    """加载生效配置。

    :param config_path: 指定的 config.toml 路径；**未指定时自动发现**项目根的
        ``config.toml``（存在即加载）。指定了但文件不存在会报错提醒
        （区别于「未提供」——写错路径多半是用户笔误）。
    :param overrides: 最高优先级的键值覆盖（CLI 选项等），同样深合并。
    :return: 合并后的完整配置字典（深拷贝，调用方可随意修改）。
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_path is None:
        auto = PROJECT_ROOT / "config.toml"
        if auto.is_file():
            config_path = auto
    if config_path is not None:
        path = pathlib.Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"配置文件不存在：{path}")
        with open(path, "rb") as fh:
            user_cfg = tomllib.load(fh)
        config = deep_merge(config, user_cfg)
    if overrides:
        config = deep_merge(config, overrides)
    return config
