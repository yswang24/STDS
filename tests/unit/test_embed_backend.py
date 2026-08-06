from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_ark_text_embedding_batches_256_and_preserves_order(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, batch):
            self.batch = list(batch)

        def json(self):
            # 故意逆序，验证按服务返回的 index 恢复原顺序。
            return {
                "data": [
                    {"index": index, "embedding": [float(text[1:]), 1.0]}
                    for index, text in reversed(list(enumerate(self.batch)))
                ]
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(kwargs["json"]["input"])

    monkeypatch.setattr(settings, "EMBED_BACKEND", "ark")
    monkeypatch.setattr(settings, "ARK_API_BASE_URL", "https://ark.test/api/v3/")
    monkeypatch.setattr(settings, "ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr(settings, "ARK_EMBED_API_KEY", "")
    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "ep-embed-test")
    monkeypatch.setattr(settings, "ARK_EMBED_MODE", "text")
    monkeypatch.setattr(embed_module.httpx, "post", fake_post)

    backend = embed_module.get_embed_backend()
    vectors = backend.embed([f"t{index}" for index in range(513)])

    assert [len(call[1]["json"]["input"]) for call in calls] == [256, 256, 1]
    assert all(
        call[0] == "https://ark.test/api/v3/embeddings"
        for call in calls
    )
    assert all(
        call[1]["headers"]["Authorization"] == "Bearer ark-test-key"
        for call in calls
    )
    assert all(
        call[1]["json"]["encoding_format"] == "float"
        and call[1]["json"]["model"] == "ep-embed-test"
        for call in calls
    )
    assert vectors[0] == [0.0, 1.0]
    assert vectors[256] == [256.0, 1.0]
    assert vectors[-1] == [512.0, 1.0]
    assert backend._api_available is True


def test_ark_text_embedding_discards_all_real_vectors_if_one_batch_fails(
    monkeypatch,
):
    call_count = 0

    class Response:
        def __init__(self, status_code, batch):
            self.status_code = status_code
            self.batch = batch

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [99.0, 1.0]}
                    for index, _ in enumerate(self.batch)
                ]
            }

    def fake_post(_url, **kwargs):
        nonlocal call_count
        call_count += 1
        return Response(200 if call_count == 1 else 400, kwargs["json"]["input"])

    monkeypatch.setattr(settings, "EMBED_BACKEND", "ark")
    monkeypatch.setattr(settings, "ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr(settings, "ARK_EMBED_API_KEY", "")
    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "ep-embed-test")
    monkeypatch.setattr(settings, "ARK_EMBED_MODE", "text")
    monkeypatch.setattr(embed_module.httpx, "post", fake_post)

    backend = embed_module.get_embed_backend()
    vectors = backend.embed([f"text-{index}" for index in range(300)])

    assert call_count == 2
    assert len(vectors) == 300
    assert all(len(vector) == embed_module.MockEmbed.DIM for vector in vectors)
    assert backend._api_available is False


def test_ark_multimodal_embedding_uses_dedicated_wire_format(monkeypatch):
    captured = []

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [[0.25, 0.75]]}]}

    def fake_post(url, **kwargs):
        captured.append((url, kwargs))
        return Response()

    monkeypatch.setattr(settings, "EMBED_BACKEND", "ark")
    monkeypatch.setattr(settings, "ARK_API_BASE_URL", "https://ark.test/api/v3")
    monkeypatch.setattr(settings, "ARK_API_KEY", "chat-key")
    monkeypatch.setattr(settings, "ARK_EMBED_API_KEY", "embed-key")
    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "vision-model")
    monkeypatch.setattr(settings, "ARK_EMBED_MODE", "multimodal")
    monkeypatch.setattr(embed_module.httpx, "post", fake_post)

    backend = embed_module.get_embed_backend()
    vectors = backend.embed(["移动吊具", "夹持Tray"])

    assert vectors == [[0.25, 0.75], [0.25, 0.75]]
    assert len(captured) == 2
    assert captured[0][0] == "https://ark.test/api/v3/embeddings/multimodal"
    assert captured[0][1]["headers"]["Authorization"] == "Bearer embed-key"
    assert captured[0][1]["json"] == {
        "model": "vision-model",
        "input": [{"type": "text", "text": "移动吊具"}],
        "encoding_format": "float",
    }


def test_ark_embedding_requires_key_model_and_valid_mode(monkeypatch):
    monkeypatch.setattr(settings, "EMBED_BACKEND", "ark")
    monkeypatch.setattr(settings, "ARK_API_KEY", "")
    monkeypatch.setattr(settings, "ARK_EMBED_API_KEY", "")
    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "")
    monkeypatch.setattr(settings, "ARK_EMBED_MODE", "text")

    with pytest.raises(ValueError, match="ARK_API_KEY"):
        embed_module.get_embed_backend()

    monkeypatch.setattr(settings, "ARK_API_KEY", "ark-test-key")
    with pytest.raises(ValueError, match="ARK_EMBED_MODEL"):
        embed_module.get_embed_backend()

    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "ep-embed-test")
    monkeypatch.setattr(settings, "ARK_EMBED_MODE", "unknown")
    with pytest.raises(ValueError, match="ARK_EMBED_MODE"):
        embed_module.get_embed_backend()


def test_detect_backend_uses_configured_ark_without_ark_models_probe(monkeypatch):
    requested_urls = []

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr(settings, "VLLM_EMBED_BASE_URL", "")
    monkeypatch.setattr(settings, "ARK_API_KEY", "ark-test-key")
    monkeypatch.setattr(settings, "ARK_EMBED_API_KEY", "")
    monkeypatch.setattr(settings, "ARK_EMBED_MODEL", "ep-embed-test")
    monkeypatch.setattr(embed_module.httpx, "get", fake_get)

    assert embed_module._detect_backend() == "ark"
    assert requested_urls == [f"{settings.VLLM_BASE_URL}/models"]
