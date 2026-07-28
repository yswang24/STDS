"""评估经验工作簿的公共数据契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ExperienceLoadResult:
    index: "ExperienceIndex"
    issues: tuple[ExperienceIssue, ...]
    digest: str

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


# 只供类型检查使用，避免运行时循环导入。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stds.experience.index import ExperienceIndex
