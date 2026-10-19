"""report.py 单元测试：时间格式化、命名清理、结果组装与产物落盘。"""

from __future__ import annotations

import json
import pathlib

from songdetector.config import DEFAULT_CONFIG
from songdetector.report import (
    build_result,
    fmt_ts,
    render_markdown,
    result_stem,
    sanitize_stem,
    write_outputs,
)


class TestFmtTs:
    def test_cases(self):
        assert fmt_ts(0) == "0:00"
        assert fmt_ts(59.5) == "0:59"      # 向下取整
        assert fmt_ts(61) == "1:01"
        assert fmt_ts(3599) == "59:59"     # 临界：不足一小时仍是 m:ss
        assert fmt_ts(3600) == "1:00:00"   # 满一小时切 h:mm:ss
        assert fmt_ts(3671) == "1:01:11"
        assert fmt_ts(-3) == "0:00"        # 负数防御


class TestStem:
    def test_sanitize_illegal_chars(self):
        assert sanitize_stem('a<b>c:d*e?f"g|h\\i/j') == "a_b_c_d_e_f_g_h_i_j"

    def test_result_stem_is_file_stem(self, tmp_path: pathlib.Path):
        """replay 联动约定：结果主名 = 源文件主名（日期+录播名随下载名带进来）。"""
        d = tmp_path / "Downloads"
        d.mkdir()
        f = d / "2026-10-10 full_28 low.mp4"
        f.touch()
        assert result_stem(f) == "2026-10-10 full_28 low"


def make_result(tmp_path: pathlib.Path, include_music: bool = True) -> dict:
    """构造最小可落盘的结果对象。"""
    src = tmp_path / "demo.mp4"
    src.write_bytes(b"x" * 16)
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    config["output"]["include_music"] = include_music
    segments = {
        "singing": [
            {
                "start": 12.0,
                "end": 75.5,
                "duration": 63.5,
                "mean_score": 0.71,
                "peak_score": 0.95,
                "peak_class": "Singing",
                # 音乐主导模式歌曲段附带的判读字段
                "vocal_density": 0.62,
                "mean_speech": 0.03,
            }
        ],
        "music": [
            {
                "start": 5.0,
                "end": 11.5,
                "duration": 6.5,
                "mean_score": 0.55,
                "peak_score": 0.6,
                "peak_class": "Background music",
                "mean_speech": 0.2,
            }
        ],
    }
    return build_result(
        source_path=src,
        duration_sec=120.0,
        model_path="https://tfhub.dev/google/yamnet/1",
        class_map_sha1="deadbeef",
        group_class_names={"singing": ["Singing"], "music": ["Music"], "speech": ["Speech"]},
        class_warnings=[],
        config=config,
        frame_count=249,
        segments=segments,
        frame_scores=None,
    )


class TestBuildResult:
    def test_structure_and_ts_fields(self, tmp_path):
        result = make_result(tmp_path)
        assert result["stem"] == "demo"
        seg = result["segments"]["singing"][0]
        assert seg["start_ts"] == "0:12" and seg["end_ts"] == "1:15"
        assert seg["duration_ts"] == "1:03"
        assert seg["kind"] == "singing"
        # 必须可 JSON 序列化（frame_scores=None 也要兼容）
        json.dumps(result, ensure_ascii=False)


class TestWriteOutputs:
    def test_writes_json_and_md(self, tmp_path):
        result = make_result(tmp_path)
        out = tmp_path / "results"
        json_path, md_path = write_outputs(result, out)
        assert json_path.exists() and md_path.exists()
        assert json_path.name == f"{result['stem']}.analysis.json"

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["source"]["duration_sec"] == 120.0

        md = md_path.read_text(encoding="utf-8")
        assert "唱歌片段（1）" in md
        assert "音乐/伴奏片段（1）" in md
        assert "0:12" in md  # 起点时间出现在表格中
        # 歌曲段携带判读列：唱歌尖峰占比与说话占比
        assert "唱歌尖峰" in md
        assert "62%" in md
        assert "0.03" in md

    def test_no_music_flag_in_md(self, tmp_path):
        result = make_result(tmp_path, include_music=False)
        md = render_markdown(result)
        assert "--no-music" in md
        assert "音乐/伴奏片段（1）" not in md
