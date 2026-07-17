"""P0 + P1 修复验证。"""
from __future__ import annotations

import asyncio

import pytest

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.common_chart import load_common_chart, match_common_chart
from stds.domain.models import Source, StdsElement, StdsResult
from stds.engine.decision_codec import decode
from stds.llm.prompts import load_prompt


# ---------- #1 L2 不再误匹配 ----------


def test_l2_no_description_in_match():
    """10DM 应匹配 fv=10(25DMX),不是 fv=4(10DMX)。"""
    charts = load_charts()
    v, _ = decode(charts["020 02A"], "WFB,WPF,D,10DM,NIBU")
    assert v[4] == 10.0


def test_l2_exact_numeric_match():
    charts = load_charts()
    v, _ = decode(charts["202 010"], "T,90,NB")
    assert v[2] == 0.012


# ---------- #2 common_chart ----------


def test_common_chart_load():
    assert len(load_common_chart()) == 33


def test_common_chart_keyword_match():
    hit = match_common_chart("转身90度", load_common_chart())
    assert hit is not None and hit.chartcode == "202 010"


def test_common_chart_short_keyword_rejected():
    """2 字符关键词不匹配(避免'拿取'宽泛命中)。"""
    hit = match_common_chart("拿取泡棉", load_common_chart())
    assert hit is None  # "拿取"只有2 字符


def test_resolver_common_chart_fast_path():
    charts = load_charts()
    deps = Deps(charts=charts, cache=AutoCache())
    el = StdsElement(1, "转身90度", "L", "S", freq=1.0, norm_key="转身90度")
    res = asyncio.run(resolve(el, deps))
    assert res.source == Source.CACHE and res.chartcode == "202 010"


# ---------- #3 T1 seed from common_chart ----------


def test_common_chart_as_t1_seed():
    from stds.retrieval.embed import MockEmbed
    from stds.retrieval.history_index import HistoryIndex

    idx = HistoryIndex(MockEmbed())
    rows = [{"操作内容": r["操作内容"], "动作代码": r["动作代码"], "决策描述": r["决策描述"] or ""} for r in load_common_chart()]
    idx.build_from_edited(rows)
    hits = idx.knn("转身90度", k=3)
    assert hits[0].chartcode == "202 010"


# ---------- #5 FastAPI asyncio ----------


def test_api_jobs_endpoint_async():
    """POST /jobs 用 asyncio.create_task(不是 asyncio.run)。"""
    from fastapi.testclient import TestClient
    from stds.api.main import create_app

    client = TestClient(create_app())
    resp = client.post("/jobs", json={"line_name": "L", "station_op": "S"})
    assert resp.status_code == 200
    assert "job_id" in resp.json()


# ---------- #6 LLM default bias ----------


def test_prompt_contains_pick_rules():
    """pick_value prompt 应包含选值规则。"""
    prompt = load_prompt("pick_value")
    assert "操作描述" in prompt
    assert "下标" in prompt or "index" in prompt
