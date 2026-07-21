"""LLM 结构化输出封装。后端:auto / vllm / custom / ollama / mock。

vllm 和 custom 都是 OpenAI 兼容 API(/v1/chat/completions),只是配置不同。
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Type

import httpx
from pydantic import BaseModel

from stds.config.settings import settings
from stds.llm.prompts import load_prompt, render_prompt

logger = logging.getLogger("stds.llm")


class LLMError(Exception):
    pass


# ---------- Mock ----------

class _MockLLM:
    def __init__(self, default_index: int = 0):
        self._default_index = default_index
        self.call_count = 0

    async def structured(
        self,
        prompt: str,
        schema: Type[BaseModel],
        model: Optional[str] = None,
        retries: int = 2,
        *,
        exact_system_prompt: bool = False,
    ):
        self.call_count += 1
        fields = getattr(schema, "model_fields", {})
        if "index" in fields:
            return schema(index=self._default_index, reason="mock")
        if "auto" in fields:
            return schema(auto=0)
        if "operation" in fields:
            marker = "用户输入:"
            operation = prompt.split(marker, 1)[1].split("\n", 1)[0].strip() if marker in prompt else "mock operation"
            return schema(operation=[operation or "mock operation"])
        return schema.model_validate({})


_mock = _MockLLM()


def get_mock_llm() -> _MockLLM:
    return _mock


# ---------- OpenAI 兼容客户端(vLLM / 自定义 API 通用) ----------

class _OpenAIClient:
    """任意 OpenAI 兼容 API:/v1/chat/completions,强制 JSON 输出。"""

    def __init__(self, base_url: str, api_key: str, model: str, extra_headers: dict = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}

    async def structured(
        self,
        prompt: str,
        schema: Type[BaseModel],
        model: Optional[str] = None,
        retries: int = 2,
        *,
        exact_system_prompt: bool = False,
    ):
        model = model or self.model
        if exact_system_prompt:
            full_prompt = prompt
            messages = [{"role": "system", "content": prompt}]
        else:
            schema_instr = render_prompt("schema_format", schema=schema.model_json_schema())
            full_prompt = prompt + "\n" + schema_instr
            messages = [
                {"role": "system", "content": load_prompt("system")},
                {"role": "user", "content": full_prompt},
            ]
        headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
        logger.debug(f"[LLM] model={model} backend={self.base}")
        logger.debug(f"[LLM] prompt:\n{full_prompt[:500]}...")
        for attempt in range(retries + 1):
            try:
                r = httpx.post(
                    f"{self.base}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 512,
                        "response_format": {"type": "json_object"},
                        "stream": False,
                    },
                    timeout=120,
                )
                content = r.json()["choices"][0]["message"]["content"]
                logger.debug(f"[LLM] response: {content[:300]}")
                text = content.strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                data = json.loads(text)
                return schema.model_validate(data)
            except Exception as e:
                last = e
        raise LLMError(f"OpenAI 兼容 API 结构化失败(重试{retries}次): {last}")


# ---------- Ollama ----------

class _OllamaClient:
    def __init__(self):
        self.base = settings.OLLAMA_BASE
        self.model = settings.LLM_MODEL

    async def structured(
        self,
        prompt: str,
        schema: Type[BaseModel],
        model: Optional[str] = None,
        retries: int = 2,
        *,
        exact_system_prompt: bool = False,
    ):
        model = model or self.model
        if exact_system_prompt:
            full_prompt = prompt
        else:
            schema_instr = render_prompt("schema_format", schema=schema.model_json_schema())
            full_prompt = prompt + "\n" + schema_instr
        for attempt in range(retries + 1):
            try:
                r = httpx.post(
                    f"{self.base}/api/generate",
                    json={"model": model, "prompt": full_prompt, "format": "json", "stream": False},
                    timeout=120,
                )
                data = json.loads(r.json()["response"])
                return schema.model_validate(data)
            except Exception as e:
                last = e
        raise LLMError(f"Ollama 结构化失败: {last}")


# ---------- 自动检测 ----------

_backend = None


def _detect_backend():
    # vLLM
    try:
        r = httpx.get(f"{settings.VLLM_BASE_URL}/models", timeout=3,
                       headers={"Authorization": f"Bearer {settings.VLLM_API_KEY}"})
        if r.status_code == 200:
            return "vllm"
    except Exception:
        pass
    # Custom
    if settings.CUSTOM_API_BASE_URL and settings.CUSTOM_LLM_MODEL:
        try:
            r = httpx.get(f"{settings.CUSTOM_API_BASE_URL}/models", timeout=3,
                           headers={"Authorization": f"Bearer {settings.CUSTOM_API_KEY}"})
            if r.status_code == 200:
                return "custom"
        except Exception:
            pass
    # Ollama
    try:
        r = httpx.get(f"{settings.OLLAMA_BASE}/api/tags", timeout=3)
        if r.status_code == 200:
            return "ollama"
    except Exception:
        pass
    return "mock"


def _get_extra_headers() -> dict:
    """解析 CUSTOM_API_EXTRA_HEADERS(JSON 字符串)。"""
    raw = settings.CUSTOM_API_EXTRA_HEADERS.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _get_backend():
    global _backend
    if _backend is None:
        forced = settings.LLM_BACKEND.lower()
        if forced == "auto":
            _backend = _detect_backend()
        else:
            _backend = forced
    return _backend


def _make_client(backend: str):
    if backend == "vllm":
        return _OpenAIClient(settings.VLLM_BASE_URL, settings.VLLM_API_KEY, settings.VLLM_LLM_MODEL)
    elif backend == "custom":
        return _OpenAIClient(
            settings.CUSTOM_API_BASE_URL, settings.CUSTOM_API_KEY, settings.CUSTOM_LLM_MODEL,
            extra_headers=_get_extra_headers(),
        )
    else:
        return _OllamaClient()


async def structured(prompt: str, schema: Type[BaseModel], model: Optional[str] = None, retries: int = 2):
    """统一接口:根据配置自动选择后端。"""
    backend = _get_backend()
    if backend == "mock":
        return await _mock.structured(prompt, schema, model, retries)
    return await _make_client(backend).structured(prompt, schema, model, retries)


async def structured_system(
    prompt: str,
    schema: Type[BaseModel],
    model: Optional[str] = None,
    retries: int = 2,
):
    """将 prompt 原样作为 system message 发送，不拼接任何额外提示文本。"""
    backend = _get_backend()
    if backend == "mock":
        return await _mock.structured(
            prompt,
            schema,
            model,
            retries,
            exact_system_prompt=True,
        )
    return await _make_client(backend).structured(
        prompt,
        schema,
        model,
        retries,
        exact_system_prompt=True,
    )
