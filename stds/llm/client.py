"""LLM 结构化输出封装。后端:auto / vllm / deepseek / ark / custom / ollama / mock。

vllm、deepseek、ark 和 custom 都是 OpenAI 兼容 API，只是配置不同。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Type
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from stds.config.settings import settings
from stds.llm.prompts import load_prompt, render_prompt

logger = logging.getLogger("stds.llm")


class LLMError(Exception):
    pass


SUPPORTED_LLM_BACKENDS = frozenset(
    {"auto", "vllm", "deepseek", "ark", "custom", "ollama", "mock"}
)
OLLAMA_SYSTEM_EXECUTION_PROMPT = (
    "请严格执行 system 中的任务，并且只输出符合指定 JSON Schema 的 JSON。"
)


@dataclass(frozen=True)
class LLMRuntimeOptions:
    """单次分析任务的 LLM 覆盖项；ContextVar 保证并发任务互不串配置。"""

    backend: Optional[str] = None
    model: Optional[str] = None
    ollama_base_url: Optional[str] = None


_runtime_options: ContextVar[LLMRuntimeOptions] = ContextVar(
    "stds_llm_runtime_options",
    default=LLMRuntimeOptions(),
)


def _normalize_ollama_base_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ollama 地址必须是有效的 http(s) URL")
    # 同时兼容官方文档常写的 .../api 和项目历史配置使用的服务根地址。
    if normalized.endswith("/api"):
        normalized = normalized[:-4]
    return normalized


def get_llm_runtime_options() -> LLMRuntimeOptions:
    return _runtime_options.get()


@contextmanager
def llm_runtime(
    *,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    ollama_base_url: Optional[str] = None,
) -> Iterator[LLMRuntimeOptions]:
    """为当前同步/异步任务临时指定后端和模型，退出后自动恢复。"""
    normalized_backend = str(backend).strip().lower() if backend else None
    if normalized_backend and normalized_backend not in SUPPORTED_LLM_BACKENDS:
        raise ValueError(f"不支持的 LLM 后端: {backend}")
    normalized_model = str(model).strip() if model else None
    options = LLMRuntimeOptions(
        backend=normalized_backend,
        model=normalized_model,
        ollama_base_url=_normalize_ollama_base_url(ollama_base_url),
    )
    token = _runtime_options.set(options)
    try:
        yield options
    finally:
        _runtime_options.reset(token)


class _LLMResponseError(LLMError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _schema_name(schema: Type[BaseModel]) -> str:
    """vLLM/OpenAI json_schema name 仅保留安全字符。"""
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)
    return (name or "structured_output")[:64]


def _redact_sensitive_text(value: Any) -> str:
    """避免上游错误回显或异常文本把已配置凭证带入日志/API。"""
    text = str(value)
    configured = {
        settings.VLLM_API_KEY,
        settings.DEEPSEEK_API_KEY,
        settings.ARK_API_KEY,
        settings.ARK_EMBED_API_KEY,
        settings.CUSTOM_API_KEY,
    }
    for secret in configured:
        secret = str(secret or "").strip()
        if len(secret) >= 6 and secret.upper() != "EMPTY":
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(bearer\s+)[^\s,;\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text


def _response_error_message(status_code: int, data: Any) -> str:
    message = ""
    code = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or error)
            code = str(error.get("code") or error.get("type") or "")
        elif error is not None:
            message = str(error)
        if not message:
            message = str(data.get("message") or data.get("detail") or "")
        keys = ",".join(sorted(str(key) for key in data.keys()))
    else:
        message = str(data)
        keys = type(data).__name__
    suffix = f" code={code}" if code else ""
    detail = message[:1000] if message else f"response keys={keys}"
    return _redact_sensitive_text(f"HTTP {status_code}{suffix}: {detail}")


def _json_from_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_blocks = [
            str(block.get("text") or block.get("content") or "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") in {"text", "output_text"}
        ]
        if text_blocks:
            content = "".join(text_blocks)
        else:
            return content
    if not isinstance(content, str):
        raise ValueError(f"message.content 类型不受支持: {type(content).__name__}")

    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _parse_chat_response(response) -> Any:
    status_code = int(getattr(response, "status_code", 200))
    try:
        data = response.json()
    except Exception as exc:
        body = _redact_sensitive_text(
            str(getattr(response, "text", ""))[:1000]
        )
        raise _LLMResponseError(
            status_code,
            f"HTTP {status_code}: 非 JSON 响应 {body!r}",
        ) from exc
    if status_code >= 400:
        raise _LLMResponseError(
            status_code,
            _response_error_message(status_code, data),
        )
    if not isinstance(data, dict):
        raise _LLMResponseError(
            status_code,
            f"HTTP {status_code}: 响应顶层应为 object，实际为 {type(data).__name__}",
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _LLMResponseError(
            status_code,
            "OpenAI 兼容响应缺少 choices；" + _response_error_message(status_code, data),
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _LLMResponseError(status_code, "choices[0] 不是 object")
    message = choice.get("message")
    if isinstance(message, dict):
        if message.get("parsed") is not None:
            return message["parsed"]
        content = message.get("content")
    else:
        content = choice.get("text")
    if content is None:
        raise _LLMResponseError(status_code, "choices[0] 中没有 message.content")
    logger.debug(
        "[LLM] response: %s",
        _redact_sensitive_text(str(content)[:300]),
    )
    return _json_from_content(content)


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
        if "translated_operation" in fields:
            marker = "待处理操作内容："
            operation = (
                prompt.split(marker, 1)[1].split("\n", 1)[0].strip()
                if marker in prompt
                else ""
            )
            return schema(translated_operation=operation)
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

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: dict = None,
        *,
        vllm_json_schema: bool = False,
        json_keyword_required: bool = False,
        supports_json_response_format: bool = True,
    ):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}
        self.vllm_json_schema = vllm_json_schema
        self.json_keyword_required = json_keyword_required
        self.supports_json_response_format = supports_json_response_format

    def _request_payload(
        self,
        model: str,
        messages: list,
        schema: Type[BaseModel],
        *,
        legacy_vllm: bool,
        use_json_response_format: bool = True,
    ) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": settings.MAX_TOKENS,
            "stream": False,
        }
        json_schema = schema.model_json_schema()
        if self.vllm_json_schema:
            if legacy_vllm:
                # vLLM 旧版 OpenAI server 使用顶层 guided_json。
                payload["guided_json"] = json_schema
            else:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": _schema_name(schema),
                        "schema": json_schema,
                    },
                }
        elif use_json_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

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
        use_json_response_format = self.supports_json_response_format and not (
            self.json_keyword_required and "json" not in full_prompt.lower()
        )
        if not use_json_response_format:
            logger.debug(
                "[LLM] prompt 不含 JSON 关键字，按服务约束省略 response_format"
            )
        logger.debug(f"[LLM] model={model} backend={self.base}")
        logger.debug(
            "[LLM] prompt:\n%s...",
            _redact_sensitive_text(full_prompt[:500]),
        )
        retry_count = 0
        legacy_vllm = False
        while True:
            try:
                r = await asyncio.to_thread(
                    httpx.post,
                    f"{self.base}/chat/completions",
                    headers=headers,
                    json=self._request_payload(
                        model,
                        messages,
                        schema,
                        legacy_vllm=legacy_vllm,
                        use_json_response_format=use_json_response_format,
                    ),
                    timeout=120,
                )
                data = _parse_chat_response(r)
                return schema.model_validate(data)
            except Exception as exc:
                last = exc
                if (
                    self.vllm_json_schema
                    and not legacy_vllm
                    and isinstance(exc, _LLMResponseError)
                    and exc.status_code in {400, 422}
                ):
                    legacy_vllm = True
                    logger.warning(
                        "[LLM] vLLM 不接受 json_schema，降级使用 guided_json: %s",
                        exc,
                    )
                    continue
                if retry_count >= retries:
                    break
                if isinstance(exc, _LLMResponseError) and (
                    exc.status_code == 429 or exc.status_code >= 500
                ):
                    await asyncio.sleep(min(2 ** retry_count, 4))
                retry_count += 1
        message = _redact_sensitive_text(last)
        raise LLMError(
            f"OpenAI 兼容 API 结构化失败(重试{retries}次): {message}"
        )


# ---------- Ollama ----------

class _OllamaClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base = _normalize_ollama_base_url(base_url or settings.OLLAMA_BASE)
        self.model = model or settings.OLLAMA_LLM_MODEL

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
                payload = {
                    "model": model,
                    "prompt": (
                        OLLAMA_SYSTEM_EXECUTION_PROMPT
                        if exact_system_prompt
                        else full_prompt
                    ),
                    "format": schema.model_json_schema(),
                    "stream": False,
                    "options": {"temperature": 0.1},
                }
                if exact_system_prompt:
                    payload["system"] = full_prompt
                r = await asyncio.to_thread(
                    httpx.post,
                    f"{self.base}/api/generate",
                    json=payload,
                    timeout=120,
                )
                response_data = r.json()
                if not isinstance(response_data, dict):
                    raise ValueError(
                        "Ollama 响应顶层不是 JSON object: "
                        f"{type(response_data).__name__}"
                    )
                if r.status_code >= 400 or response_data.get("error"):
                    message = response_data.get("error") or f"HTTP {r.status_code}"
                    raise _LLMResponseError(r.status_code, f"Ollama: {message}")
                response_content = response_data.get("response")
                if not isinstance(response_content, str) or not response_content.strip():
                    thinking = response_data.get("thinking") or ""
                    raise ValueError(
                        "Ollama 返回空 response: "
                        f"model={model}, "
                        f"done_reason={response_data.get('done_reason') or 'unknown'}, "
                        f"thinking_chars={len(str(thinking))}"
                    )
                data = _json_from_content(response_content)
                return schema.model_validate(data)
            except Exception as exc:
                last = exc
                if attempt < retries and isinstance(exc, _LLMResponseError) and (
                    exc.status_code == 429 or exc.status_code >= 500
                ):
                    await asyncio.sleep(min(2 ** attempt, 4))
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
    # 方舟运行时接口不依赖 GET /models。配置完整时即可选中，
    # 真实鉴权与模型可用性由首次 chat/completions 请求校验。
    if settings.ARK_API_KEY.strip() and settings.ARK_LLM_MODEL.strip():
        return "ark"
    # DeepSeek
    if settings.DEEPSEEK_API_KEY:
        try:
            r = httpx.get(
                f"{settings.DEEPSEEK_API_BASE_URL.rstrip('/')}/models",
                timeout=3,
                headers={
                    "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"
                },
            )
            if r.status_code == 200:
                return "deepseek"
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
    runtime_backend = get_llm_runtime_options().backend
    if runtime_backend and runtime_backend != "auto":
        return runtime_backend
    if runtime_backend == "auto":
        return _detect_backend()
    if _backend is None:
        forced = settings.LLM_BACKEND.lower()
        if forced == "auto":
            _backend = _detect_backend()
        else:
            _backend = forced
    return _backend


def _make_client(backend: str):
    runtime = get_llm_runtime_options()
    if backend == "vllm":
        return _OpenAIClient(
            settings.VLLM_BASE_URL,
            settings.VLLM_API_KEY,
            settings.VLLM_LLM_MODEL,
            vllm_json_schema=True,
        )
    elif backend == "deepseek":
        if not settings.DEEPSEEK_API_KEY.strip():
            raise LLMError(
                "DeepSeek API Key 未配置，请设置环境变量 DEEPSEEK_API_KEY"
            )
        return _OpenAIClient(
            settings.DEEPSEEK_API_BASE_URL,
            settings.DEEPSEEK_API_KEY,
            settings.DEEPSEEK_LLM_MODEL,
            json_keyword_required=True,
        )
    elif backend == "ark":
        if not settings.ARK_API_KEY.strip():
            raise LLMError(
                "火山引擎方舟 API Key 未配置，请设置环境变量 ARK_API_KEY"
            )
        ark_model = runtime.model or settings.ARK_LLM_MODEL
        if not ark_model.strip():
            raise LLMError(
                "火山引擎方舟模型未配置，请设置环境变量 ARK_LLM_MODEL"
            )
        return _OpenAIClient(
            settings.ARK_API_BASE_URL,
            settings.ARK_API_KEY,
            ark_model,
            # 方舟通用 Chat 参数文档未承诺所有模型都支持
            # response_format；Schema 提示词与 Pydantic 校验仍会约束输出。
            supports_json_response_format=False,
        )
    elif backend == "custom":
        return _OpenAIClient(
            settings.CUSTOM_API_BASE_URL, settings.CUSTOM_API_KEY, settings.CUSTOM_LLM_MODEL,
            extra_headers=_get_extra_headers(),
        )
    elif backend == "ollama":
        return _OllamaClient(
            base_url=runtime.ollama_base_url,
            model=runtime.model,
        )
    raise LLMError(f"不支持的 LLM 后端: {backend}")


async def structured(prompt: str, schema: Type[BaseModel], model: Optional[str] = None, retries: int = 2):
    """统一接口:根据配置自动选择后端。"""
    backend = _get_backend()
    selected_model = model or get_llm_runtime_options().model
    if backend == "mock":
        return await _mock.structured(prompt, schema, selected_model, retries)
    return await _make_client(backend).structured(
        prompt,
        schema,
        selected_model,
        retries,
    )


async def structured_system(
    prompt: str,
    schema: Type[BaseModel],
    model: Optional[str] = None,
    retries: int = 2,
):
    """将 prompt 原样作为 system message 发送，不拼接任何额外提示文本。"""
    backend = _get_backend()
    selected_model = model or get_llm_runtime_options().model
    if backend == "mock":
        return await _mock.structured(
            prompt,
            schema,
            selected_model,
            retries,
            exact_system_prompt=True,
        )
    return await _make_client(backend).structured(
        prompt,
        schema,
        selected_model,
        retries,
        exact_system_prompt=True,
    )
