"""通用 OpenAI 兼容 Chat Completions 客户端，服务于 LLM 派生流水线。

任何兼容 OpenAI Chat Completions 协议的服务（阿里云百炼/DashScope、StepFun、
OpenAI、智谱等）都通过 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 接入，切换厂商
只需要改这三个配置。

思考模式控制因厂商而异，拆成两层配置：

- ``LLM_THINKING_MODE``：语义意图（off / low / medium / high），与厂商无关；
- ``LLM_THINKING_DIALECT``：表达方式，即语义意图翻译成请求体里的哪些字段。

方言对照（OpenAI SDK 里 ``extra_body`` 传的字段等价于直接合并进请求体顶层）：

- ``enable_thinking``：阿里云 DashScope qwen 系列。off -> ``{"enable_thinking": false}``；
  low/medium/high -> ``{"enable_thinking": true}``（该协议不暴露档位）。
- ``reasoning_effort``：OpenAI o 系列 / StepFun step 系列。该协议无法完全关闭
  思考，off 退化为最低档 ``{"reasoning_effort": "low"}``，其余档位一一对应。
- ``thinking_type``：智谱 GLM / Anthropic 风格。off -> ``{"thinking": {"type": "disabled"}}``；
  其余 -> ``{"thinking": {"type": "enabled"}}``。
- ``none``：不发送任何思考参数（模型无思考能力，或厂商拒绝未知字段）。

结构化输出风格 ``LLM_STRUCTURED_OUTPUT``：

- ``json_schema``：请求体带严格 JSON Schema（默认，最强约束）；
- ``json_object``：只声明 JSON 输出，结构靠 system prompt 与本地校验兜底；
- ``none``：完全不带 response_format，结构靠 system prompt 与本地校验兜底。
"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from job_market.config import Settings

ThinkingMode = Literal["off", "low", "medium", "high"]
ThinkingDialect = Literal["enable_thinking", "reasoning_effort", "thinking_type", "none"]
StructuredOutput = Literal["json_schema", "json_object", "none"]

_THINKING_PARAMS: dict[str, dict[str, dict[str, Any]]] = {
    "enable_thinking": {
        "off": {"enable_thinking": False},
        "low": {"enable_thinking": True},
        "medium": {"enable_thinking": True},
        "high": {"enable_thinking": True},
    },
    "reasoning_effort": {
        "off": {"reasoning_effort": "low"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "high"},
    },
    "thinking_type": {
        "off": {"thinking": {"type": "disabled"}},
        "low": {"thinking": {"type": "enabled"}},
        "medium": {"thinking": {"type": "enabled"}},
        "high": {"thinking": {"type": "enabled"}},
    },
    "none": {
        "off": {},
        "low": {},
        "medium": {},
        "high": {},
    },
}


def thinking_params(dialect: str, mode: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(_THINKING_PARAMS[dialect][mode])
    except KeyError as exc:
        raise ValueError(
            f"Unsupported thinking dialect/mode combination: {dialect}/{mode}"
        ) from exc


def chat_completions_url(base_url: str) -> str:
    """接受 OpenAI 风格 base_url 或完整 chat/completions 地址，统一返回后者。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


@dataclass(frozen=True)
class ChatResult:
    request_id: str | None
    finish_reason: str
    output: BaseModel
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_prompt_tokens: int | None = None


class LLMAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        finish_reason: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cached_prompt_tokens: int | None = None,
    ):
        super().__init__(message)
        self.request_id = request_id
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.cached_prompt_tokens = cached_prompt_tokens


class OpenAICompatibleChatClient:
    """对 OpenAI 兼容 endpoint 的最小同步客户端（urllib，无 SDK 依赖）。

    `profile.endpoint` 仅作为审计记录；实际请求地址由 settings.llm_base_url
    解析得到，两者始终一致。
    """

    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required when LLM extraction is enabled")
        self.api_key = settings.llm_api_key.get_secret_value()
        self.endpoint = chat_completions_url(settings.llm_base_url)
        self.timeout_seconds = settings.llm_request_timeout_seconds

    def build_request_body(
        self,
        profile: Any,
        source_input: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": profile.model,
            "messages": profile.messages(source_input),
            "temperature": 0,
            "max_tokens": profile.max_tokens,
            "stream": False,
        }
        body.update(copy.deepcopy(profile.thinking_params))
        response_format = _response_format(profile)
        if response_format is not None:
            body["response_format"] = response_format
        return body

    def extract(
        self,
        profile: Any,
        source_input: dict[str, Any],
    ) -> ChatResult:
        request_body = self.build_request_body(profile, source_input)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "job-market-monitor/llm-derivation",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LLMAPIError(
                f"LLM API {self.endpoint} returned HTTP {exc.code}: {_safe_api_error(body)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMAPIError(f"LLM API request failed: {exc.reason}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise LLMAPIError(f"LLM API response failed validation: {exc}") from exc

        try:
            choice = payload["choices"][0]
            finish_reason = str(choice["finish_reason"])
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMAPIError("LLM API response did not contain a chat choice") from exc
        usage = payload.get("usage") or {}
        request_id = _optional_string(payload.get("id"))
        prompt_tokens = _optional_int(usage.get("prompt_tokens"))
        completion_tokens = _optional_int(usage.get("completion_tokens"))
        total_tokens = _optional_int(usage.get("total_tokens"))
        prompt_token_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_token_details, dict):
            prompt_token_details = {}
        cached_prompt_tokens = _optional_int(prompt_token_details.get("cached_tokens"))
        if cached_prompt_tokens is None:
            cached_prompt_tokens = _optional_int(usage.get("cached_tokens"))
        if cached_prompt_tokens is None:
            cached_prompt_tokens = _optional_int(usage.get("prompt_cache_hit_tokens"))
        if finish_reason != "stop":
            raise LLMAPIError(
                f"LLM generation ended with {finish_reason!r}",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            )
        if not isinstance(content, str) or not content.strip():
            raise LLMAPIError(
                "LLM response contained no structured content",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            )
        try:
            output = profile.output_model.model_validate_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMAPIError(
                f"LLM structured output is invalid: {exc}",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            ) from exc
        return ChatResult(
            request_id=request_id,
            finish_reason=finish_reason,
            output=output,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
        )


def _response_format(profile: Any) -> dict[str, Any] | None:
    style = getattr(profile, "structured_output", "json_schema")
    if style == "json_schema":
        schema_name = f"{profile.name}_{profile.version}".replace("-", "_").replace("+", "_")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": profile.schema,
            },
        }
    if style == "json_object":
        return {"type": "json_object"}
    return None


def _safe_api_error(body: str) -> str:
    try:
        payload = json.loads(body)
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message[:1000]
    except json.JSONDecodeError:
        pass
    return body[:1000]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
