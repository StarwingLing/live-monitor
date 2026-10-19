"""classes.py 单元测试：class map 解析、分组构建与防御警告、组聚合。"""

from __future__ import annotations

import numpy as np

from songdetector.classes import (
    MUSIC_IDX,
    SINGING_IDX,
    SPEECH_IDX,
    aggregate_group,
    build_groups,
    find_classes_by_name,
    parse_class_map,
)

MINI_CSV = (
    "index,mid,display_name\n"
    '0,/m/09x0r,"Speech"\n'
    '1,/m/02zsn,"Child speech, kid speaking"\n'
    "2,/m/028v0c,Conversation\n"
)


class TestParseClassMap:
    def test_quoted_comma_field(self):
        """display_name 含逗号的带引号字段必须正确解析。"""
        names = parse_class_map(MINI_CSV)
        assert names == ["Speech", "Child speech, kid speaking", "Conversation"]

    def test_missing_index_raises(self):
        """索引跳号（不连续）应报错——分组索引依赖位置正确。"""
        bad = "index,mid,display_name\n0,/a,A\n2,/b,B\n"
        try:
            parse_class_map(bad)
            raise AssertionError("应当抛出 ValueError")
        except ValueError as exc:
            assert "不连续" in str(exc)


def names_with_anchors(total: int = 300) -> list[str]:
    """构造一套「锚点类名正确」的伪类名表（其余位置填占位名）。"""
    names = [f"c{i}" for i in range(total)]
    anchors = {
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
    for idx, nm in anchors.items():
        if idx < total:  # 小类表场景只放得下部分锚点
            names[idx] = nm
    return names


class TestBuildGroups:
    def test_default_groups_no_warning(self):
        groups, warnings = build_groups(names_with_anchors())
        assert warnings == []
        assert set(groups["singing"]) == set(SINGING_IDX)
        assert set(groups["music"]) == set(MUSIC_IDX)
        assert set(groups["speech"]) == set(SPEECH_IDX)
        # music 组不应包含已划入唱歌组的类
        assert not (set(groups["music"]) & set(groups["singing"]))

    def test_anchor_mismatch_warns(self):
        names = names_with_anchors()
        names[24] = "Unexpected"
        _, warnings = build_groups(names)
        assert any("索引 24" in w and "锚点不符" in w for w in warnings)

    def test_short_class_map_warns(self):
        """类总数少于默认组最大索引（模型换代防御）。"""
        _, warnings = build_groups(names_with_anchors(total=100))
        assert any("超出类总数" in w for w in warnings)

    def test_override_parses_ints_and_drops_out_of_range(self):
        names = names_with_anchors()
        override = {"singing": [24, "25", 999], "music": [132], "speech": [0]}
        groups, warnings = build_groups(names, override)
        assert groups["singing"] == [24, 25]
        assert groups["music"] == [132]
        assert any("越界" in w for w in warnings)

    def test_override_empty_group_falls_back(self):
        override = {"singing": [], "music": [132], "speech": [0]}
        groups, warnings = build_groups(names_with_anchors(), override)
        assert groups["singing"] == sorted(SINGING_IDX)
        assert any("回退默认分组" in w for w in warnings)


class TestAggregateGroup:
    def test_max_and_global_argmax(self):
        scores = np.array(
            [
                [0.1, 0.9, 0.0, 0.2, 0.0],
                [0.0, 0.3, 0.0, 0.8, 0.1],
                [0.05, 0.05, 0.05, 0.05, 0.05],
            ]
        )
        group_max, group_arg = aggregate_group(scores, [1, 3])
        assert group_max.tolist() == [0.9, 0.8, 0.05]
        # 返回的是全局类索引（1 / 3），可直接用于类名查询
        assert group_arg.tolist() == [1, 3, 1]


class TestFindByName:
    def test_regex_search(self):
        names = names_with_anchors()
        hits = find_classes_by_name(names, r"^sing")
        assert (24, "Singing") in hits
