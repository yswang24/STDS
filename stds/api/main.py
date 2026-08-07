"""Step 16:FastAPI + SSE 接口层。

端点:
  POST /experience-contexts 上传并校验经验工作簿
  POST /jobs              启动整工位任务
  GET  /jobs/{id}/stream  SSE 流式进度
  GET  /reviews           复核队列(status=need_review)
  POST /reviews/{id}      提交复核(apply_edits + 飞轮回灌)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from stds.cascade.resolver import Deps
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.experience import load_experience_workbook
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

_MAX_EXPERIENCE_FILE_BYTES = 10 * 1024 * 1024
_EXPERIENCE_CONTEXT_TTL_S = 24 * 60 * 60
_MAX_EXPERIENCE_CONTEXTS = 32


@dataclass(frozen=True)
class ExperienceUploadContext:
    """一次已校验上传的不可变快照；作业启动后不再依赖注册表。"""

    context_id: str
    digest: str
    source_name: str
    result: object
    created_at: float
    expires_at: float


class ExperienceContextRegistry:
    """单进程有界上传上下文；生产多进程部署可替换为共享存储。"""

    def __init__(self, *, max_entries: int, ttl_s: float):
        self.max_entries = max(1, int(max_entries))
        self.ttl_s = max(1.0, float(ttl_s))
        self._items: OrderedDict[str, ExperienceUploadContext] = OrderedDict()
        self._digest_ids: dict[str, str] = {}
        self._lock = RLock()

    def _discard(self, context_id: str) -> None:
        context = self._items.pop(context_id, None)
        if context is not None and self._digest_ids.get(context.digest) == context_id:
            self._digest_ids.pop(context.digest, None)

    def _purge_expired(self, now: float) -> None:
        for context_id, context in tuple(self._items.items()):
            if context.expires_at <= now:
                self._discard(context_id)

    def put(self, result: object, source_name: str) -> ExperienceUploadContext:
        now = time.time()
        digest = str(getattr(result, "digest", "") or "")
        with self._lock:
            self._purge_expired(now)
            existing_id = self._digest_ids.get(digest)
            if existing_id:
                existing = self._items.get(existing_id)
                if existing is not None:
                    refreshed = ExperienceUploadContext(
                        context_id=existing.context_id,
                        digest=digest,
                        source_name=source_name or existing.source_name,
                        result=result,
                        created_at=existing.created_at,
                        expires_at=now + self.ttl_s,
                    )
                    self._items[existing_id] = refreshed
                    self._items.move_to_end(existing_id)
                    return refreshed
            context_id = uuid.uuid4().hex
            context = ExperienceUploadContext(
                context_id=context_id,
                digest=digest,
                source_name=source_name,
                result=result,
                created_at=now,
                expires_at=now + self.ttl_s,
            )
            self._items[context_id] = context
            self._digest_ids[digest] = context_id
            while len(self._items) > self.max_entries:
                self._discard(next(iter(self._items)))
            return context

    def get(self, context_id: str) -> Optional[ExperienceUploadContext]:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            context = self._items.get(str(context_id or ""))
            if context is not None:
                self._items.move_to_end(context.context_id)
            return context

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._digest_ids.clear()


_experience_contexts = ExperienceContextRegistry(
    max_entries=_MAX_EXPERIENCE_CONTEXTS,
    ttl_s=_EXPERIENCE_CONTEXT_TTL_S,
)


def _get_deps(
    *,
    use_common_chart: bool = False,
    use_semantic_experience: bool = True,
    experience_context: Optional[ExperienceUploadContext] = None,
) -> Deps:
    result = experience_context.result if experience_context is not None else None
    experience_index = getattr(result, "index", None)
    common_index = getattr(result, "common_index", None)
    if common_index is None and experience_index is not None:
        common_index = getattr(experience_index, "common_index", None)
    if experience_index is not None and not getattr(experience_index, "available", False):
        experience_index = None
    return Deps(
        charts=_charts,
        cache=_cache,
        use_common_chart=use_common_chart,
        use_semantic_experience=use_semantic_experience,
        common_entries=tuple(getattr(result, "common_entries", ()) or ()),
        common_index=common_index,
        part_weight_index=_part_weight_index,
        experience_index=experience_index,
        experience_scope=(
            f"upload:{experience_context.digest}"
            if experience_context is not None
            else ""
        ),
    )


class JobRequest(BaseModel):
    line_name: str = ""
    station_op: str = ""
    use_common_chart: bool = False
    use_semantic_experience: bool = True
    experience_context_id: Optional[str] = None
    llm_backend: Optional[
        Literal["auto", "vllm", "deepseek", "ark", "custom", "ollama", "mock"]
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
    experience_context = None
    if req.experience_context_id:
        experience_context = _experience_contexts.get(req.experience_context_id)
        if experience_context is None:
            raise HTTPException(status_code=404, detail="经验上传上下文不存在或已过期")
    if req.use_common_chart:
        if experience_context is None:
            raise HTTPException(
                status_code=422,
                detail="启用 Common Chart 前必须先上传经验文件并传入 experience_context_id",
            )
        if not getattr(experience_context.result, "common_entries", ()):
            raise HTTPException(
                status_code=422,
                detail="当前经验文件没有有效的 Common_Chart 记录",
            )

    job_id = str(uuid.uuid4())[:8]
    deps_options = {"use_common_chart": req.use_common_chart}
    # 默认值由 _get_deps 契约提供，保留已有只代理 Common
    # 开关的调用方；显式关闭时才需要覆盖默认经验辅助模式。
    if not req.use_semantic_experience:
        deps_options["use_semantic_experience"] = False
    if experience_context is None:
        # 保留旧调用契约，现有不使用上传经验的 JSON 客户端无需改变。
        deps = _get_deps(**deps_options)
    else:
        deps = _get_deps(**deps_options, experience_context=experience_context)
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
        "use_semantic_experience": req.use_semantic_experience,
        "experience_context_id": (
            experience_context.context_id if experience_context else None
        ),
        "experience_digest": (
            experience_context.digest if experience_context else None
        ),
        "llm_backend": req.llm_backend,
        "llm_model": req.llm_model,
    }


def _experience_counts(result: object) -> dict[str, int]:
    index = getattr(result, "index", None)
    return {
        "chartcode": len(getattr(index, "records", ()) or ()),
        "parameter": len(getattr(index, "parameter_records", ()) or ()),
        "common": len(getattr(result, "common_entries", ()) or ()),
        "est_fixed_time": sum(
            1
            for entry in (getattr(result, "common_entries", ()) or ())
            if getattr(getattr(entry, "kind", None), "value", "") == "fixed_time"
        ),
    }


@api.post("/experience-contexts")
async def upload_experience_context(file: UploadFile = File(...)):
    """上传并校验一份经验工作簿，返回后续 JSON 作业可引用的上下文。"""
    source_name = Path(str(file.filename or "experience.xlsx")).name
    if Path(source_name).suffix.casefold() != ".xlsx":
        raise HTTPException(status_code=422, detail="经验文件必须是 .xlsx")
    raw = await file.read(_MAX_EXPERIENCE_FILE_BYTES + 1)
    await file.close()
    if not raw:
        raise HTTPException(status_code=422, detail="经验文件为空")
    if len(raw) > _MAX_EXPERIENCE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="经验文件超过 10 MB 限制")

    result = await asyncio.to_thread(
        load_experience_workbook,
        raw,
        _charts,
        source_name=source_name,
    )
    errors = [issue for issue in result.issues if issue.severity == "error"]
    fatal_errors = [issue for issue in errors if issue.sheet != "Common_Chart"]
    counts = _experience_counts(result)
    if fatal_errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "经验文件校验失败",
                "issues": [asdict(issue) for issue in fatal_errors],
            },
        )
    if not any(counts[key] for key in ("chartcode", "parameter", "common")):
        raise HTTPException(status_code=422, detail="经验文件中没有可用记录")

    context = _experience_contexts.put(result, source_name)
    return {
        "experience_context_id": context.context_id,
        "digest": context.digest,
        "source_name": context.source_name,
        "expires_at": context.expires_at,
        "counts": counts,
        "issues": [asdict(issue) for issue in result.issues],
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
