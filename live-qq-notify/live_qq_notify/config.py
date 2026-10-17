# -*- coding: utf-8 -*-
"""配置加载:只管**进程级**参数(库路径/轮询节奏)。

业务配置(通知开关、群映射、文案模板、NapCat 地址与 token、QQ 小号)
全部存共享 live.db(qq_notify / qq_notify_global 两表),由 bili-monitor
页面读写、本进程每轮热加载——因此本文件刻意保持极简。
"""
import os
import pathlib
import tomllib

# 项目根目录(live_qq_notify/ 的上一级)
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"

# 本进程自有运行时产物目录(状态文件 state.json);不与共享库混放
DATA_DIR = ROOT_DIR / "data"

_DEFAULTS = {
    "db": {
        # 共享数据库路径:服务器部署设 /opt/replay/data/live.db;
        # 本地联调可指 ../bili-monitor/data/live.db(或本地造的测试库)
        "path": "data/live.db",
    },
    "bilibili": {
        # 直播状态接口基址。默认 B 站官方;测试时可覆盖为仿直播页
        # (如 http://127.0.0.1:8004,响应结构与 getInfo 同构),用于
        # 人工制造开播/下播翻转验证通知链路——覆盖期间真实状态被遮蔽,
        # 测试完务必还原
        "api_base": "https://api.live.bilibili.com",
    },
    "poll": {
        "interval": 15,   # 轮询间隔(秒;2026-10-14 用户定稿 15s 缩短开播检测延迟)
        "timeout": 10,    # 单个 HTTP 请求超时(秒)
        # B 站连续失败 N 轮后打一条 WARNING(便于发现接口/网络异常;
        # 失败轮沿用上轮状态,不会误判下播)
        "backoff_rounds": 5,
    },
}


def load_config() -> dict:
    """读 config.toml 并与默认值合并(缺失整段/单键都回退)。"""
    cfg: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            cfg = tomllib.load(f)
    merged = {}
    for section, defaults in _DEFAULTS.items():
        merged[section] = {**defaults, **cfg.get(section, {})}
    return merged


def db_path(cfg: dict) -> pathlib.Path:
    """共享库绝对路径(相对路径按项目根解析)。"""
    p = pathlib.Path(cfg["db"]["path"])
    return p if p.is_absolute() else (ROOT_DIR / p)


def state_path() -> pathlib.Path:
    """状态文件路径(data/state.json;可用环境变量覆盖便于测试)。"""
    return pathlib.Path(os.environ.get("LQN_STATE_PATH", str(DATA_DIR / "state.json")))
