"""请求级零件重量一致性缓存。

缓存池只保存检索层返回的 :class:`PartWeightMatch`，不保存带父工序信息的
``NumericContext``。这样同一请求中相同零件始终复用第一次可靠匹配，同时每个
父工序仍可生成自己的 ``query_name`` 和 ``group_id``。
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from stds.retrieval.part_weight_index import (
    PartWeightMatch,
    normalize_part_name,
)

PartWeightMatcher = Callable[
    [str],
    Awaitable[Optional[PartWeightMatch]],
]


@dataclass(frozen=True)
class _CanonicalMatch:
    match: PartWeightMatch


class PartWeightPool:
    """在一个 ``Deps``/请求内统一零件重量匹配结果。

    - 查询名使用与重量索引相同的 ``normalize_part_name`` 归一化；
    - ``None`` 也会缓存，避免重复执行已确认无可靠结果的检索；
    - 同名并发查询通过逐键锁合并为一次底层调用；
    - 异常和取消不会写入缓存，后续调用可以重试；
    - 有零件号时只按保留标点的零件号统一；无零件号时才按标准名称统一；
    - 查询结果一经写入（包括 ``None``）便不允许后续别名改写。
    """

    def __init__(self) -> None:
        self._query_cache: dict[
            str,
            Optional[_CanonicalMatch],
        ] = {}
        self._query_locks: dict[str, asyncio.Lock] = {}
        self._canonical: dict[tuple[str, str], _CanonicalMatch] = {}

    @staticmethod
    def _identity_key(
        match: PartWeightMatch,
    ) -> Optional[tuple[str, str]]:
        # 零件号中的连字符、斜杠等属于身份的一部分，不能使用会删除标点的
        # normalize_part_name（例如 P-1 与 P1 必须保持为两个零件）。
        part_no = unicodedata.normalize(
            "NFKC",
            str(match.part_no or ""),
        ).casefold().strip()
        part_no = re.sub(r"\s+", "", part_no)
        if part_no:
            return ("part_no", part_no)
        matched_name = normalize_part_name(match.matched_name)
        if matched_name:
            return ("matched_name", matched_name)
        return None

    def _canonicalize(
        self,
        match: PartWeightMatch,
    ) -> _CanonicalMatch:
        identity = self._identity_key(match)
        if identity is not None and identity in self._canonical:
            return self._canonical[identity]
        entry = _CanonicalMatch(match)
        if identity is not None:
            self._canonical[identity] = entry
        return entry

    def _remember_known_aliases(
        self,
        entry: _CanonicalMatch,
        discovered: PartWeightMatch,
    ) -> None:
        """已知标准名称再次作为查询时无需重复访问索引。

        查询缓存一经发布即不可变；尤其不能用后来发现的别名覆盖先前的
        ``None``。零件号不作为名称查询别名，避免查询名归一化删除其标点。
        """
        for value in (
            entry.match.matched_name,
            discovered.matched_name,
        ):
            alias_key = normalize_part_name(value)
            if not alias_key:
                continue
            if alias_key not in self._query_cache:
                self._query_cache[alias_key] = entry

    async def match(
        self,
        query: str,
        matcher: PartWeightMatcher,
    ) -> Optional[PartWeightMatch]:
        """返回请求级一致的重量匹配；底层失败时原样抛出且不缓存。"""
        key = normalize_part_name(query)
        if not key:
            return None

        if key in self._query_cache:
            cached = self._query_cache[key]
            return cached.match if cached is not None else None

        lock = self._query_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._query_cache:
                cached = self._query_cache[key]
                return cached.match if cached is not None else None

            # matcher 抛出异常或任务被取消时不会执行下面的写缓存逻辑。
            match = await matcher(query)
            # 查询期间，另一个别名可能已解析为当前标准名称。可靠的先到结果
            # 优先，不能被当前较晚返回的 None 或冲突重量覆盖。
            if key in self._query_cache:
                cached = self._query_cache[key]
                return cached.match if cached is not None else None
            if match is None:
                self._query_cache[key] = None
                return None

            entry = self._canonicalize(match)
            self._query_cache[key] = entry
            self._remember_known_aliases(entry, match)
            return entry.match
