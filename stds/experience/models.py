"""评估经验工作簿的公共数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class ExperienceIssue:
    """单条经验导入问题；行级问题不会阻塞其他有效经验。"""

    severity: str
    code: str
    message: str
    sheet: str = ""
    row: Optional[int] = None
    field: str = ""


@dataclass(frozen=True)
class ExperienceEntry:
    """已经通过图表码校验、并完成参数经验绑定的动作经验。"""

    experience_id: str
    operation_label: str
    normalized_operation: str
    chartcode: str
    chart_row: int
    parameter_row: Optional[int] = None
    parameter_text: str = ""
    variable_hints: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParameterExperienceEntry:
    """独立于 Chartcode 选择经验保存的参数选择经验。"""

    experience_id: str
    operation_label: str
    normalized_operation: str
    chartcode: str
    parameter_row: int
    parameter_text: str
    variable_hints: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExperienceContext:
    """一次动作匹配结果；必须沿用到对应图表的参数遍历阶段。"""

    experience_id: str
    operation_label: str
    chartcode: str
    match_type: str
    similarity: float
    chart_row: int
    parameter_row: Optional[int]
    parameter_text: str
    variable_hints: dict[int, str]


class CommonChartKind(str, Enum):
    """上传经验中 Common_Chart 行的求值方式。"""

    FORMULA = "formula"
    FIXED_TIME = "fixed_time"


@dataclass(frozen=True)
class CommonChartEntry:
    """一条已完成校验的 Common_Chart 快速决策。"""

    operation_label: str
    normalized_operation: str
    chartcode: str
    decision: str
    cv: str
    frequency: float
    source_time_s: float
    time_s: float
    keywords: tuple[str, ...]
    normalized_keywords: tuple[str, ...]
    row: int
    kind: CommonChartKind
    values: dict[int, float] = field(default_factory=dict)
    keyword_fields: tuple[str, ...] = ()

    @property
    def output_signature(self) -> tuple:
        """判断两个关键词命中是否产生等价结果。"""
        return (
            self.kind.value,
            self.chartcode,
            self.cv,
            round(self.frequency, 9),
            round(self.time_s, 9),
            tuple(sorted(
                (int(variable), round(float(value), 9))
                for variable, value in self.values.items()
            )),
        )


@dataclass(frozen=True)
class CommonChartMatch:
    """Common_Chart 命中结果及其单元格/行级审计信息。"""

    entry: CommonChartEntry
    keyword: str
    match_type: str
    similarity: float = 1.0
    matched_field: str = ""


@dataclass(frozen=True)
class ExperienceLoadResult:
    index: "ExperienceIndex"
    issues: tuple[ExperienceIssue, ...]
    digest: str
    common_entries: tuple[CommonChartEntry, ...] = ()
    common_index: Optional["CommonChartSemanticIndex"] = None

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


# 只供类型检查使用，避免运行时循环导入。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stds.experience.common_index import CommonChartSemanticIndex
    from stds.experience.index import ExperienceIndex
