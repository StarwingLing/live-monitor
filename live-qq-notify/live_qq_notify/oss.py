# -*- coding: utf-8 -*-
"""报告图 OSS 中转（v0.3.0，official 通道专用）。

背景（2026-10-18 服务器端到端实测）：QQ 官方群富媒体接口
POST /v2/groups/{group_openid}/files **只收公网可访问 URL**——官方 Python SDK
(botpy) 的 post_group_file 也只有 url 字段；multipart 文件体直传被平台拒绝
（HTTP 400 + code 40093007「富媒体文件下载失败」）。本地生成的报告图没有
公网地址，因此先上传阿里云 OSS 私有桶 → 换短时签名 URL → 走
official.send_group_media_url（与开播封面同一条已验证通道）→ 发送完成后
即删对象，云端不残留。

客户端策略与 replay/bili 同款：上传走内网 endpoint（同地域零公网流量费），
签名恒用公网域名（QQ 服务器拉取）；凭据在本项目 config.toml [oss] 段
（服务器上与 replay 同一份 AK，config 不入 git）。napcat 通道不经本模块
（CQ file:// 本地图直发）。
"""
from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

_upload_client = None   # 懒加载单例（内网 endpoint，上传/删除用）
_sign_client = None     # 懒加载单例（公网 endpoint，签名用）
_init_error = ""        # 初始化失败原因（重复调用不重复报错）


class OssError(RuntimeError):
    """OSS 中转失败（未启用/凭据缺失/上传或签名出错）。"""


def reset_for_test() -> None:
    """单测用：清空懒加载单例与错误缓存。"""
    global _upload_client, _sign_client, _init_error
    _upload_client = _sign_client = None
    _init_error = ""


def _ensure_clients(cfg: dict) -> None:
    """懒加载双客户端（内网上传 + 公网签名），失败统一抛 OssError。"""
    global _upload_client, _sign_client, _init_error
    if _sign_client is not None:
        return
    if _init_error:
        raise OssError(_init_error)
    o = cfg["oss"]
    if not o.get("enabled"):
        raise OssError("[oss] 未启用——official 通道发送报告图需要 OSS 中转"
                       "(服务器 config.toml [oss] 段,与 replay 同一份 AK)")
    if not (o.get("bucket") and o.get("access_key_id") and o.get("access_key_secret")):
        raise OssError("[oss] 配置不完整(bucket/access_key_id/access_key_secret)")
    try:
        import alibabacloud_oss_v2 as oss
        region = o.get("region", "cn-hangzhou")
        provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=o["access_key_id"],
            access_key_secret=o["access_key_secret"])

        c_pub = oss.config.load_default()
        c_pub.region = region
        c_pub.endpoint = f"oss-{region}.aliyuncs.com"
        c_pub.credentials_provider = provider
        _sign_client = oss.Client(c_pub)

        c_int = oss.config.load_default()
        c_int.region = region
        # 内网上传（同机同地域零流量费）；显式关掉则与签名共用公网 endpoint
        if o.get("internal_upload", True):
            c_int.endpoint = f"oss-{region}-internal.aliyuncs.com"
        else:
            c_int.endpoint = f"oss-{region}.aliyuncs.com"
        c_int.credentials_provider = provider
        _upload_client = oss.Client(c_int)
    except OssError:
        raise
    except Exception as e:
        _init_error = f"OSS 客户端初始化失败: {e!r}"
        raise OssError(_init_error) from e


def report_key(room_id: int) -> str:
    """报告图对象键：qq-reports/<房间>/<时间戳>.png（短时存在，发完即删）。"""
    return f"qq-reports/{room_id}/{time.strftime('%Y%m%dT%H%M%S')}.png"


def upload_and_sign(cfg: dict, png_path: Path, key: str,
                    ttl_min: int = 30) -> str:
    """上传 PNG 并返回签名 URL（默认 30 分钟有效；QQ 拉取在秒级，足够）。

    失败抛 OssError（调用方按"文本已发、图缺"降级，不影响下播文本）。
    """
    _ensure_clients(cfg)
    o = cfg["oss"]
    import alibabacloud_oss_v2 as oss
    try:
        _upload_client.put_object(oss.models.object_basic.PutObjectRequest(
            bucket=o["bucket"], key=key, body=png_path.read_bytes()))
        return _sign_client.presign(
            oss.models.object_basic.GetObjectRequest(
                bucket=o["bucket"], key=key),
            expires=timedelta(minutes=ttl_min)).url
    except Exception as e:
        raise OssError(f"报告图上传/签名失败: {e!r}") from e


def delete(cfg: dict, key: str) -> None:
    """发送后清理对象（尽力而为，失败静默——残留数百 KB 对象无害，
    下次人工清理桶时顺带）。"""
    try:
        _ensure_clients(cfg)
        import alibabacloud_oss_v2 as oss
        _upload_client.delete_object(
            oss.models.object_basic.DeleteObjectRequest(
                bucket=cfg["oss"]["bucket"], key=key))
    except Exception:
        pass
