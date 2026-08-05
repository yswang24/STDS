"""随输入上传的 STDS 经验工作簿加载与动作身份匹配测试。"""
from __future__ import annotations

import asyncio
import math
from io import BytesIO
from types import SimpleNamespace

from openpyxl import Workbook

from stds.experience import load_experience_workbook


def _workbook_bytes(chart_rows, parameter_rows, *, with_ids=False) -> bytes:
    workbook = Workbook()
    chart_sheet = workbook.active
    chart_sheet.title = "chartcode选择经验"
    chart_headers = ["操作内容", "参数选择"]
    parameter_headers = ["操作内容", "动作代码", "参数选择经验"]
    if with_ids:
        chart_headers.insert(0, "经验ID")
        parameter_headers.insert(0, "经验ID")
    chart_sheet.append(chart_headers)
    for row in chart_rows:
        chart_sheet.append(row)

    parameter_sheet = workbook.create_sheet("参数选择经验")
    parameter_sheet.append(parameter_headers)
    for row in parameter_rows:
        parameter_sheet.append(row)

    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_same_chartcode_keeps_turn_and_bend_parameter_identity_separate():
    source = _workbook_bytes(
        [
            ("转身", "202 010"),
            ("弯腰", "202 010"),
        ],
        [
            (
                "转身",
                "202 010",
                "参数V1：选择Turn（默认值）；\n参数V2：选择180度",
            ),
            (
                "弯腰",
                "202 010",
                "参数V1：选择No Twist or Turn（默认值）；\n参数V3：选择45度",
            ),
        ],
    )

    result = load_experience_workbook(source, {"202 010": object()})
    turn = asyncio.run(
        result.index.match(
            "人工A向右转身90度",
            expected_chartcode="202010",
        )
    )
    bend = asyncio.run(
        result.index.match(
            "人工A弯腰拿取零件",
            expected_chartcode="202 010",
        )
    )

    assert turn is not None
    assert bend is not None
    assert turn.chartcode == bend.chartcode == "202 010"
    assert turn.experience_id != bend.experience_id
    assert turn.operation_label == "转身"
    assert bend.operation_label == "弯腰"
    assert "Turn" in turn.variable_hints[1]
    assert "No Twist or Turn" in bend.variable_hints[1]
    assert turn.parameter_row == 2
    assert bend.parameter_row == 3


def test_contains_match_uses_longest_most_specific_operation():
    source = _workbook_bytes(
        [
            ("拿取", "050 221"),
            ("拿取1组小料", "050 141"),
        ],
        [
            ("拿取", "050 221", "参数V1：选择Simple"),
            ("拿取1组小料", "050 141", "参数V1：选择FS"),
        ],
    )
    result = load_experience_workbook(
        BytesIO(source),
        {"050 221": object(), "050 141": object()},
    )

    match = asyncio.run(result.index.match("人工A拿取1组小料后移动"))

    assert match is not None
    assert match.operation_label == "拿取1组小料"
    assert match.chartcode == "050 141"
    assert match.match_type == "contains"
    assert match.variable_hints[1] == "选择FS"


def test_invalid_chartcode_is_warning_and_does_not_block_valid_rows():
    source = _workbook_bytes(
        [
            ("转身", "202 010"),
            ("黏贴", "EST C00"),
        ],
        [
            ("转身", "202 010", "参数V1：选择Turn"),
            ("黏贴", "EST C00", "参数V1：选择5秒"),
        ],
    )

    result = load_experience_workbook(source, {"202 010": object()})

    assert result.index.available
    assert len(result.digest) == 64
    assert any(
        issue.code == "invalid_chartcode"
        and issue.severity == "warning"
        and issue.row == 3
        for issue in result.issues
    )
    assert asyncio.run(result.index.match("人工A转身")) is not None
    assert asyncio.run(result.index.match("人工A黏贴标签")) is None


def test_invalid_parameter_unit_is_warned_and_only_that_variable_hint_is_disabled():
    source = _workbook_bytes(
        [("上升吊具", "061 22A")],
        [(
            "上升吊具",
            "061 22A",
            "参数V1：选择Empty Hoist/Balancer（默认值）；"
            "参数V3：默认选择61m（默认值）",
        )],
    )
    chart = SimpleNamespace(
        options={
            (1, 1): [
                SimpleNamespace(
                    description="Empty Hoist/Balancer",
                    metric_abbrev="EHB",
                )
            ],
            (3, 1): [
                SimpleNamespace(
                    description="24 in / 61 cm",
                    metric_abbrev="61VTX",
                )
            ],
        }
    )

    result = load_experience_workbook(source, {"061 22A": chart})
    match = asyncio.run(result.index.match("人工A上升吊具"))

    assert match is not None
    assert 1 in match.variable_hints
    assert 3 not in match.variable_hints
    assert any(
        issue.code == "invalid_parameter_measurement"
        and issue.row == 2
        and issue.field == "V3"
        for issue in result.issues
    )


def test_parameter_rows_bind_by_experience_id_not_chartcode():
    source = _workbook_bytes(
        [
            ("EXP-TURN", "转身", "202 010"),
            ("EXP-BEND", "弯腰", "202 010"),
        ],
        [
            ("EXP-BEND", "弯腰", "202 010", "参数V1：弯腰规则"),
            ("EXP-TURN", "转身", "202 010", "参数V1：转身规则"),
        ],
        with_ids=True,
    )
    result = load_experience_workbook(source, {"202 010": object()})

    turn = asyncio.run(result.index.match("转身", expected_chartcode="202 010"))
    bend = asyncio.run(result.index.match("弯腰", expected_chartcode="202 010"))

    assert turn is not None and turn.experience_id == "EXP-TURN"
    assert bend is not None and bend.experience_id == "EXP-BEND"
    assert turn.variable_hints[1] == "转身规则"
    assert bend.variable_hints[1] == "弯腰规则"


def test_unbound_parameter_row_is_available_in_independent_pool():
    source = _workbook_bytes(
        [("移动", "050 222")],
        [("拿取", "050 221", "参数V1：选择Simple")],
    )
    result = load_experience_workbook(
        source,
        {"050 221": object(), "050 222": object()},
    )

    assert len(result.index.records) == 1
    assert len(result.index.parameter_records) == 1
    assert asyncio.run(
        result.index.match("人工A拿取零件", expected_chartcode="050 221")
    ) is None

    parameter_match = asyncio.run(
        result.index.match_parameters(
            "人工A拿取零件",
            expected_chartcode="050221",
        )
    )

    assert parameter_match is not None
    assert parameter_match.operation_label == "拿取"
    assert parameter_match.chartcode == "050 221"
    assert parameter_match.chart_row == 0
    assert parameter_match.variable_hints[1] == "选择Simple"
    assert asyncio.run(
        result.index.match_parameters("人工A拿取零件")
    ) is None


def test_parameter_pool_isolates_actions_that_share_chartcode():
    source = _workbook_bytes(
        [],
        [
            ("转身", "202 010", "参数V1：选择Turn"),
            ("弯腰", "202 010", "参数V1：选择No Twist or Turn"),
        ],
    )
    result = load_experience_workbook(source, {"202 010": object()})

    assert len(result.index.records) == 0
    assert len(result.index.parameter_records) == 2
    turn = asyncio.run(
        result.index.match_parameters(
            "人工A向右转身",
            chartcode="202 010",
        )
    )
    bend = asyncio.run(
        result.index.match_parameters(
            "人工A弯腰拿取零件",
            chartcode="202010",
        )
    )

    assert turn is not None
    assert bend is not None
    assert turn.operation_label == "转身"
    assert bend.operation_label == "弯腰"
    assert turn.variable_hints[1] == "选择Turn"
    assert bend.variable_hints[1] == "选择No Twist or Turn"


def test_conflicting_parameter_rules_for_same_action_are_all_disabled():
    source = _workbook_bytes(
        [],
        [
            ("转身", "202 010", "参数V1：选择Turn"),
            ("转身", "202 010", "参数V1：选择No Twist or Turn"),
        ],
    )
    result = load_experience_workbook(source, {"202 010": object()})

    assert result.index.parameter_records == ()
    assert asyncio.run(
        result.index.match_parameters("转身", chartcode="202 010")
    ) is None
    assert any(
        issue.code == "conflicting_parameter_experience"
        for issue in result.issues
    )


def test_conflicting_explicit_parameter_ids_are_all_disabled():
    source = _workbook_bytes(
        [],
        [
            ("PARAM-SHARED", "转身", "202 010", "参数V1：选择Turn"),
            (
                "PARAM-SHARED",
                "弯腰",
                "202 010",
                "参数V1：选择No Twist or Turn",
            ),
        ],
        with_ids=True,
    )
    result = load_experience_workbook(source, {"202 010": object()})

    assert result.index.parameter_records == ()
    assert any(
        issue.code == "conflicting_parameter_experience_id"
        for issue in result.issues
    )


def test_identical_parameter_rows_are_deduplicated():
    source = _workbook_bytes(
        [],
        [
            ("转身", "202 010", "参数V1：选择Turn"),
            ("转身", "202010", "参数V1：选择Turn"),
        ],
    )
    result = load_experience_workbook(source, {"202 010": object()})

    assert len(result.index.parameter_records) == 1
    match = asyncio.run(
        result.index.match_parameters("人工A转身", chartcode="202010")
    )
    assert match is not None
    assert match.parameter_row == 2


def test_unbound_parameter_pool_validates_each_variable_measurement():
    source = _workbook_bytes(
        [],
        [(
            "上升吊具",
            "061 22A",
            "参数V1：选择Empty Hoist/Balancer（默认值）；"
            "参数V3：默认选择61m（默认值）",
        )],
    )
    chart = SimpleNamespace(
        options={
            (1, 1): [
                SimpleNamespace(
                    description="Empty Hoist/Balancer",
                    metric_abbrev="EHB",
                )
            ],
            (3, 1): [
                SimpleNamespace(
                    description="24 in / 61 cm",
                    metric_abbrev="61VTX",
                )
            ],
        }
    )
    result = load_experience_workbook(source, {"061 22A": chart})

    match = asyncio.run(
        result.index.match_parameters(
            "人工A上升吊具",
            chartcode="06122A",
        )
    )

    assert match is not None
    assert 1 in match.variable_hints
    assert 3 not in match.variable_hints
    assert any(
        issue.code == "invalid_parameter_measurement"
        and issue.row == 2
        and issue.field == "V3"
        for issue in result.issues
    )


def test_parameter_text_without_vn_hint_is_not_an_available_record():
    source = _workbook_bytes(
        [],
        [("转身", "202 010", "默认选择Turn")],
    )
    result = load_experience_workbook(source, {"202 010": object()})

    assert result.index.records == ()
    assert result.index.parameter_records == ()
    assert not result.index.available
    assert any(
        issue.code == "unparsed_parameter_experience"
        and issue.row == 2
        and issue.field == "参数选择经验"
        for issue in result.issues
    )


def test_parameter_record_is_removed_when_all_vn_hints_are_invalid():
    source = _workbook_bytes(
        [],
        [("上升吊具", "061 22A", "参数V3：默认选择61m")],
    )
    chart = SimpleNamespace(
        options={
            (3, 1): [
                SimpleNamespace(
                    description="24 in / 61 cm",
                    metric_abbrev="61VTX",
                )
            ],
        }
    )
    result = load_experience_workbook(source, {"061 22A": chart})

    assert result.index.parameter_records == ()
    assert not result.index.available
    assert any(
        issue.code == "invalid_parameter_measurement"
        and issue.row == 2
        and issue.field == "V3"
        for issue in result.issues
    )
    assert any(
        issue.code == "no_valid_parameter_experience"
        and issue.row == 2
        for issue in result.issues
    )


def test_vn_not_present_in_chart_options_is_removed_with_warning():
    source = _workbook_bytes(
        [],
        [("转身", "202 010", "参数V9：默认选择Unknown")],
    )
    chart = SimpleNamespace(
        options={
            (1, 1): [
                SimpleNamespace(
                    description="Turn",
                    metric_abbrev="T",
                )
            ],
        }
    )
    result = load_experience_workbook(source, {"202 010": chart})

    assert result.index.parameter_records == ()
    assert not result.index.available
    assert any(
        issue.code == "invalid_parameter_variable"
        and issue.row == 2
        and issue.field == "V9"
        for issue in result.issues
    )


def test_lexical_conflict_returns_none_but_expected_chartcode_can_disambiguate():
    source = _workbook_bytes(
        [
            ("调整", "202 010"),
            ("调整", "050 222"),
        ],
        [],
    )
    result = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
    )

    assert asyncio.run(result.index.match("调整")) is None
    selected = asyncio.run(
        result.index.match("调整", expected_chartcode="050222")
    )
    assert selected is not None
    assert selected.chartcode == "050 222"


class _SemanticEmbed:
    def __init__(self, query_vector):
        self.query_vector = query_vector
        self.embed_calls = 0

    def embed(self, texts):
        self.embed_calls += 1
        vectors = {
            "旋转身体": [1.0, 0.0],
            "弯曲身体": [0.0, 1.0],
        }
        return [vectors[text] for text in texts]

    def embed_one(self, text):
        return list(self.query_vector)


class _TiedSemanticEmbed:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0]


def test_semantic_match_is_fallback_and_rejects_low_confidence_or_tie():
    source = _workbook_bytes(
        [
            ("旋转身体", "202 010"),
            ("弯曲身体", "050 222"),
        ],
        [],
    )

    confident_backend = _SemanticEmbed([1.0, 0.05])
    confident = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
        embed_backend=confident_backend,
    )
    match = asyncio.run(confident.index.match("改变人物朝向"))
    assert match is not None
    assert match.operation_label == "旋转身体"
    assert match.match_type == "semantic"
    assert confident_backend.embed_calls == 1

    tied = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
        embed_backend=_TiedSemanticEmbed(),
    )
    assert asyncio.run(tied.index.match("改变人物姿态")) is None

    low = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
        embed_backend=_SemanticEmbed([1.0, 1.0]),
    )
    low.index.similarity_margin = 0.0
    assert asyncio.run(low.index.match("动作未知")) is None


def test_chartcode_selection_is_semantic_only_top1_and_keeps_stable_tie():
    source = _workbook_bytes(
        [
            ("旋转身体", "202 010"),
            ("弯曲身体", "050 222"),
        ],
        [],
    )

    exact_text = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
        embed_backend=_SemanticEmbed([1.0, 0.0]),
    )
    semantic = asyncio.run(
        exact_text.index.match_chartcode_semantic("旋转身体")
    )
    assert semantic is not None
    assert semantic.operation_label == "旋转身体"
    assert semantic.match_type == "semantic"

    tied = load_experience_workbook(
        source,
        {"202 010": object(), "050 222": object()},
        embed_backend=_TiedSemanticEmbed(),
    )
    top1 = asyncio.run(
        tied.index.match_chartcode_semantic("改变人物姿态")
    )
    assert top1 is not None
    assert top1.chart_row == 2
    assert top1.operation_label == "旋转身体"


def test_chartcode_semantic_threshold_accepts_070_and_rejects_below():
    source = _workbook_bytes(
        [
            ("旋转身体", "202 010"),
            ("弯曲身体", "050 222"),
        ],
        [],
    )
    charts = {"202 010": object(), "050 222": object()}

    accepted = load_experience_workbook(
        source,
        charts,
        embed_backend=_SemanticEmbed([0.70, -math.sqrt(1 - 0.70**2)]),
    )
    match = asyncio.run(
        accepted.index.match_chartcode_semantic("人物改变朝向")
    )
    assert match is not None
    assert math.isclose(match.similarity, 0.70, abs_tol=1e-9)

    rejected = load_experience_workbook(
        source,
        charts,
        embed_backend=_SemanticEmbed([0.699, -math.sqrt(1 - 0.699**2)]),
    )
    assert asyncio.run(
        rejected.index.match_chartcode_semantic("人物改变朝向")
    ) is None


def test_parameter_contexts_can_be_bound_by_full_experience_identity():
    source = _workbook_bytes(
        [
            ("EXP-TURN", "转身", "202 010"),
            ("EXP-BEND", "弯腰", "202 010"),
        ],
        [
            ("EXP-TURN", "转身", "202 010", "参数V1：选择Turn"),
            (
                "EXP-BEND",
                "弯腰",
                "202 010",
                "参数V1：选择No Twist or Turn",
            ),
        ],
        with_ids=True,
    )
    result = load_experience_workbook(source, {"202 010": object()})

    all_contexts = result.index.parameter_contexts_for_chartcode("202010")
    bound = result.index.parameter_contexts_for_chartcode(
        "202 010",
        experience_id="EXP-TURN",
        operation_key="转身",
    )

    assert [context.experience_id for context in all_contexts] == [
        "EXP-TURN",
        "EXP-BEND",
    ]
    assert len(bound) == 1
    assert bound[0].experience_id == "EXP-TURN"
    assert bound[0].variable_hints[1] == "选择Turn"
