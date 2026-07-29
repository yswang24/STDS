"""Parent-scoped grouping for explicitly numbered repeated actions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


_ORDINAL_CLASSIFIERS = "个颗枚只件处点根套组次步孔"
_CHINESE_ORDINAL_DIGITS = "〇零一二两三四五六七八九十百千万"
_ORDINAL_PHRASE_RE = re.compile(
    rf"第\s*(?:\d+|[{_CHINESE_ORDINAL_DIGITS}]+)"
    rf"\s*[{_ORDINAL_CLASSIFIERS}]"
)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RepeatedActionGroup:
    """Children whose operations differ only by an ordinal phrase."""

    group_id: str
    canonical_operation: str
    child_indexes: tuple[int, ...]


@dataclass(frozen=True)
class RepeatedActionResolution:
    """Repeated-action groups and their 1-based child-index lookup."""

    groups: tuple[RepeatedActionGroup, ...]
    by_child_index: dict[int, RepeatedActionGroup]

    def group_for(self, index: int) -> Optional[RepeatedActionGroup]:
        """Return the repeated-action group for a 1-based child index."""
        return self.by_child_index.get(index)


def _without_ordinal_phrases(operation: str) -> Tuple[str, bool]:
    canonical, substitutions = _ORDINAL_PHRASE_RE.subn("", operation)
    if not substitutions:
        return operation, False
    return _WHITESPACE_RE.sub(" ", canonical).strip(), True


def build_repeated_action_groups(
    child_operations: Sequence[str],
) -> RepeatedActionResolution:
    """Group numbered repetitions within one parent's child operations.

    Indexes are 1-based to match the child numbering used by the surrounding
    pipeline. Operations without a supported ordinal phrase are deliberately
    excluded, even when their original text is duplicated.
    """

    indexes_by_canonical: Dict[str, List[int]] = {}
    for child_index, operation in enumerate(child_operations, start=1):
        canonical, changed = _without_ordinal_phrases(operation)
        if not changed or not canonical:
            continue
        indexes_by_canonical.setdefault(canonical, []).append(child_index)

    groups = []
    by_child_index: Dict[int, RepeatedActionGroup] = {}
    for canonical, child_indexes in indexes_by_canonical.items():
        if len(child_indexes) < 2:
            continue
        group = RepeatedActionGroup(
            group_id=f"RG{len(groups) + 1}",
            canonical_operation=canonical,
            child_indexes=tuple(child_indexes),
        )
        groups.append(group)
        for child_index in child_indexes:
            by_child_index[child_index] = group

    return RepeatedActionResolution(
        groups=tuple(groups),
        by_child_index=by_child_index,
    )
