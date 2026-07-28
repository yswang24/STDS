"""可复用的单条动作拆解与工时分析服务。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Optional, Sequence

from stds.cascade import rules
from stds.cascade.numeric import NumericContext
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
from stds.pipeline.trace_output import decision_reason

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
            sum(item.result.time_s for item in self.items if item.result is not None),
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
            if result is None or result.source in {Source.MACHINE, Source.UNRESOLVED}:
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
                    DECISION_REASON_HEADER: decision_reason(result, item.error),
                }
            )
        return rows


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
    weight_resolution = (
        await resolve_part_weight_groups(
            operation,
            split.operations,
            deps,
        )
        if split.actor == "人工"
        else PartWeightGroupResolution()
    )
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
    decompose_elapsed_s = time.perf_counter() - decompose_started
    await _notify(on_decomposed, split, decompose_elapsed_s)

    completed = 0
    analysis_started = time.perf_counter()

    async def analyze_child(index: int, child: str) -> OperationAnalysisItem:
        nonlocal completed
        item_started = time.perf_counter()
        item = OperationAnalysisItem(
            index,
            len(split.operations),
            child,
            display_operation=split.output_operations[index - 1],
            split_needs_review=split.needs_review,
        )
        try:
            element = StdsElement(
                number=number,
                operation_des=child,
                line_name=line_name,
                station_op=station_op,
                freq=freq,
                norm_key=normalize(child) or child,
            )
            item.result = await resolve_with_actor(
                resolver,
                element,
                deps,
                split.actor,
                numeric_context=weight_resolution.contexts.get(index),
                part_context_resolved=weight_resolution.attempted,
            )
        except Exception as exc:
            logger.exception("Operation child analysis failed: operation=%r", child)
            item.error = f"{type(exc).__name__}: {exc}"
        finally:
            item.elapsed_s = time.perf_counter() - item_started
            completed += 1
            await _notify(on_progress, item, completed, len(split.operations))
        return item

    items = await asyncio.gather(
        *(
            analyze_child(index, child)
            for index, child in enumerate(split.operations, start=1)
        )
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
