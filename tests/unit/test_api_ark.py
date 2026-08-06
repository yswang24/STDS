"""火山引擎方舟 API 任务参数契约。"""

import pytest
from pydantic import ValidationError

from stds.api.main import JobRequest
from stds.config.settings import Settings


def test_job_request_accepts_ark_backend():
    request = JobRequest(llm_backend="ark", llm_model="ep-test")

    assert request.llm_backend == "ark"
    assert request.llm_model == "ep-test"


def test_job_request_still_rejects_unknown_backend():
    with pytest.raises(ValidationError):
        JobRequest(llm_backend="volcengine")


def test_settings_repr_does_not_expose_api_keys():
    configured = Settings(
        ARK_API_KEY="ark-sensitive-sentinel",
        ARK_EMBED_API_KEY="embed-sensitive-sentinel",
        DEEPSEEK_API_KEY="deepseek-sensitive-sentinel",
        CUSTOM_API_KEY="custom-sensitive-sentinel",
    )

    rendered = repr(configured)

    assert "sensitive-sentinel" not in rendered
