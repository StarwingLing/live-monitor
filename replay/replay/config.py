# -*- coding: utf-8 -*-
"""配置加载（replay 项目）。

v2.2 架构改版后本项目是「采集 + 录制/切片执行 + 转码上云管线 + 回放站」
的完整载体，自有配置覆盖全部运行面：

1. 本项目 config.toml（模板 config.example.toml）七段：
   - [server]   端口 / root_path；
   - [bili]     与 bili-monitor 的桥接项（SSO 密钥路径、Cookie 名、
                biliup 投稿部署项）；
   - [capture]  采集进程参数（轮询/快照/cookie/admin_password 等，
                v2.2 前位于 bili-monitor config）；
   - [recording] 录制/切片/分段轮换参数（v2.1 起 full_split_hours/
                bridge_min）；
   - [oss]      云端桶与 AK、留存与归档参数（**唯一一处**，AK 不双处）；
   - [retention] 弹幕/互动明细保留期（每日维护消费）；
   - [tier]     用户分层阈值（capture 每日重算消费）；
2. bili-monitor 只保留自身 web 的 [server]/[web]/[tier]/[capture]
   （admin_password），不再有录制/OSS 配置。

路径推导（本地与服务器布局一致）：
- 本项目 data 目录：<项目根>/data/（live.db、recordings/、cookies.json、
  自有 web_secret.key——v2.2 起为共享数据属主；服务器部署时自
  /opt/bili-monitor/data 平移而来）；
- bili-monitor data 目录：[bili].data_dir 显式指定，或默认 <项目根>/
  ../bili-monitor/data/（仅取 web_secret.key 做 SSO 验签）。
"""
import pathlib
import tomllib
from typing import Any

# 项目根（replay/ 包的上一级）
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"

# 本项目数据目录：共享数据库 / 录像 / 运行时 cookie（v2.2 起本侧属主）
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "live.db"


def _load_own() -> dict[str, dict[str, Any]]:
    """读本项目 config.toml（缺文件返回空 dict，全部走默认值）。"""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


_OWN = _load_own()

# ---------------------------------------------------------------------------
# 默认值（缺段/缺键回退）。[capture]/[recording]/[oss]/[retention]/[tier]
# 五段自 bili-monitor/config.py 的 _DEFAULTS 对应项拷贝（v2.2 归属迁入，
# 高能参数已剔除）；此后演进在本文件独立进行。
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "listen_host": "127.0.0.1",
        "listen_port": 8003,
        "root_path": "",
    },
    "bili": {
        # bili-monitor 侧数据目录（仅用于定位其 web_secret.key 做 SSO 验签）
        "data_dir": "",
        "bili_secret_path": "",
        "bili_cookie_name": "bili_auth",
        # v2.0.0 切片一键投稿（biliup-rs 子进程）：二进制与 cookies 均为
        # 部署项（本地下载后 scp，不入库不同步——同 blivedm 本地 wheel 套路）
        "biliup_bin": "",                          # 空 = 投稿功能未部署（禁用）
        "biliup_cookies": "data/biliup-cookies.json",
        "biliup_timeout_sec": 1800,                # 上传超时（3Mbps 上行按文件大小放宽）
        # v2.3.0 本地投稿器认领窗口（秒）：part 任务 pending 等待本地投稿器
        # 认领；超时未认领由服务器兜底执行（3Mbps 慢路径）
        "submit_worker_wait_sec": 25,
    },
    "capture": {
        "poll_interval_sec": 120,
        "snapshot_interval_sec": 60,
        "reconnect_max_wait": 60,
        # 下播宽限：检测到"未开播"后等待复播的时长。期间按轮询间隔反复复核，
        # 主播临时调网/网络波动造成的短暂"假下播"不会拆场（默认 10 分钟）
        "end_grace_sec": 600,
        # 短间隔场次合并——上一场下播到下一场开播的间隔 ≤ 该秒数的两场
        # 直播合并为一场（默认 600 秒 = 10 分钟）
        "session_merge_gap_sec": 600,
        "flush_interval_sec": 1,
        "reload_interval_sec": 30,
        # cookie 保活周期（小时）：访问 B站刷新短时效凭据并检查登录态
        "cookie_keepalive_hours": 6,
        # 登录态：完整 Cookie 串（浏览器 F12 复制）；留空=匿名采集
        "cookie": "",
        # X-Admin-Token 旧通道口令（空 = 本地调试模式，不强制登录）
        "admin_password": "",
        # 直播间表情表（弹幕表情图片渲染用）的刷新周期（小时）
        "emoticon_refresh_hours": 24,
    },
    "retention": {
        "danmaku_days": 90,
        "interaction_days": 90,
    },
    # v1.2 OSS 云端扩容与直链播放。enabled=false 或缺 AK 时所有 OSS 行为
    # 静默跳过，系统回到纯本地行为（回滚位）。
    "oss": {
        "enabled": False,
        # 上传走内网 endpoint（ECS 同地域免流量费）；签名给浏览器必须
        # 公网 endpoint（内网域名公网不可解析）——代码固定由 region 推导
        "endpoint": "oss-cn-hangzhou-internal.aliyuncs.com",
        "region": "cn-hangzhou",
        "bucket": "",
        # v1.4 双 bucket → v2.1 全量热桶：所有视频的处理完成落点
        # （录播原片/low、切片及其 low）；录播原片 10 天热窗口后归档主桶 IA
        "clips_bucket": "",
        "access_key_id": "",
        "access_key_secret": "",
        "url_expiry_sec": 7200,     # 播放/下载签名 URL 有效期
        # v2.1 零留存（用户 2026-10-12 定稿）：所有视频仅录制/处理期在本地，
        # 处理完成上云即释（录播释放守卫=原片+low 双上云）
        "full_keep_hours": 96,      # 代码默认保持旧值兼容；服务器配置 0
        "clips_keep_hours": 120,    # 同上；服务器配置 0
        "clips_low_keep_hours": 720,   # 切片低清预览本地窗口；服务器配置 0
        # v2.2.1 回灌窗口（用户 2026-10-13 定稿）：切片动线从 OSS 回灌的原片
        # 独立留存 24h（判据 = recordings.restored_at 非空），与 full_keep_hours
        # 互不干扰——零留存（full_keep_hours=0）不再误杀刚回灌的文件
        "restore_keep_hours": 24,
        "rec_archive_after_days": 10,  # 录播原片 mid 热窗口天数，到期归档主桶(IA)
        "scan_interval_sec": 600,   # 转码+上云线程扫描周期
        # v2.3.0 管线闸门：本地调试/只读 OSS 场景置 false——转码+上云+归档+
        # 释放管线涉及真实云端对象与本地文件删除，绝不该在测试机上自动跑
        "pipeline_enabled": True,
    },
    # 录制功能总配置（整场录制 / 直播截取 / 手动切片 / v2.1 分段轮换+桥接）。
    "recording": {
        # 功能总开关：false 时录制接口直接拒绝（本地开发未装 ffmpeg 可置 false）
        "enabled": True,
        # 按钮页整场录制画质。v2.4.0 起定 10000=原画（旧值 200）：实测
        # B 站晚间对「原画以下档位」的请求限档（qn=150/400 只能拿到 720p
        # 的 250 档），显式请求原画（qn=10000）在登录态有效时稳定发放
        # 原画（2026-10-19 qn 矩阵实测）。原画=主播推流源画质（无 4K 推流
        # 即 1080P，码率与旧档最高档一致）
        "full_qn": 10000,
        # 自动全程录制画质（v2.4.0 起同样定 10000=原画，原因见 full_qn）
        "auto_full_qn": 10000,
        # 自动全程录制对账轮询间隔（秒）：开播头最多缺 ~35s
        "auto_full_poll_sec": 30,
        # v2.3.1：连续起流失败的退避期（秒）——本房最近两条 auto 行均
        # failed 时，该秒数内不再排队新行（延迟直播等持续拉不到流的
        # 场景降频重试，防 failed 行刷屏归档列表）；0 = 关闭退避
        "auto_full_retry_backoff_sec": 300,
        # 全局并发整场录制上限（手动 + 自动共用）
        "max_concurrent": 2,
        # ffmpeg 分段时长（秒）
        "segment_sec": 10,
        # ---- 整场录制参数 ----
        # v2.2.3 移除 full_retention_days / max_total_full_gb（保留期/总量
        # 水位自动删行）——留存策略定稿：云端永久保存、无任何自动清理，
        # 磁盘安全由 emergency_free_gb 紧急清理兜底（已上云只删本地文件）
        # ---- v2.1：2 小时自动分段 + 桥接片 ----
        "full_split_hours": 2,       # 到点轮换新段（0 = 关闭）
        "bridge_min": 10,            # 桥接片单侧分钟数（旧尾+新头，0 = 关闭）
        # ---- v0.11 手动切片 ----
        "min_clip_sec": 10,          # 切片最短时长（秒，API 校验）
        "max_total_manual_clips_gb": 0.0,   # 切片总量水位；0=取消（切片独立存续）
        # ---- v0.11 磁盘紧急清理 ----
        "emergency_free_gb": 5.0,    # 磁盘剩余低于该值时无视水位删最旧成品
        # ---- v0.11 归档布局 ----
        # 时长低于该秒数的录制行按"独立片段"归档（如逐首录制的歌切）
        "standalone_clip_max_sec": 1800,
        # ffmpeg 可执行文件路径：默认走 PATH；本地 Windows 可指向 tools/ffmpeg.exe
        "ffmpeg_path": "ffmpeg",
    },
    # 用户分层阈值（capture 每日维护重算与 writer 实时近似共用）
    "tier": {
        "window_days": 30,
        "core_gift_per_session_yuan": 10.0,
        "active_min_danmaku": 10,
    },
}

_CONFIG: dict[str, dict[str, Any]] = {
    sec: {**defaults, **_OWN.get(sec, {})}
    for sec, defaults in _DEFAULTS.items()
}


def get_config() -> dict[str, dict[str, Any]]:
    """返回已合并默认值的完整配置（进程内单例，启动读一次不热更新）。"""
    return _CONFIG


# ---- 导出常用视图（供各模块 from .config import X 直接取用）----
SERVER: dict[str, Any] = _CONFIG["server"]
BILI: dict[str, Any] = _CONFIG["bili"]

# bili-monitor 侧数据目录（仅 SSO 密钥定位用）：显式配置 > 兄弟目录默认
BILI_DATA_DIR = (pathlib.Path(BILI["data_dir"]) if BILI["data_dir"]
                 else ROOT_DIR.parent / "bili-monitor" / "data")
# bili-monitor 登录 Cookie 的签名密钥文件（SSO 桥接验签用）
BILI_SECRET_PATH = (pathlib.Path(BILI["bili_secret_path"])
                    if BILI["bili_secret_path"]
                    else BILI_DATA_DIR / "web_secret.key")
# bili-monitor 的会话 Cookie 名
BILI_COOKIE_NAME = str(BILI["bili_cookie_name"])
