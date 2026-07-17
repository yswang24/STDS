"""Step 14:asyncio 并发执行器。按记录 fan-out + 限流。"""
from __future__ import annotations

import asyncio
from typing import Callable

from stds.config.settings import settings
from stds.cascade.resolver import Deps, resolve
from stds.data.repo import load_records_by_station
from stds.pipeline.state import RecordStatus, StateManager


class EventBus:
    """简单的内存事件总线,供 SSE 推送。"""

    def __init__(self):
        self._queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(job_id, []).append(q)
        return q

    async def publish(self, job_id: str, record_no: int, result):
        data = {"record_no": record_no, "time_s": result.time_s if hasattr(result, "time_s") else None}
        for q in self._queues.get(job_id, []):
            await q.put(data)


BUS = EventBus()


async def run_station(line: str, station: str, job_id: str, deps: Deps, state: StateManager):
    """并发处理整个工位:加载元素 -> 限流 resolve -> mark状态 -> 推事件。"""
    # 清理过期状态(防累积)
    state.cleanup_old_jobs(ttl_days=settings.STATE_TTL_DAYS)
    els = load_records_by_station(line, station)
    state.mark_many(job_id, els, RecordStatus.COMPUTING)
    sem = asyncio.Semaphore(settings.CONCURRENCY_LIMIT)

    async def one(el):
        async with sem:
            try:
                res = await resolve(el, deps)
                status = RecordStatus.NEED_REVIEW if res.needs_review else RecordStatus.DONE
                state.mark(job_id, el.number, status, res)
                await BUS.publish(job_id, el.number, res)
            except Exception as e:
                state.mark(job_id, el.number, RecordStatus.NEED_REVIEW, error=str(e))

    await asyncio.gather(*(one(e) for e in els))
