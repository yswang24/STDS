"""Step 16:FastAPI + SSE 接口层。

端点:
  POST /jobs              启动整工位任务
  GET  /jobs/{id}/stream  SSE 流式进度
  GET  /reviews           复核队列(status=need_review)
  POST /reviews/{id}      提交复核(apply_edits + 飞轮回灌)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from stds.cascade.resolver import Deps
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.llm.client import llm_runtime
from stds.pipeline.runner import BUS, run_station
from stds.pipeline.state import RecordStatus, StateManager
from stds.retrieval.part_weight_index import load_part_weight_index
from stds.review.apply import apply_edits
from stds.review.flywheel import on_review_confirmed

api = FastAPI(title="STDS 工时分析系统")

# 全局单例(实际部署时用依赖注入)
_state = StateManager()
_charts = load_charts()
_cache = AutoCache()
_part_weight_index = load_part_weight_index()


def _get_deps(*, use_common_chart: bool = False) -> Deps:
    return Deps(
        charts=_charts,
        cache=_cache,
        use_common_chart=use_common_chart,
        part_weight_index=_part_weight_index,
    )


class JobRequest(BaseModel):
    line_name: str = ""
    station_op: str = ""
    use_common_chart: bool = False
    llm_backend: Optional[
        Literal["auto", "vllm", "custom", "ollama", "mock"]
    ] = None
    llm_model: Optional[str] = None
    ollama_base_url: Optional[str] = None


async def _run_station_with_llm(
    req: JobRequest,
    job_id: str,
    deps: Deps,
) -> None:
    """在任务级上下文中运行，避免并发作业的 Ollama 模型配置相互污染。"""
    with llm_runtime(
        backend=req.llm_backend,
        model=req.llm_model,
        ollama_base_url=req.ollama_base_url,
    ):
        await run_station(req.line_name, req.station_op, job_id, deps, _state)


@api.post("/jobs")
async def start_job(req: JobRequest):
    """启动整工位任务。用 asyncio.create_task 在同一事件循环里运行,确保 SSE 能推到。"""
    job_id = str(uuid.uuid4())[:8]
    deps = _get_deps(use_common_chart=req.use_common_chart)
    # 在返回 job_id 前先验证运行时配置；后台任务内会重新进入同一配置上下文。
    try:
        with llm_runtime(
            backend=req.llm_backend,
            model=req.llm_model,
            ollama_base_url=req.ollama_base_url,
        ):
            pass
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    asyncio.create_task(_run_station_with_llm(req, job_id, deps))
    return {
        "job_id": job_id,
        "use_common_chart": req.use_common_chart,
        "llm_backend": req.llm_backend,
        "llm_model": req.llm_model,
    }


@api.get("/jobs/{job_id}/stream")
async def stream(job_id: str):
    q = BUS.subscribe(job_id)

    async def gen():
        while True:
            data = await q.get()
            yield {"event": "update", "data": json.dumps(data, default=str, ensure_ascii=False)}

    return EventSourceResponse(gen())


@api.get("/reviews")
def list_reviews(job_id: str):
    return _state.list_by_status(job_id, RecordStatus.NEED_REVIEW)


class ReviewEdits(BaseModel):
    chartcode: Optional[str] = None
    decision: Optional[str] = None
    time_s: Optional[float] = None


@api.post("/reviews/{job_id}/{record_no}")
async def submit_review(job_id: str, record_no: int, edits: ReviewEdits):
    rec = _state.get(job_id, record_no)
    if not rec:
        return {"error": "not found"}
    # 反序列化 result(简化:从 result_json 取,实际应用 StdsResult)
    result_json = rec.get("result_json")
    # 这里简化:只更新状态标记为 done,实际需 apply_edits + 飞轮回灌
    _state.mark(job_id, record_no, RecordStatus.DONE, result={"edited": True, **edits.model_dump()})
    return {"ok": True}


def create_app() -> FastAPI:
    return api
