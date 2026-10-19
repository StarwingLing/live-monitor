"""song-detector：本地视频/音频「唱歌 / 音乐段落」检测工具。

对给定媒体文件抽取 16kHz 单声道音轨，用 YAMNet 模型逐帧打分，
聚合出唱歌段（singing）与音乐/伴奏段（music）的时间清单，
输出 JSON + Markdown 报告，并附带本地网页校对页（点击跳段播放）。

详细设计见各模块 docstring 与《使用说明.md》。
"""

__version__ = "0.1.0"
