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
        "DB_PATH", str(Path(__file__).resolve().parents[2] / "stms.db")
    )
    # LLM 后端:auto / vllm / deepseek / custom / ollama / mock
    LLM_BACKEND: str = os.environ.get("LLM_BACKEND", "auto")
    # vLLM(OpenAI 兼容)
    VLLM_BASE_URL: str = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
    VLLM_API_KEY: str = os.environ.get("VLLM_API_KEY", "EMPTY")
    VLLM_LLM_MODEL: str = os.environ.get("VLLM_LLM_MODEL", "qwen3-14b")
    # Embedding 端点:默认留空 → 回退到 VLLM_BASE_URL(对话共用);
    # 若有独立 embedding 服务(如 8001)则填其 /v1 地址
    VLLM_EMBED_BASE_URL: str = os.environ.get("VLLM_EMBED_BASE_URL", "")
    VLLM_EMBED_MODEL: str = os.environ.get("VLLM_EMBED_MODEL", "qwen3-embedding")
    # Embedding 与聊天后端解耦；DeepSeek 聊天可同时使用 vLLM/Ollama 向量。
    EMBED_BACKEND: str = os.environ.get("EMBED_BACKEND", "auto")
    # DeepSeek 官方 OpenAI 兼容 API（仅聊天；官方当前不提供 embedding 端点）
    DEEPSEEK_API_BASE_URL: str = os.environ.get(
        "DEEPSEEK_API_BASE_URL",
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
    DEEPSEEK_LLM_MODEL: str = os.environ.get(
        "DEEPSEEK_LLM_MODEL",
        "deepseek-v4-flash",
    )
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
    MAX_TOKENS: int = int(os.environ.get("MAX_TOKENS", "4096"))
    STATE_TTL_DAYS: int = int(os.environ.get("STATE_TTL_DAYS", "7"))
    PART_WEIGHT_XLSX_PATH: str = os.environ.get(
        "PART_WEIGHT_XLSX_PATH",
        "/Users/wangyushan/Desktop/重量信息汇总20260723.xlsx",
    )
    PART_WEIGHT_SIMILARITY_THRESHOLD: float = float(
        os.environ.get("PART_WEIGHT_SIMILARITY_THRESHOLD", "0.85")
    )
    PART_WEIGHT_SIMILARITY_MARGIN: float = float(
        os.environ.get("PART_WEIGHT_SIMILARITY_MARGIN", "0.05")
    )


settings = Settings()
