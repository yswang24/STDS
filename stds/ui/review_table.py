"""人工审核表的纯数据行操作，供 Streamlit 编辑器复用。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from stds.pipeline.output_schema import (
    DECOMPOSITION_HEADERS,
    LINE_HEADER,
    NUMBER_HEADER,
    OUTPUT_OPERATION_HEADER,
    PRODUCT_MODEL_HEADER,
    PROJECT_HEADER,
    STATION_DESCRIPTION_HEADER,
    STATION_HEADER,
    TRANSLATED_OPERATION_HEADER,
)

_COPIED_METADATA_HEADERS = (
    PROJECT_HEADER,
    PRODUCT_MODEL_HEADER,
    LINE_HEADER,
    STATION_HEADER,
    STATION_DESCRIPTION_HEADER,
)


def normalize_review_editor_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """转为编辑器稳定文本行，并按当前顺序重建序号。"""
    normalized = []
    for index, row in enumerate(rows, start=1):
        normalized.append(
            {
                header: (
                    index
                    if header == NUMBER_HEADER
                    else "" if row.get(header) is None else str(row.get(header))
                )
                for header in DECOMPOSITION_HEADERS
            }
        )
    return normalized


def insert_review_editor_row(
    rows: Sequence[Mapping[str, object]],
    position: int,
) -> list[dict[str, object]]:
    """在一基位置插入新动作，并复用相邻行的 PF 元数据。"""
    current = normalize_review_editor_rows(rows)
    if position < 1 or position > len(current) + 1:
        raise ValueError("插入位置超出当前审核表范围")

    inserted = {header: "" for header in DECOMPOSITION_HEADERS}
    if current:
        neighbour_index = position - 2 if position > 1 else 0
        neighbour = current[neighbour_index]
        for header in _COPIED_METADATA_HEADERS:
            inserted[header] = neighbour.get(header, "")
    inserted[OUTPUT_OPERATION_HEADER] = ""
    inserted[TRANSLATED_OPERATION_HEADER] = ""
    current.insert(position - 1, inserted)
    return normalize_review_editor_rows(current)


def delete_review_editor_rows(
    rows: Sequence[Mapping[str, object]],
    positions: Sequence[int],
) -> list[dict[str, object]]:
    """删除一基行号集合并保持剩余顺序。"""
    current = normalize_review_editor_rows(rows)
    selected = {int(position) for position in positions}
    if any(position < 1 or position > len(current) for position in selected):
        raise ValueError("删除位置超出当前审核表范围")
    return normalize_review_editor_rows(
        [
            row
            for index, row in enumerate(current, start=1)
            if index not in selected
        ]
    )
