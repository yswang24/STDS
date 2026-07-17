"""Step 15a:复核编辑应用。UI 提交的修正覆盖到 result。"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional


def apply_edits(state, edits: dict):
    """复核 UI 提交的修正覆盖到 result。edits: {chartcode, decision, time_s}"""
    return replace(
        state,
        chartcode=edits.get("chartcode") or state.chartcode,
        decision=edits.get("decision") or state.decision,
        time_s=edits["time_s"] if edits.get("time_s") is not None else state.time_s,
        confidence=1.0,
        edited=True,
        needs_review=False,
    )
