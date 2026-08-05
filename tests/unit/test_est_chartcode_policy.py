from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.domain.chartcode_policy import general_chart_candidates
from stds.domain.models import Source, StdsElement
from stds.retrieval.chartcode_index import ChartcodeIndex
from stds.retrieval.embed import MockEmbed
from stds.retrieval.history_index import HistoryIndex


async def _human(_text):
    return False


async def _first_option(_operation, candidates):
    return candidates[0], 1.0, "test-first-option"


def _element(operation: str) -> StdsElement:
    return StdsElement(1, operation, "L", "S", 1.0, operation)


def test_general_chart_candidates_exclude_est_codes():
    candidates = general_chart_candidates(load_charts())
    assert "EST C00" not in candidates
    assert "EST V00" not in candidates
    assert len(candidates) == 62


def test_general_chartcode_index_excludes_est_codes():
    index = ChartcodeIndex(MockEmbed())
    index.build(load_charts())
    indexed_codes = {code for code, _title, _vector in index._chartcodes}
    assert "EST C00" not in indexed_codes
    assert "EST V00" not in indexed_codes
    assert len(indexed_codes) == 62


def test_default_llm_selector_hides_and_rejects_est_codes(monkeypatch):
    module = importlib.import_module("stds.llm.select_chartcode")
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        return schema(Chartcode="EST C00")

    monkeypatch.setattr(module, "structured", fake_structured)
    result = asyncio.run(module.select_chartcode("普通人工动作", load_charts()))

    assert "EST C00" not in seen["prompt"]
    assert "EST V00" not in seen["prompt"]
    assert result is None


def test_resolver_rejects_est_returned_by_unrestricted_selector():
    seen = {}

    async def invalid_selector(_operation, charts):
        seen["charts"] = charts
        return "EST C00"

    result = asyncio.run(resolve(
        _element("无法识别的人工动作"),
        Deps(
            charts=load_charts(),
            cache=AutoCache(),
            llm_classify=_human,
            llm_select_chartcode=invalid_selector,
            llm_pick_value=_first_option,
        ),
    ))

    assert "EST C00" not in seen["charts"]
    assert "EST V00" not in seen["charts"]
    assert result.source == Source.UNRESOLVED
    assert result.chartcode is None


def test_history_cannot_reuse_est_code_without_explicit_experience():
    class EstHistory:
        async def knn(self, _operation, k=5):
            return [
                SimpleNamespace(
                    text="历史固定估算",
                    chartcode="EST C00",
                    decision="5S",
                    score=0.99,
                )
                for _ in range(3)
            ]

    selector_called = {"value": False}

    async def no_general_match(_operation, charts):
        selector_called["value"] = True
        assert "EST C00" not in charts
        return None

    result = asyncio.run(resolve(
        _element("与历史估算相似的人工动作"),
        Deps(
            charts=load_charts(),
            cache=AutoCache(),
            history_index=EstHistory(),
            llm_classify=_human,
            llm_select_chartcode=no_general_match,
            llm_pick_value=_first_option,
        ),
    ))

    assert selector_called["value"] is True
    assert result.source == Source.UNRESOLVED
    assert result.chartcode is None


def test_est_codes_are_not_added_to_general_history_index():
    index = HistoryIndex(MockEmbed())
    index.build_from_edited([{
        "操作内容": "固定估算历史",
        "动作代码": "EST C00",
        "决策描述": "5S",
    }])
    assert asyncio.run(index.knn("固定估算历史", k=1)) == []

    index.add(
        "人工确认的固定估算",
        SimpleNamespace(chartcode="EST V00", decision="5S"),
    )
    assert asyncio.run(index.knn("人工确认的固定估算", k=1)) == []


def test_explicit_experience_can_select_est_code():
    class EstExperienceIndex:
        available = True
        source_name = "test-experience.xlsx"
        parameter_records = ()

        async def match_chartcode_semantic(
            self,
            _operation,
            *,
            expected_chartcode=None,
        ):
            if expected_chartcode not in (None, "EST C00"):
                return None
            return SimpleNamespace(
                experience_id="est-c00",
                operation_label="黏贴",
                chartcode="EST C00",
                match_type="semantic",
                similarity=1.0,
                chart_row=2,
                parameter_row=None,
                variable_hints={},
            )

    async def selector_must_not_run(_operation, _charts):
        raise AssertionError("明确 EST 经验命中后不应调用普通 LLM 选码")

    result = asyncio.run(resolve(
        _element("人工黏贴保护膜"),
        Deps(
            charts=load_charts(),
            cache=AutoCache(),
            experience_index=EstExperienceIndex(),
            experience_scope="experience:test",
            llm_classify=_human,
            llm_select_chartcode=selector_must_not_run,
            llm_pick_value=_first_option,
        ),
    ))

    assert result.source == Source.FORMULA
    assert result.chartcode == "EST C00"
    assert result.decision == "5S"
    assert result.time_s == 5.0
    assert result.cv == "C"
