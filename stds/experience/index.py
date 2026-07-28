"""动作经验的精确、包含和小规模语义匹配。"""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from typing import Callable, Optional

from stds.experience.models import ExperienceContext, ExperienceEntry
from stds.retrieval.embed import EmbedBackend, get_embed_backend

_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_operation(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return _NORMALIZE_RE.sub("", text)


def normalize_chartcode(value: object) -> str:
    """图表码匹配键；返回大写、无空白形式。"""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return re.sub(r"\s+", "", text)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1e-9
    norm_b = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (norm_a * norm_b)


class ExperienceIndex:
    """请求级经验索引，不持有或修改全局经验状态。"""

    def __init__(
        self,
        entries: list[ExperienceEntry],
        *,
        digest: str = "",
        source_name: str = "",
        embed_backend: Optional[EmbedBackend] = None,
        embed_backend_factory: Callable[[], EmbedBackend] = get_embed_backend,
        similarity_threshold: float = 0.85,
        similarity_margin: float = 0.05,
    ):
        self.entries = tuple(entries)
        self.digest = digest
        self.source_name = source_name
        self._embed = embed_backend
        self._embed_backend_factory = embed_backend_factory
        self.similarity_threshold = similarity_threshold
        self.similarity_margin = similarity_margin
        self._semantic_vectors: Optional[list[list[float]]] = None
        self._build_lock: Optional[asyncio.Lock] = None

    @property
    def available(self) -> bool:
        return bool(self.entries)

    @property
    def records(self) -> tuple[ExperienceEntry, ...]:
        """面向 UI/诊断的只读别名。"""
        return self.entries

    def _candidates(
        self,
        expected_chartcode: Optional[str],
    ) -> list[ExperienceEntry]:
        if expected_chartcode is None:
            return list(self.entries)
        expected_key = normalize_chartcode(expected_chartcode)
        return [
            entry
            for entry in self.entries
            if normalize_chartcode(entry.chartcode) == expected_key
        ]

    @staticmethod
    def _identity(entry: ExperienceEntry) -> tuple[str, str, str]:
        return (
            entry.experience_id,
            entry.normalized_operation,
            normalize_chartcode(entry.chartcode),
        )

    @classmethod
    def _unique(cls, entries: list[ExperienceEntry]) -> Optional[ExperienceEntry]:
        identities = {cls._identity(entry) for entry in entries}
        if len(identities) != 1:
            return None
        return entries[0]

    @staticmethod
    def _context(
        entry: ExperienceEntry,
        match_type: str,
        similarity: float,
    ) -> ExperienceContext:
        return ExperienceContext(
            experience_id=entry.experience_id,
            operation_label=entry.operation_label,
            chartcode=entry.chartcode,
            match_type=match_type,
            similarity=float(similarity),
            chart_row=entry.chart_row,
            parameter_row=entry.parameter_row,
            parameter_text=entry.parameter_text,
            variable_hints=dict(entry.variable_hints),
        )

    async def _ensure_semantic_vectors(self) -> None:
        if self._semantic_vectors is not None:
            return
        if self._build_lock is None:
            self._build_lock = asyncio.Lock()
        async with self._build_lock:
            if self._semantic_vectors is not None:
                return
            if self._embed is None:
                self._embed = self._embed_backend_factory()
            texts = [entry.operation_label for entry in self.entries]
            self._semantic_vectors = (
                await asyncio.to_thread(self._embed.embed, texts)
                if texts
                else []
            )

    async def match(
        self,
        operation_des: str,
        *,
        expected_chartcode: Optional[str] = None,
    ) -> Optional[ExperienceContext]:
        """匹配动作，并把同一动作身份绑定的参数经验一起返回。

        精确或包含层一旦出现并列冲突便返回 None，不会用语义分数掩盖冲突。
        """
        query = normalize_operation(operation_des)
        if not query:
            return None
        candidates = self._candidates(expected_chartcode)
        if not candidates:
            return None

        exact = [
            entry for entry in candidates
            if entry.normalized_operation == query
        ]
        if exact:
            entry = self._unique(exact)
            return self._context(entry, "exact", 1.0) if entry else None

        contained = [
            entry for entry in candidates
            if entry.normalized_operation
            and entry.normalized_operation in query
        ]
        if contained:
            longest = max(len(entry.normalized_operation) for entry in contained)
            most_specific = [
                entry for entry in contained
                if len(entry.normalized_operation) == longest
            ]
            entry = self._unique(most_specific)
            return self._context(entry, "contains", 1.0) if entry else None

        await self._ensure_semantic_vectors()
        if self._embed is None or not self._semantic_vectors:
            return None
        query_vector = await asyncio.to_thread(self._embed.embed_one, operation_des)
        allowed_ids = {id(entry) for entry in candidates}
        scored = sorted(
            (
                (_cosine(query_vector, vector), entry)
                for entry, vector in zip(self.entries, self._semantic_vectors)
                if id(entry) in allowed_ids
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < self.similarity_threshold:
            return None

        top_score, top_entry = scored[0]
        top_identity = self._identity(top_entry)
        second_score = next(
            (
                score
                for score, entry in scored[1:]
                if self._identity(entry) != top_identity
            ),
            -1.0,
        )
        if top_score - second_score < self.similarity_margin:
            return None
        return self._context(top_entry, "semantic", top_score)
