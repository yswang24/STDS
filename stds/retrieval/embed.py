"""Embedding 后端:auto / vllm / custom / ollama / mock。

vllm 和 custom 都是 OpenAI 兼容 API(/v1/embeddings),只是配置不同。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import List, Protocol

import httpx

from stds.config.settings import settings

Vector = List[float]


class EmbedBackend(Protocol):
    def embed(self, texts: List[str]) -> List[Vector]: ...
    def embed_one(self, text: str) -> Vector: ...


# ---------- Mock ----------

class MockEmbed:
    DIM = 64

    def _text_to_vec(self, text: str) -> Vector:
        h = hashlib.sha512(text.encode("utf-8")).hexdigest()
        vec = []
        for i in range(self.DIM):
            byte = int(h[i * 2 : i * 2 + 2], 16)
            vec.append(byte / 255.0)
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: List[str]) -> List[Vector]:
        return [self._text_to_vec(t) for t in texts]

    def embed_one(self, text: str) -> Vector:
        return self._text_to_vec(text)


# ---------- OpenAI 兼容 embedding(vLLM / 自定义通用) ----------

class _OpenAIEmbed:
    """任意 OpenAI 兼容 /v1/embeddings。API 不支持时自动降级 MockEmbed。"""

    def __init__(self, base_url: str, api_key: str, model: str, extra_headers: dict = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}
        self._fallback = MockEmbed()
        self._api_available = None  # None=未检测, True=可用, False=降级

    def embed(self, texts: List[str]) -> List[Vector]:
        if self._api_available is False:
            return self._fallback.embed(texts)
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            r = httpx.post(
                f"{self.base}/embeddings",
                headers=headers,
                json={"model": self.model, "input": texts},
                timeout=30,
            )
            if r.status_code != 200:
                self._api_available = False
                return self._fallback.embed(texts)
            data = r.json()["data"]
            self._api_available = True
            return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]
        except Exception:
            self._api_available = False
            return self._fallback.embed(texts)

    def embed_one(self, text: str) -> Vector:
        return self.embed([text])[0]


# ---------- Ollama ----------

class OllamaEmbed:
    def __init__(self, model: str = None):
        self.model = model or settings.EMBED_MODEL
        self.base = settings.OLLAMA_BASE

    def embed(self, texts: List[str]) -> List[Vector]:
        r = httpx.post(
            f"{self.base}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        return r.json()["embeddings"]

    def embed_one(self, text: str) -> Vector:
        return self.embed([text])[0]


# ---------- 自动选择 ----------

_backend = None


def _detect_backend():
    embed_base = settings.VLLM_EMBED_BASE_URL or settings.VLLM_BASE_URL
    try:
        r = httpx.get(f"{embed_base.rstrip('/')}/models", timeout=3,
                       headers={"Authorization": f"Bearer {settings.VLLM_API_KEY}"})
        if r.status_code == 200:
            return "vllm"
    except Exception:
        pass
    if settings.CUSTOM_API_BASE_URL and settings.CUSTOM_EMBED_MODEL:
        try:
            r = httpx.get(f"{settings.CUSTOM_API_BASE_URL}/models", timeout=3,
                           headers={"Authorization": f"Bearer {settings.CUSTOM_API_KEY}"})
            if r.status_code == 200:
                return "custom"
        except Exception:
            pass
    try:
        r = httpx.get(f"{settings.OLLAMA_BASE}/api/tags", timeout=3)
        if r.status_code == 200:
            return "ollama"
    except Exception:
        pass
    return "mock"


def _get_extra_headers() -> dict:
    raw = settings.CUSTOM_API_EXTRA_HEADERS.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def get_embed_backend() -> EmbedBackend:
    # 优先用独立 embedding 端点;未配置则回退到对话端点 VLLM_BASE_URL
    embed_base = settings.VLLM_EMBED_BASE_URL or settings.VLLM_BASE_URL
    # 向量后端与聊天后端独立。例如聊天使用 DeepSeek 时，仍可显式指定
    # EMBED_BACKEND=ollama 或 vllm；auto 才执行可用性探测。
    forced = settings.EMBED_BACKEND.lower()
    if forced == "vllm":
        return _OpenAIEmbed(embed_base, settings.VLLM_API_KEY, settings.VLLM_EMBED_MODEL)
    elif forced == "custom":
        return _OpenAIEmbed(
            settings.CUSTOM_API_BASE_URL, settings.CUSTOM_API_KEY, settings.CUSTOM_EMBED_MODEL,
            extra_headers=_get_extra_headers(),
        )
    elif forced == "ollama":
        return OllamaEmbed()
    elif forced == "mock":
        return MockEmbed()
    # auto
    global _backend
    if _backend is None:
        _backend = _detect_backend()
    if _backend == "vllm":
        return _OpenAIEmbed(embed_base, settings.VLLM_API_KEY, settings.VLLM_EMBED_MODEL)
    elif _backend == "custom":
        return _OpenAIEmbed(
            settings.CUSTOM_API_BASE_URL, settings.CUSTOM_API_KEY, settings.CUSTOM_EMBED_MODEL,
            extra_headers=_get_extra_headers(),
        )
    elif _backend == "ollama":
        return OllamaEmbed()
    return MockEmbed()
