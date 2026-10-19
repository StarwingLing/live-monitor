# song-detector 项目专属约定（子代理说明）

> 本文件只放 song-detector 项目专属的规则与状态；跨项目通用规则见仓库根
> [AGENTS.md](../AGENTS.md)。凡涉及本项目的对话：先读仓库根文件，再读本文件
> 与本项目《使用说明.md》。

## 项目定位（勿违背）

- **本地工具**：对下载到本机的视频/音频文件做「唱歌/音乐段落」检测（YAMNet 本地推理），
  输出时间段清单 + 本地网页校对页，供 replay 录播切片前定位歌曲位置。
- **识别仅在本地完成**（用户 2026-10-14 确认）：不部署服务器、不注册 systemd、
  不开公网端口、不上传任何数据；若未来要改此边界，须用户明确同意并更新本文件。
- **与 replay 的联动边界（2026-10-15 用户定稿）**：replay 切片抽屉提供
  「下载low以进行识别」（落盘名 = `<录播日期> <录播名> low.mp4`，本工具
  结果主名 = 源文件主名，即以录播日期+录播名对号）与「加载本地识别文件」
  （纯前端读取 .analysis.json，不上传）；本工具自身仍不读 replay 的库/代码，
  联动仅靠文件命名约定与 JSON 结构（replay 侧实现见其 v2.2.3）。
- 不做曲名识别（识曲需外部指纹服务，超出范围）。

## 环境与运行

- uv 项目（Python 3.12），依赖：tensorflow 2.21.0、tensorflow-hub 0.16.1、numpy、
  fastapi、uvicorn；dev：pytest。依赖 2026-10-14 已装（uv.lock 已生成）。
- 本机须有 ffmpeg/ffprobe（当前 9.0.2 gyan full，已验证）。
- 常用命令（实施后生效）：
  - `uv run python -m songdetector analyze <文件或目录> [--config config.toml]`
  - `uv run python -m songdetector serve`（本地校对页，127.0.0.1:8765）
  - `uv run python -m songdetector classes`（打印三类别组实际类名，核对分组）
  - `uv run pytest`（冒烟用例标记 slow：`-m slow` 单跑）
- 目录：`models/` = TFHub 模型缓存（TFHUB_CACHE_DIR 指向这里，gitignore）；
  `results/` = 分析产物（gitignore）。

## 关键技术决策记录（详见 config 注释）

- YAMNet（TFHub `google/yamnet/1`），帧窗 0.96s/步长 0.48s，分块帧对齐推理；
- 类别组按官方 class map 索引：singing = {24~32, 213, 233, 249, 250}，
  music = 132~276 剔除唱歌索引，speech = {0,1,2,3,4,5,63,65}（config 可覆盖）；
- 段落聚合：迟滞双阈值 → singing 优先吸收重叠 → speech 抑制 BGM 误报 →
  合并（3s）→ 最短段过滤；
- **「2 分钟规则」（用户 2026-10-15 定稿）**：只有连续 ≥2 分钟被定位为唱歌的
  段落才算唱歌片段——`config.toml` 已持久化 `min_duration = 120`，调整此口径
  须用户明确同意。
- **歌曲判定模式 `songs_from_music = true`（2026-10-15 用 full_28 真实录播标定）**：
  唱歌片段按「music 过阈 + 说话抑制 + 连续 ≥min_duration」判定（∪ 唱歌尖峰段），
  附唱歌尖峰占比/说话占比两列供人工判读。依据：带伴奏演唱整体落 Music 子树、
  Singing 类只呈尖峰，严格模式出不了 2 分钟段。回退严格模式改 config 即可。
- **边界修形 `[boundary]`（2026-10-15 第二轮标定，用户 5 个参考点）**：干净伴奏
  碎片吸收（≤6s/说话<0.15/≥10s）+ 说话尾延（滑窗 0.30、上限 8s、仅 music 段）；
  4 个结束端误差 ≤4s。起始端无信号（前奏人声尖峰 0.00~0.02），保持伴奏起点
  （偏早 7~17s 属切片安全侧）。调参时警惕两个已验证的过拟合陷阱：music.exit
  降到 0.10 会令后半场结构重组；merge_gap 放大会向后吸附聊天 BGM。
- **首次真实验证数据**：full_28（房间 <房间号>，2026-10-10，3h43m low 版
  1.36GB）经 OSS 签名直链下载于 `tests/_tmp/full_28_low.mp4`（gitignore），
  结果在 `results/2026-10-10_full_28_low.analysis.*`（replay 联动命名约定）。
