"""请求级 Common_Chart 关键词优先、语义回退索引。"""
from __future__ import annotations

import asyncio
import math
from typing import Callable, Optional, Sequence

from stds.experience.common_chart import _match_common_chart_keywords
from stds.experience.models import CommonChartEntry, CommonChartMatch
from stds.retrieval.embed import EmbedBackend, MockEmbed, get_embed_backend


def common_semantic_document(entry: CommonChartEntry) -> str:
    """构造语义文档：操作内容加该行全部有效关键词。"""
    operation = str(entry.operation_label or "").strip()
    keywords = [
        str(keyword).strip()
        for keyword, normalized in zip(
            entry.keywords,
            entry.normalized_keywords,
        )
        if normalized and str(keyword).strip()
    ]
    return "\n".join((
        f"操作内容：{operation}",
        f"关键词：{'；'.join(keywords)}",
    ))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(x) * float(x) for x in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return -1.0
    return dot / (norm_a * norm_b)


def _valid_vectors(vectors: object, expected: int) -> bool:
    if not isinstance(vectors, (list, tuple)) or len(vectors) != expected:
        return False
    dimension: Optional[int] = None
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or not vector:
            return False
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            return False
        try:
            if not all(math.isfinite(float(value)) for value in vector):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _backend_available(backend: Optional[EmbedBackend]) -> bool:
    """Mock 或远端已降级到 Mock 时，禁止产生伪语义命中。"""
    if backend is None or isinstance(backend, MockEmbed):
        return False
    return getattr(backend, "_api_available", None) is not False


class CommonChartSemanticIndex:
    """Common_Chart 请求级索引。

    匹配顺序固定为关键词精确、最长单向包含、语义 Top1。词法层一旦有
    候选但输出冲突，立即返回 ``None``。文档向量首次需要时才构建；同一
    实例上的并发首次查询由一把异步锁合并为一次构建。
    """

    def __init__(
        self,
        entries: Sequence[CommonChartEntry] = (),
        *,
        embed_backend: Optional[EmbedBackend] = None,
        embed_backend_factory: Callable[[], EmbedBackend] = get_embed_backend,
        similarity_threshold: float = 0.70,
    ) -> None:
        self.entries = tuple(entries)
        self.similarity_threshold = float(similarity_threshold)
        self._embed = embed_backend
        self._embed_backend_factory = embed_backend_factory
        self._semantic_vectors: Optional[tuple[tuple[float, ...], ...]] = None
        self._semantic_unavailable = False
        self._build_lock: Optional[asyncio.Lock] = None

    @property
    def available(self) -> bool:
        return bool(self.entries)

    async def _ensure_semantic_vectors(self) -> bool:
        if self._semantic_vectors is not None:
            return True
        if self._semantic_unavailable or not self.entries:
            return False
        if self._build_lock is None:
            self._build_lock = asyncio.Lock()

        async with self._build_lock:
            if self._semantic_vectors is not None:
                return True
            if self._semantic_unavailable:
                return False
            try:
                if self._embed is None:
                    self._embed = self._embed_backend_factory()
                if not _backend_available(self._embed):
                    self._semantic_unavailable = True
                    return False
                documents = [
                    common_semantic_document(entry)
                    for entry in self.entries
                ]
                vectors = await asyncio.to_thread(self._embed.embed, documents)
                # OpenAI 兼容后端请求失败时会返回 Mock 向量并设置此状态；
                # 这些向量只能用于测试占位，不能用于业务语义命中。
                if (
                    not _backend_available(self._embed)
                    or not _valid_vectors(vectors, len(documents))
                ):
                    self._semantic_unavailable = True
                    return False
                self._semantic_vectors = tuple(
                    tuple(float(value) for value in vector)
                    for vector in vectors
                )
                return True
            except Exception:
                self._semantic_unavailable = True
                return False

    async def match(self, operation_des: str) -> Optional[CommonChartMatch]:
        """返回关键词命中，或无词法候选时阈值 0.70 的语义 Top1。"""
        keyword_match, had_keyword_candidate = _match_common_chart_keywords(
            operation_des,
            self.entries,
        )
        if keyword_match is not None:
            return keyword_match
        if had_keyword_candidate:
            return None
        if not str(operation_des or "").strip():
            return None
        if not await self._ensure_semantic_vectors():
            return None
        if self._embed is None or self._semantic_vectors is None:
            return None

        try:
            query_vector = await asyncio.to_thread(
                self._embed.embed_one,
                operation_des,
            )
        except Exception:
            return None
        if (
            not _backend_available(self._embed)
            or not _valid_vectors([query_vector], 1)
        ):
            return None

        scored = [
            (_cosine(query_vector, vector), entry)
            for entry, vector in zip(self.entries, self._semantic_vectors)
        ]
        # Top1 only，不设 margin；同分时按 Excel 行号稳定选择。
        score, entry = min(scored, key=lambda item: (-item[0], item[1].row))
        if score < self.similarity_threshold:
            return None
        return CommonChartMatch(
            entry=entry,
            keyword="",
            match_type="semantic",
            similarity=float(score),
        )


__all__ = ["CommonChartSemanticIndex", "common_semantic_document"]
