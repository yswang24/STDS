"""Prompt 统一管理:从 .txt 文件加载,支持热更新(改文件即生效)。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """加载 prompt 文件(name 不含 .txt 后缀)。"""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt(name: str, **kwargs) -> str:
    """加载并渲染 prompt 模板(支持 {variable} 替换)。"""
    template = load_prompt(name)
    return template.format(**kwargs)
