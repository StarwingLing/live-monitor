"""audio.py 单元测试：命令构造（含中文路径）、真实 ffmpeg 小样本探测/抽取/读取。

真实 ffmpeg 部分只生成 1 秒音频，秒级完成，不打 slow 标记（本机必装 ffmpeg）。
"""

from __future__ import annotations

import pathlib
import subprocess

import numpy as np
import pytest

from songdetector.audio import (
    AudioError,
    Wav16kReader,
    build_extract_cmd,
    build_probe_cmd,
    extract_wav,
    probe_media,
)


class TestCommandBuild:
    def test_probe_cmd(self, tmp_path: pathlib.Path):
        f = tmp_path / "视 频.mp4"
        cmd = build_probe_cmd(f)
        assert cmd[:5] == ["ffprobe", "-v", "error", "-print_format", "json"]
        assert cmd[-1] == str(f)  # 中文+空格路径原样位于列表尾参，不经 shell

    def test_extract_cmd_flags(self, tmp_path: pathlib.Path):
        src = tmp_path / "a.mp4"
        dst = tmp_path / "b.wav"
        cmd = build_extract_cmd(src, dst)
        # 关键参数逐项核对：丢视频流、单声道、16k、wav 容器
        assert cmd[:4] == ["ffmpeg", "-y", "-v", "error"]
        for flag in ("-vn", "-ac", "1", "-ar", "16000", "-f", "wav"):
            assert flag in cmd
        assert cmd[-1] == str(dst)


@pytest.fixture(scope="module")
def one_sec_wav(tmp_path_factory) -> pathlib.Path:
    """ffmpeg 生成 1 秒 440Hz 正弦 WAV（16k 单声道，模拟 ffmpeg 全链路）。"""
    out = tmp_path_factory.mktemp("audio") / "正弦 测试.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=16000",
         "-ac", "1", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out


class TestRealFfmpeg:
    def test_probe(self, one_sec_wav: pathlib.Path):
        info = probe_media(one_sec_wav)
        assert info["has_audio"] is True
        assert info["duration"] == pytest.approx(1.0, abs=0.05)

    def test_probe_missing_file(self, tmp_path: pathlib.Path):
        with pytest.raises(AudioError):
            probe_media(tmp_path / "不存在.mp4")

    def test_extract_and_read(self, one_sec_wav: pathlib.Path, tmp_path: pathlib.Path):
        dst = tmp_path / "out.wav"
        extract_wav(one_sec_wav, dst)
        with Wav16kReader(dst) as reader:
            assert reader.n_samples == pytest.approx(16000, abs=64)
            chunk = reader.read(0, 100)
            assert len(chunk) == 100
            assert chunk.dtype == np.float32
            assert np.all(np.abs(chunk) <= 1.0)
            # 尾部越界读：返回到文件末尾为止
            tail = reader.read(reader.n_samples - 10, 1000)
            assert len(tail) == 10

    def test_reader_rejects_wrong_format(self, tmp_path: pathlib.Path):
        """44.1kHz 立体声 wav 应被拒绝（防止静默算错时间轴）。"""
        bad = tmp_path / "bad.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
             "-ac", "2", str(bad)],
            check=True, capture_output=True, text=True,
        )
        with pytest.raises(AudioError):
            Wav16kReader(bad)
