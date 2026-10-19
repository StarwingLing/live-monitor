# -*- coding: utf-8 -*-
"""v2.3.1 改动点的进程内测试：延迟直播拉流兜底 + 自动对账失败退避。

背景：2026-10-19 某主播（房间 <房间号>）开启 300 秒延迟直播，早间
场次 70 分钟持续起流失败（138 条 failed 行）。排查结论：
① 延迟模式下 getRoomPlayInfo 的 flv 参数（protocol=0）不发放任何直链
   （code=0 但 playurl 为空），hls 参数（protocol=1）发放 http_hls 流，
   但旧白名单不收 http_hls——本版 fetch_playurl 第一层改 flv→hls 两遍
   参数，_extract_playurl 白名单补 http_hls；
② 对账循环每 30s 插一条必失败的 requested 行刷爆归档列表——本版
   manager._auto_full_hold 在「本房最近两条 auto 行均 failed」时按退避期
   压住新行插入（单条失败仍保持 30s 快重试）。

覆盖：
1. _extract_playurl：http_hls 解析（URL 拼接 + qn）、flv 优先于 http_hls、
   未知协议跳过、hls_fmp4 回归；
2. fetch_playurl（HTTP 层全部 mock，不打真实 B 站）：
   - 延迟模式：flv 空 → hls 有 http_hls → 取到 URL，两遍参数顺序正确；
   - 普通模式：flv 即有 → 不再发第二遍请求；
   - 老接口兜底回归（L1 全空 → L2 durl）；
   - 三层全空 → RuntimeError；
3. _auto_full_hold：连续两失败+未过退避期=压住 / 超期放开 / 单条失败
   不压 / 非连续不压 / backoff=0 关闭 / ended_at 缺失或畸形不误压。

运行：uv run python tests/test_v2_3_1.py
"""
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay.capture.recording import (
    PLAYINFO_API, PLAYURL_LEGACY_API, ROOM_INDEX_API,
    _extract_playurl, fetch_playurl)
from replay.capture.manager import _auto_full_hold

results: list[bool] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# HTTP mock 基建（fetch_playurl 用：async with session.get(...) as resp）
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, data: dict):
        self._data = data

    async def json(self, content_type=None):
        return self._data


class _Ctx:
    def __init__(self, coro):
        self._coro = coro

    async def __aenter__(self):
        return await self._coro

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """按 (url, params) 分发的假 aiohttp 会话；记录每次调用参数供断言。"""

    def __init__(self, handler):
        self.calls: list[tuple[str, dict]] = []
        self._handler = handler

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _Ctx(self._respond(url, params or {}))

    async def _respond(self, url, params):
        return FakeResp(self._handler(url, params))


def _stream_pi(proto: str, host: str, base: str, extra: str, qn: int) -> dict:
    """构造单流 playurl_info（结构同 B 站 getRoomPlayInfo 响应）。"""
    codec = {"codec_name": "avc", "current_qn": qn,
             "url_info": [{"host": host, "base_url": base, "extra": extra}]}
    stream = {"protocol_name": proto,
              "format": [{"codec": [codec]}]}
    return {"playurl": {"stream": [stream]}}


def _empty_pi() -> dict:
    return {"playurl": None}


# ---------------------------------------------------------------------------
# 1. _extract_playurl
# ---------------------------------------------------------------------------
def test_extract():
    pi = _stream_pi("http_hls", "http://cdn.example/", "live/x.m3u8", "?t=1", 10000)
    got = _extract_playurl(pi)
    check("http_hls 解析 URL 拼接", got == ("http://cdn.example/live/x.m3u8?t=1", 10000),
          f"实际 {got}")

    # flv 与 http_hls 同时存在 → flv 优先
    both = {"playurl": {"stream": [
        _stream_pi("http_hls", "http://cdn.example/", "a.m3u8", "", 10000)["playurl"]["stream"][0],
        _stream_pi("flv", "http://flv.example/", "a.flv", "", 150)["playurl"]["stream"][0],
    ]}}
    got = _extract_playurl(both)
    check("flv 优先于 http_hls", got is not None and got[0].startswith("http://flv.example/"),
          f"实际 {got}")

    # 未知协议 → 跳过 → None
    got = _extract_playurl(_stream_pi("dash", "http://c/", "a", "", 1))
    check("未知协议返回 None", got is None, f"实际 {got}")

    # hls_fmp4 白名单回归
    got = _extract_playurl(_stream_pi("hls_fmp4", "http://h/", "b.m4s", "", 80))
    check("hls_fmp4 白名单回归", got == ("http://h/b.m4s", 80), f"实际 {got}")

    # 空结构防御
    check("空结构返回 None", _extract_playurl({}) is None)
    check("None 返回 None", _extract_playurl(None) is None)


# ---------------------------------------------------------------------------
# 2. fetch_playurl（三层接口 mock）
# ---------------------------------------------------------------------------
def _ok_l1(pi: dict) -> dict:
    return {"code": 0, "data": {"playurl_info": pi}}


def test_fetch_delayed():
    """延迟模式：flv 空 → hls 有 http_hls → 取到；两遍参数顺序正确。"""
    def handler(url, params):
        if url == PLAYINFO_API:
            if params.get("protocol") == 0:
                return _ok_l1(_empty_pi())
            if params.get("protocol") == 1:
                return _ok_l1(_stream_pi("http_hls", "http://cdn/", "x.m3u8", "", 10000))
        return {"code": -1, "message": "unexpected"}

    s = FakeSession(handler)
    url, qn = asyncio.run(fetch_playurl(s, 7734200, 150))
    protos = [p.get("protocol") for u, p in s.calls if u == PLAYINFO_API]
    check("延迟模式取到 http_hls URL", url == "http://cdn/x.m3u8" and qn == 10000,
          f"实际 {url} qn={qn}")
    check("两遍参数顺序 flv→hls", protos == [0, 1], f"实际 {protos}")


def test_fetch_normal():
    """普通模式：flv 即有 → 不发第二遍请求。"""
    def handler(url, params):
        if url == PLAYINFO_API:
            return _ok_l1(_stream_pi("flv", "http://flv/", "a.flv", "", 150))
        return {"code": -1}

    s = FakeSession(handler)
    url, qn = asyncio.run(fetch_playurl(s, 1, 150))
    n_l1 = sum(1 for u, _ in s.calls if u == PLAYINFO_API)
    check("flv 直取", url == "http://flv/a.flv" and qn == 150, f"实际 {url} qn={qn}")
    check("只请求一遍 L1", n_l1 == 1, f"实际 {n_l1} 遍")


def test_fetch_legacy_fallback():
    """L1 两遍全空 → L2 老接口 durl 兜底（回归）。"""
    def handler(url, params):
        if url == PLAYINFO_API:
            return _ok_l1(_empty_pi())
        if url == PLAYURL_LEGACY_API:
            return {"code": 0, "data": {"durl": [{"url": "http://legacy/a.flv?t=9"}],
                                        "current_qn": 150}}
        return {"code": -1}

    s = FakeSession(handler)
    url, qn = asyncio.run(fetch_playurl(s, 1, 150))
    check("L2 老接口兜底", url == "http://legacy/a.flv?t=9", f"实际 {url}")


def test_fetch_all_fail():
    """三层全空 → RuntimeError 且报错含最后层信息。"""
    def handler(url, params):
        if url == PLAYINFO_API:
            return _ok_l1(_empty_pi())
        if url == PLAYURL_LEGACY_API:
            return {"code": -400, "message": "nope"}
        return {"code": -352}

    s = FakeSession(handler)
    try:
        asyncio.run(fetch_playurl(s, 1, 150))
        check("三层全空抛 RuntimeError", False)
    except RuntimeError as e:
        msg = str(e)
        check("三层全空抛 RuntimeError", "三层拉流接口均失败" in msg, f"实际 {msg}")
        # v2.3.1：报错逐层累积（旧实现只留最后一层，掩盖关键线索）
        check("报错含 L1 flv 层", "getRoomPlayInfo(protocol=0) code=0" in msg,
              f"实际 {msg}")
        check("报错含 L1 hls 层", "getRoomPlayInfo(protocol=1) code=0" in msg,
              f"实际 {msg}")
        check("报错含 L2 层", "playUrl code=-400" in msg, f"实际 {msg}")
        check("报错含 L3 层", "getInfoByRoom code=-352" in msg, f"实际 {msg}")


# ---------------------------------------------------------------------------
# 3. _auto_full_hold
# ---------------------------------------------------------------------------
def test_hold():
    now = dt.datetime(2026, 10, 19, 12, 0, 0)
    two_fail = [{"status": "failed", "ended_at": "2026-10-19T11:58:00"},
                {"status": "failed", "ended_at": "2026-10-19T11:57:30"}]
    check("连续两失败·末次 2 分钟前 → 压住",
          _auto_full_hold(two_fail, 300, now))
    check("连续两失败·末次 10 分钟前 → 放开",
          not _auto_full_hold([{**two_fail[0], "ended_at": "2026-10-19T11:50:00"},
                               two_fail[1]], 300, now))
    check("单条失败 → 不压住", not _auto_full_hold([two_fail[0]], 300, now))
    check("空列表 → 不压住", not _auto_full_hold([], 300, now))
    check("第二条非 failed → 不压住",
          not _auto_full_hold([two_fail[0], {"status": "finished",
                                             "ended_at": "2026-10-19T11:57:30"}],
                              300, now))
    check("backoff=0 关闭退避", not _auto_full_hold(two_fail, 0, now))
    check("ended_at 为空 → 不误压",
          not _auto_full_hold([{"status": "failed", "ended_at": ""},
                               two_fail[1]], 300, now))
    check("ended_at 畸形 → 不误压",
          not _auto_full_hold([{"status": "failed", "ended_at": "昨天"},
                               two_fail[1]], 300, now))


def main() -> int:
    test_extract()
    test_fetch_delayed()
    test_fetch_normal()
    test_fetch_legacy_fallback()
    test_fetch_all_fail()
    test_hold()
    total, passed = len(results), sum(results)
    print(f"\n==== test_v2_3_1: {passed}/{total} 通过 ====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
