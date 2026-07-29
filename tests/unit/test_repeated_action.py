"""Tests for parent-scoped repeated-action grouping."""
from __future__ import annotations

from stds.pipeline.repeated_action import build_repeated_action_groups


def test_four_numbered_bolts_form_one_group():
    resolution = build_repeated_action_groups(
        [
            "操作人员用拧紧枪拧紧电池包第一个螺栓",
            "操作人员用拧紧枪拧紧电池包第二颗螺栓",
            "操作人员用拧紧枪拧紧电池包第3枚螺栓",
            "操作人员用拧紧枪拧紧电池包第四只螺栓",
        ]
    )

    assert len(resolution.groups) == 1
    group = resolution.groups[0]
    assert group.group_id == "RG1"
    assert group.canonical_operation == "操作人员用拧紧枪拧紧电池包螺栓"
    assert group.child_indexes == (1, 2, 3, 4)
    assert all(resolution.group_for(index) is group for index in range(1, 5))
    assert resolution.group_for(0) is None
    assert resolution.group_for(5) is None


def test_align_and_tighten_actions_form_separate_groups_with_stable_ids():
    resolution = build_repeated_action_groups(
        [
            "操作人员用拧紧枪对准电池包第一颗螺栓",
            "操作人员用拧紧枪拧紧电池包第一颗螺栓",
            "操作人员用拧紧枪对准电池包第二颗螺栓",
            "操作人员用拧紧枪拧紧电池包第二颗螺栓",
        ]
    )

    assert [
        (group.group_id, group.canonical_operation, group.child_indexes)
        for group in resolution.groups
    ] == [
        ("RG1", "操作人员用拧紧枪对准电池包螺栓", (1, 3)),
        ("RG2", "操作人员用拧紧枪拧紧电池包螺栓", (2, 4)),
    ]


def test_manual_and_tool_actions_remain_separate():
    resolution = build_repeated_action_groups(
        [
            "操作人员手动拧紧第一颗螺栓",
            "操作人员用拧紧枪拧紧第一颗螺栓",
            "操作人员手动拧紧第二颗螺栓",
            "操作人员用拧紧枪拧紧第二颗螺栓",
        ]
    )

    assert [group.child_indexes for group in resolution.groups] == [
        (1, 3),
        (2, 4),
    ]
    assert [group.canonical_operation for group in resolution.groups] == [
        "操作人员手动拧紧螺栓",
        "操作人员用拧紧枪拧紧螺栓",
    ]


def test_engineering_numbers_are_preserved_and_keep_torque_groups_separate():
    resolution = build_repeated_action_groups(
        [
            "操作人员以5Nm拧紧第一颗螺栓",
            "操作人员以8Nm拧紧第一颗螺栓",
            "操作人员以5Nm拧紧第二颗螺栓",
            "操作人员以8Nm拧紧第二颗螺栓",
        ]
    )

    assert [group.child_indexes for group in resolution.groups] == [
        (1, 3),
        (2, 4),
    ]
    assert [group.canonical_operation for group in resolution.groups] == [
        "操作人员以5Nm拧紧螺栓",
        "操作人员以8Nm拧紧螺栓",
    ]


def test_singletons_and_unchanged_duplicate_text_do_not_group():
    resolution = build_repeated_action_groups(
        [
            "操作人员检查螺栓",
            "操作人员检查螺栓",
            "操作人员拧紧第一颗螺栓",
        ]
    )

    assert resolution.groups == ()
    assert resolution.by_child_index == {}
    assert resolution.group_for(1) is None


def test_removing_an_ordinal_phrase_cleans_surrounding_spacing():
    resolution = build_repeated_action_groups(
        [
            "操作人员拧紧 第一个 螺栓",
            "操作人员拧紧 第二个 螺栓",
        ]
    )

    assert resolution.groups[0].canonical_operation == "操作人员拧紧 螺栓"
