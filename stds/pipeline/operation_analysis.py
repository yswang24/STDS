"""可复用的单条动作拆解与工时分析服务。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Optional, Sequence

from stds.cascade import rules
from stds.cascade.numeric import NumericContext, PartIdentityContext
from stds.cascade.resolver import (
    Deps,
    PartWeightGroupResolution,
    resolve,
    resolve_part_weight_groups,
)
from stds.cascade.rules import normalize
from stds.domain.models import Source, StdsElement, StdsResult
from stds.llm.decompose import decompose_operation
from stds.llm.translate_operation import (
    OutputTranslator,
    translate_operation_for_display,
    translate_operation_for_output,
)
from stds.pipeline.output_schema import (
    CHARTCODE_HEADER,
    CV_HEADER,
    DECISION_HEADER,
    DECISION_REASON_HEADER,
    FREQ_HEADER,
    NUMBER_HEADER,
    OUTPUT_OPERATION_HEADER,
    PROJECT_HEADER,
    STATION_HEADER,
    STDS_HEADER,
    TIME_HEADER,
    TRANSLATED_OPERATION_HEADER,
)
from stds.pipeline.repeated_action import build_repeated_action_groups
from stds.pipeline.trace_output import result_trace_items, serialize_trace

logger = logging.getLogger("stds.operation_analysis")

Resolver = Callable[..., Awaitable[StdsResult]]
Decomposer = Callable[[str], Awaitable[Sequence[str]]]
DecomposedCallback = Callable[["OperationSplit", float], object]
ItemProgressCallback = Callable[["OperationAnalysisItem", int, int], object]


@dataclass(frozen=True)
class OperationSplit:
    actor: str
    operations: tuple[str, ...]
    source: str
    needs_review: bool = False
    error: Optional[str] = None
    display_operations: tuple[str, ...] = ()

    @property
    def output_operations(self) -> tuple[str, ...]:
        """最终展示文本；拆解原文仍保留在 operations 中用于工时分析。"""
        return self.display_operations or self.operations


@dataclass
class OperationAnalysisItem:
    index: int
    total: int
    operation: str
    display_operation: Optional[str] = None
    result: Optional[StdsResult] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0
    split_needs_review: bool = False
    repeated_action_trace: Optional[tuple[str, str, str]] = None
    actor: str = "人工"

    @property
    def output_operation(self) -> str:
        """翻译后的展示文本；无翻译结果时回退拆解原文。"""
        return self.display_operation or self.operation

    @property
    def status(self) -> str:
        if self.error or self.result is None:
            return "失败"
        if (
            self.split_needs_review
            or self.result.needs_review
            or self.result.source == Source.UNRESOLVED
        ):
            return "待复核"
        return "成功"


@dataclass
class OperationAnalysis:
    original_operation: str
    number: object
    line_name: str
    station_op: str
    split: OperationSplit
    items: list[OperationAnalysisItem]
    decompose_elapsed_s: float
    analysis_elapsed_s: float
    total_elapsed_s: float

    @property
    def status(self) -> str:
        statuses = {item.status for item in self.items}
        if "失败" in statuses:
            return "失败"
        if self.split.needs_review or "待复核" in statuses:
            return "待复核"
        return "成功"

    @property
    def total_time_s(self) -> Optional[float]:
        if self.status != "成功":
            return None
        return round(
            sum(
                item.result.time_s
                for item in self.items
                if item.actor != "设备" and item.result is not None
            ),
            2,
        )

    def decomposition_rows(self) -> list[dict]:
        """拆解阶段按 PF 拆解表字段返回当前已经产生的内容。"""
        return [
            {
                NUMBER_HEADER: self.number,
                PROJECT_HEADER: self.line_name,
                STATION_HEADER: self.station_op,
                OUTPUT_OPERATION_HEADER: item.operation,
                TRANSLATED_OPERATION_HEADER: item.output_operation,
            }
            for item in self.items
        ]

    def detail_rows(self) -> list[dict]:
        """按最终工时表字段返回单条链路已经产生的内容。"""
        rows = []
        for item in self.items:
            result = item.result
            if (
                item.actor == "设备"
                or result is None
                or result.source in {Source.MACHINE, Source.UNRESOLVED}
            ):
                decision, chartcode, cv, freq, time_value = ("NA",) * 5
            else:
                decision = result.decision or "NA"
                chartcode = result.chartcode or "NA"
                cv = result.cv or "NA"
                freq = result.freq if result.freq is not None else "NA"
                time_value = result.time_s if item.status == "成功" else "NA"
            rows.append(
                {
                    NUMBER_HEADER: self.number,
                    PROJECT_HEADER: self.line_name,
                    STATION_HEADER: self.station_op,
                    STDS_HEADER: item.output_operation,
                    DECISION_HEADER: decision,
                    CHARTCODE_HEADER: chartcode,
                    CV_HEADER: cv,
                    FREQ_HEADER: freq,
                    TIME_HEADER: time_value,
                    DECISION_REASON_HEADER: _item_decision_reason(item),
                }
            )
        return rows


def _item_decision_reason(item: OperationAnalysisItem) -> str:
    trace = result_trace_items(item.result, item.error)
    repeated_trace = item.repeated_action_trace
    if repeated_trace is None:
        return serialize_trace(trace)
    result_has_trace = (
        item.result is not None
        and repeated_trace in (item.result.trace or [])
    )
    if not result_has_trace:
        trace = [repeated_trace, *trace]
    return serialize_trace(trace)


@dataclass(frozen=True)
class _OperationAnalysisUnit:
    """一次解析调用及其对应的原始拆解动作。"""

    child_indexes: tuple[int, ...]
    resolve_operation: str
    actor: str
    repeated_group_id: Optional[str] = None


def _build_analysis_units(
    operations: Sequence[str],
    actors: Sequence[str],
    weight_resolution: PartWeightGroupResolution,
) -> list[_OperationAnalysisUnit]:
    """按重复人工动作组生成解析单元，并隔离设备及不同重量上下文。"""
    if len(operations) != len(actors):
        raise ValueError("operations 与 actors 数量必须一致")

    repeated = build_repeated_action_groups(operations)
    units: list[_OperationAnalysisUnit] = []
    grouped_indexes: set[int] = set()

    for group in repeated.groups:
        human_indexes = tuple(
            child_index
            for child_index in group.child_indexes
            if actors[child_index - 1] != "设备"
        )
        if len(human_indexes) < 2:
            continue

        context_partitions: list[
            tuple[
                Optional[NumericContext],
                Optional[PartIdentityContext],
                list[int],
            ]
        ] = []
        for child_index in human_indexes:
            context = weight_resolution.contexts.get(child_index)
            identity_context = weight_resolution.identity_contexts.get(
                child_index
            )
            partition = next(
                (
                    indexes
                    for candidate, candidate_identity, indexes
                    in context_partitions
                    if (
                        candidate is context
                        and candidate_identity is identity_context
                    )
                ),
                None,
            )
            if partition is None:
                partition = []
                context_partitions.append(
                    (context, identity_context, partition)
                )
            partition.append(child_index)
            grouped_indexes.add(child_index)

        for _, _, child_indexes in context_partitions:
            indexes = tuple(child_indexes)
            if len(indexes) == 1:
                child_index = indexes[0]
                units.append(
                    _OperationAnalysisUnit(
                        child_indexes=indexes,
                        resolve_operation=operations[child_index - 1],
                        actor=actors[child_index - 1],
                    )
                )
                continue
            units.append(
                _OperationAnalysisUnit(
                    child_indexes=indexes,
                    resolve_operation=group.canonical_operation,
                    actor=actors[indexes[0] - 1],
                    repeated_group_id=group.group_id,
                )
            )

    for child_index, child in enumerate(operations, start=1):
        if child_index not in grouped_indexes:
            units.append(
                _OperationAnalysisUnit(
                    child_indexes=(child_index,),
                    resolve_operation=child,
                    actor=actors[child_index - 1],
                )
            )
    return sorted(units, key=lambda unit: unit.child_indexes[0])


async def _notify(callback: Optional[Callable], *args) -> None:
    if callback is None:
        return
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("Operation analysis callback failed", exc_info=True)


async def classify_operation_actor(
    operation: str,
    deps: Deps,
) -> tuple[str, str]:
    """只判定动作主体，不执行拆解；供初始拆解与人工审核后的新增动作共用。"""
    machine = rules.rule_machine(operation)
    classify_source = "规则判定"
    if machine is None:
        classifier = getattr(deps, "llm_classify", None)
        if classifier is None:
            machine = False
            classify_source = "未配置分类器，按人工"
        else:
            machine = await classifier(operation)
            classify_source = "LLM判定"
    return ("设备" if machine else "人工"), classify_source


async def split_operation(
    operation: str,
    deps: Deps,
    *,
    decomposer: Decomposer = decompose_operation,
) -> OperationSplit:
    """判定主体；设备保持原动作，人工调用 Dify 原 Prompt 拆解。"""
    fallback = OperationSplit(
        actor="人工",
        operations=(operation,),
        source="拆解失败回退",
        needs_review=True,
    )
    try:
        actor, classify_source = await classify_operation_actor(operation, deps)

        if actor == "设备":
            return OperationSplit(
                actor="设备",
                operations=(operation,),
                source=f"{classify_source}（设备动作不拆解）",
            )

        operations = tuple(
            str(child).strip()
            for child in await decomposer(operation)
            if str(child).strip()
        )
        if not operations:
            raise ValueError("拆解结果 operation 不能为空")
        return OperationSplit(
            actor="人工",
            operations=operations,
            source=f"{classify_source} + Dify原Prompt",
        )
    except Exception as exc:
        logger.exception("Operation decomposition failed: operation=%r", operation)
        return replace(fallback, error=f"{type(exc).__name__}: {exc}")


async def resolve_with_actor(
    resolver: Resolver,
    element: StdsElement,
    deps: Deps,
    actor: str,
    *,
    numeric_context: Optional[NumericContext] = None,
    part_identity_context: Optional[PartIdentityContext] = None,
    part_context_resolved: bool = False,
) -> StdsResult:
    """按解析器签名传递可用上下文，同时兼容旧的二参数测试解析器。"""
    try:
        parameters = inspect.signature(resolver).parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        parameters = {}
        accepts_kwargs = False

    kwargs = {}
    if accepts_kwargs or "machine_hint" in parameters:
        kwargs["machine_hint"] = actor == "设备"
    if accepts_kwargs or "numeric_context" in parameters:
        kwargs["numeric_context"] = numeric_context
    if accepts_kwargs or "part_identity_context" in parameters:
        kwargs["part_identity_context"] = part_identity_context
    if accepts_kwargs or "part_context_resolved" in parameters:
        kwargs["part_context_resolved"] = part_context_resolved
    return await resolver(element, deps, **kwargs)


async def analyze_operation(
    operation: str,
    deps: Deps,
    *,
    number: int = 1,
    line_name: str = "手动输入",
    station_op: str = "手动输入",
    freq: float = 1.0,
    resolver: Resolver = resolve,
    decomposer: Decomposer = decompose_operation,
    translator: OutputTranslator = translate_operation_for_output,
    on_decomposed: Optional[DecomposedCallback] = None,
    on_progress: Optional[ItemProgressCallback] = None,
) -> OperationAnalysis:
    """单条输入的完整两阶段分析，结果按拆解顺序返回。"""
    started = time.perf_counter()
    decompose_started = time.perf_counter()
    split = await split_operation(operation, deps, decomposer=decomposer)
    child_actors = tuple(
        "设备"
        if rules.is_explicit_machine_action(child)
        else split.actor
        for child in split.operations
    )
    human_children = tuple(
        (child_index, child)
        for child_index, (child, actor) in enumerate(
            zip(split.operations, child_actors),
            start=1,
        )
        if actor == "人工"
    )
    if human_children:
        human_weight_resolution = await resolve_part_weight_groups(
            operation,
            tuple(child for _, child in human_children),
            deps,
        )
        weight_resolution = PartWeightGroupResolution(
            contexts={
                original_index: human_weight_resolution.contexts[human_index]
                for human_index, (original_index, _) in enumerate(
                    human_children,
                    start=1,
                )
                if human_index in human_weight_resolution.contexts
            },
            identity_contexts={
                original_index: human_weight_resolution.identity_contexts[
                    human_index
                ]
                for human_index, (original_index, _) in enumerate(
                    human_children,
                    start=1,
                )
                if human_index
                in human_weight_resolution.identity_contexts
            },
            attempted=human_weight_resolution.attempted,
        )
    else:
        weight_resolution = PartWeightGroupResolution()
    unique_operations = tuple(dict.fromkeys(split.operations))
    translated_operations = await asyncio.gather(
        *(
            translate_operation_for_display(child, translator=translator)
            for child in unique_operations
        )
    )
    display_by_operation = dict(zip(unique_operations, translated_operations))
    display_operations = tuple(
        display_by_operation[child]
        for child in split.operations
    )
    split = replace(split, display_operations=display_operations)
    analysis_units = _build_analysis_units(
        split.operations,
        child_actors,
        weight_resolution,
    )
    decompose_elapsed_s = time.perf_counter() - decompose_started
    await _notify(on_decomposed, split, decompose_elapsed_s)

    completed = 0
    analysis_started = time.perf_counter()

    def element_for(operation_des: str) -> StdsElement:
        return StdsElement(
            number=number,
            operation_des=operation_des,
            line_name=line_name,
            station_op=station_op,
            freq=freq,
            norm_key=normalize(operation_des) or operation_des,
        )

    async def analyze_unit(
        unit: _OperationAnalysisUnit,
    ) -> list[OperationAnalysisItem]:
        nonlocal completed
        item_started = time.perf_counter()
        items = [
            OperationAnalysisItem(
                child_index,
                len(split.operations),
                split.operations[child_index - 1],
                actor=unit.actor,
                display_operation=split.output_operations[child_index - 1],
                split_needs_review=split.needs_review,
            )
            for child_index in unit.child_indexes
        ]
        repeated_trace = (
            (
                "RepeatedActionConsistency",
                f"{unit.repeated_group_id}: {unit.resolve_operation}",
                f"members={list(unit.child_indexes)}",
            )
            if unit.repeated_group_id is not None
            else None
        )
        for item in items:
            item.repeated_action_trace = repeated_trace
        try:
            result = await resolve_with_actor(
                resolver,
                element_for(unit.resolve_operation),
                deps,
                unit.actor,
                numeric_context=(
                    None
                    if unit.actor == "设备"
                    else weight_resolution.contexts.get(unit.child_indexes[0])
                ),
                part_identity_context=(
                    None
                    if unit.actor == "设备"
                    else weight_resolution.identity_contexts.get(
                        unit.child_indexes[0]
                    )
                ),
                part_context_resolved=(
                    unit.actor != "设备" and weight_resolution.attempted
                ),
            )
            if unit.actor == "设备" and result.source != Source.MACHINE:
                # 有效主体是最终输出约束。即使外部注入的 resolver 忽略
                # machine_hint 或返回了旧人工结果，也不能泄漏人工工时。
                result = StdsResult.machine_placeholder(
                    element_for(unit.resolve_operation)
                )
            for item in items:
                base_trace = list(result.trace or result_trace_items(result))
                child_trace = (
                    [repeated_trace, *base_trace]
                    if repeated_trace is not None
                    else base_trace
                )
                # 复制单次结果，不按一致性组成员数改写 freq 或 time_s。
                item.result = replace(
                    result,
                    element=element_for(item.operation),
                    freq=freq,
                    trace=child_trace,
                )
        except Exception as exc:
            logger.exception(
                "Operation analysis unit failed: operation=%r children=%s",
                unit.resolve_operation,
                unit.child_indexes,
            )
            error = f"{type(exc).__name__}: {exc}"
            for item in items:
                item.error = error
        finally:
            elapsed_s = time.perf_counter() - item_started
            per_item_elapsed_s = elapsed_s / len(items)
            for item in items:
                item.elapsed_s = per_item_elapsed_s
                completed += 1
                await _notify(on_progress, item, completed, len(split.operations))
        return items

    item_groups = await asyncio.gather(
        *(analyze_unit(unit) for unit in analysis_units)
    )
    items = sorted(
        (item for group in item_groups for item in group),
        key=lambda item: item.index,
    )
    analysis_elapsed_s = time.perf_counter() - analysis_started
    return OperationAnalysis(
        original_operation=operation,
        number=number,
        line_name=line_name,
        station_op=station_op,
        split=split,
        items=items,
        decompose_elapsed_s=decompose_elapsed_s,
        analysis_elapsed_s=analysis_elapsed_s,
        total_elapsed_s=time.perf_counter() - started,
    )
