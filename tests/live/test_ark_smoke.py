"""Opt-in 火山引擎方舟真实连通性测试。

默认跳过，避免 CI 或本地普通测试产生费用。测试只报告状态码和 request id，
不打印凭证、请求头或模型返回正文。
"""
from __future__ import annotations

import os

import httpx
import pytest

from stds.config.settings import settings


pytestmark = pytest.mark.live


def _required(name: str) -> str:
    value = str(getattr(settings, name, os.environ.get(name, ""))).strip()
    if not value:
        pytest.skip(f"未配置 {name}")
    return value


def _live_base_and_key() -> tuple[str, str]:
    if os.environ.get("RUN_ARK_SMOKE", "").strip() != "1":
        pytest.skip("设置 RUN_ARK_SMOKE=1 后才执行真实方舟调用")
    base = settings.ARK_API_BASE_URL.rstrip("/")
    return base, _required("ARK_API_KEY")


def _safe_failure(response: httpx.Response) -> str:
    request_id = response.headers.get("x-request-id", "")
    return f"HTTP {response.status_code}; request_id={request_id or 'unknown'}"


def test_live_ark_chat_completion() -> None:
    base, api_key = _live_base_and_key()
    model = _required("ARK_LLM_MODEL")

    response = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "user", "content": "只回复一个 JSON：{\"ok\":true}"}
            ],
            "max_tokens": 32,
            "stream": False,
        },
        timeout=60,
    )

    assert response.status_code == 200, _safe_failure(response)
    body = response.json()
    assert body.get("id")
    assert body.get("choices")
    assert body["choices"][0].get("message", {}).get("content")


def test_live_ark_embedding() -> None:
    base, chat_key = _live_base_and_key()
    model = _required("ARK_EMBED_MODEL")
    api_key = settings.ARK_EMBED_API_KEY.strip() or chat_key
    mode = settings.ARK_EMBED_MODE.strip().lower()
    if mode in {"multimodal", "vision"}:
        path = "embeddings/multimodal"
        payload = {
            "model": model,
            "input": [{"type": "text", "text": "人工移动吊具"}],
            "encoding_format": "float",
        }
    else:
        path = "embeddings"
        payload = {
            "model": model,
            "input": ["人工移动吊具"],
            "encoding_format": "float",
        }

    response = httpx.post(
        f"{base}/{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=60,
    )

    assert response.status_code == 200, _safe_failure(response)
    body = response.json()
    data = body.get("data")
    assert data
    item = data[0] if isinstance(data, list) else data
    assert isinstance(item, dict) and item.get("embedding")
