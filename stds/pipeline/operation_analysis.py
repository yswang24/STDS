"""可复用的单条动作拆解与工时分析服务。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Optional, Sequence

from stds.cascade import rules
from stds.cascade.resolver import Deps, resolve
from stds.cascade.rules import normalize
from stds.domain.models import Source, StdsElement, StdsResult
from stds.llm.decompose import decompose_operation

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


@dataclass
class OperationAnalysisItem:
    index: int
    total: int
    operation: str
    result: Optional[StdsResult] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0
    split_needs_review: bool = False

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
        """拆解阶段仅返回最终输出格式中已经产生的字段。"""
        return [
            {
                "序号": self.number,
                "工位号": self.station_op,
                "操作内容": item.operation,
            }
            for item in self.items
        ]

    def detail_rows(self) -> list[dict]:
        return [
            {
                "拆解序号": f"{item.index}/{item.total}",
                "operation": item.operation,
                "Chartcode": item.result.chartcode if item.result else "",
                "决策串": item.result.decision if item.result else "",
                "标准时间（秒）": (
                    item.result.time_s
                    if item.result is not None and item.status == "成功"
                    else None
                ),
                "分析耗时（秒）": round(item.elapsed_s, 2),
                "状态": item.status,
                "错误": item.error or "",
            }
            for item in self.items
        ]


async def _notify(callback: Optional[Callable], *args) -> None:
    if callback is None:
        return
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("Operation analysis callback failed", exc_info=True)


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

        if machine:
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
) -> StdsResult:
    """调用支持 machine_hint 的解析器，同时兼容旧的二参数测试解析器。"""
    try:
        parameters = inspect.signature(resolver).parameters.values()
        accepts_hint = any(
            parameter.name == "machine_hint"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_hint = False
    if accepts_hint:
        return await resolver(element, deps, machine_hint=actor == "设备")
    return await resolver(element, deps)


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
    on_decomposed: Optional[DecomposedCallback] = None,
    on_progress: Optional[ItemProgressCallback] = None,
) -> OperationAnalysis:
    """单条输入的完整两阶段分析，结果按拆解顺序返回。"""
    started = time.perf_counter()
    decompose_started = time.perf_counter()
    split = await split_operation(operation, deps, decomposer=decomposer)
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
            item.result = await resolve_with_actor(resolver, element, deps, split.actor)
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
        station_op=station_op,
        split=split,
        items=items,
        decompose_elapsed_s=decompose_elapsed_s,
        analysis_elapsed_s=analysis_elapsed_s,
        total_elapsed_s=time.perf_counter() - started,
    )
