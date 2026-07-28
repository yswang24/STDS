"""从随主输入上传的 Excel 中加载 Chartcode 与参数选择经验。"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Optional, Union

from openpyxl import load_workbook

from stds.experience.index import (
    ExperienceIndex,
    normalize_chartcode,
    normalize_operation,
)
from stds.experience.models import (
    ExperienceEntry,
    ExperienceIssue,
    ExperienceLoadResult,
)
from stds.retrieval.embed import EmbedBackend

WorkbookSource = Union[str, Path, bytes, bytearray, memoryview, BinaryIO]

_CHART_SHEET = "chartcode选择经验"
_PARAMETER_SHEET = "参数选择经验"
_VARIABLE_MARKER = re.compile(
    r"(?:参数\s*)?[VＶvｖ]\s*(\d+)\s*[：:]",
    re.UNICODE,
)
_MEASUREMENT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(cm|厘米|m|米|in|英寸|ft|度|°)(?![a-z])",
    re.IGNORECASE,
)
_PLAIN_NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")


def _source_bytes(source: WorkbookSource) -> tuple[bytes, str]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        return path.read_bytes(), path.name
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), ""
    if not hasattr(source, "read"):
        raise TypeError("source 必须是 path、bytes、BytesIO 或二进制文件对象")

    inferred_name = Path(str(getattr(source, "name", ""))).name
    original_position: Optional[int] = None
    try:
        original_position = source.tell()
    except (AttributeError, OSError):
        pass
    try:
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass
        data = source.read()
    finally:
        if original_position is not None:
            try:
                source.seek(original_position)
            except (AttributeError, OSError):
                pass
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("文件对象必须以二进制方式读取")
    return bytes(data), inferred_name


def _header_map(worksheet) -> dict[str, int]:
    try:
        header = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True))
    except StopIteration:
        return {}
    return {
        str(value).strip(): index
        for index, value in enumerate(header)
        if value is not None and str(value).strip()
    }


def _text(value: object) -> str:
    return str(value or "").strip()


def _cell(row: tuple, index: Optional[int]) -> object:
    return row[index] if index is not None and index < len(row) else None


def split_variable_hints(parameter_text: str) -> dict[int, str]:
    """将“参数Vn：...”拆成当前变量可直接检索的提示。"""
    matches = list(_VARIABLE_MARKER.finditer(parameter_text or ""))
    hints: dict[int, str] = {}
    for index, marker in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(parameter_text)
        hint = parameter_text[marker.end():end].strip().rstrip("；;").strip()
        variable_number = int(marker.group(1))
        if not hint:
            continue
        if variable_number in hints:
            hints[variable_number] = f"{hints[variable_number]}\n{hint}"
        else:
            hints[variable_number] = hint
    return hints


def _derived_experience_id(operation_key: str, chartcode: str) -> str:
    return f"auto:{operation_key}|{normalize_chartcode(chartcode)}"


def _canonical_measurement(number: str, unit: str) -> tuple[str, float]:
    value = float(number)
    normalized_unit = unit.casefold()
    if normalized_unit in {"cm", "厘米"}:
        return "length_m", value / 100.0
    if normalized_unit in {"m", "米"}:
        return "length_m", value
    if normalized_unit in {"in", "英寸"}:
        return "length_m", value * 0.0254
    if normalized_unit == "ft":
        return "length_m", value * 0.3048
    return "angle_deg", value


def _invalid_measurements(
    hint: str,
    chart,
    variable_number: int,
) -> list[str]:
    """发现经验默认值与当前图表候选明显不一致的单位/数值。"""
    requested = [
        (match.group(0), *_canonical_measurement(match.group(1), match.group(2)))
        for match in _MEASUREMENT_RE.finditer(hint)
    ]
    if not requested:
        return []

    options_by_node = getattr(chart, "options", None)
    if not hasattr(options_by_node, "items"):
        return []
    candidates = [
        option
        for (candidate_variable, _), options in options_by_node.items()
        if candidate_variable == variable_number
        for option in options
    ]
    candidate_measurements = [
        _canonical_measurement(match.group(1), match.group(2))
        for candidate in candidates
        for text in (
            str(candidate.description or ""),
            str(candidate.metric_abbrev or ""),
        )
        for match in _MEASUREMENT_RE.finditer(text)
    ]
    candidate_plain_numbers = {
        float(number)
        for candidate in candidates
        for number in _PLAIN_NUMBER_RE.findall(
            str(candidate.description or "")
        )
    }

    invalid = []
    for raw, kind, value in requested:
        if value == 0 and 0.0 in candidate_plain_numbers:
            continue
        tolerance = 0.005 if kind == "length_m" else 0.01
        if not any(
            candidate_kind == kind
            and abs(candidate_value - value) <= tolerance
            for candidate_kind, candidate_value in candidate_measurements
        ):
            # 角度图表的候选常只写“45/90/180”，没有再写“度”。
            if kind == "angle_deg" and value in candidate_plain_numbers:
                continue
            invalid.append(raw)
    return invalid


def _empty_result(
    digest: str,
    source_name: str,
    issues: list[ExperienceIssue],
    embed_backend: Optional[EmbedBackend],
) -> ExperienceLoadResult:
    return ExperienceLoadResult(
        index=ExperienceIndex(
            [],
            digest=digest,
            source_name=source_name,
            embed_backend=embed_backend,
        ),
        issues=tuple(issues),
        digest=digest,
    )


def load_experience_workbook(
    source: WorkbookSource,
    charts: dict,
    *,
    source_name: str = "",
    embed_backend: Optional[EmbedBackend] = None,
) -> ExperienceLoadResult:
    """加载经验工作簿；无效行只产生 warning，不影响其他有效行。"""
    raw, inferred_name = _source_bytes(source)
    digest = hashlib.sha256(raw).hexdigest()
    resolved_source_name = source_name or inferred_name
    issues: list[ExperienceIssue] = []
    try:
        workbook = load_workbook(
            io.BytesIO(raw),
            data_only=True,
            read_only=True,
        )
    except Exception as exc:
        issues.append(ExperienceIssue(
            severity="error",
            code="invalid_workbook",
            message=f"无法读取经验工作簿: {exc}",
        ))
        return _empty_result(digest, resolved_source_name, issues, embed_backend)

    chart_lookup = {
        normalize_chartcode(chartcode): chartcode
        for chartcode in charts
        if normalize_chartcode(chartcode)
    }
    entries: list[ExperienceEntry] = []
    try:
        if _CHART_SHEET not in workbook.sheetnames:
            issues.append(ExperienceIssue(
                severity="error",
                code="missing_sheet",
                message=f"缺少工作表“{_CHART_SHEET}”",
                sheet=_CHART_SHEET,
            ))
            return _empty_result(
                digest, resolved_source_name, issues, embed_backend,
            )

        chart_sheet = workbook[_CHART_SHEET]
        chart_headers = _header_map(chart_sheet)
        operation_col = chart_headers.get("操作内容")
        code_col = chart_headers.get("动作代码")
        if code_col is None:
            # 兼容 V1.2：该列虽名为“参数选择”，实际内容是 Chartcode。
            code_col = chart_headers.get("参数选择")
        id_col = chart_headers.get("经验ID")
        if operation_col is None or code_col is None:
            issues.append(ExperienceIssue(
                severity="error",
                code="missing_header",
                message="chartcode选择经验缺少“操作内容”或“动作代码/参数选择”列",
                sheet=_CHART_SHEET,
            ))
            return _empty_result(
                digest, resolved_source_name, issues, embed_backend,
            )

        provisional: list[ExperienceEntry] = []
        for row_number, row in enumerate(
            chart_sheet.iter_rows(min_row=2, values_only=True),
            start=2,
        ):
            operation_label = _text(_cell(row, operation_col))
            operation_key = normalize_operation(operation_label)
            raw_chartcode = _text(_cell(row, code_col))
            chartcode = chart_lookup.get(normalize_chartcode(raw_chartcode))
            if not operation_key and not raw_chartcode:
                continue
            if not operation_key:
                issues.append(ExperienceIssue(
                    "warning", "empty_operation", "操作内容为空，已忽略",
                    _CHART_SHEET, row_number, "操作内容",
                ))
                continue
            if chartcode is None:
                issues.append(ExperienceIssue(
                    "warning", "invalid_chartcode",
                    f"Chartcode“{raw_chartcode}”不在当前图表库中，已忽略",
                    _CHART_SHEET, row_number, "动作代码",
                ))
                continue
            explicit_id = _text(_cell(row, id_col))
            experience_id = explicit_id or _derived_experience_id(
                operation_key,
                chartcode,
            )
            provisional.append(ExperienceEntry(
                experience_id=experience_id,
                operation_label=operation_label,
                normalized_operation=operation_key,
                chartcode=chartcode,
                chart_row=row_number,
            ))

        # 同一个显式经验 ID 不可指向不同的动作或图表，否则全部禁用。
        signatures_by_id: dict[str, set[tuple[str, str]]] = {}
        for entry in provisional:
            signatures_by_id.setdefault(entry.experience_id, set()).add(
                (entry.normalized_operation, normalize_chartcode(entry.chartcode))
            )
        conflicted_ids = {
            experience_id
            for experience_id, signatures in signatures_by_id.items()
            if len(signatures) > 1
        }
        for experience_id in sorted(conflicted_ids):
            issues.append(ExperienceIssue(
                "warning", "conflicting_experience_id",
                f"经验ID“{experience_id}”对应多个动作或Chartcode，相关行已禁用",
                _CHART_SHEET,
            ))

        # 完全相同的经验行保留第一条，防止重复内容制造虚假歧义。
        seen: set[tuple[str, str, str]] = set()
        for entry in provisional:
            identity = (
                entry.experience_id,
                entry.normalized_operation,
                normalize_chartcode(entry.chartcode),
            )
            if entry.experience_id in conflicted_ids or identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)

        if _PARAMETER_SHEET in workbook.sheetnames:
            parameter_sheet = workbook[_PARAMETER_SHEET]
            parameter_headers = _header_map(parameter_sheet)
            parameter_operation_col = parameter_headers.get("操作内容")
            parameter_code_col = parameter_headers.get("动作代码")
            parameter_text_col = parameter_headers.get("参数选择经验")
            parameter_id_col = parameter_headers.get("经验ID")
            if (
                parameter_operation_col is None
                or parameter_code_col is None
                or parameter_text_col is None
            ):
                issues.append(ExperienceIssue(
                    "error", "missing_header",
                    "参数选择经验缺少“操作内容”“动作代码”或“参数选择经验”列",
                    _PARAMETER_SHEET,
                ))
            else:
                by_id: dict[str, list[int]] = {}
                by_operation_code: dict[tuple[str, str], list[int]] = {}
                for index, entry in enumerate(entries):
                    by_id.setdefault(entry.experience_id, []).append(index)
                    by_operation_code.setdefault(
                        (
                            entry.normalized_operation,
                            normalize_chartcode(entry.chartcode),
                        ),
                        [],
                    ).append(index)

                bindings: dict[int, tuple[int, str, dict[int, str]]] = {}
                conflicted_bindings: set[int] = set()
                for row_number, row in enumerate(
                    parameter_sheet.iter_rows(min_row=2, values_only=True),
                    start=2,
                ):
                    operation_label = _text(
                        _cell(row, parameter_operation_col)
                    )
                    operation_key = normalize_operation(operation_label)
                    raw_chartcode = _text(_cell(row, parameter_code_col))
                    chartcode = chart_lookup.get(
                        normalize_chartcode(raw_chartcode)
                    )
                    parameter_text = _text(_cell(row, parameter_text_col))
                    explicit_id = _text(_cell(row, parameter_id_col))
                    if not operation_key and not raw_chartcode and not parameter_text:
                        continue
                    if chartcode is None:
                        issues.append(ExperienceIssue(
                            "warning", "invalid_chartcode",
                            f"Chartcode“{raw_chartcode}”不在当前图表库中，已忽略",
                            _PARAMETER_SHEET, row_number, "动作代码",
                        ))
                        continue
                    if explicit_id:
                        candidate_indexes = [
                            index
                            for index in by_id.get(explicit_id, [])
                            if normalize_chartcode(entries[index].chartcode)
                            == normalize_chartcode(chartcode)
                        ]
                    else:
                        candidate_indexes = by_operation_code.get(
                            (operation_key, normalize_chartcode(chartcode)),
                            [],
                        )
                    if len(candidate_indexes) != 1:
                        issues.append(ExperienceIssue(
                            "warning", "unbound_parameter_experience",
                            "参数经验无法唯一绑定到“经验ID”或“操作内容+动作代码”，已忽略",
                            _PARAMETER_SHEET, row_number,
                        ))
                        continue
                    target = candidate_indexes[0]
                    new_binding = (
                        row_number,
                        parameter_text,
                        split_variable_hints(parameter_text),
                    )
                    old_binding = bindings.get(target)
                    if old_binding is not None and old_binding[1:] != new_binding[1:]:
                        conflicted_bindings.add(target)
                        issues.append(ExperienceIssue(
                            "warning", "conflicting_parameter_experience",
                            "同一动作身份存在不同参数经验，全部忽略",
                            _PARAMETER_SHEET, row_number,
                        ))
                        continue
                    bindings[target] = new_binding

                entries = [
                    ExperienceEntry(
                        experience_id=entry.experience_id,
                        operation_label=entry.operation_label,
                        normalized_operation=entry.normalized_operation,
                        chartcode=entry.chartcode,
                        chart_row=entry.chart_row,
                        parameter_row=(
                            bindings[index][0]
                            if index in bindings and index not in conflicted_bindings
                            else None
                        ),
                        parameter_text=(
                            bindings[index][1]
                            if index in bindings and index not in conflicted_bindings
                            else ""
                        ),
                        variable_hints=(
                            bindings[index][2]
                            if index in bindings and index not in conflicted_bindings
                            else {}
                        ),
                    )
                    for index, entry in enumerate(entries)
                ]

                # 参数经验最终仍须落到当前图表的真实候选。单位或数值明显
                # 不存在时禁用该 Vn 提示，避免例如“61m”静默套到“61cm”。
                validated_entries = []
                for entry in entries:
                    chart = charts[entry.chartcode]
                    valid_hints = dict(entry.variable_hints)
                    for variable_number, hint in entry.variable_hints.items():
                        invalid_values = _invalid_measurements(
                            hint,
                            chart,
                            variable_number,
                        )
                        if not invalid_values:
                            continue
                        valid_hints.pop(variable_number, None)
                        issues.append(ExperienceIssue(
                            "warning",
                            "invalid_parameter_measurement",
                            (
                                f"参数V{variable_number}中的"
                                f"{'、'.join(invalid_values)}不在当前图表候选中，"
                                "该变量经验已禁用"
                            ),
                            _PARAMETER_SHEET,
                            entry.parameter_row,
                            f"V{variable_number}",
                        ))
                    validated_entries.append(
                        replace(entry, variable_hints=valid_hints)
                    )
                entries = validated_entries
        else:
            issues.append(ExperienceIssue(
                "warning", "missing_sheet",
                f"缺少工作表“{_PARAMETER_SHEET}”，Chartcode经验仍可使用",
                _PARAMETER_SHEET,
            ))
    finally:
        workbook.close()

    index = ExperienceIndex(
        entries,
        digest=digest,
        source_name=resolved_source_name,
        embed_backend=embed_backend,
    )
    return ExperienceLoadResult(index=index, issues=tuple(issues), digest=digest)
