# -*- coding: utf-8 -*-
"""QQ 官方机器人 OpenAPI 客户端(v0.2.0 官方通道,与 onebot.py 平级)。

官方文档 bot.q.qq.com/wiki(2026-10-17 实测核对);与 NapCat 通道的差异:
- 鉴权无"登录态"概念:AppID+AppSecret 换 AccessToken(7200s),无风控踢线;
- 目标群用 group_openid(机器人×群维度标识,只能从事件获取),不是 QQ 群号;
- 富媒体不能内联在文本里:封面图须先上传拿 file_info 再以 msg_type=7 单发。

本模块只做 HTTP 封装,不含业务重试(重试/等待策略在 main 循环,与 NapCat 同款)。
"""
import time

import httpx

API_BASE = "https://api.bot.qq.com"


class OfficialError(RuntimeError):
    """官方 API 调用失败(网络层/凭据/业务码)。"""


class OfficialClient:
    """官方通道客户端:持有 AccessToken 缓存(进程内惰性刷新)。

    用法:main 循环里按全局配置构造(channel=official 时),多个群/多条消息
    复用同一实例——token 获取一次管 ~2 小时,过期前提前重取。

    探活语义:probe()=AccessToken 可获取(凭据有效+网络通);真正能否送达
    (群主是否开了机器人主动发言权限)只在发送时暴露。
    """

    # token 提前刷新余量(秒):官方口径"过期前 60s 内请求可得新 token",
    # 留 120s 更稳,避免临界竞态
    _REFRESH_MARGIN = 120

    def __init__(self, app_id: str, app_secret: str,
                 client: httpx.Client | None = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = client or httpx.Client(timeout=10.0)
        self._token = ""
        self._token_exp = 0.0  # 过期时刻(epoch 秒);0=未获取

    # ------------------------------------------------------------------ token
    def _fetch_token(self) -> None:
        """POST /app/getAppAccessToken 换新 token 并记缓存过期时刻。

        失败(凭据错/网络断/请求过频)抛 OfficialError;expires_in 官方
        返回字符串或整数,统一按 int 处理。
        """
        try:
            r = self.client.post(
                f"{API_BASE}/app/getAppAccessToken",
                json={"appId": self.app_id, "clientSecret": self.app_secret})
            body = r.json()
        except httpx.HTTPError as e:
            raise OfficialError(f"官方接口不可达({e.__class__.__name__})") from e
        if "access_token" not in body:
            # 常见 code:100016=appid/secret 错误,100001=请求过频,10004=机器人不存在
            raise OfficialError(f"AccessToken 获取失败: {str(body)[:160]}")
        self._token = str(body["access_token"])
        try:
            ttl = int(body.get("expires_in", 7200))
        except (TypeError, ValueError):
            ttl = 7200
        self._token_exp = time.time() + max(60, ttl)

    def access_token(self, force: bool = False) -> str:
        """取有效 token(缓存命中直接返回;临期/force 时重取)。"""
        if force or not self._token or time.time() >= self._token_exp - self._REFRESH_MARGIN:
            self._fetch_token()
        return self._token

    def probe(self) -> str:
        """探活:token 可获取即认为通道就绪;返回 app_id 供日志标识。"""
        self.access_token()
        return self.app_id

    # ------------------------------------------------------------------ 发送
    def _post_json(self, path: str, payload: dict, auth: bool = True) -> dict:
        """统一 POST:非 2xx 或响应含 code(业务错)都抛 OfficialError。"""
        headers = {"Authorization": f"QQBot {self.access_token()}"} if auth else {}
        try:
            r = self.client.post(f"{API_BASE}{path}", json=payload,
                                 headers=headers)
        except httpx.HTTPError as e:
            raise OfficialError(f"请求失败({e.__class__.__name__})") from e
        try:
            body = r.json()
        except ValueError:
            raise OfficialError(f"响应非 JSON(HTTP {r.status_code})") from None
        if r.status_code != 200 or ("code" in body and body.get("code") not in (0, None)):
            # 常见业务错:40034100 超频控 / 40054010 URL 不在白名单 /
            # 50055001 发送异常(多为群主未开主动发言权限) / 40054007 超长
            raise OfficialError(f"官方 API 业务错(HTTP {r.status_code}): {str(body)[:160]}")
        return body

    def send_group_text(self, group_openid: str, content: str) -> None:
        """发一条群文本主动消息(不带 msg_id/event_id 即主动;单群 20/min、1000/天)。"""
        self._post_json(f"/v2/groups/{group_openid}/messages",
                        {"msg_type": 0, "content": content})

    def send_group_media_url(self, group_openid: str, url: str) -> None:
        """富媒体图片发送:URL 直传换 file_info(免下载到本地),再以 msg_type=7 发出。

        实测修正(2026-10-17 服务器定位):files 接口**只认 JSON 请求体**——
        表单体会返回 HTTP 200 + 表单形态响应(file_info=57&file_info=25&...),
        JSON 解析必失败(当日生产日志"封面上传响应非 JSON"即此因,图片从未
        发出过);改走 _post_json(JSON 体)后实测 200 返回
        {file_uuid, file_info, ttl, id, raw_url},msg_type=7 发图端到端成功。
        srv_send_msg=False=仅上传不代发;两步任一失败抛 OfficialError
        (调用方按"文本已发、封面降级"处理,不影响开播文本通知本身)。

        2026-10-18 补充(报告图链路实测):群富媒体**只收公网可访问 URL**——
        multipart 文件体直传被平台拒绝(HTTP 400 + 40093007「富媒体文件
        下载失败」,官方 SDK post_group_file 亦仅 url 字段)。本地生成的
        报告图因此经 OSS 中转后走本方法(见 oss.py);曾实现的
        send_group_media_file multipart 路线已删除。
        """
        body = self._post_json(f"/v2/groups/{group_openid}/files",
                               {"file_type": 1, "url": url, "srv_send_msg": False})
        file_info = body.get("file_info") if isinstance(body, dict) else None
        if not file_info:
            raise OfficialError(f"富媒体上传失败: {str(body)[:160]}")
        self._post_json(f"/v2/groups/{group_openid}/messages",
                        {"msg_type": 7, "media": {"file_info": file_info}})
