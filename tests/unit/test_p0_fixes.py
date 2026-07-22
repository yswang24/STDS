"""P0 + P1 修复验证。"""
from __future__ import annotations

import asyncio

import pytest

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.common_chart import load_common_chart, match_common_chart
from stds.domain.models import Source, StdsElement, StdsResult
from stds.engine.decision_codec import decode, decode_with_trace
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


def test_decode_with_trace_keeps_each_variable_choice():
    charts = load_charts()
    values, low_conf, trace = decode_with_trace(charts["202 010"], "T,90,NB")
    assert values
    assert low_conf is False
    assert trace
    assert all(len(step) == 3 and step[0].startswith("V") for step in trace)


def test_decode_marks_unmatched_token_low_confidence():
    charts = load_charts()
    _, low_conf, trace = decode_with_trace(charts["050 03B"], "BOGUS_TOKEN")
    assert low_conf is True
    assert any("unmatched-token" in step[2] for step in trace)


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
    deps = Deps(charts=charts, cache=AutoCache(), use_common_chart=True)
    el = StdsElement(1, "转身90度", "L", "S", freq=1.0, norm_key="转身90度")
    res = asyncio.run(resolve(el, deps))
    assert res.source == Source.CACHE and res.chartcode == "202 010"
    assert any(step[0].startswith("V") for step in res.trace)


def test_resolver_defaults_to_common_chart_disabled():
    async def no_chartcode(operation, charts):
        return None

    charts = load_charts()
    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=no_chartcode,
    )
    el = StdsElement(1, "转身90度", "L", "S", freq=1.0, norm_key="转身90度")
    res = asyncio.run(resolve(el, deps))

    assert deps.common_rows == []
    assert res.source == Source.UNRESOLVED
    assert not any(
        isinstance(step, (list, tuple))
        and step
        and str(step[0]).startswith("T0.5_common")
        for step in res.trace
    )


def test_disabling_common_chart_skips_its_existing_t0_cache():
    async def no_chartcode(operation, charts):
        return None

    charts = load_charts()
    cache = AutoCache()
    el = StdsElement(1, "转身90度", "L", "S", freq=1.0, norm_key="转身90度")

    enabled_result = asyncio.run(
        resolve(el, Deps(charts=charts, cache=cache, use_common_chart=True))
    )
    disabled_result = asyncio.run(
        resolve(
            el,
            Deps(
                charts=charts,
                cache=cache,
                use_common_chart=False,
                llm_select_chartcode=no_chartcode,
            ),
        )
    )

    assert enabled_result.source == Source.CACHE
    assert disabled_result.source == Source.UNRESOLVED


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
    assert resp.json()["use_common_chart"] is False


def test_api_jobs_forwards_common_chart_switch(monkeypatch):
    from fastapi.testclient import TestClient
    from stds.api import main

    captured = {}
    original_get_deps = main._get_deps

    def capture_get_deps(*, use_common_chart=False):
        captured["use_common_chart"] = use_common_chart
        return original_get_deps(use_common_chart=use_common_chart)

    monkeypatch.setattr(main, "_get_deps", capture_get_deps)
    client = TestClient(main.create_app())
    resp = client.post(
        "/jobs",
        json={
            "line_name": "L",
            "station_op": "S",
            "use_common_chart": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["use_common_chart"] is False
    assert captured["use_common_chart"] is False


# ---------- #6 LLM default bias ----------


def test_prompt_contains_pick_rules():
    """pick_value prompt 应包含选值规则。"""
    prompt = load_prompt("pick_value")
    assert "操作描述" in prompt
    assert "下标" in prompt or "index" in prompt
