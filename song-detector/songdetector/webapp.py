"""本地网页校对页（FastAPI）：发起分析、轮询进度、浏览结果、点击跳段播放。

只监听 127.0.0.1（本地工具，见项目 AGENTS.md 的「识别仅本地」边界）。
接口一览：

- ``POST /api/tasks``        ``{"path": "文件或目录"}`` → ``{"task_id": ...}``
- ``GET  /api/tasks/{id}``   ``{"status","stage","progress","error","result_ids"}``
- ``GET  /api/results``      已分析文件摘要列表
- ``GET  /api/results/{rid}`` 完整结果 JSON（含逐帧分数）
- ``GET  /api/media/{rid}``  源媒体文件（FileResponse，浏览器可拖动播放）
- ``GET  /``                 校对页（static/index.html）
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
import re
import threading
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from songdetector.analyze import analyze_path
from songdetector.config import RESULTS_DIR

# 结果 ID（stem）合法形态：非空且不含路径分隔符（/ 与 \ 是穿越关键，
# 其余字符如中文/空格放行——stem 生成时已清理过 Windows 非法字符）
_SAFE_RID = re.compile(r"^[^\\/]+$")


def _is_safe_rid(rid: str) -> bool:
    """结果 ID 安全校验：拒绝路径穿越与空 ID（详见 _SAFE_RID 说明）。"""
    if not rid or rid in (".", ".."):
        return False
    return bool(_SAFE_RID.match(rid))


class TaskRequest(BaseModel):
    """POST /api/tasks 的请求体。

    注意：必须定义在**模块级**——本文件启用了 PEP 563（延迟注解求值），
    函数内定义的模型类注解会以字符串形式留存且无法在模块命名空间解析，
    FastAPI 会把它误判为 query 标量参数。
    """

    path: str


def _results_dir(cfg: dict) -> pathlib.Path:
    """结果目录（config 的 output.dir 可覆盖，默认项目 results/）。"""
    return pathlib.Path(cfg.get("output", {}).get("dir") or RESULTS_DIR)


def create_app(cfg: dict) -> FastAPI:
    app = FastAPI(title="song-detector 校对页", docs_url=None, redoc_url=None)
    results_dir = _results_dir(cfg)

    # ---- 分析任务状态（内存态；网页是常驻进程，任务表不必落盘）----
    tasks: dict[str, dict] = {}
    lock = threading.Lock()

    def _update(task_id: str, **fields) -> None:
        with lock:
            tasks[task_id].update(fields)

    def _run_task(task_id: str, path: str) -> None:
        """后台线程：跑 analyze_path，把进度/结果回写到任务表。"""
        def progress(stage: str, frac: float) -> None:
            _update(task_id, stage=stage, progress=min(frac, 1.0))

        try:
            entries = analyze_path(path, cfg, progress)
        except Exception as exc:  # 防御：任何异常都转成任务失败态
            _update(task_id, status="error", error=f"{exc!r}")
            return
        ok = [e for e in entries if e["ok"]]
        if not ok:
            first_error = entries[0].get("error", "未知错误")
            _update(task_id, status="error", error=first_error)
            return
        _update(
            task_id,
            status="done",
            progress=1.0,
            stage="完成",
            result_ids=[e["result"]["stem"] for e in ok],
            errors=[e.get("error") for e in entries if not e["ok"]],
        )

    @app.post("/api/tasks")
    def create_task(req: TaskRequest):
        path = pathlib.Path(req.path).expanduser()
        if not path.exists():
            raise HTTPException(400, f"路径不存在：{req.path}")
        task_id = uuid.uuid4().hex[:12]
        with lock:
            tasks[task_id] = {
                "id": task_id,
                "status": "running",
                "stage": "启动",
                "progress": 0.0,
                "error": None,
                "result_ids": [],
                "errors": [],
            }
        threading.Thread(target=_run_task, args=(task_id, str(path)), daemon=True).start()
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        with lock:
            task = tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return task

    # ---- 结果浏览 ----

    def _load_result(rid: str) -> dict:
        """按 stem 读取结果 JSON（防路径穿越：见 _is_safe_rid）。"""
        if not _is_safe_rid(rid):
            raise HTTPException(400, "非法的结果 ID")
        path = results_dir / f"{rid}.analysis.json"
        if not path.is_file():
            raise HTTPException(404, "结果不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/results")
    def list_results():
        if not results_dir.is_dir():
            return []
        items = []
        for jp in results_dir.glob("*.analysis.json"):
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # 写一半/损坏的文件直接跳过
            items.append(
                {
                    "id": data.get("stem", jp.stem.removesuffix(".analysis")),
                    "file": data.get("source", {}).get("file", "?"),
                    "duration_sec": data.get("source", {}).get("duration_sec", 0),
                    "singing_count": len(data.get("segments", {}).get("singing", [])),
                    "music_count": len(data.get("segments", {}).get("music", [])),
                    "analyzed_at": data.get("analyzed_at", ""),
                }
            )
        items.sort(key=lambda x: x["analyzed_at"], reverse=True)
        return items

    @app.get("/api/results/{rid}")
    def get_result(rid: str):
        return _load_result(rid)

    @app.get("/api/media/{rid}")
    def get_media(rid: str):
        data = _load_result(rid)
        src = pathlib.Path(data["source"]["path"])
        if not src.is_file():
            raise HTTPException(404, f"源文件已不在本机：{src}")
        media_type = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
        return FileResponse(src, media_type=media_type, filename=src.name)

    # ---- 静态校对页（挂载放最后：FastAPI 按注册顺序匹配，API 路由优先）----
    static_dir = pathlib.Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app
