"""最终展示操作内容的中文化规则。"""
from __future__ import annotations

import asyncio

from stds.llm.translate_operation import (
    TranslateOperationOut,
    build_translate_operation_prompt,
    contains_latin_letters,
    translate_operation_for_output,
)


def test_translate_prompt_requires_part_names_to_be_preserved():
    prompt = build_translate_operation_prompt("Manual install Front End Module")
    assert "专业零件名称" in prompt
    assert "必须保留原文" in prompt
    assert "待处理操作内容：Manual install Front End Module" in prompt


def test_chinese_only_operation_skips_llm(monkeypatch):
    async def should_not_call(*args, **kwargs):
        raise AssertionError("纯中文内容不应调用翻译模型")

    monkeypatch.setattr("stds.llm.translate_operation.structured", should_not_call)
    result = asyncio.run(translate_operation_for_output("操作人员拿取前围板"))

    assert result == "操作人员拿取前围板"
    assert contains_latin_letters(result) is False


def test_mixed_operation_uses_structured_translation(monkeypatch):
    captured = {}

    async def fake_structured(prompt, schema):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return TranslateOperationOut(
            translated_operation="操作人员安装 Front End Module"
        )

    monkeypatch.setattr("stds.llm.translate_operation.structured", fake_structured)
    result = asyncio.run(
        translate_operation_for_output("Manual install Front End Module")
    )

    assert result == "操作人员安装 Front End Module"
    assert captured["schema"] is TranslateOperationOut
    assert "Manual install Front End Module" in captured["prompt"]
