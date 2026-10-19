# -*- coding: utf-8 -*-
"""official 层单测(httpx.MockTransport,零真实网络):token 缓存与临期重取 /
发送参数 / 业务错分类 / 封面上传+发图两步 / 探活失败。

运行:uv run python tests/test_official.py
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import official  # noqa: E402

results = []


def check(name: str, ok: bool):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}")


def make_client(mode: str, calls: list):
    """按 mode 分派假响应;calls 记录 (method, path, body) 供断言。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path,
                      request.content.decode("utf-8", "replace")))
        if path == "/app/getAppAccessToken":
            if mode == "bad_token":
                return httpx.Response(200, json={
                    "code": 100016, "message": "appid or secret error"})
            return httpx.Response(200, json={
                "access_token": "T0KEN", "expires_in": 7200})
        if path.endswith("/messages"):
            if mode == "send_reject":
                return httpx.Response(200, json={
                    "code": 40034100, "message": "超频控"})
            return httpx.Response(200, json={"id": "MSG1", "timestamp": "t"})
        if path.endswith("/files"):
            if mode == "upload_fail":
                return httpx.Response(200, json={
                    "code": 1, "message": "upload fail"})
            # 真实接口 2026-10-17 实测形态(无 code 字段,含 ttl/raw_url)
            return httpx.Response(200, json={
                "file_uuid": "U1", "file_info": "FILE_INFO_X",
                "ttl": "604800", "id": "F1", "raw_url": "https://r/x.jpg"})
        return httpx.Response(404, json={"code": -1, "message": "no route"})

    return httpx.Client(transport=httpx.MockTransport(handler))


# 1. token 获取 + 进程内缓存(两次 access_token 只发一次真实请求)
calls = []
oc = official.OfficialClient("123", "sec", client=make_client("ok", calls))
check("token 获取", oc.access_token() == "T0KEN")
oc.access_token()
check("token 缓存复用(不重复请求)",
      sum(1 for c in calls if c[1] == "/app/getAppAccessToken") == 1)

# 2. 临期重取:_token_exp 推到过期线内 → 重新获取
oc._token_exp = 0.0
oc.access_token()
check("临期自动重取",
      sum(1 for c in calls if c[1] == "/app/getAppAccessToken") == 2)

# 3. probe:凭据有效即通道就绪
check("probe 返回 app_id", oc.probe() == "123")

# 4. 发文本:路径/鉴权头/请求体形状
calls.clear()
oc2 = official.OfficialClient("123", "sec", client=make_client("ok", calls))
oc2.send_group_text("OPENID1", "开播啦")
m, p, body = calls[-1]
check("发文本:POST /v2/groups/{openid}/messages 且 msg_type=0",
      m == "POST" and p == "/v2/groups/OPENID1/messages"
      and json.loads(body) == {"msg_type": 0, "content": "开播啦"})

# 5. 业务错(官方 HTTP 200 + code!=0)→ OfficialError
oc3 = official.OfficialClient("123", "sec", client=make_client("send_reject", []))
try:
    oc3.send_group_text("OPENID1", "x")
    check("业务错抛 OfficialError", False)
except official.OfficialError as e:
    check("业务错抛 OfficialError(含错误码)", "40034100" in str(e))

# 6. 封面两步:URL 直传(files,**JSON 体**——表单体实测会被接口回表单响应)
#    → msg_type=7 发图
calls.clear()
oc4 = official.OfficialClient("123", "sec", client=make_client("ok", calls))
oc4.send_group_media_url("OPENID2", "https://i0.hdslb.com/cover.jpg")
upload = [c for c in calls if c[1].endswith("/files")]
sendm = [c for c in calls if c[1].endswith("/messages")]
check("封面上传:POST files JSON 体(file_type=1/url/srv_send_msg=False)",
      len(upload) == 1 and json.loads(upload[0][2]) ==
      {"file_type": 1, "url": "https://i0.hdslb.com/cover.jpg",
       "srv_send_msg": False})
check("封面发图:messages msg_type=7 + file_info",
      len(sendm) == 1 and json.loads(sendm[0][2]) ==
      {"msg_type": 7, "media": {"file_info": "FILE_INFO_X"}})

# 7. 上传失败 → OfficialError(调用方按"文本已发,仅图缺"降级)
oc5 = official.OfficialClient("123", "sec", client=make_client("upload_fail", []))
try:
    oc5.send_group_media_url("OPENID2", "https://x/1.jpg")
    check("上传失败抛 OfficialError", False)
except official.OfficialError:
    check("上传失败抛 OfficialError", True)

# 8. 凭据错误(100016)→ OfficialError
oc6 = official.OfficialClient("bad", "bad", client=make_client("bad_token", []))
try:
    oc6.probe()
    check("凭据错误 probe 抛 OfficialError", False)
except official.OfficialError as e:
    check("凭据错误 probe 抛 OfficialError(含 100016)", "100016" in str(e))

# (v0.3.0 曾有 multipart 本地文件直传用例;2026-10-18 服务器实测该通道被
#  平台拒绝 40093007——群富媒体只收公网 URL,方法与用例均已删除,
#  报告图改经 OSS 中转走 send_group_media_url,见 oss.py 与 test_oss.py)

print("\n汇总:", "全部通过" if all(results) else f"{results.count(False)} 项失败")
raise SystemExit(0 if all(results) else 1)
