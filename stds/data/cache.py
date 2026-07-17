"""T0 精确缓存:归一化操作文本 -> StdsResult。首条命中即零 LLM。"""
from __future__ import annotations

from typing import Optional


class AutoCache:
    def __init__(self):
        self._store: dict = {}

    def get(self, norm_key: str) -> Optional[object]:
        return self._store.get(norm_key)

    def put(self, norm_key: str, result: object) -> None:
        self._store[norm_key] = result

    def clear(self) -> None:
        self._store.clear()
