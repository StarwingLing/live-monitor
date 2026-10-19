"""段落聚合：逐帧组分数 → 唱歌/音乐时间段（纯函数，便于单元测试）。

流程：

1. 对 singing / music 两组分数各跑**迟滞状态机**出原始段；
2. 同组内相邻段间隔 <= merge_gap 的**合并**；
3. **组间裁决**：music 段减去 singing 段的重叠（唱歌优先，剩余独立保留）；
4. **BGM 抑制**：music 段内 speech 平均分超阈值 → 判「说话+背景音乐」丢弃；
5. **最短段过滤**：时长 < min_duration 丢弃；
6. 为每段计算统计属性（均分/峰值/峰值类名）。

时间约定：帧时间戳取窗口中心（0.48s 栅格）；段 start/end 均为帧中心时间，
end 取「第一个低于退出阈值的帧」中心（略保守，宁可多含 0.48s 尾巴）。
"""

from __future__ import annotations

import numpy as np


def hysteresis_segments(
    times: np.ndarray, scores: np.ndarray, enter: float, exit_: float
) -> list[dict]:
    """迟滞双阈值状态机：把逐帧分数切成时间段。

    - 未在段中且 score >= enter → 开段（start = 该帧时间）；
    - 在段中且 score < exit → 关段（end = 该帧时间，即第一个失败帧）；
    - 到数组末尾仍在段中 → 以最后一帧时间关段。

    双阈值的意义：进段标准高（enter）、退段标准低（exit_），
    段内偶尔一帧抖动到中间值不会把段切碎。
    """
    segments: list[dict] = []
    in_segment = False
    start = 0.0
    for t, s in zip(times, scores):
        if not in_segment:
            if s >= enter:
                in_segment = True
                start = float(t)
        else:
            if s < exit_:
                segments.append({"start": start, "end": float(t)})
                in_segment = False
    if in_segment:
        segments.append({"start": start, "end": float(times[-1])})
    return segments


def merge_segments(segments: list[dict], gap: float) -> list[dict]:
    """把间隔 <= gap 的相邻段合并（段间低分间隙被吸收）。"""
    if not segments:
        return []
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        if seg["start"] - merged[-1]["end"] <= gap:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append(dict(seg))
    return merged


def subtract_segments(target: list[dict], cuts: list[dict]) -> list[dict]:
    """从 target 段集合中挖掉 cuts 段集合覆盖的时间（用于 music 减 singing）。

    一段可能被切成零到多段；长度近零的碎屑（< 1e-9 秒）直接丢弃。
    """
    result: list[dict] = []
    for seg in target:
        pieces = [dict(seg)]
        for cut in cuts:
            next_pieces: list[dict] = []
            for piece in pieces:
                # 无重叠：整段保留
                if cut["end"] <= piece["start"] or cut["start"] >= piece["end"]:
                    next_pieces.append(piece)
                    continue
                # 有重叠：保留左右不重叠的部分
                if cut["start"] > piece["start"]:
                    next_pieces.append(
                        {"start": piece["start"], "end": cut["start"]}
                    )
                if cut["end"] < piece["end"]:
                    next_pieces.append({"start": cut["end"], "end": piece["end"]})
            pieces = [p for p in next_pieces if p["end"] - p["start"] > 1e-9]
        result.extend(pieces)
    return result


def suppress_by_speech(
    music_segments: list[dict],
    times: np.ndarray,
    speech: np.ndarray,
    threshold: float,
) -> tuple[list[dict], list[dict]]:
    """BGM 抑制：music 段内 speech 平均分 > threshold → 判定「说话+背景音乐」。

    :return: ``(保留段, 丢弃段)``——丢弃段单独返回，便于报告/调试时说明原因。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for seg in music_segments:
        # 段内帧 = 时间落在 [start, end] 的帧（边界帧中心时间可能恰在边界上）
        mask = (times >= seg["start"] - 1e-9) & (times <= seg["end"] + 1e-9)
        mean_speech = float(speech[mask].mean()) if mask.any() else 0.0
        seg = dict(seg)
        seg["mean_speech"] = mean_speech
        (dropped if mean_speech > threshold else kept).append(seg)
    return kept, dropped


def _segment_stats(
    seg: dict,
    times: np.ndarray,
    group_scores: np.ndarray,
    group_argmax: np.ndarray,
    class_names: list[str],
) -> dict:
    """给单个段落补统计属性：时长、均分、峰值、峰值帧上的组内最高分类名。"""
    mask = (times >= seg["start"] - 1e-9) & (times <= seg["end"] + 1e-9)
    if mask.any():
        vals = group_scores[mask]
        peak_frame = int(np.argmax(vals))
        peak_idx = int(group_argmax[mask][peak_frame])
        mean_score = float(vals.mean())
        peak_score = float(vals.max())
        # 峰值类名做一次越界防御（classes.top_class_name 已兜底）
        from songdetector.classes import top_class_name

        peak_class = top_class_name(class_names, peak_idx)
    else:  # 理论上不会发生（段落由帧状态机产生），防御空段
        mean_score = peak_score = 0.0
        peak_class = ""
    out = dict(seg)
    out["duration"] = out["end"] - out["start"]
    out["mean_score"] = mean_score
    out["peak_score"] = peak_score
    out["peak_class"] = peak_class
    return out


def union_segments(a: list[dict], b: list[dict]) -> list[dict]:
    """区间并集：两组段落合并为不重叠的有序段列表（歌曲模式的 singing∪music）。"""
    merged = sorted(a + b, key=lambda s: s["start"])
    out: list[dict] = []
    for seg in merged:
        if out and seg["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], seg["end"])
        else:
            out.append(dict(seg))
    return out


def _mean_over(times: np.ndarray, values: np.ndarray, start: float, end: float) -> float:
    """[start, end] 区间内 values 的均值（无帧时返回 0）。"""
    mask = (times >= start - 1e-9) & (times <= end + 1e-9)
    return float(values[mask].mean()) if mask.any() else 0.0


def absorb_clean_fragments(
    kept: list[dict],
    frags: list[dict],
    times: np.ndarray,
    speech: np.ndarray,
    bcfg: dict,
) -> int:
    """把被分数凹陷切出的**干净伴奏短碎片**并回相邻的保留段。

    动机（full_28 标定）：一段歌中间 music 分数偶尔凹陷到退出阈下几秒
    （如 0.17 < exit 0.20），关段后重开的小碎片不足 min_duration 被过滤，
    造成整段尾部缺漏（#1 少了 33 秒）。

    吸收条件（三条同时满足，防「聊天期 BGM」混入）：
    1. 碎片与保留段的间隔 <= fragment_absorb_gap（默认 6s，凹陷断口的量级）；
    2. 碎片内说话占比 < fragment_absorb_max_speech（默认 0.15——聊天 BGM 的
       说话普遍 ≥0.3，干净伴奏 ≈0.04，界线分明）；
    3. 碎片时长 >= fragment_absorb_min_len（默认 10s，排除偶发尖峰）。

    :return: 被吸收的碎片数（报告/日志用）。
    """
    gap_limit = float(bcfg.get("fragment_absorb_gap", 6.0))
    speech_limit = float(bcfg.get("fragment_absorb_max_speech", 0.15))
    min_len = float(bcfg.get("fragment_absorb_min_len", 10.0))
    absorbed = 0
    for f in frags:
        if f["end"] - f["start"] < min_len:
            continue
        if _mean_over(times, speech, f["start"], f["end"]) >= speech_limit:
            continue
        for k in kept:
            if f["end"] <= k["start"]:
                gap = k["start"] - f["end"]
            elif f["start"] >= k["end"]:
                gap = f["start"] - k["end"]
            else:
                gap = 0.0  # 已重叠（理论上不会，防御）
            if 0.0 <= gap <= gap_limit:
                k["start"] = min(k["start"], f["start"])
                k["end"] = max(k["end"], f["end"])
                absorbed += 1
    return absorbed


def extend_tails_by_speech(
    segments: list[dict],
    times: np.ndarray,
    speech: np.ndarray,
    bcfg: dict,
) -> None:
    """结束端「说话尾部延伸」：段尾若是主播在说话（伴奏被说话压住时 music 分数
    已塌、但人耳里伴奏还在走），把结束点沿说话向后延伸，上限 tail_extend_sec。

    判据：从段尾起，2 秒滑窗（4 帧）说话均分 >= tail_speech_floor 的最后一个
    窗口尾即新终点；同时不越过下一段起点（防吞并相邻歌）。

    full_28 标定：#3/#4/#5 的真值结束点均在「唱歌→说话(伴奏仍响)→伴奏停」
    拐点后 9~11 秒，floor=0.30 / cap=8s 时 4 个参考端点误差 ≤3 秒。
    """
    floor = float(bcfg.get("tail_speech_floor", 0.30))
    cap = float(bcfg.get("tail_extend_sec", 8.0))
    win = 4  # 4 帧 ≈ 1.92s 滑窗
    ordered = sorted(segments, key=lambda s: s["start"])
    for i, seg in enumerate(ordered):
        hi = min(seg["end"] + cap, float(times[-1]))
        idx = np.where((times >= seg["end"] - 0.01) & (times <= hi))[0]
        ext = seg["end"]
        for k in range(len(idx) - win + 1):
            if speech[idx[k]: idx[k] + win].mean() >= floor:
                ext = float(times[idx[k] + win - 1])
        # 不越过下一段起点（留 0.1s 缝）
        if i + 1 < len(ordered):
            ext = min(ext, ordered[i + 1]["start"] - 0.1)
        seg["end"] = max(seg["end"], ext)


def build_segments(
    times: np.ndarray,
    singing: np.ndarray,
    singing_argmax: np.ndarray,
    music: np.ndarray,
    music_argmax: np.ndarray,
    speech: np.ndarray,
    class_names: list[str],
    cfg: dict,
) -> dict[str, list[dict]]:
    """完整聚合流水线：帧分数 → ``{"singing": [...], "music": [...]}`` 段落列表。

    两种歌曲判定模式（cfg["songs_from_music"]）：

    - **True（默认，音乐主导模式）**：实测带伴奏人声演唱在 YAMNet 整体落在
      Music 子树、Singing 类只呈尖峰，故「唱歌片段」= music 迟滞段（含 BGM
      抑制）∪ singing 迟滞段，再合并过滤；统计属性用 music 分数，并附
      vocal_density（段内唱歌分 >= vocal_floor 的帧占比，区分有人唱/纯伴奏）
      与 mean_speech。此时 music 输出为空（已并入歌曲段）。
    - **False（严格模式）**：只按 Singing 类迟滞段输出，music 段独立成节
      （singing 优先吸收重叠部分）。

    每个段落字典含：start / end / duration（秒）、mean_score / peak_score、
    peak_class；时间字符串格式化由 report 层负责。
    """
    merge_gap = float(cfg.get("merge_gap", 3.0))
    min_duration = float(cfg.get("min_duration", 10.0))
    music_cfg = cfg.get("music", {})
    vocal_floor = float(cfg.get("singing", {}).get("vocal_floor", 0.15))

    # 1) 两组各自跑迟滞状态机 + 同组合并
    singing_segs = merge_segments(
        hysteresis_segments(
            times, singing, float(cfg["singing"]["enter"]), float(cfg["singing"]["exit"])
        ),
        merge_gap,
    )
    music_segs = merge_segments(
        hysteresis_segments(
            times, music, float(music_cfg["enter"]), float(music_cfg["exit"])
        ),
        merge_gap,
    )

    # 2) BGM 抑制（可关）：music 段内其实在说话的丢弃
    if music_cfg.get("require_low_speech", True):
        music_segs, _dropped = suppress_by_speech(
            music_segs, times, speech, float(music_cfg.get("speech_threshold", 0.5))
        )

    if cfg.get("songs_from_music", True):
        # ---- 音乐主导模式：歌曲段 = music ∪ singing，统一过滤与统计 ----
        bcfg = cfg.get("boundary", {})
        # 边界修形②（先于并集，只作用于 music 段）：「说话压伴奏」的收尾沿
        # 说话延伸——尾延的前提是伴奏确实在走（段以 music 收尾），纯唱歌段
        # （清唱尖峰撑起）后接的说话只是聊天，不能延伸，故必须在 union 前做。
        extend_tails_by_speech(music_segs, times, speech, bcfg)
        uni = union_segments(music_segs, singing_segs)
        song_segs = [s for s in uni if s["end"] - s["start"] >= min_duration]
        frags = [s for s in uni if s["end"] - s["start"] < min_duration]
        # 边界修形①：吸收被凹陷切出的干净伴奏碎片（#1 类缺漏）
        absorb_clean_fragments(song_segs, frags, times, speech, bcfg)
        # 吸收可能桥接出重叠段（一个碎片同时贴两段），合并之
        song_segs = union_segments(song_segs, song_segs)
        # 统计属性用 music 分数（伴奏轨是歌曲段的稳定主体）
        song_segs = [
            _segment_stats(s, times, music, music_argmax, class_names)
            for s in song_segs
        ]
        for seg in song_segs:
            mask = (times >= seg["start"] - 1e-9) & (times <= seg["end"] + 1e-9)
            seg["vocal_density"] = float((singing[mask] >= vocal_floor).mean())
            seg["mean_speech"] = float(speech[mask].mean())
        return {"singing": song_segs, "music": []}

    # ---- 严格模式：singing 优先，music 挖掉与 singing 重叠的部分 ----
    # （一首歌的纯伴奏前奏会这样独立成 music 段，属预期行为）
    music_segs = subtract_segments(music_segs, singing_segs)

    # 最短段过滤（放在合并与裁决之后，避免碎片先被过滤再无法合并）
    singing_segs = [s for s in singing_segs if s["end"] - s["start"] >= min_duration]
    music_segs = [s for s in music_segs if s["end"] - s["start"] >= min_duration]

    # 统计属性
    singing_segs = [
        _segment_stats(s, times, singing, singing_argmax, class_names)
        for s in singing_segs
    ]
    music_segs = [
        _segment_stats(m, times, music, music_argmax, class_names) for m in music_segs
    ]
    return {"singing": singing_segs, "music": music_segs}
