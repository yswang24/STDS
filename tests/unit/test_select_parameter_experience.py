from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from stds.experience.models import ExperienceContext


module = importlib.import_module("stds.llm.select_parameter_experience")


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
        match_type="parameter-pool",
        similarity=1.0,
        chart_row=10 + index,
        parameter_row=20 + index,
        parameter_text=f"完整参数文本-{index}；V1：提示-{index}；V2：保持整条经验",
        variable_hints={1: f"提示-{index}", 2: "保持整条经验"},
    )


def test_multiple_candidates_are_all_rendered_and_valid_index_is_returned(
    monkeypatch,
):
    candidates = [_context(0), _context(1), _context(2)]
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return schema(index=2, reason="动作语义最符合经验动作-2")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, reason = asyncio.run(module.select_parameter_experience(
        "人工A转身并放置零件",
        "202 010",
        candidates,
    ))

    assert selected == 2
    assert reason == "动作语义最符合经验动作-2"
    assert seen["schema"] is module.ParameterExperiencePick
    prompt = seen["prompt"]
    assert "人工A转身并放置零件" in prompt
    assert "202 010" in prompt
    for index in range(3):
        assert f'"index": {index}' in prompt
        assert f'"experience_id": "exp-{index}"' in prompt
        assert f'"operation_label": "经验动作-{index}"' in prompt
        assert f'"parameter_row": {20 + index}' in prompt
        assert f"完整参数文本-{index}" in prompt
        assert f"提示-{index}" in prompt


def test_no_candidate_and_one_candidate_do_not_call_llm(monkeypatch):
    async def forbidden_structured(_prompt, _schema):
        raise AssertionError("无候选或单候选不应调用 LLM")

    monkeypatch.setattr(module, "structured", forbidden_structured)

    assert asyncio.run(module.select_parameter_experience(
        "转身",
        "202 010",
        [],
    ))[0] is None
    assert asyncio.run(module.select_parameter_experience(
        "转身",
        "202 010",
        [_context(0)],
    ))[0] == 0


def test_only_final_chartcode_candidates_enter_prompt_and_original_index_is_used(
    monkeypatch,
):
    candidates = [
        _context(0, chartcode="999 999", operation_label="错误图表码动作"),
        _context(1, chartcode="202010", operation_label="转身"),
        _context(2, chartcode="202 010", operation_label="弯腰"),
    ]
    seen = {}

    async def fake_structured(prompt, schema):
        seen["prompt"] = prompt
        return schema(index=1, reason="当前动作是转身")

    monkeypatch.setattr(module, "structured", fake_structured)
    selected, _reason = asyncio.run(module.select_parameter_experience(
        "人工A转身",
        "202 010",
        candidates,
    ))

    assert selected == 1
    assert "错误图表码动作" not in seen["prompt"]
    assert '"index": 1' in seen["prompt"]
    assert '"index": 2' in seen["prompt"]


def test_invalid_index_is_rejected_without_clamping_or_first_fallback(monkeypatch):
    candidates = [_context(0), _context(1)]

    async def out_of_range(_prompt, _schema):
        return SimpleNamespace(index=99, reason="越界")

    monkeypatch.setattr(module, "structured", out_of_range)
    selected, reason = asyncio.run(module.select_parameter_experience(
        "人工A转身",
        "202 010",
        candidates,
    ))

    assert selected is None
    assert "索引无效" in reason


def test_non_integer_index_and_llm_exception_are_safe(monkeypatch):
    candidates = [_context(0), _context(1)]

    async def non_integer(_prompt, _schema):
        return SimpleNamespace(index="1", reason="错误类型")

    monkeypatch.setattr(module, "structured", non_integer)
    assert asyncio.run(module.select_parameter_experience(
        "人工A转身",
        "202 010",
        candidates,
    ))[0] is None

    async def malformed_response(_prompt, _schema):
        raise ValueError("not JSON")

    monkeypatch.setattr(module, "structured", malformed_response)
    selected, reason = asyncio.run(module.select_parameter_experience(
        "人工A转身",
        "202 010",
        candidates,
    ))
    assert selected is None
    assert "选择失败" in reason
