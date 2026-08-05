"""Streamlit 上传经验上下文的状态边界回归测试。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import stds.experience


APP_PATH = Path(__file__).parents[2] / "stds" / "ui" / "app.py"


def _valid_experience_result(*, source_name: str = ""):
    index = SimpleNamespace(
        available=True,
        records=(object(),),
        parameter_records=(object(),),
        source_name=source_name,
    )
    common = SimpleNamespace(kind=SimpleNamespace(value="fixed_time"))
    return SimpleNamespace(
        index=index,
        issues=(),
        common_entries=(common,),
    )


def test_common_enabled_without_uploaded_common_blocks_both_entry_points():
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()

    assert not app.exception
    assert any(
        "Common_Chart" in warning.value and "暂停" in warning.value
        for warning in app.warning
    )
    batch_button = next(
        button for button in app.button
        if button.label == "🚀 开始批量分析"
    )
    single_button = next(
        button for button in app.button
        if button.label == "🔍 分析"
    )
    assert batch_button.disabled is True
    assert single_button.disabled is True


def test_semantic_experience_toggle_defaults_on_and_is_independent_from_common():
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()

    semantic_toggle = next(
        toggle for toggle in app.toggle
        if toggle.label == "启用经验语义向量检索"
    )
    common_toggle = next(
        toggle for toggle in app.toggle
        if toggle.label == "启用 T0.5 Common Chart"
    )
    assert semantic_toggle.value is True
    assert semantic_toggle.disabled is False

    common_toggle.set_value(False).run()
    semantic_toggle = next(
        toggle for toggle in app.toggle
        if toggle.label == "启用经验语义向量检索"
    )
    assert semantic_toggle.value is True
    assert semantic_toggle.disabled is False


def test_switching_or_removing_experience_upload_invalidates_old_outputs(
    monkeypatch,
):
    monkeypatch.setattr(
        stds.experience,
        "load_experience_workbook",
        lambda data, charts, source_name="": _valid_experience_result(
            source_name=source_name,
        ),
    )
    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
    experience_upload = app.file_uploader[1]

    app.session_state.batch_output = {"stale": True}
    app.session_state.batch_flow = {
        "stage": "editing",
        "run_id": 7,
        "run_signature": ("stale",),
    }
    app.session_state.single_output = {"stale": True}
    experience_upload.upload("experience-a.xlsx", b"a").run()

    assert not app.exception
    assert app.session_state.batch_output is None
    assert app.session_state.single_output is None
    assert app.session_state.batch_flow["stage"] == "idle"
    assert app.session_state.active_experience_key == (
        hashlib.sha256(b"a").hexdigest(),
        "experience-a.xlsx",
    )
    assert any(
        "1 条有效 Chartcode" in message.value
        and "1 条有效参数" in message.value
        and "1 条有效 Common_Chart" in message.value
        and "EST 固定时间 1 条" in message.value
        for message in app.success
    )
    single_button = next(
        button for button in app.button
        if button.label == "🔍 分析"
    )
    assert single_button.disabled is False

    app.session_state.batch_output = {"stale": True}
    app.session_state.batch_flow = {
        "stage": "editing",
        "run_id": 8,
        "run_signature": ("stale",),
    }
    app.session_state.single_output = {"stale": True}
    app.file_uploader[1].clear().upload("experience-b.xlsx", b"b").run()

    assert not app.exception
    assert app.session_state.batch_output is None
    assert app.session_state.single_output is None
    assert app.session_state.batch_flow["stage"] == "idle"
    assert app.session_state.active_experience_key == (
        hashlib.sha256(b"b").hexdigest(),
        "experience-b.xlsx",
    )

    app.session_state.batch_output = {"stale": True}
    app.session_state.batch_flow = {
        "stage": "editing",
        "run_id": 9,
        "run_signature": ("stale",),
    }
    app.session_state.single_output = {"stale": True}
    app.file_uploader[1].clear().run()

    assert not app.exception
    assert app.session_state.batch_output is None
    assert app.session_state.single_output is None
    assert app.session_state.batch_flow["stage"] == "idle"
    assert app.session_state.active_experience_key is None
    assert any("Common_Chart" in warning.value for warning in app.warning)
