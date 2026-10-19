"""ffmpeg/ffprobe 封装：音轨探测、抽 16kHz 单声道 WAV、WAV 流式读取。

设计要点：

- 所有子进程一律 **列表参数**（不经 shell 拼接），天然兼容含空格/中文的路径；
- 抽音轨只解码音频流（``-vn``），长视频也不会全量解码视频轨，速度只与音频时长相关；
- ``Wav16kReader`` 按需 ``setpos + readframes`` 流式读样本，配合 model.py 的
  分块推理，全程不在内存里持有整段波形（3 小时音频 float32 约 690MB）。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import wave

import numpy as np

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class AudioError(Exception):
    """音频探测/抽取失败（无音轨、文件损坏、ffmpeg 报错等）。"""


def build_probe_cmd(path: pathlib.Path) -> list[str]:
    """构造 ffprobe 探测命令（JSON 输出，含容器与流信息）。"""
    return [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]


def probe_media(path: pathlib.Path, timeout: float = 120) -> dict:
    """探测媒体文件：是否有音轨、总时长（秒）。

    :return: ``{"has_audio": bool, "duration": float | None}``
    :raises AudioError: ffprobe 失败（文件不存在/损坏/非媒体）。
    """
    proc = subprocess.run(
        build_probe_cmd(path), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe 失败：{(proc.stderr or '').strip()[-300:]}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    # 时长优先取容器级 format.duration；个别封装缺失时回退音频流自身的 duration
    duration = None
    raw = info.get("format", {}).get("duration")
    if raw is None:
        for s in streams:
            if s.get("codec_type") == "audio" and s.get("duration") is not None:
                raw = s["duration"]
                break
    if raw is not None:
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            duration = None
    return {"has_audio": has_audio, "duration": duration}


def build_extract_cmd(src: pathlib.Path, dst: pathlib.Path) -> list[str]:
    """构造抽音轨命令：丢弃视频流、单声道、重采样 16kHz、输出 s16 WAV。"""
    return [
        FFMPEG, "-y", "-v", "error",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", str(dst),
    ]


def extract_wav(
    src: pathlib.Path, dst: pathlib.Path, timeout: float | None = None
) -> None:
    """抽取 16kHz 单声道 16bit WAV 到 dst。

    :raises AudioError: ffmpeg 返回非零或产物为空（附 stderr 尾部便于排障）。
    """
    proc = subprocess.run(
        build_extract_cmd(src, dst), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg 抽音轨失败：{(proc.stderr or '').strip()[-300:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("ffmpeg 抽音轨产物为空")


class Wav16kReader:
    """16kHz 单声道 s16 WAV 的流式样本读取器。

    提供 model.py 分块推理所需的协议：``n_samples`` 属性 + ``read(start, count)``。
    返回 float32 波形，幅度归一到 [-1, 1]（int16 / 32768，YAMNet 期望口径）。
    """

    def __init__(self, path: pathlib.Path):
        self._wf = wave.open(str(path), "rb")
        params = self._wf.getparams()
        # ffmpeg 的 "-ac 1 -ar 16000 -f wav" 产物必然满足以下参数；不满足说明
        # 拿到了非预期文件（比如用户手动指了别的 wav），尽早报错而不是悄悄算错
        if params.nchannels != 1 or params.sampwidth != 2 or params.framerate != 16000:
            self._wf.close()
            raise AudioError(
                f"非预期的 WAV 参数（需要 16kHz 单声道 s16）："
                f"channels={params.nchannels}, sampwidth={params.sampwidth}, "
                f"rate={params.framerate}"
            )
        self.n_samples = self._wf.getnframes()

    def read(self, start: int, count: int) -> np.ndarray:
        """读取 ``[start, start+count)`` 样本区间的 float32 波形。"""
        self._wf.setpos(int(start))
        raw = self._wf.readframes(int(count))
        # 小端 int16 → float32；空读返回空数组（调用方自行结束循环）
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    def close(self) -> None:
        self._wf.close()

    def __enter__(self) -> "Wav16kReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
