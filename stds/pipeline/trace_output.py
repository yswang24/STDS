"""单条与批量工时结果共用的决策链输出格式。"""
from __future__ import annotations

import json
from typing import Optional

from stds.domain.models import Source, StdsResult

EXCEL_CELL_TEXT_LIMIT = 32767
TRACE_FIELD_LIMIT = 8000


def serialize_trace(trace: list) -> str:
    """将 trace 序列化为稳定、可被下游再次解析的 JSON。"""
    steps = []
    truncated = False
    for item in trace or []:
        if isinstance(item, dict):
            variable = item.get("变量", item.get("variable", item.get("step", "")))
            choice = item.get("选择", item.get("choice", item.get("description", "")))
            reason = item.get("原因", item.get("reason", ""))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            variable, choice, reason = item[:3]
        else:
            variable, choice, reason = "", str(item), ""
        values = [str(variable), str(choice), str(reason)]
        for index, value in enumerate(values):
            if len(value) > TRACE_FIELD_LIMIT:
                values[index] = value[: TRACE_FIELD_LIMIT - 1] + "…"
                truncated = True
        steps.append({"变量": values[0], "选择": values[1], "原因": values[2]})

    marker = {"变量": "TRUNCATED", "选择": "", "原因": "trace 超出 Excel 单元格上限，已截断"}
    serialized = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= EXCEL_CELL_TEXT_LIMIT and not truncated:
        return serialized

    kept = []
    for step in steps:
        candidate = json.dumps(
            [*kept, step, marker], ensure_ascii=False, separators=(",", ":")
        )
        if len(candidate) > EXCEL_CELL_TEXT_LIMIT:
            truncated = True
            break
        kept.append(step)
    if truncated:
        kept.append(marker)
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))


def result_trace_items(
    result: Optional[StdsResult],
    error: Optional[str] = None,
    *,
    prefix: str = "",
) -> list:
    """提取最终决策结果的选择与原因，不混入拆解阶段信息。"""
    label = lambda value: f"{prefix}:{value}" if prefix else value
    if result is None:
        return [(label("ERROR"), "", error or "未知错误")]
    if result.trace:
        return [
            (label(str(item[0])), item[1], item[2])
            if isinstance(item, (list, tuple)) and len(item) >= 3
            else (label("trace"), str(item), "")
            for item in result.trace
        ]
    if result.needs_review or result.source == Source.UNRESOLVED:
        return [
            (label("UNRESOLVED"), result.chartcode or "", "未能完成决策解析，需要人工复核")
        ]
    if result.source == Source.MACHINE:
        return [
            (label("T2_machine"), "设备动作", "判定为设备动作，跳过人工标准时间计算")
        ]
    return []


def decision_reason(
    result: Optional[StdsResult],
    error: Optional[str] = None,
) -> str:
    """返回最终结果列使用的决策链 JSON。"""
    return serialize_trace(result_trace_items(result, error))
