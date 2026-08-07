from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from stds.domain.models import MostChart
from stds.experience.models import ExperienceContext


module = importlib.import_module("stds.llm.select_chartcode_experience")


def _chart(chartcode: str, title: str) -> MostChart:
    return MostChart(
        chartcode=chartcode,
        title=title,
        formula="1",
        value_added=False,
        developed_in_seconds=True,
        options={},
    )


def _context(
    index: int,
    *,
    chartcode: str = "202 010",
    operation_label: str | None = None,
) -> ExperienceContext:
    return ExperienceContext(
        experience_id=f"exp-{index}",
        operation_label=operation_label or f"经验动作-{index}",
        chartcode=chartcode,
        match_type="chartcode-pool",
        similarity=1.0,
        chart_row=10 + index,
        parameter_row=20 + index,
        parameter_text=f"不得暴露的参数文本-{index}",
        variable_hints={1: f"不得暴露的变量提示-{index}"},
    )


def test_all_valid_rows_are_rendered_without_parameter_content(monkeypatch):
    candidates = [
        _context(0, operation_label="转身"),
        _context(1, operation_label="弯腰"),
        _context(2, chartcode="017 071", operation_label="移动吊具"),
    ]
    charts = {
        "202 010": _chart("202 010", "Body motion"),
        "017 071": _chart("017 071", "Move with hoist"),
    }
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return schema(index=2, reason="动作描述为移动吊具")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A移动吊具",
        candidates,
        charts,
    ))

    assert selected == 2
    assert reason == "动作描述为移动吊具"
    assert seen["schema"] is module.ChartcodeExperiencePick
    prompt = seen["prompt"]
    assert "人工A移动吊具" in prompt
    for index in range(3):
        assert f'"index": {index}' in prompt
        assert f'"experience_id": "exp-{index}"' in prompt
        assert f'"chart_row": {10 + index}' in prompt
    assert '"chart_title": "Body motion"' in prompt
    assert '"chart_title": "Move with hoist"' in prompt
    assert "不得暴露的参数文本" not in prompt
    assert "不得暴露的变量提示" not in prompt
    assert '"parameter_text"' not in prompt
    assert '"variable_hints"' not in prompt


def test_duplicate_chartcodes_remain_independent_rows(monkeypatch):
    candidates = [
        _context(0, operation_label="转身"),
        _context(1, operation_label="弯腰"),
    ]
    charts = {"202 010": _chart("202 010", "Body motion")}
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        return schema(index=1, reason="当前动作是弯腰")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, _reason = asyncio.run(module.select_chartcode_experience(
        "人工A弯腰",
        candidates,
        charts,
    ))

    assert selected == 1
    assert seen["prompt"].count('"chartcode": "202 010"') == 2
    assert '"operation_label": "转身"' in seen["prompt"]
    assert '"operation_label": "弯腰"' in seen["prompt"]


def test_unavailable_chart_is_filtered_and_original_index_is_returned(monkeypatch):
    candidates = [
        _context(0, chartcode="999 999", operation_label="无效经验"),
        _context(1, chartcode="202010", operation_label="转身"),
    ]
    charts = {"202 010": _chart("202 010", "Body motion")}
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        return schema(index=1, reason="匹配转身")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, _reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身",
        candidates,
        charts,
    ))

    assert selected == 1
    assert '"index": 0' not in seen["prompt"]
    assert "无效经验" not in seen["prompt"]
    assert '"index": 1' in seen["prompt"]


def test_est_candidate_is_allowed_when_present_in_charts(monkeypatch):
    candidates = [_context(0, chartcode="EST C00", operation_label="等待5秒")]
    charts = {"EST C00": _chart("EST C00", "Custom constant time")}
    calls = []

    async def fake_structured(prompt, schema):
        calls.append(prompt)
        return schema(index=0, reason="显式经验与等待动作一致")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, _reason = asyncio.run(module.select_chartcode_experience(
        "等待5秒",
        candidates,
        charts,
    ))

    assert selected == 0
    assert len(calls) == 1
    assert "EST C00" in calls[0]


def test_single_candidate_still_calls_llm_and_can_be_rejected(monkeypatch):
    candidates = [_context(0, operation_label="转身")]
    charts = {"202 010": _chart("202 010", "Body motion")}
    calls = []

    async def fake_structured(prompt, schema):
        calls.append(prompt)
        return schema(index=-1, reason="该经验与移动吊具无关")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A移动吊具",
        candidates,
        charts,
    ))

    assert selected is None
    assert reason == "该经验与移动吊具无关"
    assert len(calls) == 1


def test_no_valid_candidates_do_not_call_llm(monkeypatch):
    async def forbidden_structured(_prompt, _schema):
        raise AssertionError("没有有效候选时不应调用 LLM")

    monkeypatch.setattr(module, "structured", forbidden_structured)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身",
        [_context(0, chartcode="999 999")],
        {"202 010": _chart("202 010", "Body motion")},
    ))

    assert selected is None
    assert "没有可用" in reason


def test_invalid_indexes_and_types_never_fall_back_to_first(monkeypatch):
    candidates = [_context(0), _context(1)]
    charts = {"202 010": _chart("202 010", "Body motion")}

    async def out_of_range(_prompt, _schema):
        return SimpleNamespace(index=99, reason="越界")

    monkeypatch.setattr(module, "structured", out_of_range)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身", candidates, charts,
    ))
    assert selected is None
    assert "索引无效" in reason

    async def wrong_type(_prompt, _schema):
        return SimpleNamespace(index="0", reason="错误类型")

    monkeypatch.setattr(module, "structured", wrong_type)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身", candidates, charts,
    ))
    assert selected is None
    assert "类型无效" in reason

    async def bool_index(_prompt, _schema):
        return SimpleNamespace(index=True, reason="布尔值")

    monkeypatch.setattr(module, "structured", bool_index)
    assert asyncio.run(module.select_chartcode_experience(
        "人工A转身", candidates, charts,
    ))[0] is None


def test_llm_exception_and_invalid_candidate_container_are_safe(monkeypatch):
    candidates = [_context(0)]
    charts = {"202 010": _chart("202 010", "Body motion")}

    async def malformed_response(_prompt, _schema):
        raise ValueError("not JSON")

    monkeypatch.setattr(module, "structured", malformed_response)
    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身", candidates, charts,
    ))
    assert selected is None
    assert "选择失败" in reason

    selected, reason = asyncio.run(module.select_chartcode_experience(
        "人工A转身", 42, charts,
    ))
    assert selected is None
    assert "候选无效" in reason
