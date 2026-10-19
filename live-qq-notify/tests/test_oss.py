# -*- coding: utf-8 -*-
"""oss 层单测(报告图 OSS 中转,v0.3.0):以桩 SDK 模块注入 sys.modules,
零真实网络——覆盖未启用/配置不完整报错、上传+签名调用形态(内网上传/
公网签名双客户端)、对象键格式、删除尽力而为。

运行:uv run python tests/test_oss.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_qq_notify import oss  # noqa: E402

results = []


def check(name: str, ok: bool):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}")


# ---- 桩 SDK:记录 put_object/presign/delete_object 调用 ------------------------
calls: list[tuple] = []


class _StaticCredentialsProvider:
    def __init__(self, access_key_id, access_key_secret):
        calls.append(("creds", access_key_id, access_key_secret))


class _PutObjectRequest:
    def __init__(self, bucket, key, body=None):
        self.bucket, self.key, self.body = bucket, key, body
        calls.append(("put", bucket, key, len(body or b"")))


class _GetObjectRequest:
    def __init__(self, bucket, key):
        self.bucket, self.key = bucket, key
        calls.append(("get_req", bucket, key))


class _DeleteObjectRequest:
    def __init__(self, bucket, key):
        self.bucket, self.key = bucket, key
        calls.append(("del", bucket, key))


class _PresignResult:
    url = "https://bucket.oss-cn-hangzhou.aliyuncs.com/signed?sig=STUB"


class _Client:
    def __init__(self, config):
        self.endpoint = config.endpoint
        calls.append(("client", config.endpoint))

    def put_object(self, req):
        calls.append(("put_ok", req.key))

    def presign(self, req, expires=None):
        calls.append(("presign", req.key, str(expires)))
        return _PresignResult()

    def delete_object(self, req):
        calls.append(("del_ok", req.key))


class _Config:
    def __init__(self):
        self.region = ""
        self.endpoint = ""
        self.credentials_provider = None


stub = types.ModuleType("alibabacloud_oss_v2")
stub.credentials = types.SimpleNamespace(
    StaticCredentialsProvider=_StaticCredentialsProvider)
stub.models = types.SimpleNamespace(object_basic=types.SimpleNamespace(
    PutObjectRequest=_PutObjectRequest,
    GetObjectRequest=_GetObjectRequest,
    DeleteObjectRequest=_DeleteObjectRequest))
stub.config = types.SimpleNamespace(load_default=lambda: _Config())
stub.Client = _Client
sys.modules["alibabacloud_oss_v2"] = stub

CFG = {"oss": {"enabled": True, "region": "cn-hangzhou", "bucket": "bkt",
               "access_key_id": "AK", "access_key_secret": "SK",
               "internal_upload": True}}
CFG_OFF = {"oss": {"enabled": False}}

tmp_png = Path(__file__).resolve().parent / "_tmp_report.png"
tmp_png.write_bytes(b"\x89PNG-stub")

# 1. 未启用 → OssError
oss.reset_for_test()
try:
    oss.upload_and_sign(CFG_OFF, tmp_png, "k")
    check("未启用抛 OssError", False)
except oss.OssError as e:
    check("未启用抛 OssError", "[oss] 未启用" in str(e))

# 2. 配置不完整 → OssError
oss.reset_for_test()
bad = {"oss": {**CFG["oss"], "bucket": ""}}
try:
    oss.upload_and_sign(bad, tmp_png, "k")
    check("bucket 缺失抛 OssError", False)
except oss.OssError as e:
    check("bucket 缺失抛 OssError", "配置不完整" in str(e))

# 3. 上传+签名:内网上传客户端 + 公网签名客户端,签名 URL 返回
oss.reset_for_test()
calls.clear()
url = oss.upload_and_sign(CFG, tmp_png, "qq-reports/1/x.png")
endpoints = [c[1] for c in calls if c[0] == "client"]
check("双客户端:内网上传+公网签名",
      endpoints == ["oss-cn-hangzhou.aliyuncs.com",
                    "oss-cn-hangzhou-internal.aliyuncs.com"])
check("上传对象键与字节数",
      any(c[0] == "put" and c[2] == "qq-reports/1/x.png" and c[3] == 9
          for c in calls))
check("签名请求命中同键并带 TTL(30 分钟)",
      any(c[0] == "presign" and c[1] == "qq-reports/1/x.png"
          and "0:30:00" in c[2] for c in calls))
check("返回签名 URL", url.endswith("sig=STUB"))

# 4. internal_upload=False → 两个客户端都走公网
oss.reset_for_test()
calls.clear()
oss.upload_and_sign({"oss": {**CFG["oss"], "internal_upload": False}},
                     tmp_png, "k2")
check("关内网开关后双公网客户端",
      [c[1] for c in calls if c[0] == "client"]
      == ["oss-cn-hangzhou.aliyuncs.com"] * 2)

# 5. 对象键格式
key = oss.report_key(7734200)
check("对象键格式 qq-reports/<房>/<时间戳>.png",
      key.startswith("qq-reports/7734200/") and key.endswith(".png")
      and len(key.split("/")[-1]) == len("20260101T000000.png"))

# 6. 删除尽力而为:未启用配置也不抛
oss.delete(CFG_OFF, "k")
check("删除失败静默(未启用不抛)", True)
calls.clear()
oss.delete(CFG, "qq-reports/1/x.png")
check("删除命中对象键",
      any(c[0] == "del" and c[2] == "qq-reports/1/x.png" for c in calls))

tmp_png.unlink()
print(f"\n{sum(results)}/{len(results)} 项通过")
sys.exit(0 if all(results) else 1)
