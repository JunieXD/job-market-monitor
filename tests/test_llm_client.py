from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from job_market.config import Settings
from job_market.llm_client import (
    LLMAPIError,
    OpenAICompatibleChatClient,
    chat_completions_url,
    thinking_params,
)
from job_market.llm_derivations import load_profile

VALID_OUTPUT = {
    "job_family": None,
    "specializations": [],
    "domains": [],
    "seniority": None,
    "experience": None,
    "education": None,
    "skills": [],
    "languages": [],
    "work_conditions": [],
    "responsibilities": [],
    "qualifications": [],
}


def make_client(settings: Settings) -> OpenAICompatibleChatClient:
    return OpenAICompatibleChatClient(settings)


def test_chat_completions_url_appends_path_for_openai_style_base() -> None:
    assert chat_completions_url("https://example.test/v1") == (
        "https://example.test/v1/chat/completions"
    )
    assert chat_completions_url("https://example.test/v1/") == (
        "https://example.test/v1/chat/completions"
    )


def test_chat_completions_url_keeps_full_endpoint() -> None:
    full = "https://example.test/step_plan/v1/chat/completions"
    assert chat_completions_url(full) == full


@pytest.mark.parametrize(
    ("dialect", "mode", "expected"),
    [
        ("enable_thinking", "off", {"enable_thinking": False}),
        ("enable_thinking", "low", {"enable_thinking": True}),
        ("enable_thinking", "high", {"enable_thinking": True}),
        ("reasoning_effort", "off", {"reasoning_effort": "low"}),
        ("reasoning_effort", "low", {"reasoning_effort": "low"}),
        ("reasoning_effort", "high", {"reasoning_effort": "high"}),
        ("thinking_type", "off", {"thinking": {"type": "disabled"}}),
        ("thinking_type", "low", {"thinking": {"type": "enabled"}}),
        ("none", "off", {}),
        ("none", "high", {}),
    ],
)
def test_thinking_params_cover_provider_dialects(dialect: str, mode: str, expected: dict) -> None:
    assert thinking_params(dialect, mode) == expected


def test_thinking_params_reject_unknown_combination() -> None:
    with pytest.raises(ValueError, match="thinking dialect/mode"):
        thinking_params("bogus", "off")


def test_client_requires_api_key() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY is required"):
        make_client(Settings())


def test_request_body_merges_thinking_params_and_json_schema() -> None:
    settings = Settings(llm_api_key="test-key")
    profile = load_profile(settings)
    body = make_client(settings).build_request_body(profile, {"source_job": {}})

    assert body["model"] == "qwen3.7-flash"
    assert body["enable_thinking"] is False
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == profile.schema
    # 修改返回的思考参数不能影响 profile 的冻结配置
    body["enable_thinking"] = True
    assert profile.thinking_params == {"enable_thinking": False}


def test_request_body_follows_dialect_and_output_style_overrides() -> None:
    settings = Settings(
        llm_api_key="test-key",
        llm_base_url="https://api.stepfun.com/step_plan/v1/chat/completions",
        llm_thinking_dialect="reasoning_effort",
        llm_thinking_mode="off",
        llm_structured_output="json_object",
    )
    profile = load_profile(settings)
    body = make_client(settings).build_request_body(profile, {"source_job": {}})

    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}


def test_request_body_omits_response_format_when_disabled() -> None:
    settings = Settings(llm_api_key="test-key", llm_structured_output="none")
    profile = load_profile(settings)
    body = make_client(settings).build_request_body(profile, {"source_job": {}})

    assert "response_format" not in body


def _fake_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode("utf-8")
    return response


def test_extract_parses_usage_and_dashscope_cache_field(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_api_key="test-key")
    profile = load_profile(settings)
    payload = {
        "id": "req-1",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(VALID_OUTPUT)}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 40,
        },
    }
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> MagicMock:
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _fake_response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = make_client(settings).extract(profile, {"source_job": {"title": "后端"}})

    assert captured["url"] == (
        "https://llm-pgvogg2xvi2bdy4d.cn-beijing.maas.aliyuncs.com"
        "/compatible-mode/v1/chat/completions"
    )
    assert captured["auth"] == "Bearer test-key"
    request_body = captured["body"]
    assert isinstance(request_body, dict)
    assert request_body["enable_thinking"] is False
    assert result.request_id == "req-1"
    assert result.finish_reason == "stop"
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (
        100,
        50,
        150,
    )
    assert result.cached_prompt_tokens == 40


def test_extract_raises_api_error_with_usage_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(llm_api_key="test-key")
    profile = load_profile(settings)

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> MagicMock:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            None,
            io.BytesIO(
                json.dumps({"error": {"code": "InvalidApiKey", "message": "invalid key"}}).encode()
            ),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMAPIError, match="HTTP 401.*invalid key"):
        make_client(settings).extract(profile, {"source_job": {}})


def test_extract_rejects_non_stop_finish_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_api_key="test-key")
    profile = load_profile(settings)
    payload = {
        "choices": [{"finish_reason": "length", "message": {"content": json.dumps(VALID_OUTPUT)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: _fake_response(payload),
    )
    with pytest.raises(LLMAPIError, match="'length'") as excinfo:
        make_client(settings).extract(profile, {"source_job": {}})
    assert excinfo.value.finish_reason == "length"
    assert excinfo.value.prompt_tokens == 10


def test_extract_rejects_invalid_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_api_key="test-key")
    profile = load_profile(settings)
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"job_family": 1}'}}],
        "usage": {},
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: _fake_response(payload),
    )
    with pytest.raises(LLMAPIError, match="structured output is invalid"):
        make_client(settings).extract(profile, {"source_job": {}})
