"""端到端冒烟（真模型，标记 slow）：合成媒体 → analyze_file 全流程。

合成内容：12s 四音循环正弦「旋律」+ 4s 静音 + 6s 粉噪声，共 22s。
**只验证管道正确性与产物结构，不断言识别准确率**——合成音不代表真实唱歌
（440Hz 纯音在 YAMNet 里更像 Telephone，这是模型特性不是缺陷）。

首次运行需要模型已下载（models/ 缓存或可联网），加载约半分钟。
"""

from __future__ import annotations

import json
import math
import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.slow

TOTAL_SEC = 22.0
#: 帧数公式：floor((总样本 - 15360) / 7680) + 1
EXPECTED_FRAMES = math.floor((TOTAL_SEC * 16000 - 15360) / 7680) + 1


@pytest.fixture(scope="module")
def synth_media(tmp_path_factory) -> pathlib.Path:
    """生成合成测试媒体（含中文文件名，顺带覆盖路径兼容性）。"""
    out = tmp_path_factory.mktemp("e2e") / "合成 唱歌测试.wav"
    # aevalsrc 表达式里的逗号需按 filtergraph 语法转义（\\, → \,）
    melody = "aevalsrc=0.3*sin(2*PI*(220+110*floor(mod(t\\,4)))*t):d=12:s=16000:c=mono"
    silence = "aevalsrc=0:d=4:s=16000:c=mono"
    noise = "anoisesrc=colour=pink:duration=6:sample_rate=16000:amplitude=0.3"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", melody,
         "-f", "lavfi", "-i", silence,
         "-f", "lavfi", "-i", noise,
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[out]",
         "-map", "[out]", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out


@pytest.fixture(scope="module")
def analyzed(synth_media: pathlib.Path, tmp_path_factory):
    """整模块只跑一次完整分析（模型加载+推理是主要耗时）。"""
    from songdetector.analyze import analyze_file
    from songdetector.config import load_config

    out_dir = tmp_path_factory.mktemp("results")
    cfg = load_config(overrides={
        # 小块长加速推理；小阈值参数让段落更容易出现（便于断言字段结构）
        "model": {"chunk_seconds": 8},
        "merge_gap": 0.5,
        "min_duration": 1.0,
        "output": {"dir": str(out_dir)},
    })
    result, json_path, md_path = analyze_file(synth_media, cfg)
    return result, json_path, md_path, out_dir


class TestEndToEnd:
    def test_outputs_exist(self, analyzed):
        _, json_path, md_path, _ = analyzed
        assert json_path.exists() and json_path.name.endswith(".analysis.json")
        assert md_path.exists() and md_path.name.endswith(".analysis.md")

    def test_json_structure(self, analyzed):
        result, json_path, _, _ = analyzed
        data = json.loads(json_path.read_text(encoding="utf-8"))
        # 顶层结构完整性
        for key in ("schema_version", "stem", "analyzed_at", "source", "model",
                    "config", "frames", "segments", "frame_scores"):
            assert key in data, f"缺少顶层字段 {key}"
        # 帧数与逐帧分数严格一致（帧对齐算法正确性的关键断言）
        assert data["frames"]["count"] == EXPECTED_FRAMES
        for col in ("t", "singing", "music", "speech"):
            assert len(data["frame_scores"][col]) == EXPECTED_FRAMES
        # 帧中心时间：首帧 0.48s，等差 0.48s
        assert data["frame_scores"]["t"][0] == pytest.approx(0.48, abs=0.01)
        assert data["frames"]["hop_sec"] == 0.48
        # 源信息
        assert data["source"]["duration_sec"] == pytest.approx(TOTAL_SEC, abs=0.5)
        assert result["stem"] == data["stem"]

    def test_segments_valid(self, analyzed):
        _, json_path, _, _ = analyzed
        data = json.loads(json_path.read_text(encoding="utf-8"))
        for kind in ("singing", "music"):
            segs = data["segments"][kind]
            prev_end = -1.0
            for seg in segs:
                for field in ("start", "end", "duration", "start_ts", "end_ts",
                              "mean_score", "peak_score", "kind"):
                    assert field in seg, f"{kind} 段缺字段 {field}"
                assert seg["kind"] == kind
                assert 0 <= seg["start"] < seg["end"] <= TOTAL_SEC + 1
                assert seg["duration"] == pytest.approx(seg["end"] - seg["start"], abs=0.01)
                assert 0.0 <= seg["mean_score"] <= 1.0
                assert seg["start"] >= prev_end  # 组内时间有序不重叠
                prev_end = seg["end"]

    def test_markdown_readable(self, analyzed):
        _, _, md_path, _ = analyzed
        md = md_path.read_text(encoding="utf-8")
        assert "唱歌/音乐段落分析报告" in md
        assert "唱歌片段（" in md
        assert "音乐/伴奏片段（" in md

    def test_no_audio_file_fails_cleanly(self, tmp_path):
        """纯视频（无音轨）应给出明确错误而非崩溃。"""
        from songdetector.analyze import AnalysisError, analyze_file
        from songdetector.config import load_config

        silent_video = tmp_path / "无音轨.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
             "-an", str(silent_video)],
            check=True, capture_output=True, text=True,
        )
        cfg = load_config(overrides={"output": {"dir": str(tmp_path)}})
        with pytest.raises(AnalysisError, match="音轨"):
            analyze_file(silent_video, cfg)
