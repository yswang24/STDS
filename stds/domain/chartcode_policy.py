"""Chartcode eligibility rules shared by resolver and LLM selection."""
from __future__ import annotations

from collections.abc import Mapping


EXPERIENCE_ONLY_CHARTCODES = frozenset({"EST C00", "EST V00"})
_NORMALIZED_EXPERIENCE_ONLY = frozenset(
    "".join(chartcode.upper().split())
    for chartcode in EXPERIENCE_ONLY_CHARTCODES
)


def is_experience_only_chartcode(chartcode: object) -> bool:
    """Return whether a code may only come from explicit experience/Common."""
    normalized = "".join(str(chartcode or "").strip().upper().split())
    return normalized in _NORMALIZED_EXPERIENCE_ONLY


def general_chart_candidates(charts: Mapping) -> dict:
    """Exclude experience-only codes from unrestricted selection candidates."""
    return {
        chartcode: chart
        for chartcode, chart in charts.items()
        if not is_experience_only_chartcode(chartcode)
    }
