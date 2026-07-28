"""从 STDS 操作描述中提取被处理的零件/物料名称。"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from pydantic import BaseModel, Field, field_validator

from stds.llm.client import structured
from stds.llm.prompts import render_prompt
from stds.retrieval.part_weight_index import normalize_part_name

logger = logging.getLogger("stds.llm.extract_part_name")


class PartNameExtraction(BaseModel):
    part_name: Optional[str] = None
    reason: str = ""

    @field_validator("part_name", mode="before")
    @classmethod
    def normalize_empty(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.casefold() in {"null", "none", "无", "没有", "未找到"}:
            return None
        return text


class PartOperationGroup(BaseModel):
    """同一个物理零件在一组拆解子工序中的作用范围。"""

    part_name: str
    child_indexes: list[int]
    reason: str = ""

    @field_validator("part_name", mode="before")
    @classmethod
    def normalize_part_name_value(cls, value):
        return str(value or "").strip()

    @field_validator("child_indexes", mode="before")
    @classmethod
    def normalize_child_indexes(cls, value):
        values = value if isinstance(value, (list, tuple)) else []
        indexes = []
        for item in values:
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if index > 0 and index not in indexes:
                indexes.append(index)
        return indexes


class PartGroupExtraction(BaseModel):
    groups: list[PartOperationGroup] = Field(default_factory=list)


async def extract_part_name(operation_des: str) -> Optional[str]:
    """调用 LLM 提取零件名称，并拒绝不来自原文的臆造结果。"""
    operation_des = str(operation_des or "").strip()
    if not operation_des:
        return None

    prompt = render_prompt(
        "extract_part_name",
        operation_des=operation_des,
    )
    output: PartNameExtraction = await structured(
        prompt,
        PartNameExtraction,
    )
    part_name = output.part_name
    if not part_name:
        return None

    normalized_part = normalize_part_name(part_name)
    normalized_operation = normalize_part_name(operation_des)
    if not normalized_part or normalized_part not in normalized_operation:
        logger.warning(
            "LLM 提取的零件名不在原描述中，已忽略: operation=%r, part=%r",
            operation_des,
            part_name,
        )
        return None
    return part_name.strip()


async def extract_part_groups(
    parent_operation: str,
    child_operations: Sequence[str],
) -> tuple[PartOperationGroup, ...]:
    """一次 LLM 调用确定父工序内的零件操作链，索引均为 1-based。"""
    parent_operation = str(parent_operation or "").strip()
    children = tuple(str(child or "").strip() for child in child_operations)
    if not children:
        return ()

    child_menu = "\n".join(
        f"[{index}] {child}"
        for index, child in enumerate(children, start=1)
    )
    prompt = render_prompt(
        "extract_part_groups",
        parent_operation=parent_operation,
        child_operations=child_menu,
    )
    output: PartGroupExtraction = await structured(
        prompt,
        PartGroupExtraction,
    )

    combined_source = "\n".join((parent_operation, *children))
    normalized_source = normalize_part_name(combined_source)
    candidate_groups = []
    index_counts: dict[int, int] = {}
    for group in output.groups:
        normalized_name = normalize_part_name(group.part_name)
        indexes = tuple(
            index
            for index in group.child_indexes
            if 1 <= index <= len(children)
        )
        if (
            not normalized_name
            or normalized_name not in normalized_source
            or not indexes
        ):
            logger.warning(
                "忽略无效父工序零件分组: part=%r, indexes=%r",
                group.part_name,
                group.child_indexes,
            )
            continue
        candidate_groups.append((group, indexes))
        for index in indexes:
            index_counts[index] = index_counts.get(index, 0) + 1

    valid_groups = []
    for group, indexes in candidate_groups:
        # 同一子工序被分给多个零件时属于歧义项，不自动施加任何重量。
        unambiguous = [
            index for index in indexes if index_counts.get(index) == 1
        ]
        if unambiguous:
            valid_groups.append(
                PartOperationGroup(
                    part_name=group.part_name,
                    child_indexes=unambiguous,
                    reason=group.reason,
                )
            )
    return tuple(valid_groups)
