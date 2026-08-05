from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from stds.api import main
from stds.data.cache import decision_cache_scope


def _experience_workbook(
    *,
    operation: str = "转身",
    include_common: bool = True,
    malformed_common: bool = False,
) -> bytes:
    workbook = Workbook()
    chart_sheet = workbook.active
    chart_sheet.title = "chartcode选择经验"
    chart_sheet.append(["操作内容", "参数选择"])
    chart_sheet.append([operation, "202 010"])
    if include_common:
        common = workbook.create_sheet("Common_Chart")
        common.append(
            ["操作内容", "错误表头"]
            if malformed_common
            else [
                "操作内容",
                "决策描述",
                "动作代码",
                "增值/非增值(C/V)",
                "频率",
                "时间",
                "关键词描述1",
            ]
        )
        if malformed_common:
            common.append([operation, "bad"])
        else:
            common.append([operation, "T,90,NB", "202 010", "V", 1, 0.72, operation])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def _clear_registry():
    main._experience_contexts.clear()
    yield
    main._experience_contexts.clear()


def test_upload_experience_context_returns_digest_counts_and_reuses_summary():
    client = TestClient(main.create_app())
    payload = _experience_workbook()

    first = client.post(
        "/experience-contexts",
        files={"file": ("经验A.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    second = client.post(
        "/experience-contexts",
        files={"file": ("同内容不同名.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert first.status_code == 200
    body = first.json()
    assert len(body["digest"]) == 64
    assert body["counts"]["chartcode"] == 1
    assert body["counts"]["common"] == 1
    assert body["counts"]["est_fixed_time"] == 0
    assert second.status_code == 200
    assert second.json()["experience_context_id"] == body["experience_context_id"]
    assert second.json()["digest"] == body["digest"]


def test_job_requires_valid_uploaded_common_context(monkeypatch):
    async def no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "_run_station_with_llm", no_run)
    client = TestClient(main.create_app())

    missing = client.post("/jobs", json={"use_common_chart": True})
    unknown = client.post(
        "/jobs",
        json={
            "use_common_chart": True,
            "experience_context_id": "missing",
        },
    )

    assert missing.status_code == 422
    assert unknown.status_code == 404

    upload = client.post(
        "/experience-contexts",
        files={"file": ("无Common.xlsx", _experience_workbook(include_common=False))},
    )
    assert upload.status_code == 200
    no_common = client.post(
        "/jobs",
        json={
            "use_common_chart": True,
            "experience_context_id": upload.json()["experience_context_id"],
        },
    )
    assert no_common.status_code == 422


def test_job_injects_uploaded_snapshot_and_scope(monkeypatch):
    captured = {}

    async def capture_run(req, job_id, deps):
        captured["req"] = req
        captured["job_id"] = job_id
        captured["deps"] = deps

    monkeypatch.setattr(main, "_run_station_with_llm", capture_run)
    client = TestClient(main.create_app())
    upload = client.post(
        "/experience-contexts",
        files={"file": ("经验.xlsx", _experience_workbook())},
    ).json()

    response = client.post(
        "/jobs",
        json={
            "line_name": "L",
            "station_op": "S",
            "use_common_chart": True,
            "experience_context_id": upload["experience_context_id"],
        },
    )

    assert response.status_code == 200
    assert response.json()["experience_digest"] == upload["digest"]
    assert response.json()["use_semantic_experience"] is True
    deps = captured["deps"]
    assert deps.experience_index is not None
    assert len(deps.common_entries) == 1
    assert deps.use_semantic_experience is True
    assert deps.experience_scope == f"upload:{upload['digest']}"
    assert decision_cache_scope(deps).endswith("|common=1|semantic=1")


def test_job_can_disable_semantic_experience_and_isolates_cache(monkeypatch):
    captured = {}

    async def capture_run(_req, _job_id, deps):
        captured["deps"] = deps

    monkeypatch.setattr(main, "_run_station_with_llm", capture_run)
    client = TestClient(main.create_app())
    upload = client.post(
        "/experience-contexts",
        files={"file": ("经验.xlsx", _experience_workbook())},
    ).json()
    context_id = upload["experience_context_id"]

    disabled = client.post(
        "/jobs",
        json={
            "experience_context_id": context_id,
            "use_semantic_experience": False,
        },
    )

    assert disabled.status_code == 200
    assert disabled.json()["use_semantic_experience"] is False
    deps = captured["deps"]
    assert deps.use_semantic_experience is False
    assert decision_cache_scope(deps).endswith("|common=0|semantic=0")

    enabled_deps = main._get_deps(
        use_semantic_experience=True,
        experience_context=main._experience_contexts.get(context_id),
    )
    assert decision_cache_scope(enabled_deps).endswith("|common=0|semantic=1")
    assert decision_cache_scope(enabled_deps) != decision_cache_scope(deps)


def test_get_deps_injects_request_level_common_semantic_index():
    semantic_index = object()
    result = SimpleNamespace(
        digest="semantic",
        index=SimpleNamespace(available=True),
        common_entries=(),
        common_index=semantic_index,
    )
    context = main.ExperienceUploadContext(
        context_id="context",
        digest="semantic",
        source_name="experience.xlsx",
        result=result,
        created_at=1.0,
        expires_at=2.0,
    )

    deps = main._get_deps(experience_context=context)

    assert deps.common_index is semantic_index
    assert deps.use_semantic_experience is True


def test_upload_file_switch_produces_isolated_contexts():
    client = TestClient(main.create_app())
    first = client.post(
        "/experience-contexts",
        files={"file": ("A.xlsx", _experience_workbook(operation="转身"))},
    ).json()
    second = client.post(
        "/experience-contexts",
        files={"file": ("B.xlsx", _experience_workbook(operation="弯腰"))},
    ).json()

    assert first["digest"] != second["digest"]
    assert first["experience_context_id"] != second["experience_context_id"]
    first_deps = main._get_deps(
        use_common_chart=True,
        experience_context=main._experience_contexts.get(first["experience_context_id"]),
    )
    second_deps = main._get_deps(
        use_common_chart=True,
        experience_context=main._experience_contexts.get(second["experience_context_id"]),
    )
    assert decision_cache_scope(first_deps) != decision_cache_scope(second_deps)


def test_upload_rejects_wrong_extension_and_corrupt_xlsx():
    client = TestClient(main.create_app())

    wrong_type = client.post(
        "/experience-contexts",
        files={"file": ("经验.xls", b"not-xlsx")},
    )
    corrupt = client.post(
        "/experience-contexts",
        files={"file": ("经验.xlsx", b"not-xlsx")},
    )

    assert wrong_type.status_code == 422
    assert corrupt.status_code == 422


def test_bad_common_sheet_does_not_block_other_experience_when_common_is_off(monkeypatch):
    async def no_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "_run_station_with_llm", no_run)
    client = TestClient(main.create_app())
    upload = client.post(
        "/experience-contexts",
        files={
            "file": (
                "Common损坏.xlsx",
                _experience_workbook(malformed_common=True),
            )
        },
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["counts"]["chartcode"] == 1
    assert body["counts"]["common"] == 0
    allowed = client.post(
        "/jobs",
        json={"experience_context_id": body["experience_context_id"]},
    )
    blocked = client.post(
        "/jobs",
        json={
            "experience_context_id": body["experience_context_id"],
            "use_common_chart": True,
        },
    )
    assert allowed.status_code == 200
    assert blocked.status_code == 422


def test_experience_context_registry_expires_and_evicts(monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr(main.time, "time", lambda: now["value"])
    registry = main.ExperienceContextRegistry(max_entries=1, ttl_s=10)

    first = registry.put(SimpleNamespace(digest="a"), "A.xlsx")
    second = registry.put(SimpleNamespace(digest="b"), "B.xlsx")
    assert registry.get(first.context_id) is None
    assert registry.get(second.context_id) is not None

    now["value"] = 111.0
    assert registry.get(second.context_id) is None
