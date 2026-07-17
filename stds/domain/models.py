"""纯数据模型(pydantic/dataclass 风格,用标准库 dataclass)。

ValueOption / MostChart / StdsElement / StdsResult。
engine 层零 LLM、零 IO,这些模型是它们之间的契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Source(str, Enum):
    CACHE = "cache"
    KNN = "knn"
    MACHINE = "machine"
    RAG = "rag"
    FORMULA = "formula"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ValueOption:
    """决策树的一个取值叶子(对应 formula 表一行)。"""

    variable_number: int
    range_number: int
    value_number: int
    description: str          # ValueDescription,给 LLM/decode 看
    metric_abbrev: Optional[str]  # ValueMetricAbbrev,可 null
    formula_value: float      # ValueFormulaValue,代入 Vn 的数
    next_variable: int        # 0 = 决策树终点
    next_range: int


@dataclass
class MostChart:
    """一个 MOST 数据卡(Chartcode)的内存决策图。"""

    chartcode: str            # 完整码 "020 02A"(连接键)
    title: str
    formula: str              # ChartFormula(已 lstrip '=')
    value_added: bool         # ChartValueAdded -> C/V
    developed_in_seconds: bool  # ChartDevelopedInSeconds
    options: dict             # {(var_no, range_no): [ValueOption, ...]}

    def candidates(self, var_no: int, range_no: int) -> list:
        # ★ 双键取候选 -- 这一行修掉了 Dify 忽略 range 的 bug
        return self.options.get((var_no, range_no), [])


@dataclass
class StdsElement:
    """一条待分析的作业元素(输入)。"""

    number: int
    operation_des: str
    line_name: str
    station_op: str
    freq: float = 1.0
    norm_key: str = ""


@dataclass
class StdsResult:
    """一条作业元素的计算产出。"""

    element: StdsElement
    chartcode: Optional[str]
    decision: str             # 决策串 "PSF,,E,25DMX,NIBU"
    time_s: float             # 单次公式时间 × freq(由公式求值,不读历史)
    cv: str                   # "C" / "V"
    freq: float
    source: Source
    confidence: float
    needs_review: bool
    trace: list = field(default_factory=list)  # 每步 (变量, 选中项, 理由)
    edited: bool = False

    @classmethod
    def machine_placeholder(cls, el: StdsElement) -> "StdsResult":
        return cls(el, None, "", 0.0, "V", el.freq, Source.MACHINE, 1.0, False)

    @classmethod
    def unresolved(cls, el: StdsElement, cc: Optional[str]) -> "StdsResult":
        r = cls(el, cc, "", 0.0, "V", el.freq, Source.UNRESOLVED, 0.0, True)
        return r

    def mark_review(self) -> "StdsResult":
        self.needs_review = True
        return self
