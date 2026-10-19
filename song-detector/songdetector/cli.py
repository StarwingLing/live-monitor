"""命令行入口：``songdetector analyze / serve / classes``。

用法示例：

    uv run songdetector analyze "D:/录播/full_30/seg_001.mp4"
    uv run songdetector analyze "D:/录播/full_30/" --no-music --no-frames
    uv run songdetector serve --port 8765
    uv run songdetector classes          # 打印三个类别分组的实际类名（人工核对用）
"""

from __future__ import annotations

import argparse
import sys


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="songdetector",
        description="本地视频/音频唱歌与音乐段落检测（YAMNet），详见《使用说明.md》",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="分析文件或目录，输出时间段报告")
    p_analyze.add_argument("path", help="媒体文件或目录路径")
    p_analyze.add_argument("--config", default=None, help="config.toml 路径（可缺省）")
    p_analyze.add_argument(
        "--no-music", action="store_true", help="报告不含音乐/伴奏段（只看唱歌段）"
    )
    p_analyze.add_argument(
        "--no-frames", action="store_true", help="结果 JSON 不带逐帧分数（更小）"
    )

    p_serve = sub.add_parser("serve", help="启动本地网页校对页（默认 127.0.0.1:8765）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--config", default=None)

    p_classes = sub.add_parser("classes", help="打印三类别分组的实际类名并核对锚点")
    p_classes.add_argument("--config", default=None)
    return parser


def _cmd_analyze(args: argparse.Namespace) -> int:
    from songdetector.analyze import analyze_path
    from songdetector.config import load_config

    overrides = {}
    if args.no_music:
        overrides.setdefault("output", {})["include_music"] = False
    if args.no_frames:
        overrides["save_frame_scores"] = False
    cfg = load_config(args.config, overrides)

    # 进度打印：阶段切换即打印；同阶段内每 10% 打印一档，避免刷屏
    printed = {"key": None}

    def progress(stage: str, frac: float) -> None:
        key = (stage, int(frac * 10))
        if key != printed["key"]:
            printed["key"] = key
            print(f"  [{stage}] {min(frac, 1.0):.0%}", flush=True)

    entries = analyze_path(args.path, cfg, progress)
    failed = 0
    for entry in entries:
        if entry["ok"]:
            segs = entry["result"]["segments"]
            n_sing = len(segs["singing"])
            n_music = len(segs["music"])
            print(f"✔ {entry['path'].name}：唱歌段 {n_sing}，音乐段 {n_music}")
            print(f"    报告：{entry['md']}")
        else:
            failed += 1
            print(f"✘ {entry['path'].name}：{entry['error']}")
    if failed:
        print(f"完成：{len(entries) - failed} 成功 / {failed} 失败")
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from songdetector.config import load_config
    from songdetector.webapp import create_app

    cfg = load_config(args.config)
    app = create_app(cfg)
    print(f"校对页启动：http://{args.host}:{args.port}/ （Ctrl+C 停止）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_classes(args: argparse.Namespace) -> int:
    from songdetector.classes import build_groups, parse_class_map
    from songdetector.config import load_config
    from songdetector.model import class_map_csv_text, load_model

    cfg = load_config(args.config)
    print(f"加载模型：{cfg['model']['path']}（首次可能需要下载，已缓存则约半分钟）...")
    net = load_model(cfg["model"]["path"])
    names = parse_class_map(class_map_csv_text(net))
    groups, warnings = build_groups(names, cfg.get("classes"))
    titles = {
        "singing": "唱歌组（主输出）",
        "music": "音乐/伴奏组",
        "speech": "说话组（BGM 抑制参照）",
    }
    for group in ("singing", "music", "speech"):
        idxs = groups[group]
        print(f"\n== {titles[group]}（{len(idxs)} 类）==")
        # 每行最多 6 个类名，控制输出宽度
        line: list[str] = []
        for i in idxs:
            line.append(f"{i}:{names[i]}")
            if len(line) == 6:
                print("  " + " | ".join(line))
                line = []
        if line:
            print("  " + " | ".join(line))
    if warnings:
        print("\n警告：")
        for w in warnings:
            print(f"  - {w}")
        return 1
    print("\n锚点核对全部通过，分组与官方 class map 一致。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "classes":
        return _cmd_classes(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
