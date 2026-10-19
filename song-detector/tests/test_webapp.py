"""webapp.py 快速测试：API 语义与任务错误流（不加载模型，秒级完成）。

任务成功流（真分析）已由 test_smoke_e2e + 手动浏览器冒烟覆盖；
这里覆盖：空列表、非法路径 400、非媒体文件的快速失败任务、404 防御。
"""

from __future__ import annotations

import pathlib
import time

from fastapi.testclient import TestClient

from songdetector.config import load_config
from songdetector.webapp import create_app


def make_client() -> TestClient:
    return TestClient(create_app(load_config()))


class TestBasics:
    def test_index_served(self):
        with make_client() as client:
            r = client.get("/")
            assert r.status_code == 200
            assert "校对台" in r.text

    def test_results_empty_dir(self):
        with make_client() as client:
            r = client.get("/api/results")
            assert r.status_code == 200
            assert isinstance(r.json(), list)

    def test_post_nonexistent_path_400(self):
        with make_client() as client:
            r = client.post("/api/tasks", json={"path": "X:/不存在.mp4"})
            assert r.status_code == 400
            assert "路径不存在" in r.text

    def test_result_detail_404_and_traversal_guard(self):
        with make_client() as client:
            assert client.get("/api/results/不存在").status_code == 404
            assert client.get("/api/media/不存在").status_code == 404


class TestRidGuard:
    """rid 校验的穷尽单测（HTTP 客户端会做 URL 规范化，端点层探测不可靠，
    故直接测校验函数本身——它才是防路径穿越的关键）。"""

    def test_unsafe_rids_rejected(self):
        from songdetector.webapp import _is_safe_rid

        # 注：HTTP 层已做 URL 解码，含 / 或 \ 的 rid 到达此处即被拒；
        # 字面 "..%2F" 只是普通文件名（无分隔符），不构成穿越，属放行范围
        for bad in ("", ".", "..", "../..", "a/b", "a\\b", "/etc/passwd"):
            assert not _is_safe_rid(bad), f"{bad!r} 应被拒绝"

    def test_normal_rids_accepted(self):
        from songdetector.webapp import _is_safe_rid

        for good in ("full_30__seg_001", "_tmp__网页冒烟", "a b.mp4__x",
                     "目录__文件 名", "abc.def"):
            assert _is_safe_rid(good), f"{good!r} 应放行"


class TestTaskErrorFlow:
    def test_non_media_file_fails_fast(self, tmp_path: pathlib.Path):
        """非媒体文件：ffprobe 快速报错，任务转 error，全程不加载模型。"""
        bad = tmp_path / "这只是文本.txt"
        bad.write_text("不是媒体", encoding="utf-8")
        with make_client() as client:
            r = client.post("/api/tasks", json={"path": str(bad)})
            assert r.status_code == 200
            task_id = r.json()["task_id"]
            # 快速失败流应在数秒内到达终态（ffprobe 报错 → AnalysisError）
            for _ in range(20):
                t = client.get(f"/api/tasks/{task_id}").json()
                if t["status"] != "running":
                    break
                time.sleep(0.3)
            assert t["status"] == "error"
            assert t["error"]
