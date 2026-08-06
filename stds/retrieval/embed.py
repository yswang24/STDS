"""Embedding 后端:auto / vllm / ark / custom / ollama / mock。

vllm、ark(text 模式)和 custom 都使用 OpenAI 兼容 embeddings 协议。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, List, Optional, Protocol

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
    """任意 OpenAI 兼容 embeddings；失败后标记不可用于业务语义命中。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: Optional[dict] = None,
        *,
        batch_size: Optional[int] = None,
        request_extras: Optional[dict] = None,
        retries: int = 2,
    ):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}
        self.batch_size = max(1, int(batch_size)) if batch_size else None
        self.request_extras = request_extras or {}
        self.retries = max(0, int(retries))
        self._fallback = MockEmbed()
        self._api_available = None  # None=未检测, True=可用, False=降级

    @property
    def semantic_available(self) -> bool:
        return self._api_available is not False

    def _request_batch(self, texts: List[str]) -> List[Vector]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        payload = {
            "model": self.model,
            "input": texts,
            **self.request_extras,
        }
        response = None
        for attempt in range(self.retries + 1):
            response = httpx.post(
                f"{self.base}/embeddings",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if response.status_code == 200:
                break
            if (
                attempt < self.retries
                and (response.status_code == 429 or response.status_code >= 500)
            ):
                time.sleep(min(2 ** attempt, 4))
                continue
            raise RuntimeError(
                f"embedding API 请求失败: HTTP {response.status_code}"
            )
        if response is None or response.status_code != 200:
            raise RuntimeError("embedding API 请求失败")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError("embedding 响应缺少 data 列表")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        if len(vectors) != len(texts) or any(
            not isinstance(vector, list) or not vector
            for vector in vectors
        ):
            raise ValueError("embedding 响应数量或向量格式无效")
        return vectors

    def embed(self, texts: List[str]) -> List[Vector]:
        if not texts:
            return []
        if self._api_available is False:
            return self._fallback.embed(texts)
        try:
            batch_size = self.batch_size or len(texts)
            vectors = []
            for start in range(0, len(texts), batch_size):
                vectors.extend(self._request_batch(texts[start:start + batch_size]))
            self._api_available = True
            return vectors
        except Exception:
            self._api_available = False
            # 任一批失败后必须整体回退，不能混合真实向量和占位向量。
            return self._fallback.embed(texts)

    def embed_one(self, text: str) -> Vector:
        return self.embed([text])[0]


class _ArkMultimodalEmbed:
    """方舟多模态向量接口；当前业务以纯文本逐条请求。"""

    def __init__(self, base_url: str, api_key: str, model: str, retries: int = 2):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.retries = max(0, int(retries))
        self._fallback = MockEmbed()
        self._api_available = None

    @property
    def semantic_available(self) -> bool:
        return self._api_available is not False

    @staticmethod
    def _extract_vector(body: Any) -> Vector:
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("multimodal embedding 响应缺少 data")
        if not items or not isinstance(items[0], dict):
            raise ValueError("multimodal embedding data 格式无效")
        vector = items[0].get("embedding")
        # 部分多模态模型把融合向量包装为单元素二维列表。
        if (
            isinstance(vector, list)
            and len(vector) == 1
            and isinstance(vector[0], list)
        ):
            vector = vector[0]
        if not isinstance(vector, list) or not vector:
            raise ValueError("multimodal embedding 向量格式无效")
        return vector

    def _request_one(self, text: str) -> Vector:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "input": [{"type": "text", "text": text}],
            "encoding_format": "float",
        }
        response = None
        for attempt in range(self.retries + 1):
            response = httpx.post(
                f"{self.base}/embeddings/multimodal",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if response.status_code == 200:
                return self._extract_vector(response.json())
            if (
                attempt < self.retries
                and (response.status_code == 429 or response.status_code >= 500)
            ):
                time.sleep(min(2 ** attempt, 4))
                continue
            raise RuntimeError(
                "multimodal embedding API 请求失败: "
                f"HTTP {response.status_code}"
            )
        raise RuntimeError("multimodal embedding API 请求失败")

    def embed(self, texts: List[str]) -> List[Vector]:
        if not texts:
            return []
        if self._api_available is False:
            return self._fallback.embed(texts)
        try:
            vectors = [self._request_one(text) for text in texts]
            self._api_available = True
            return vectors
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
    # 方舟运行时没有文档化的 /models 探测接口；配置齐全时直接选择，
    # 实际可用性由首次 embeddings 请求确认。
    ark_key = settings.ARK_EMBED_API_KEY.strip() or settings.ARK_API_KEY.strip()
    if ark_key and settings.ARK_EMBED_MODEL.strip():
        return "ark"
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


def _make_ark_embed() -> EmbedBackend:
    api_key = settings.ARK_EMBED_API_KEY.strip() or settings.ARK_API_KEY.strip()
    if not api_key:
        raise ValueError(
            "火山引擎方舟 embedding API Key 未配置，请设置 ARK_API_KEY "
            "或 ARK_EMBED_API_KEY"
        )
    model = settings.ARK_EMBED_MODEL.strip()
    if not model:
        raise ValueError(
            "火山引擎方舟向量模型未配置，请设置 ARK_EMBED_MODEL"
        )
    mode = settings.ARK_EMBED_MODE.strip().lower().replace("-", "_")
    if mode in {"text", "openai", "openai_text"}:
        return _OpenAIEmbed(
            settings.ARK_API_BASE_URL,
            api_key,
            model,
            batch_size=256,
            request_extras={"encoding_format": "float"},
        )
    if mode in {"multimodal", "vision"}:
        return _ArkMultimodalEmbed(
            settings.ARK_API_BASE_URL,
            api_key,
            model,
        )
    raise ValueError(
        "ARK_EMBED_MODE 仅支持 text/openai_text 或 multimodal"
    )


def get_embed_backend() -> EmbedBackend:
    # 优先用独立 embedding 端点;未配置则回退到对话端点 VLLM_BASE_URL
    embed_base = settings.VLLM_EMBED_BASE_URL or settings.VLLM_BASE_URL
    # 向量后端与聊天后端独立。例如聊天使用 DeepSeek 时，仍可显式指定
    # EMBED_BACKEND=ollama 或 vllm；auto 才执行可用性探测。
    forced = settings.EMBED_BACKEND.lower()
    if forced == "vllm":
        return _OpenAIEmbed(embed_base, settings.VLLM_API_KEY, settings.VLLM_EMBED_MODEL)
    elif forced == "ark":
        return _make_ark_embed()
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
    elif _backend == "ark":
        return _make_ark_embed()
    elif _backend == "custom":
        return _OpenAIEmbed(
            settings.CUSTOM_API_BASE_URL, settings.CUSTOM_API_KEY, settings.CUSTOM_EMBED_MODEL,
            extra_headers=_get_extra_headers(),
        )
    elif _backend == "ollama":
        return OllamaEmbed()
    return MockEmbed()
