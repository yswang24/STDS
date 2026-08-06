"""Dify 动作拆解 Prompt 与结构化输出契约。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from stds.config.settings import settings
from stds.llm.decompose import (
    DIFY_INPUT_PLACEHOLDER,
    DecomposeOut,
    build_decompose_prompt,
    decompose_operation,
)
from stds.llm.client import (
    LLMError,
    OLLAMA_SYSTEM_EXECUTION_PROMPT,
    _OllamaClient,
    _OpenAIClient,
    _detect_backend,
    _make_client,
    get_llm_runtime_options,
    llm_runtime,
    structured_system,
)
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
        status_code = 200

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


def test_vllm_client_uses_json_schema_response_format(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": '{"operation":["操作人员安装零件"]}',
                                }
                            ]
                        }
                    }
                ]
            }

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)
    client = _OpenAIClient(
        "http://vllm/v1",
        "key",
        "qwen",
        vllm_json_schema=True,
    )
    result = asyncio.run(client.structured("拆解", DecomposeOut))

    assert result.operation == ["操作人员安装零件"]
    assert captured["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "DecomposeOut",
            "schema": DecomposeOut.model_json_schema(),
        },
    }
    assert "guided_json" not in captured


def test_vllm_client_falls_back_to_legacy_guided_json(monkeypatch):
    payloads = []

    class Response:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    def fake_post(url, **kwargs):
        payloads.append(kwargs["json"])
        if len(payloads) == 1:
            return Response(
                400,
                {"error": {"message": "json_schema is not supported"}},
            )
        return Response(
            200,
            {
                "choices": [
                    {"message": {"content": '{"operation":["操作人员拿取零件"]}'}}
                ]
            },
        )

    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)
    client = _OpenAIClient(
        "http://legacy-vllm/v1",
        "key",
        "qwen",
        vllm_json_schema=True,
    )
    result = asyncio.run(client.structured("拆解", DecomposeOut, retries=0))

    assert result.operation == ["操作人员拿取零件"]
    assert payloads[0]["response_format"]["type"] == "json_schema"
    assert payloads[1]["guided_json"] == DecomposeOut.model_json_schema()
    assert "response_format" not in payloads[1]


def test_deepseek_client_uses_official_openai_compatible_contract(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"operation":["操作人员拿取零件"]}'}}
                ]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_API_BASE_URL",
        "https://api.deepseek.com/",
    )
    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_API_KEY",
        "deepseek-test-key",
    )
    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_LLM_MODEL",
        "deepseek-v4-flash",
    )
    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)

    result = asyncio.run(
        _make_client("deepseek").structured("拆解", DecomposeOut, retries=0)
    )

    assert result.operation == ["操作人员拿取零件"]
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer deepseek-test-key"
    assert captured["payload"]["model"] == "deepseek-v4-flash"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["stream"] is False


def test_auto_detection_prefers_configured_deepseek_over_custom(monkeypatch):
    requested_urls = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return Response(200 if url == "https://api.deepseek.com/models" else 404)

    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_API_BASE_URL",
        "https://api.deepseek.com",
    )
    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_API_KEY",
        "deepseek-test-key",
    )
    monkeypatch.setattr("stds.llm.client.httpx.get", fake_get)

    assert _detect_backend() == "deepseek"
    assert requested_urls == [
        f"{settings.VLLM_BASE_URL}/models",
        "https://api.deepseek.com/models",
    ]


def test_deepseek_omits_json_mode_when_exact_prompt_lacks_json_keyword(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"operation":["操作人员安装零件"]}'}}
                ]
            }

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(
        "stds.llm.client.settings.DEEPSEEK_API_KEY",
        "deepseek-test-key",
    )
    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)

    result = asyncio.run(
        _make_client("deepseek").structured(
            "只返回指定对象",
            DecomposeOut,
            retries=0,
            exact_system_prompt=True,
        )
    )

    assert result.operation == ["操作人员安装零件"]
    assert "response_format" not in captured


def test_deepseek_client_requires_api_key(monkeypatch):
    monkeypatch.setattr("stds.llm.client.settings.DEEPSEEK_API_KEY", "")

    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        _make_client("deepseek")


def test_ark_client_uses_official_openai_compatible_contract(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"operation":["操作人员拿取零件"]}'}}
                ]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(
        "stds.llm.client.settings.ARK_API_BASE_URL",
        "https://ark.cn-beijing.volces.com/api/v3/",
    )
    monkeypatch.setattr("stds.llm.client.settings.ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr("stds.llm.client.settings.ARK_LLM_MODEL", "ep-test")
    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)

    result = asyncio.run(
        _make_client("ark").structured("请用 JSON 拆解", DecomposeOut, retries=0)
    )

    assert result.operation == ["操作人员拿取零件"]
    assert captured["url"] == (
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer ark-test-key"
    assert captured["payload"]["model"] == "ep-test"
    assert "response_format" not in captured["payload"]
    assert captured["payload"]["stream"] is False


def test_ark_client_requires_api_key(monkeypatch):
    monkeypatch.setattr("stds.llm.client.settings.ARK_API_KEY", "")
    monkeypatch.setattr("stds.llm.client.settings.ARK_LLM_MODEL", "ep-test")

    with pytest.raises(LLMError, match="ARK_API_KEY"):
        _make_client("ark")


def test_ark_client_requires_model(monkeypatch):
    monkeypatch.setattr("stds.llm.client.settings.ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr("stds.llm.client.settings.ARK_LLM_MODEL", "")

    with pytest.raises(LLMError, match="ARK_LLM_MODEL"):
        _make_client("ark")


def test_auto_detection_uses_configured_ark_without_models_probe(monkeypatch):
    requested_urls = []

    class Response:
        status_code = 404

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return Response()

    monkeypatch.setattr("stds.llm.client.settings.ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr("stds.llm.client.settings.ARK_LLM_MODEL", "ep-test")
    monkeypatch.setattr("stds.llm.client.httpx.get", fake_get)

    assert _detect_backend() == "ark"
    assert requested_urls == [f"{settings.VLLM_BASE_URL}/models"]


def test_ark_server_error_cannot_echo_configured_api_key(monkeypatch):
    sentinel = "ark-sensitive-sentinel-key"

    class Response:
        status_code = 401

        def json(self):
            return {
                "error": {
                    "code": "Unauthorized",
                    "message": f"invalid bearer {sentinel}",
                }
            }

    monkeypatch.setattr("stds.llm.client.settings.ARK_API_KEY", sentinel)
    monkeypatch.setattr("stds.llm.client.settings.ARK_LLM_MODEL", "ep-test")
    monkeypatch.setattr(
        "stds.llm.client.httpx.post",
        lambda *args, **kwargs: Response(),
    )

    with pytest.raises(LLMError) as exc_info:
        asyncio.run(
            _make_client("ark").structured("输出 JSON", DecomposeOut, retries=0)
        )

    assert sentinel not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_openai_error_response_reports_real_service_message(monkeypatch):
    class Response:
        status_code = 429

        def json(self):
            return {
                "error": {
                    "type": "rate_limit_error",
                    "code": "rpm_limit",
                    "message": "request rate exceeded",
                }
            }

    monkeypatch.setattr("stds.llm.client.httpx.post", lambda *args, **kwargs: Response())
    client = _OpenAIClient("http://test/v1", "key", "model")

    with pytest.raises(LLMError, match=r"HTTP 429.*rpm_limit.*request rate exceeded"):
        asyncio.run(client.structured("拆解", DecomposeOut, retries=0))


def test_ollama_client_sends_json_schema_and_selected_model(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"response": '{"operation":["操作人员安装零件"]}'}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)
    client = _OllamaClient("http://ollama.test:11434/api", "qwen3:8b")
    result = asyncio.run(client.structured("拆解操作", DecomposeOut))

    assert result.operation == ["操作人员安装零件"]
    assert captured["url"] == "http://ollama.test:11434/api/generate"
    assert captured["model"] == "qwen3:8b"
    assert captured["format"] == DecomposeOut.model_json_schema()
    assert captured["stream"] is False
    assert captured["options"] == {"temperature": 0.1}


def test_ollama_runtime_override_applies_to_system_prompt(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"response": '{"operation":["操作人员拿取零件"]}'}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs["json"])
        return Response()

    monkeypatch.setattr("stds.llm.client.httpx.post", fake_post)
    assert get_llm_runtime_options().backend is None

    with llm_runtime(
        backend="ollama",
        model="gemma3:4b",
        ollama_base_url="http://127.0.0.1:11435",
    ):
        result = asyncio.run(
            structured_system("完整系统提示", DecomposeOut, retries=0)
        )
        assert get_llm_runtime_options().model == "gemma3:4b"

    assert result.operation == ["操作人员拿取零件"]
    assert captured["url"] == "http://127.0.0.1:11435/api/generate"
    assert captured["model"] == "gemma3:4b"
    assert captured["prompt"] == OLLAMA_SYSTEM_EXECUTION_PROMPT
    assert captured["system"] == "完整系统提示"
    assert get_llm_runtime_options().backend is None


def test_ollama_empty_response_has_actionable_error(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "model": "qwen3:8b",
                "response": "",
                "thinking": "正在分析",
                "done_reason": "stop",
            }

    monkeypatch.setattr(
        "stds.llm.client.httpx.post",
        lambda *args, **kwargs: Response(),
    )
    client = _OllamaClient("http://localhost:11434", "qwen3:8b")

    with pytest.raises(
        LLMError,
        match=r"空 response.*qwen3:8b.*done_reason=stop.*thinking_chars=4",
    ):
        asyncio.run(
            client.structured(
                "完整系统提示",
                DecomposeOut,
                retries=0,
                exact_system_prompt=True,
            )
        )


def test_ollama_runtime_rejects_invalid_base_url():
    with pytest.raises(ValueError, match="http"):
        with llm_runtime(backend="ollama", ollama_base_url="localhost:11434"):
            pass
