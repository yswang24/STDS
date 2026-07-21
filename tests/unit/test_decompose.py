"""Dify 动作拆解 Prompt 与结构化输出契约。"""
from __future__ import annotations

import asyncio
import hashlib

from stds.llm.decompose import (
    DIFY_INPUT_PLACEHOLDER,
    DecomposeOut,
    build_decompose_prompt,
    decompose_operation,
)
from stds.llm.client import _OpenAIClient
from stds.llm.prompts import load_prompt


def test_dify_decompose_prompt_is_byte_for_byte_copy():
    prompt = load_prompt("decompose_operation")
    assert len(prompt) == 1473
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "21b0cbc14df852240977004a3ac3a68f453cf26ee38b69017a2349b028fe8abb"
    )


def test_build_decompose_prompt_only_replaces_dify_input_variable():
    template = load_prompt("decompose_operation")
    operation = "Manual 将前围板安装到车身"
    rendered = build_decompose_prompt(operation)
    assert rendered == template.replace(DIFY_INPUT_PLACEHOLDER, operation)
    assert DIFY_INPUT_PLACEHOLDER not in rendered


def test_decompose_operation_uses_structured_operation_array(monkeypatch):
    captured = {}

    async def fake_structured_system(prompt, schema):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return DecomposeOut(operation=["操作人员拿取零件", "操作人员安装零件"])

    monkeypatch.setattr("stds.llm.decompose.structured_system", fake_structured_system)
    result = asyncio.run(decompose_operation("Manual 安装零件"))

    assert result == ["操作人员拿取零件", "操作人员安装零件"]
    assert captured["schema"] is DecomposeOut
    assert "用户输入:Manual 安装零件" in captured["prompt"]


def test_openai_client_sends_dify_prompt_as_the_only_system_message(monkeypatch):
    captured = {}

    class Response:
        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"operation":["操作人员拿取零件"]}'}}
                ]
            }

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)
    prompt = build_decompose_prompt("Manual 拿取零件")
    client = _OpenAIClient("http://test/v1", "key", "model")
    result = asyncio.run(
        client.structured(prompt, DecomposeOut, exact_system_prompt=True)
    )

    assert result.operation == ["操作人员拿取零件"]
    assert captured["messages"] == [{"role": "system", "content": prompt}]
