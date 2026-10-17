# -*- coding: utf-8 -*-
"""配置加载模块。

从项目根目录的 config.toml 读取配置，未配置的项回退到默认值。

v2.2 架构改版后本项目为**纯监控数据 web**（查询/分析 + 房间与账户管理）：
- capture 采集、录制/切片执行、转码+上云+释放管线均已迁 replay 项目，
  [oss] / [recording] / [retention] 配置段随之移除（改参数去 replay 侧）；
- [capture] 仅保留 admin_password——本站登录门与 X-Admin-Token 旧通道的
  生产开关（未配置 = 本地调试模式，不强制登录）；
- [server].db_path 指向共享数据库（默认本项目 data/live.db；服务器部署
  与本地联调时指向 replay 数据目录下的 live.db）。
"""
import pathlib
import tomllib
from typing import Any

# 项目根目录（bili_monitor/ 的上一级）
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"
# 数据库文件：默认 data/ 子目录；[server].db_path 可覆盖（v2.2 起共享库
# 归 replay 属主，部署/联调时指到其 data/live.db）
DATA_DIR = ROOT_DIR / "data"

# 默认值：config.toml 缺失或某项未写时的兜底（与 config.toml 内注释保持一致）
_DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "listen_host": "127.0.0.1",
        "listen_port": 8001,
        "root_path": "",
        # 共享数据库路径覆盖（空 = 本项目 data/live.db）。服务器部署设
        # /opt/replay/data/live.db；本地联调可设 ../replay/data/live.db
        "db_path": "",
    },
    "capture": {
        # 本站登录门与 X-Admin-Token 旧通道口令（空 = 本地调试模式，
        # 不强制登录）；capture 采集本身的参数已迁 replay 侧
        "admin_password": "",
    },
    # v1.3：replay 项目（独立回放站）跳转基址。总览页「录制」列跳
    # /replay/<房间号>/replay 用；服务器默认 /replay（Nginx 前缀反代
    # 127.0.0.1:8003），本地开发覆盖为 http://127.0.0.1:8003
    "web": {
        "replay_base": "/replay",
    },
    "tier": {
        # 用户分层统计窗口（天）。分层规则（v0.5 二次调整）：
        # 核心 = 舰长/提督/总督（guard_level > 0），或场均送礼 ≥ core_gift_per_session_yuan
        #        （窗口内送礼总额 ÷ 参与场次数；历史场次回顾页按"本场送礼"计算）
        # 活跃 = 任一单场弹幕数 ≥ active_min_danmaku（单场 10 条以下不记活跃）
        # 普通 = 其余
        "window_days": 30,
        "core_gift_per_session_yuan": 10.0,
        "active_min_danmaku": 10,
    },
}


def load_config() -> dict[str, dict[str, Any]]:
    """读取 config.toml 并与默认值合并，返回三层嵌套字典。

    缺失的整个 section 或单个键都会回退到 _DEFAULTS，保证调用方
    拿到的键集合是完整的，无需逐处做存在性判断。
    """
    cfg: dict[str, dict[str, Any]] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            cfg = tomllib.load(f)

    merged: dict[str, dict[str, Any]] = {}
    for section, defaults in _DEFAULTS.items():
        merged[section] = {**defaults, **cfg.get(section, {})}
    return merged


# 进程内全局缓存（配置只在启动时读一次，运行期不热更新）
_CONFIG = load_config()

# 共享数据库路径（[server].db_path 覆盖；默认本项目 data/live.db）
DB_PATH = (pathlib.Path(_CONFIG["server"]["db_path"])
           if _CONFIG["server"]["db_path"]
           else DATA_DIR / "live.db")


def get_config() -> dict[str, dict[str, Any]]:
    """返回已合并默认值的完整配置（进程内单例）。"""
    return _CONFIG
