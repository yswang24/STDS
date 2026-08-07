"""请求级 Common_Chart 语义优先、关键词回退索引。"""
from __future__ import annotations

import asyncio
import math
import threading
from typing import Callable, Optional, Sequence

from stds.experience.common_chart import _match_common_chart_keywords
from stds.experience.models import CommonChartEntry, CommonChartMatch
from stds.retrieval.embed import EmbedBackend, MockEmbed, get_embed_backend


def _common_semantic_cell_items(
    entry: CommonChartEntry,
) -> tuple[tuple[str, str], ...]:
    """返回该行参与语义召回的 ``(原始列名, 单元格值)``。"""
    operation = str(entry.operation_label or "").strip()
    items = [("操作内容", operation)] if operation else []
    keyword_fields = tuple(getattr(entry, "keyword_fields", ()) or ())
    for keyword_index, (keyword, normalized) in enumerate(
        zip(entry.keywords, entry.normalized_keywords)
    ):
        value = str(keyword).strip()
        if not normalized or not value:
            continue
        field_name = (
            str(keyword_fields[keyword_index]).strip()
            if keyword_index < len(keyword_fields)
            else ""
        ) or f"关键词描述{keyword_index + 1}"
        items.append((field_name, value))
    return tuple(items)


def common_semantic_cells(entry: CommonChartEntry) -> tuple[str, ...]:
    """返回该行参与语义召回的单元格值，保持 Excel 字段顺序。"""
    return tuple(value for _, value in _common_semantic_cell_items(entry))


def common_semantic_document(entry: CommonChartEntry) -> str:
    """兼容旧调用的行级展示文本；语义索引实际按单元格分别向量化。"""
    cells = common_semantic_cells(entry)
    if not cells:
        return ""
    operation, *keywords = cells
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

    匹配顺序固定为语义 Top1、关键词精确、最长单向包含。语义分数达到
    阈值时直接返回；低分或语义后端不可用时才沿用确定性关键词结果。
    单元格向量首次需要时才构建；同一实例上的并发首次查询由一把异步锁
    合并为一次构建。
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
        # 行是最终返回单位，但向量和审计信息都以单元格为单位。字段顺序
        # 固定为操作内容（0），随后是有效关键词的 Excel 原列顺序（1..n）。
        self._semantic_cells = tuple(
            (entry, field_order, field_name, cell_value)
            for entry in self.entries
            for field_order, (field_name, cell_value) in enumerate(
                _common_semantic_cell_items(entry),
            )
        )
        self._semantic_vectors: Optional[tuple[tuple[float, ...], ...]] = None
        self._semantic_unavailable = False
        self._build_lock: Optional[asyncio.Lock] = None
        # 部分远端后端以实例字段记录“本次是否已降级为 Mock”。查询若并发，
        # 一次成功与一次失败可能交叉覆盖该字段，使占位向量被误当成真实向量。
        # 串行执行 query embedding，并在同一临界区内检查可用状态。
        # 使用线程锁而不是 asyncio.Lock，确保同一上传索引即使被不同事件
        # 循环复用（例如 UI 多次 asyncio.run）也不会绑定到已关闭的 loop。
        self._query_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.entries)

    async def _ensure_semantic_vectors(self) -> bool:
        if self._semantic_vectors is not None:
            return True
        if self._semantic_unavailable or not self._semantic_cells:
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
                    cell_value
                    for _, _, _, cell_value in self._semantic_cells
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

    async def _embed_query(self, operation_des: str) -> Optional[tuple[float, ...]]:
        """取得一个已验证的查询向量，隔离共享后端的降级状态竞争。"""
        if self._embed is None:
            return None

        def embed_and_validate() -> Optional[tuple[float, ...]]:
            assert self._embed is not None
            with self._query_lock:
                if not _backend_available(self._embed):
                    return None
                try:
                    query_vector = self._embed.embed_one(operation_des)
                except Exception:
                    return None
                if (
                    not _backend_available(self._embed)
                    or not _valid_vectors([query_vector], 1)
                ):
                    return None
                return tuple(float(value) for value in query_vector)

        return await asyncio.to_thread(embed_and_validate)

    async def match(self, operation_des: str) -> Optional[CommonChartMatch]:
        """优先返回阈值 0.70 的语义 Top1，否则回退关键词匹配。"""
        if not str(operation_des or "").strip():
            return None

        if await self._ensure_semantic_vectors():
            if self._embed is not None and self._semantic_vectors is not None:
                try:
                    query_vector = await self._embed_query(operation_des)
                    if query_vector is not None:
                        scored = [
                            (
                                _cosine(query_vector, vector),
                                entry,
                                field_order,
                                field_name,
                                cell_value,
                            )
                            for (
                                entry,
                                field_order,
                                field_name,
                                cell_value,
                            ), vector in zip(
                                self._semantic_cells,
                                self._semantic_vectors,
                            )
                        ]
                        # 全部单元格只取 Top1，不设 margin；同分先选 Excel
                        # 行号最小，再选字段最靠前（操作内容优先）。
                        score, entry, _, matched_field, matched_cell = min(
                            scored,
                            key=lambda item: (
                                -item[0],
                                item[1].row,
                                item[2],
                            ),
                        )
                        if (
                            math.isfinite(score)
                            and score >= self.similarity_threshold
                        ):
                            return CommonChartMatch(
                                entry=entry,
                                keyword=matched_cell,
                                match_type="semantic",
                                similarity=float(score),
                                matched_field=matched_field,
                            )
                except Exception:
                    # 查询异常不影响确定性关键词路径。
                    pass

        keyword_match, _ = _match_common_chart_keywords(
            operation_des,
            self.entries,
        )
        return keyword_match


__all__ = [
    "CommonChartSemanticIndex",
    "common_semantic_cells",
    "common_semantic_document",
]
