"""YAMNet 类别分组：521 类 → singing / music / speech 三组。

默认分组索引已于 2026-10-14 对照官方 ``yamnet_class_map.csv``（tensorflow/models
仓库 research/audioset/yamnet/）逐条核对：

- **24~32 为连续的「人声演唱」簇**（Singing/Choir/Yodeling/Chant/Mantra/
  Child singing/Synthetic singing/Rapping/Humming），另有 233 Opera、
  249 Vocal music、250 A capella、213 Beatboxing；
- **132~276 为 AudioSet 的 Music 子树**（Music、Musical instrument 及各乐器、
  各音乐体裁与场景）；
- **speech 类**：0 Speech、1 Child speech、2 Conversation、3 Narration、
  4 Babbling、5 Speech synthesizer、63 Chatter、65 Hubbub。
  （YAMNet 没有 Male/Female speech 独立类。）

首跑时可用 ``songdetector classes`` 子命令打印三组实际类名做人工复核；
若模型自带的 CSV 与上述锚点类名不符（如未来换模型），本模块会给出警告。
"""

from __future__ import annotations

import csv
import io
import re

import numpy as np

#: 唱歌组索引（人声音乐表演：唱、合唱、说唱、哼唱、口技等）
SINGING_IDX: frozenset[int] = frozenset(
    {24, 25, 26, 27, 28, 29, 30, 31, 32, 213, 233, 249, 250}
)

#: 音乐组索引 = Music 子树（132~276）剔除已划入唱歌组的类
MUSIC_IDX: frozenset[int] = frozenset(set(range(132, 277)) - set(SINGING_IDX))

#: 说话组索引（用于 BGM 抑制：音乐段里是否其实在说话）
SPEECH_IDX: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5, 63, 65})

#: 默认分组的「锚点」类名——索引 → 期望 display_name。
#: 用于防御模型 CSV 版本变化：任一锚点对不上即产生警告（不阻断运行）。
_ANCHOR_NAMES: dict[int, str] = {
    0: "Speech",
    2: "Conversation",
    3: "Narration, monologue",
    24: "Singing",
    25: "Choir",
    31: "Rapping",
    32: "Humming",
    63: "Chatter",
    65: "Hubbub, speech noise, speech babble",
    132: "Music",
    133: "Musical instrument",
    213: "Beatboxing",
    233: "Opera",
    249: "Vocal music",
    250: "A capella",
    261: "Song",
    262: "Background music",
    276: "Scary music",
}


def parse_class_map(csv_text: str) -> list[str]:
    """解析 YAMNet class map CSV 文本，返回按索引排序的 display_name 列表。

    CSV 列为 ``index,mid,display_name``；display_name 可能含逗号（带引号字段），
    因此必须用 csv 模块解析而不是简单 split。
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    names: list[str | None] = []
    for row in reader:
        idx = int(row["index"])
        name = (row["display_name"] or "").strip()
        # 按索引放位；模型 CSV 索引应连续从 0 开始
        while len(names) <= idx:
            names.append(None)
        names[idx] = name
    if any(n is None for n in names):
        missing = [i for i, n in enumerate(names) if n is None]
        raise ValueError(f"class map 索引不连续，缺失：{missing[:5]}...")
    return [n for n in names if n is not None]  # 类型收窄：此处已无 None


def build_groups(
    class_names: list[str],
    override: dict | None = None,
) -> tuple[dict[str, list[int]], list[str]]:
    """构建三个类别分组的索引列表。

    :param class_names: 模型 class map 的类名列表（索引即位置）。
    :param override: config.toml 的 ``classes`` 覆盖段，
        形如 ``{"singing": [24, 25], "music": [...], "speech": [...]}``；
        None 时使用模块默认分组。
    :return: ``(groups, warnings)``：
        groups = ``{"singing": [...], "music": [...], "speech": [...]}``（升序索引）；
        warnings = 需要提示用户的问题清单（锚点类名不符 / 索引越界），
        只警告不中断——误中断比带警告运行更影响使用。
    """
    warnings: list[str] = []
    n = len(class_names)

    if override:
        # 用户自定义分组：逐组解析，越界索引剔除并警告
        groups: dict[str, list[int]] = {}
        for group in ("singing", "music", "speech"):
            raw = override.get(group)
            if not raw:
                warnings.append(f"classes.{group} 覆盖为空，回退默认分组")
                groups[group] = _default_group(group)
                continue
            valid = [i for i in (int(x) for x in raw) if 0 <= i < n]
            dropped = len(raw) - len(valid)
            if dropped:
                warnings.append(
                    f"classes.{group} 有 {dropped} 个越界索引被剔除（类总数 {n}）"
                )
            groups[group] = sorted(set(valid))
    else:
        groups = {g: _default_group(g) for g in ("singing", "music", "speech")}
        # 防御：默认组里的索引超出当前模型类数（模型换代）或锚点类名不符
        for group, indices in groups.items():
            for idx in indices:
                if idx >= n:
                    warnings.append(
                        f"默认分组 {group} 的索引 {idx} 超出类总数 {n}，"
                        "模型可能与核对时的版本不同，请运行 `songdetector classes` 复核"
                    )
                    break
        for idx, expected in _ANCHOR_NAMES.items():
            if idx < n and class_names[idx] != expected:
                warnings.append(
                    f"类名锚点不符：索引 {idx} 期望「{expected}」，"
                    f"实际「{class_names[idx]}」（模型版本可能变化）"
                )
    return groups, warnings


def _default_group(group: str) -> list[int]:
    """取默认分组的升序索引列表。"""
    mapping = {
        "singing": SINGING_IDX,
        "music": MUSIC_IDX,
        "speech": SPEECH_IDX,
    }
    return sorted(mapping[group])


def aggregate_group(
    scores: np.ndarray, indices: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    """把一批帧的 521 类分数聚合为某一组的「每帧最高分 + 对应类索引」。

    YAMNet 每类输出独立的 0~1 分（多标签），组分数取组内 max 比 sum 稳健
    （sum 会把一堆低分乐器类累加成虚高）。

    :param scores: 形状 ``[帧数, 521]`` 的分数矩阵。
    :param indices: 该组的类索引列表。
    :return: ``(group_max, group_argmax)``——每帧组内最高分，及取得最高分的类索引
        （是全局类索引，可直接用 class_names 查名）。
    """
    sub = scores[:, indices]
    local = sub.argmax(axis=1)
    return sub.max(axis=1), np.asarray(indices, dtype=int)[local]


def top_class_name(class_names: list[str], class_idx: int) -> str:
    """类索引 → 类名（越界防御，避免上层崩溃）。"""
    if 0 <= class_idx < len(class_names):
        return class_names[class_idx]
    return f"<类{class_idx}>"


# 供文档/校对用的简单展示函数：找出名字里含关键词的类（调试辅助，不影响主流程）
def find_classes_by_name(class_names: list[str], pattern: str) -> list[tuple[int, str]]:
    """按正则搜类名，返回 (索引, 类名) 列表。用于人工核对分组时快速查询。"""
    regex = re.compile(pattern, re.IGNORECASE)
    return [(i, name) for i, name in enumerate(class_names) if regex.search(name)]
