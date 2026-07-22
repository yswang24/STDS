"""全局配置:DB 路径 / LLM / 阈值。从 .env 读取,不硬编码。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()


@dataclass
class Settings:
    DB_PATH: str = os.environ.get(
        "DB_PATH", "/Users/wangyushan/Desktop/STMS-STDS20260610/stms.db"
    )
    # LLM 后端:auto / vllm / custom / ollama / mock
    LLM_BACKEND: str = os.environ.get("LLM_BACKEND", "auto")
    # vLLM(OpenAI 兼容)
    VLLM_BASE_URL: str = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
    VLLM_API_KEY: str = os.environ.get("VLLM_API_KEY", "EMPTY")
    VLLM_LLM_MODEL: str = os.environ.get("VLLM_LLM_MODEL", "qwen3-14b")
    VLLM_EMBED_MODEL: str = os.environ.get("VLLM_EMBED_MODEL", "qwen3-embedding")
    # 自定义 API(任意 OpenAI 兼容端点,如 DeepSeek / 智谱 / 自部署)
    CUSTOM_API_BASE_URL: str = os.environ.get("CUSTOM_API_BASE_URL", "http://localhost:9000/v1")
    CUSTOM_API_KEY: str = os.environ.get("CUSTOM_API_KEY", "")
    CUSTOM_LLM_MODEL: str = os.environ.get("CUSTOM_LLM_MODEL", "mimo-v2.5-pro")
    CUSTOM_EMBED_MODEL: str = os.environ.get("CUSTOM_EMBED_MODEL", "")
    CUSTOM_API_EXTRA_HEADERS: str = os.environ.get("CUSTOM_API_EXTRA_HEADERS", "")  # JSON 格式
    # Ollama
    OLLAMA_BASE: str = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
    OLLAMA_LLM_MODEL: str = os.environ.get(
        "OLLAMA_LLM_MODEL",
        os.environ.get("LLM_MODEL", "qwen3:14b"),
    )
    # 兼容旧配置名。
    LLM_MODEL: str = OLLAMA_LLM_MODEL
    EMBED_MODEL: str = os.environ.get("EMBED_MODEL", "qwen3-embedding:8b")
    CONFIDENCE_THRESHOLD: float = float(
        os.environ.get("CONFIDENCE_THRESHOLD", "0.75")
    )
    CONCURRENCY_LIMIT: int = int(os.environ.get("CONCURRENCY_LIMIT", "8"))
    STATE_TTL_DAYS: int = int(os.environ.get("STATE_TTL_DAYS", "7"))


settings = Settings()
