"""M2 测试:Step 13-16 复核闭环各层验证。"""
from __future__ import annotations

import asyncio
import os

import pytest

from stds.domain.models import Source, StdsElement, StdsResult


# ---------- Step 13: 状态表 ----------


def _make_state():
    """创建临时状态 DB(测试隔离)。"""
    from stds.pipeline.state import StateManager, _STATE_DB
    # 确保状态 DB 存在
    sm = StateManager()
    return sm


def test_state_mark_and_list():
    sm = _make_state()
    sm.mark("job1", 1, "computing")
    sm.mark("job1", 2, "need_review")
    sm.mark("job1", 3, "done")
    pending = sm.list_by_status("job1", "pending")
    assert len(pending) == 0
    reviewing = sm.list_by_status("job1", "need_review")
    assert len(reviewing) == 1 and reviewing[0]["record_no"] == 2
    sm.close()


def test_state_mark_many():
    from stds.domain.models import StdsElement
    sm = _make_state()
    els = [StdsElement(10, "a", "L", "S"), StdsElement(11, "b", "L", "S")]
    sm.mark_many("job2", els, "computing")
    rows = sm.list_by_status("job2", "computing")
    assert len(rows) == 2
    sm.close()


def test_state_upsert():
    sm = _make_state()
    sm.mark("job3", 1, "pending")
    sm.mark("job3", 1, "done")  # upsert
    rec = sm.get("job3", 1)
    assert rec["status"] == "done"
    sm.close()


# ---------- Step 15: apply_edits + flywheel ----------


def test_apply_edits():
    from stds.data.charts_loader import load_charts
    from stds.review.apply import apply_edits

    el = StdsElement(1, "扫描", "L", "S", freq=1.0)
    original = StdsResult(
        el, "060 010", "LS,", 1.2, "V", 1.0, Source.FORMULA, 0.7, False
    )
    edited = apply_edits(original, {"chartcode": "060 010", "time_s": 1.5})
    assert edited.time_s == 1.5
    assert edited.edited is True
    assert edited.needs_review is False
    assert edited.confidence == 1.0


def test_flywheel_cache_hit():
    from stds.data.cache import AutoCache
    from stds.review.flywheel import on_review_confirmed

    cache = AutoCache()

    class FakeDeps:
        pass

    FakeDeps.cache = cache
    FakeDeps.history_index = None
    FakeDeps.goldens = []

    el = StdsElement(1, "扫码", "L", "S", norm_key="扫码")
    result = StdsResult(el, "060 010", "LS,", 1.2, "V", 1.0, Source.FORMULA, 1.0, False)
    on_review_confirmed(el, result, FakeDeps())
    assert cache.get("扫码") is result  # T0 命中
    assert result.edited is True


# ---------- Step 14: 并发 mark ----------


def test_runner_concurrent_mark():
    """并发 mark 不串号。"""
    sm = _make_state()
    els = [StdsElement(i, f"op{i}", "L", "S") for i in range(20, 30)]

    async def mark_all():
        sem = asyncio.Semaphore(5)

        async def one(el):
            async with sem:
                sm.mark("job_concurrent", el.number, "done")

        await asyncio.gather(*(one(e) for e in els))

    asyncio.run(mark_all())
    done = sm.list_by_status("job_concurrent", "done")
    assert len(done) == 10
    sm.close()


# ---------- Step 16: FastAPI 端点 ----------


def test_api_reviews_endpoint():
    """GET /reviews 返回 need_review 列表。"""
    from fastapi.testclient import TestClient
    from stds.api.main import create_app, _state

    app = create_app()
    client = TestClient(app)

    # 先插入一条 need_review
    _state.mark("test_job", 1, "need_review")
    resp = client.get("/reviews?job_id=test_job")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["status"] == "need_review"


def test_api_submit_review():
    """POST /reviews/{job_id}/{no} 更新状态为 done。"""
    from fastapi.testclient import TestClient
    from stds.api.main import create_app, _state

    app = create_app()
    client = TestClient(app)

    _state.mark("test_job2", 5, "need_review")
    resp = client.post("/reviews/test_job2/5", json={"time_s": 2.0})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rec = _state.get("test_job2", 5)
    assert rec["status"] == "done"
