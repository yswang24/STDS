"""T0 精确缓存:归一化操作文本 -> StdsResult。首条命中即零 LLM。"""
from __future__ import annotations

from typing import Optional


class AutoCache:
    def __init__(self):
        self._store: dict = {}

    @staticmethod
    def _key(norm_key: str, scope: str = ""):
        """兼容旧键，同时允许按经验工作簿等运行上下文隔离缓存。"""
        return (scope, norm_key) if scope else norm_key

    def get(self, norm_key: str, *, scope: str = "") -> Optional[object]:
        return self._store.get(self._key(norm_key, scope))

    def put(self, norm_key: str, result: object, *, scope: str = "") -> None:
        self._store[self._key(norm_key, scope)] = result

    def clear(self) -> None:
        self._store.clear()
