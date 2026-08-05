"""动作经验的精确、包含和小规模语义匹配。"""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from typing import Callable, Optional

from stds.experience.models import (
    ExperienceContext,
    ExperienceEntry,
    ParameterExperienceEntry,
)
from stds.retrieval.embed import EmbedBackend, MockEmbed, get_embed_backend

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


def _valid_vectors(vectors: object, expected: int) -> bool:
    """拒绝降级占位、空向量和非有限向量，避免伪语义命中。"""
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


def _semantic_backend_available(backend: Optional[EmbedBackend]) -> bool:
    """Mock 或远端已经降级到 Mock 时，不允许用于经验语义决策。"""
    if backend is None or isinstance(backend, MockEmbed):
        return False
    semantic_available = getattr(backend, "semantic_available", None)
    if semantic_available is not None:
        return bool(semantic_available)
    return getattr(backend, "_api_available", None) is not False


class ExperienceIndex:
    """请求级经验索引，不持有或修改全局经验状态。"""

    def __init__(
        self,
        entries: Optional[list[ExperienceEntry]] = None,
        *,
        parameter_entries: Optional[list[ParameterExperienceEntry]] = None,
        digest: str = "",
        source_name: str = "",
        embed_backend: Optional[EmbedBackend] = None,
        embed_backend_factory: Callable[[], EmbedBackend] = get_embed_backend,
        similarity_threshold: float = 0.85,
        similarity_margin: float = 0.05,
        chartcode_similarity_threshold: float = 0.70,
    ):
        self.entries = tuple(entries or ())
        self._parameter_entries = tuple(parameter_entries or ())
        self.digest = digest
        self.source_name = source_name
        self._embed = embed_backend
        self._embed_backend_factory = embed_backend_factory
        self.similarity_threshold = similarity_threshold
        self.similarity_margin = similarity_margin
        self.chartcode_similarity_threshold = float(
            chartcode_similarity_threshold
        )
        self._semantic_vectors: Optional[list[list[float]]] = None
        self._parameter_semantic_vectors: Optional[list[list[float]]] = None
        self._semantic_unavailable = False
        self._parameter_semantic_unavailable = False
        self._build_lock: Optional[asyncio.Lock] = None
        self._parameter_build_lock: Optional[asyncio.Lock] = None

    @property
    def available(self) -> bool:
        return bool(self.entries or self._parameter_entries)

    @property
    def records(self) -> tuple[ExperienceEntry, ...]:
        """面向 UI/诊断的只读别名。"""
        return self.entries

    @property
    def parameter_records(self) -> tuple[ParameterExperienceEntry, ...]:
        """独立参数经验池；不改变 ``records`` 的 Chartcode 记录语义。"""
        return self._parameter_entries

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

    async def _ensure_semantic_vectors(self) -> bool:
        if self._semantic_vectors is not None:
            return True
        if self._semantic_unavailable:
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
                if not _semantic_backend_available(self._embed):
                    self._semantic_unavailable = True
                    return False
                texts = [entry.operation_label for entry in self.entries]
                vectors = (
                    await asyncio.to_thread(self._embed.embed, texts)
                    if texts
                    else []
                )
                if (
                    not _semantic_backend_available(self._embed)
                    or not _valid_vectors(vectors, len(texts))
                ):
                    self._semantic_unavailable = True
                    return False
                self._semantic_vectors = [
                    [float(value) for value in vector]
                    for vector in vectors
                ]
                return True
            except Exception:
                self._semantic_unavailable = True
                return False

    async def _ensure_parameter_semantic_vectors(self) -> bool:
        if self._parameter_semantic_vectors is not None:
            return True
        if self._parameter_semantic_unavailable:
            return False
        if self._parameter_build_lock is None:
            self._parameter_build_lock = asyncio.Lock()
        async with self._parameter_build_lock:
            if self._parameter_semantic_vectors is not None:
                return True
            if self._parameter_semantic_unavailable:
                return False
            try:
                if self._embed is None:
                    self._embed = self._embed_backend_factory()
                if not _semantic_backend_available(self._embed):
                    self._parameter_semantic_unavailable = True
                    return False
                texts = [
                    entry.operation_label
                    for entry in self._parameter_entries
                ]
                vectors = (
                    await asyncio.to_thread(self._embed.embed, texts)
                    if texts
                    else []
                )
                if (
                    not _semantic_backend_available(self._embed)
                    or not _valid_vectors(vectors, len(texts))
                ):
                    self._parameter_semantic_unavailable = True
                    return False
                self._parameter_semantic_vectors = [
                    [float(value) for value in vector]
                    for vector in vectors
                ]
                return True
            except Exception:
                self._parameter_semantic_unavailable = True
                return False

    @staticmethod
    def _parameter_identity(
        entry: ParameterExperienceEntry,
    ) -> tuple[str, str, str]:
        return (
            entry.experience_id,
            entry.normalized_operation,
            normalize_chartcode(entry.chartcode),
        )

    @classmethod
    def _unique_parameter(
        cls,
        entries: list[ParameterExperienceEntry],
    ) -> Optional[ParameterExperienceEntry]:
        identities = {cls._parameter_identity(entry) for entry in entries}
        if len(identities) != 1:
            return None
        return entries[0]

    def _parameter_context(
        self,
        entry: ParameterExperienceEntry,
        match_type: str,
        similarity: float,
    ) -> ExperienceContext:
        chart_row = next(
            (
                chart_entry.chart_row
                for chart_entry in self.entries
                if (
                    chart_entry.experience_id == entry.experience_id
                    and chart_entry.normalized_operation
                    == entry.normalized_operation
                    and normalize_chartcode(chart_entry.chartcode)
                    == normalize_chartcode(entry.chartcode)
                )
            ),
            0,
        )
        return ExperienceContext(
            experience_id=entry.experience_id,
            operation_label=entry.operation_label,
            chartcode=entry.chartcode,
            match_type=match_type,
            similarity=float(similarity),
            chart_row=chart_row,
            parameter_row=entry.parameter_row,
            parameter_text=entry.parameter_text,
            variable_hints=dict(entry.variable_hints),
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

    async def match_chartcode_semantic(
        self,
        operation_des: str,
        *,
        expected_chartcode: Optional[str] = None,
    ) -> Optional[ExperienceContext]:
        """只用语义向量选择 Chartcode 经验的 Top1。

        生产选码路径使用此接口，不经过精确或包含分支。分数达到 0.70
        即接受；不要求 Top1 与 Top2 的间隔，同分按 Excel 行号稳定选择。
        """
        if not str(operation_des or "").strip():
            return None
        candidates = self._candidates(expected_chartcode)
        if not candidates or not await self._ensure_semantic_vectors():
            return None
        if self._embed is None or not self._semantic_vectors:
            return None
        try:
            query_vector = await asyncio.to_thread(
                self._embed.embed_one,
                operation_des,
            )
        except Exception:
            return None
        if (
            not _semantic_backend_available(self._embed)
            or not _valid_vectors([query_vector], 1)
        ):
            return None

        allowed_ids = {id(entry) for entry in candidates}
        scored = [
            (_cosine(query_vector, vector), entry)
            for entry, vector in zip(self.entries, self._semantic_vectors)
            if id(entry) in allowed_ids
        ]
        if not scored:
            return None
        score, entry = min(
            scored,
            key=lambda item: (
                -item[0],
                item[1].chart_row,
                item[1].experience_id,
            ),
        )
        if score < self.chartcode_similarity_threshold:
            return None
        return self._context(entry, "semantic", score)

    def parameter_contexts_for_chartcode(
        self,
        chartcode: str,
        *,
        experience_id: Optional[str] = None,
        operation_key: Optional[str] = None,
    ) -> tuple[ExperienceContext, ...]:
        """返回某 Chartcode 下全部有效参数经验，或严格绑定的一条身份。

        ``experience_id`` 和 ``operation_key`` 同时给出时构成完整经验身份，
        防止相同 Chartcode 下的“转身/弯腰”等动作横向串用参数。
        """
        selected_key = normalize_chartcode(chartcode)
        normalized_operation = (
            normalize_operation(operation_key)
            if operation_key is not None
            else None
        )
        records = [
            entry
            for entry in self._parameter_entries
            if normalize_chartcode(entry.chartcode) == selected_key
            and (
                experience_id is None
                or entry.experience_id == str(experience_id)
            )
            and (
                normalized_operation is None
                or entry.normalized_operation == normalized_operation
            )
        ]
        records.sort(key=lambda entry: (
            entry.parameter_row,
            entry.experience_id,
            entry.normalized_operation,
        ))
        return tuple(
            self._parameter_context(entry, "parameter-candidate", 1.0)
            for entry in records
        )

    async def match_parameters(
        self,
        operation_des: str,
        chartcode: Optional[str] = None,
        *,
        expected_chartcode: Optional[str] = None,
    ) -> Optional[ExperienceContext]:
        """按已选 Chartcode 和当前动作身份检索独立参数经验。

        ``chartcode`` 与 ``expected_chartcode`` 是兼容别名。参数经验不会仅因
        某个 Chartcode 下只有一条记录就盲目命中，动作仍须通过精确、最长
        包含或有足够区分度的语义匹配。
        """
        if chartcode is not None and expected_chartcode is not None:
            if normalize_chartcode(chartcode) != normalize_chartcode(
                expected_chartcode
            ):
                return None
        selected_chartcode = (
            expected_chartcode
            if expected_chartcode is not None
            else chartcode
        )
        selected_key = normalize_chartcode(selected_chartcode)
        query = normalize_operation(operation_des)
        if not query or not selected_key:
            return None

        candidates = [
            entry
            for entry in self._parameter_entries
            if normalize_chartcode(entry.chartcode) == selected_key
        ]
        if not candidates:
            return None

        exact = [
            entry
            for entry in candidates
            if entry.normalized_operation == query
        ]
        if exact:
            entry = self._unique_parameter(exact)
            return (
                self._parameter_context(entry, "exact", 1.0)
                if entry
                else None
            )

        contained = [
            entry
            for entry in candidates
            if (
                entry.normalized_operation
                and entry.normalized_operation in query
            )
        ]
        if contained:
            longest = max(
                len(entry.normalized_operation)
                for entry in contained
            )
            most_specific = [
                entry
                for entry in contained
                if len(entry.normalized_operation) == longest
            ]
            entry = self._unique_parameter(most_specific)
            return (
                self._parameter_context(entry, "contains", 1.0)
                if entry
                else None
            )

        await self._ensure_parameter_semantic_vectors()
        if self._embed is None or not self._parameter_semantic_vectors:
            return None
        query_vector = await asyncio.to_thread(
            self._embed.embed_one,
            operation_des,
        )
        allowed_ids = {id(entry) for entry in candidates}
        scored = sorted(
            (
                (_cosine(query_vector, vector), entry)
                for entry, vector in zip(
                    self._parameter_entries,
                    self._parameter_semantic_vectors,
                )
                if id(entry) in allowed_ids
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < self.similarity_threshold:
            return None

        top_score, top_entry = scored[0]
        top_identity = self._parameter_identity(top_entry)
        second_score = next(
            (
                score
                for score, entry in scored[1:]
                if self._parameter_identity(entry) != top_identity
            ),
            -1.0,
        )
        if top_score - second_score < self.similarity_margin:
            return None
        return self._parameter_context(top_entry, "semantic", top_score)
