from __future__ import annotations

from types import SimpleNamespace

from stds.config.settings import settings
from stds.retrieval import embed as embed_module


def test_detect_backend_uses_dedicated_vllm_embedding_base(monkeypatch):
    requested_urls = []

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(settings, "VLLM_EMBED_BASE_URL", "http://embed:8001/v1")
    monkeypatch.setattr(settings, "VLLM_BASE_URL", "http://chat:8000/v1")
    monkeypatch.setattr(embed_module.httpx, "get", fake_get)

    assert embed_module._detect_backend() == "vllm"
    assert requested_urls == ["http://embed:8001/v1/models"]


def test_embedding_backend_is_independent_from_deepseek_chat(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BACKEND", "deepseek")
    monkeypatch.setattr(settings, "EMBED_BACKEND", "ollama")
    monkeypatch.setattr(settings, "EMBED_MODEL", "test-embedding")

    backend = embed_module.get_embed_backend()

    assert isinstance(backend, embed_module.OllamaEmbed)
    assert backend.model == "test-embedding"
