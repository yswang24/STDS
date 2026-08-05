"""T0 精确缓存:归一化操作文本 -> StdsResult。首条命中即零 LLM。"""
from __future__ import annotations

from typing import Optional


def decision_cache_scope(deps: object) -> str:
    """返回一次决策环境的缓存命名空间。

    上传工作簿摘要隔离 Chartcode、参数和 Common Chart 经验；Common 开关
    和经验语义检索开关也属于决策环境，必须进入键，否则开关切换
    后会先命中另一模式留下的 T0。无上传且未启用 Common 时，语义
    开关没有可作用的经验集，继续返回空串以兼容既有全局缓存键。
    """
    upload_scope = str(getattr(deps, "experience_scope", "") or "").strip()
    common_enabled = bool(getattr(deps, "use_common_chart", False))
    semantic_enabled = bool(getattr(deps, "use_semantic_experience", True))
    if not upload_scope and not common_enabled:
        return ""
    return (
        f"{upload_scope or 'upload:none'}|common={int(common_enabled)}"
        f"|semantic={int(semantic_enabled)}"
    )


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
