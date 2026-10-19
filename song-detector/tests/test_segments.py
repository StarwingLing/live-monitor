"""segments.py 单元测试：迟滞状态机、合并、裁决、抑制与完整流水线。

全部用合成分数序列驱动，不依赖模型与网络。
"""

from __future__ import annotations

import numpy as np

from songdetector.segments import (
    absorb_clean_fragments,
    build_segments,
    extend_tails_by_speech,
    hysteresis_segments,
    merge_segments,
    subtract_segments,
    suppress_by_speech,
    union_segments,
)


def times_of(n: int, step: float = 0.5) -> np.ndarray:
    """构造与真实帧时间同风格的等差时间轴（帧中心时间）。"""
    return np.array([step * (i + 1) for i in range(n)], dtype=float)


# ---------------- 迟滞状态机 ----------------

class TestHysteresis:
    def test_basic_open_close(self):
        """高分开段、低分关段；end 取第一个失败帧的时间。"""
        t = times_of(6)
        s = np.array([0.0, 0.35, 0.5, 0.5, 0.1, 0.0])
        segs = hysteresis_segments(t, s, enter=0.3, exit_=0.15)
        # 开于帧1（t=1.0，score 0.35>=0.3）；关于帧4（t=2.5，score 0.1<0.15）
        assert segs == [{"start": 1.0, "end": 2.5}]

    def test_closes_at_array_end(self):
        """高分持续到末尾：以最后一帧时间关段。"""
        t = times_of(4)
        s = np.array([0.0, 0.4, 0.5, 0.6])
        segs = hysteresis_segments(t, s, enter=0.3, exit_=0.15)
        assert segs == [{"start": 1.0, "end": 2.0}]

    def test_enter_equality_opens_and_exit_equality_stays(self):
        """边界语义：score == enter 即开段；score == exit 不关段（严格小于才关）。"""
        t = times_of(3)
        s = np.array([0.30, 0.15, 0.10])
        segs = hysteresis_segments(t, s, enter=0.30, exit_=0.15)
        assert segs == [{"start": 0.5, "end": 1.5}]

    def test_all_low_yields_nothing(self):
        t = times_of(5)
        s = np.zeros(5)
        assert hysteresis_segments(t, s, 0.3, 0.15) == []


# ---------------- 合并 ----------------

class TestMerge:
    def test_merge_within_gap(self):
        segs = [{"start": 0.0, "end": 2.0}, {"start": 2.5, "end": 4.0}]
        assert merge_segments(segs, 3.0) == [{"start": 0.0, "end": 4.0}]

    def test_no_merge_beyond_gap(self):
        segs = [{"start": 0.0, "end": 2.0}, {"start": 6.0, "end": 8.0}]
        merged = merge_segments(segs, 3.0)
        assert len(merged) == 2

    def test_empty(self):
        assert merge_segments([], 3.0) == []

    def test_chain_merge(self):
        """三段两两间隔都 <= gap，应合并为一段。"""
        segs = [
            {"start": 0.0, "end": 2.0},
            {"start": 4.0, "end": 6.0},
            {"start": 8.0, "end": 10.0},
        ]
        assert merge_segments(segs, 3.0) == [{"start": 0.0, "end": 10.0}]


# ---------------- 组间裁决（music 减 singing） ----------------

class TestSubtract:
    def test_cut_middle_two_holes(self):
        target = [{"start": 0.0, "end": 10.0}]
        cuts = [{"start": 2.0, "end": 3.0}, {"start": 5.0, "end": 6.0}]
        assert subtract_segments(target, cuts) == [
            {"start": 0.0, "end": 2.0},
            {"start": 3.0, "end": 5.0},
            {"start": 6.0, "end": 10.0},
        ]

    def test_fully_covered_disappears(self):
        target = [{"start": 2.0, "end": 3.0}]
        cuts = [{"start": 0.0, "end": 10.0}]
        assert subtract_segments(target, cuts) == []

    def test_disjoint_unchanged(self):
        target = [{"start": 0.0, "end": 1.0}]
        cuts = [{"start": 5.0, "end": 6.0}]
        assert subtract_segments(target, cuts) == target


# ---------------- BGM 抑制 ----------------

class TestSuppress:
    def test_drop_when_speechy(self):
        t = times_of(10)
        speech = np.full(10, 0.9)
        segs = [{"start": 0.5, "end": 5.0}]
        kept, dropped = suppress_by_speech(segs, t, speech, threshold=0.5)
        assert kept == []
        assert len(dropped) == 1
        assert dropped[0]["mean_speech"] > 0.8

    def test_keep_when_music_only(self):
        t = times_of(10)
        speech = np.full(10, 0.05)
        segs = [{"start": 0.5, "end": 5.0}]
        kept, dropped = suppress_by_speech(segs, t, speech, threshold=0.5)
        assert len(kept) == 1
        assert dropped == []


# ---------------- 完整流水线 ----------------

class TestBuildSegments:
    #: 严格模式配置（songs_from_music=False，按 Singing 类独立出段）
    CFG = {
        "singing": {"enter": 0.3, "exit": 0.15, "vocal_floor": 0.15},
        "songs_from_music": False,
        "music": {
            "enter": 0.4,
            "exit": 0.2,
            "require_low_speech": True,
            "speech_threshold": 0.5,
        },
        "merge_gap": 0.5,
        "min_duration": 1.0,
    }

    def test_synthetic_pipeline(self):
        """唱歌段保留；说话+音乐段被抑制；纯音乐段保留并带统计属性。"""
        n = 100
        t = times_of(n)  # 0.5 .. 50.0
        singing = np.zeros(n)
        music = np.zeros(n)
        speech = np.zeros(n)
        singing[20:40] = 0.8            # 唱歌段
        music[60:80] = 0.8; speech[60:80] = 0.7   # 说话+背景音乐 → 应被抑制
        music[85:95] = 0.8              # 纯音乐段 → 保留
        singing_argmax = np.full(n, 24)
        music_argmax = np.full(n, 132)
        names = [f"c{i}" for i in range(300)]

        out = build_segments(
            t, singing, singing_argmax, music, music_argmax, speech, names, self.CFG
        )
        # 唱歌：1 段，开于 t[20]=10.5、关于 t[40]=20.5
        assert len(out["singing"]) == 1
        seg = out["singing"][0]
        assert seg["start"] == t[20] and seg["end"] == t[40]
        assert seg["peak_class"] == "c24"
        assert seg["mean_score"] > 0.7
        # 音乐：仅剩纯音乐段（60:80 被 speech 抑制）
        assert len(out["music"]) == 1
        mus = out["music"][0]
        assert mus["start"] == t[85] and mus["end"] == t[95]
        assert mus["peak_class"] == "c132"

    def test_min_duration_filters(self):
        """短于 min_duration 的段被丢弃。"""
        n = 20
        t = times_of(n)
        singing = np.zeros(n)
        singing[0:2] = 0.9  # 两帧（间隔 1.0s 的小段）
        out = build_segments(
            t, singing, np.zeros(n, dtype=int),
            np.zeros(n), np.zeros(n, dtype=int), np.zeros(n),
            [f"c{i}" for i in range(300)],
            {**self.CFG, "min_duration": 5.0},
        )
        assert out["singing"] == []

    def test_singing_absorbs_overlap_from_music(self):
        """music 与 singing 重叠：重叠部分归 singing，music 只留残余。"""
        n = 40
        t = times_of(n)
        singing = np.zeros(n)
        music = np.zeros(n)
        singing[10:20] = 0.9
        music[5:30] = 0.9  # 更宽的 music 段包住 singing 段
        speech = np.zeros(n)
        out = build_segments(
            t, singing, np.full(n, 24), music, np.full(n, 132), speech,
            [f"c{i}" for i in range(300)],
            {**self.CFG, "min_duration": 0.5, "music": {**self.CFG["music"], "enter": 0.5}},
        )
        assert len(out["singing"]) == 1
        # music 被切成两截（singing 之前 + 之后），且不再包含 singing 区间
        music_spans = [(m["start"], m["end"]) for m in out["music"]]
        sing = out["singing"][0]
        for ms, me in music_spans:
            assert me <= sing["start"] or ms >= sing["end"]


class TestSongsFromMusicMode:
    """音乐主导模式（默认）：歌曲段 = music 抑制后 ∪ singing，统一过滤统计。"""

    CFG = {
        "singing": {"enter": 0.3, "exit": 0.15, "vocal_floor": 0.15},
        "songs_from_music": True,
        "music": {
            "enter": 0.4, "exit": 0.2,
            "require_low_speech": True, "speech_threshold": 0.5,
        },
        "merge_gap": 0.5,
        "min_duration": 1.0,
    }

    def test_union_and_stats(self):
        n = 100
        t = times_of(n)
        singing = np.zeros(n)
        music = np.zeros(n)
        speech = np.zeros(n)
        music[20:40] = 0.8                  # 歌曲主体（伴奏）
        singing[25:35] = 0.6                # 人声尖峰（在 music 段内部）
        singing[60:66] = 0.6                # 段外清唱尖峰（独立并入）
        music[70:80] = 0.8; speech[70:80] = 0.9  # 说话+BGM → 被抑制，不出现
        out = build_segments(
            t, singing, np.full(n, 24), music, np.full(n, 132), speech,
            [f"c{i}" for i in range(300)], self.CFG,
        )
        # music 节为空（已并入歌曲段），歌曲段 = [20,40] ∪ [60,66 附近] 共 2 段
        assert out["music"] == []
        assert len(out["singing"]) == 2
        first = out["singing"][0]
        assert first["start"] == t[20] and first["end"] == t[40]
        # 统计属性来自 music 分数（伴奏主体）
        assert first["peak_class"] == "c132"
        # 唱歌尖峰占比 = 段内 singing>=0.15 的帧占比（帧 25~34 共 10/21 帧 ≈ 0.48）
        assert 0.4 < first["vocal_density"] < 0.6
        assert first["mean_speech"] < 0.05
        second = out["singing"][1]
        # 清唱段（帧 60~65 尖峰，闭段帧 66 计入边界）：6/7 ≈ 0.86
        assert 0.7 < second["vocal_density"] < 0.95

    def test_min_duration_applies_to_union(self):
        """并入 union 后再过滤：不足 min_duration 的音乐段被丢弃。"""
        n = 40
        t = times_of(n)
        singing = np.zeros(n)
        music = np.zeros(n)
        music[0:3] = 0.8  # 三帧小段（间隔 1.5s < min_duration=5）
        out = build_segments(
            t, singing, np.zeros(n, dtype=int), music, np.full(n, 132),
            np.zeros(n), [f"c{i}" for i in range(300)],
            {**self.CFG, "min_duration": 5.0},
        )
        assert out["singing"] == []

    def test_clean_fragment_absorbed_and_speechy_not(self):
        """边界修形①：干净伴奏碎片并回；说话碎片与远距离碎片不并。"""
        n = 60
        t = times_of(n)  # 0.5~30s
        singing = np.zeros(n)
        music = np.zeros(n)
        speech = np.zeros(n)
        music[0:20] = 0.8                 # 保留段 0.5~10.5s
        music[24:32] = 0.8                # 干净碎片 12.5~16.5s（间隔 2s，说话≈0）
        music[46:50] = 0.8                # 远碎片 23.5~25s（间隔 7s > 6）
        music[56:60] = 0.8; speech[56:60] = 0.6  # 说话碎片 28.5~30s（说话高）
        cfg = {
            **self.CFG, "min_duration": 8.0,
            "boundary": {"fragment_absorb_gap": 6.0, "fragment_absorb_min_len": 3.0},
        }
        out = build_segments(
            t, singing, np.zeros(n, dtype=int), music, np.full(n, 132),
            speech, [f"c{i}" for i in range(300)], cfg,
        )
        # 只有干净碎片被吸收：保留段终点从 10.5 延到 16.5（远/说话碎片不并，
        # 且尾部 8s 内无说话，尾延不触发）
        assert len(out["singing"]) == 1
        assert out["singing"][0]["end"] == t[32]

    def test_tail_extension_bounded_and_stops_before_next(self):
        """边界修形②：说话尾部延伸生效且不超过 8 秒上限。"""
        n = 80
        t = times_of(n)  # 0.5~40s
        singing = np.zeros(n)
        music = np.zeros(n)
        speech = np.zeros(n)
        music[0:20] = 0.8                 # 段A 0.5~10.5s
        speech[21:26] = 0.7               # 段A 后紧跟说话（2s 滑窗达标）
        music[40:60] = 0.8                # 段B 20.5~30.5s
        out = build_segments(
            t, singing, np.zeros(n, dtype=int), music, np.full(n, 132),
            speech, [f"c{i}" for i in range(300)], self.CFG,
        )
        segs = out["singing"]
        assert len(segs) == 2
        # 滑窗约溢出说话尾 1s（窗宽 4 帧 ≈1.92s），落在 11~15s 区间
        assert 11.0 < segs[0]["end"] <= 10.5 + 8.0
        assert segs[1]["start"] == t[40]

    def test_tail_extension_off_when_silent(self):
        """段尾安静（无说话）时不延伸。"""
        n = 30
        t = times_of(n)
        music = np.zeros(n)
        music[0:20] = 0.8
        out = build_segments(
            t, np.zeros(n), np.zeros(n, dtype=int), music, np.full(n, 132),
            np.zeros(n), [f"c{i}" for i in range(300)], self.CFG,
        )
        assert out["singing"][0]["end"] == t[20]


class TestBoundaryHelpers:
    """碎片吸收与尾延的独立单元测试（不经完整流水线）。"""

    BCFG = {
        "fragment_absorb_gap": 6.0,
        "fragment_absorb_max_speech": 0.15,
        "fragment_absorb_min_len": 10.0,
        "tail_speech_floor": 0.30,
        "tail_extend_sec": 8.0,
    }

    def test_absorb_requires_min_len(self):
        t = np.arange(1, 41, dtype=float) * 0.5
        sp = np.zeros(40)
        kept = [{"start": 10.0, "end": 20.0}]
        # 5 秒碎片（< min_len=10）不吸收
        assert absorb_clean_fragments(kept, [{"start": 1.0, "end": 6.0}], t, sp, self.BCFG) == 0
        assert kept == [{"start": 10.0, "end": 20.0}]

    def test_tail_extends_to_last_talking_window(self):
        t = np.arange(1, 41, dtype=float) * 0.5  # 0.5~20s
        sp = np.zeros(40)
        sp[20:26] = 0.6  # 10.5~13s 说话
        sp[30:34] = 0.05  # 15.5~17s 安静
        segs = [{"start": 1.0, "end": 10.5}]
        extend_tails_by_speech(segs, t, sp, self.BCFG)
        # 4 帧滑窗溢出说话尾约 1s：延伸到 t[27]=14s；其后安静窗口不延伸
        assert segs[0]["end"] == t[27]
