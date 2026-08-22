from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.config import Settings
from job_market.models import (
    DailySnapshot,
    DerivationProfile,
    DerivationRun,
    Job,
    JobObservation,
    JobVersion,
    JobVersionDerivation,
    LLMCallLog,
    Source,
)
from job_market.observability import log_event

PROFILE_RESOURCE_ROOT = "derivation_profiles"
DEFAULT_PROFILE = "job-profile-v1"

EvidenceField = Literal[
    "source_key",
    "external_id",
    "external_code",
    "source_url",
    "company_name",
    "channel",
    "employment_type_id",
    "employment_type_name",
    "recruitment_project_id",
    "recruitment_project_name",
    "title",
    "description",
    "requirements",
    "published_at",
    "source_updated_at",
    "source_status",
    "recruitment_count",
    "degree_code",
    "degree_name",
    "experience_min_years",
    "experience_max_years",
    "graduation_start_at",
    "graduation_end_at",
    "department_code",
    "department_name",
    "interview_location_names",
    "is_hot",
    "locations",
    "categories",
    "business_units",
]
RequirementStrength = Literal["required", "preferred", "mentioned"]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: EvidenceField
    quote: str = Field(min_length=1, max_length=2000)


class ClassifiedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    other_name: str | None = Field(default=None, min_length=1, max_length=100)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_other_name(self) -> ClassifiedValue:
        if self.key == "other" and self.other_name is None:
            raise ValueError("other_name is required when key is 'other'")
        if self.key != "other" and self.other_name is not None:
            raise ValueError("other_name is only allowed when key is 'other'")
        return self


class SeniorityValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["intern", "entry", "mid", "senior", "lead", "expert", "manager"]
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class ExperienceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_years: int | None = Field(default=None, ge=0, le=50)
    maximum_years: int | None = Field(default=None, ge=0, le=50)
    requirement: Literal["required", "preferred"]
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_range(self) -> ExperienceValue:
        if (
            self.minimum_years is not None
            and self.maximum_years is not None
            and self.minimum_years > self.maximum_years
        ):
            raise ValueError("minimum_years must not exceed maximum_years")
        return self


class EducationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_degree: Literal[
        "high_school",
        "associate",
        "bachelor",
        "master",
        "doctorate",
        "other",
    ]
    majors: list[str] = Field(max_length=10)
    requirement: Literal["required", "preferred"]
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class SkillValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_name: str = Field(min_length=1, max_length=100)
    canonical_name: str = Field(min_length=1, max_length=100)
    category: Literal[
        "programming_language",
        "framework_library",
        "database_storage",
        "cloud_platform",
        "data_ai",
        "security_technology",
        "hardware_electronics",
        "manufacturing_quality",
        "engineering_tool",
        "design_tool",
        "business_tool",
        "methodology",
        "domain_knowledge",
        "other",
    ]
    requirement: RequirementStrength
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class LanguageValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    level: str | None = Field(default=None, min_length=1, max_length=100)
    requirement: RequirementStrength
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class WorkConditionValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "travel",
        "shift",
        "on_call",
        "relocation",
        "onsite",
        "certificate",
        "portfolio",
        "physical",
        "other",
    ]
    description: str = Field(min_length=1, max_length=200)
    requirement: RequirementStrength
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class SummaryValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=200)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


RecruitmentAudienceCategory = Literal[
    "new_graduate",
    "internship",
    "experienced",
    "trainee",
    "talent_program",
    "flexible_employment",
    "general",
    "other",
]


class RecruitmentAudienceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: list[RecruitmentAudienceCategory] = Field(max_length=3)
    graduation_years: list[int] = Field(max_length=5)
    description: str | None = Field(default=None, min_length=1, max_length=160)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)


class JobProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_family: ClassifiedValue | None
    specializations: list[ClassifiedValue] = Field(max_length=5)
    domains: list[ClassifiedValue] = Field(max_length=5)
    seniority: SeniorityValue | None
    recruitment_audience: RecruitmentAudienceValue | None = None
    experience: ExperienceValue | None
    education: EducationValue | None
    skills: list[SkillValue] = Field(max_length=30)
    languages: list[LanguageValue] = Field(max_length=10)
    work_conditions: list[WorkConditionValue] = Field(max_length=10)
    responsibilities: list[SummaryValue] = Field(max_length=3)
    qualifications: list[SummaryValue] = Field(max_length=3)

    @model_validator(mode="before")
    @classmethod
    def coalesce_duplicate_values(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["specializations"] = _coalesce_items(
            data.get("specializations"),
            lambda item: str(item.get("key")),
        )
        data["domains"] = _coalesce_items(
            data.get("domains"),
            lambda item: str(item.get("key")),
        )
        data["skills"] = _coalesce_items(
            data.get("skills"),
            lambda item: str(item.get("canonical_name", "")).casefold(),
        )
        data["languages"] = _coalesce_items(
            data.get("languages"),
            lambda item: str(item.get("name", "")).casefold(),
        )
        if isinstance(data["skills"], list) and isinstance(data["languages"], list):
            language_names = {
                str(item.get("name", "")).casefold()
                for item in data["languages"]
                if isinstance(item, dict)
            }
            data["skills"] = [
                item
                for item in data["skills"]
                if not isinstance(item, dict)
                or (
                    str(item.get("source_name", "")).casefold() not in language_names
                    and str(item.get("canonical_name", "")).casefold() not in language_names
                )
            ]
        return data


def _coalesce_items(value: Any, key: Callable[[dict[str, Any]], str]) -> Any:
    if not isinstance(value, list):
        return value
    merged: dict[str, dict[str, Any]] = {}
    for raw_item in value:
        if not isinstance(raw_item, dict):
            return value
        item = copy.deepcopy(raw_item)
        item_key = key(item)
        existing = merged.get(item_key)
        if existing is None:
            merged[item_key] = item
            continue
        evidence = existing.get("evidence")
        incoming_evidence = item.get("evidence")
        if isinstance(evidence, list) and isinstance(incoming_evidence, list):
            seen = {
                (entry.get("field"), entry.get("quote"))
                for entry in evidence
                if isinstance(entry, dict)
            }
            evidence.extend(
                entry
                for entry in incoming_evidence
                if isinstance(entry, dict) and (entry.get("field"), entry.get("quote")) not in seen
            )
            existing["evidence"] = evidence[:3]
        strengths = {"mentioned": 0, "preferred": 1, "required": 2}
        if strengths.get(item.get("requirement"), -1) > strengths.get(
            existing.get("requirement"),
            -1,
        ):
            existing["requirement"] = item["requirement"]
    return list(merged.values())


@dataclass(frozen=True)
class LoadedProfile:
    id: str
    name: str
    version: str
    provider: str
    model: str
    endpoint: str
    reasoning_effort: str
    max_tokens: int
    prompt: str
    schema: dict[str, Any]
    taxonomy: dict[str, Any]
    prompt_sha256: str
    schema_sha256: str
    taxonomy_sha256: str
    config: dict[str, Any]

    def messages(self, source_input: dict[str, Any]) -> list[dict[str, str]]:
        taxonomy = json.dumps(
            self.taxonomy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        user_input = json.dumps(
            source_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return [
            {
                "role": "system",
                "content": self.prompt,
            },
            {
                "role": "system",
                "content": f"固定受控 taxonomy（本 profile 的稳定前缀）：\n{taxonomy}",
            },
            {"role": "user", "content": f"待分析的完整 source_job：\n{user_input}"},
        ]


def load_profile(
    settings: Settings,
    resource_name: str = DEFAULT_PROFILE,
) -> LoadedProfile:
    root = resources.files("job_market").joinpath(PROFILE_RESOURCE_ROOT, resource_name)
    manifest = json.loads(root.joinpath("profile.json").read_text(encoding="utf-8"))
    prompt = root.joinpath(manifest["prompt_file"]).read_text(encoding="utf-8").strip()
    schema = json.loads(root.joinpath(manifest["schema_file"]).read_text(encoding="utf-8"))
    taxonomy = json.loads(root.joinpath(manifest["taxonomy_file"]).read_text(encoding="utf-8"))
    prompt_hash = _text_hash(prompt)
    schema_hash = _json_hash(schema)
    taxonomy_hash = _json_hash(taxonomy)
    config = {
        "name": manifest["name"],
        "definition_version": manifest["version"],
        "provider": manifest["provider"],
        "model": settings.stepfun_model,
        "endpoint": settings.stepfun_base_url,
        "reasoning_effort": settings.llm_reasoning_effort,
        "temperature": 0,
        "max_tokens": settings.llm_max_tokens,
        "prompt": prompt,
        "schema": schema,
        "taxonomy": taxonomy,
        "prompt_sha256": prompt_hash,
        "schema_sha256": schema_hash,
        "taxonomy_sha256": taxonomy_hash,
    }
    profile_id = _json_hash(config)
    return LoadedProfile(
        id=profile_id,
        name=manifest["name"],
        version=f"{manifest['version']}+{profile_id[:12]}",
        provider=manifest["provider"],
        model=settings.stepfun_model,
        endpoint=settings.stepfun_base_url,
        reasoning_effort=settings.llm_reasoning_effort,
        max_tokens=settings.llm_max_tokens,
        prompt=prompt,
        schema=schema,
        taxonomy=taxonomy,
        prompt_sha256=prompt_hash,
        schema_sha256=schema_hash,
        taxonomy_sha256=taxonomy_hash,
        config=config,
    )


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_hash(canonical)


@dataclass(frozen=True)
class StepFunResult:
    request_id: str | None
    finish_reason: str
    output: JobProfileOutput
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_prompt_tokens: int | None = None


class StepFunAPIError(RuntimeError):
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


class StepFunChatClient:
    def __init__(self, settings: Settings):
        if not settings.stepfun_api_key:
            raise ValueError("STEPFUN_API_KEY is required when LLM extraction is enabled")
        self.api_key = settings.stepfun_api_key.get_secret_value()
        self.timeout_seconds = settings.llm_request_timeout_seconds

    def extract(
        self,
        profile: LoadedProfile,
        source_input: dict[str, Any],
    ) -> StepFunResult:
        request_body = {
            "model": profile.model,
            "messages": profile.messages(source_input),
            "reasoning_effort": profile.reasoning_effort,
            "temperature": 0,
            "max_tokens": profile.max_tokens,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{profile.name}_{profile.version}".replace("-", "_").replace("+", "_"),
                    "strict": True,
                    "schema": profile.schema,
                },
            },
        }
        request = urllib.request.Request(
            profile.endpoint,
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
            raise StepFunAPIError(
                f"StepFun returned HTTP {exc.code}: {_safe_api_error(body)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise StepFunAPIError(f"StepFun request failed: {exc.reason}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise StepFunAPIError(f"StepFun response failed validation: {exc}") from exc

        try:
            choice = payload["choices"][0]
            finish_reason = str(choice["finish_reason"])
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise StepFunAPIError("StepFun response did not contain a chat choice") from exc
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
            raise StepFunAPIError(
                f"StepFun generation ended with {finish_reason!r}",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            )
        if not isinstance(content, str) or not content.strip():
            raise StepFunAPIError(
                "StepFun response contained no structured content",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            )
        try:
            output = JobProfileOutput.model_validate_json(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise StepFunAPIError(
                f"StepFun structured output is invalid: {exc}",
                request_id=request_id,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
            ) from exc
        return StepFunResult(
            request_id=request_id,
            finish_reason=finish_reason,
            output=output,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
        )


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


@dataclass(frozen=True)
class DerivationCandidate:
    task_id: int
    job_version_id: int
    source_key: str
    external_id: str
    source_input: dict[str, Any]
    input_hash: str
    request_hash: str
    attempt_count: int


class JobDerivationRepository:
    def __init__(
        self,
        engine: Engine,
        clock: Callable[[], datetime] | None = None,
    ):
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Derivation clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def ensure_profile(self, profile: LoadedProfile) -> None:
        with Session(self.engine) as session, session.begin():
            existing_version = session.scalar(
                select(DerivationProfile).where(
                    DerivationProfile.name == profile.name,
                    DerivationProfile.version == profile.version,
                )
            )
            if existing_version is not None:
                if existing_version.id != profile.id:
                    raise ValueError(
                        "Derivation profile content changed without a version bump: "
                        f"{profile.name}/{profile.version}"
                    )
                return
            session.add(
                DerivationProfile(
                    id=profile.id,
                    name=profile.name,
                    version=profile.version,
                    provider=profile.provider,
                    model=profile.model,
                    endpoint=profile.endpoint,
                    reasoning_effort=profile.reasoning_effort,
                    prompt_sha256=profile.prompt_sha256,
                    schema_sha256=profile.schema_sha256,
                    taxonomy_sha256=profile.taxonomy_sha256,
                    config=profile.config,
                    is_current=False,
                    created_at=self._now(),
                    published_at=None,
                )
            )

    def preview(
        self,
        profile: LoadedProfile,
        *,
        limit: int,
        max_attempts: int,
        stale_after: timedelta,
        source_key: str | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.execute(
                self._candidate_statement(
                    profile,
                    limit=limit,
                    max_attempts=max_attempts,
                    stale_before=self._now() - stale_after,
                    source_key=source_key,
                    channel=channel,
                )
            ).all()
            return [
                {
                    "job_version_id": version.id,
                    "source_key": source.key,
                    "external_id": job.external_id,
                    "previous_status": task.status if task is not None else None,
                    "attempt_count": task.attempt_count if task is not None else 0,
                }
                for version, job, source, task in rows
            ]

    def start_run(self, profile: LoadedProfile, *, limit: int, concurrency: int) -> str:
        run_id = str(uuid.uuid4())
        with Session(self.engine) as session, session.begin():
            session.add(
                DerivationRun(
                    id=run_id,
                    derivation_profile_id=profile.id,
                    kind="llm",
                    extractor_name=profile.name,
                    extractor_version=profile.version,
                    status="running",
                    is_current=False,
                    config={
                        "profile_id": profile.id,
                        "provider": profile.provider,
                        "model": profile.model,
                        "endpoint": profile.endpoint,
                        "reasoning_effort": profile.reasoning_effort,
                        "limit": limit,
                        "concurrency": concurrency,
                    },
                    started_at=self._now(),
                )
            )
        return run_id

    def recover_stale_runs(
        self,
        profile_id: str,
        *,
        stale_after: timedelta,
    ) -> int:
        now = self._now()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(DerivationRun)
                .where(
                    DerivationRun.derivation_profile_id == profile_id,
                    DerivationRun.status == "running",
                    DerivationRun.started_at < now - stale_after,
                )
                .values(
                    status="failed",
                    finished_at=now,
                    error="Recovered stale derivation run after an interrupted worker",
                )
            )
            return int(result.rowcount or 0)

    def claim(
        self,
        run_id: str,
        profile: LoadedProfile,
        *,
        limit: int,
        max_attempts: int,
        stale_after: timedelta,
        source_key: str | None = None,
        channel: str | None = None,
    ) -> list[DerivationCandidate]:
        now = self._now()
        with Session(self.engine) as session, session.begin():
            rows = session.execute(
                self._candidate_statement(
                    profile,
                    limit=limit,
                    max_attempts=max_attempts,
                    stale_before=now - stale_after,
                    source_key=source_key,
                    channel=channel,
                ).with_for_update(skip_locked=True, of=JobVersion)
            ).all()
            candidates: list[DerivationCandidate] = []
            for version, job, source, task in rows:
                source_input = build_source_input(version.payload)
                input_hash = _json_hash(source_input)
                request_hash = _json_hash(profile.messages(source_input))
                if task is None:
                    task = JobVersionDerivation(
                        job_version_id=version.id,
                        derivation_profile_id=profile.id,
                        derivation_run_id=run_id,
                        status="running",
                        attempt_count=1,
                        input_hash=input_hash,
                        started_at=now,
                    )
                    session.add(task)
                    session.flush()
                else:
                    task.derivation_run_id = run_id
                    task.status = "running"
                    task.attempt_count += 1
                    task.input_hash = input_hash
                    task.output = None
                    task.provider_request_id = None
                    task.finish_reason = None
                    task.prompt_tokens = None
                    task.cached_prompt_tokens = None
                    task.completion_tokens = None
                    task.total_tokens = None
                    task.started_at = now
                    task.finished_at = None
                    task.error = None
                candidates.append(
                    DerivationCandidate(
                        task_id=task.id,
                        job_version_id=version.id,
                        source_key=source.key,
                        external_id=job.external_id,
                        source_input=source_input,
                        input_hash=input_hash,
                        request_hash=request_hash,
                        attempt_count=task.attempt_count,
                    )
                )
            return candidates

    def _candidate_statement(
        self,
        profile: LoadedProfile,
        *,
        limit: int,
        max_attempts: int,
        stale_before: datetime,
        source_key: str | None,
        channel: str | None,
    ):
        task_join = and_(
            JobVersionDerivation.job_version_id == JobVersion.id,
            JobVersionDerivation.derivation_profile_id == profile.id,
        )
        retryable = or_(
            JobVersionDerivation.id.is_(None),
            and_(
                JobVersionDerivation.status == "failed",
                JobVersionDerivation.attempt_count < max_attempts,
            ),
            and_(
                JobVersionDerivation.status == "running",
                JobVersionDerivation.started_at < stale_before,
                JobVersionDerivation.attempt_count < max_attempts,
            ),
        )
        statement = (
            select(JobVersion, Job, Source, JobVersionDerivation)
            .join(Job, Job.id == JobVersion.job_id)
            .join(Source, Source.id == Job.source_id)
            .outerjoin(JobVersionDerivation, task_join)
            .where(
                Job.status == "active",
                Job.content_hash == JobVersion.content_hash,
                exists(
                    select(JobObservation.id)
                    .join(
                        DailySnapshot,
                        DailySnapshot.crawl_run_id == JobObservation.crawl_run_id,
                    )
                    .where(JobObservation.job_version_id == JobVersion.id)
                ),
                retryable,
            )
            .order_by(JobVersion.observed_at.desc(), JobVersion.id.desc())
            .limit(limit)
        )
        if source_key is not None:
            statement = statement.where(Source.key == source_key)
        if channel is not None:
            statement = statement.where(Job.channel == channel)
        return statement

    def start_call(
        self,
        candidate: DerivationCandidate,
        profile: LoadedProfile,
        run_id: str,
    ) -> str:
        call_id = str(uuid.uuid4())
        with Session(self.engine) as session, session.begin():
            session.add(
                LLMCallLog(
                    id=call_id,
                    job_version_derivation_id=candidate.task_id,
                    job_version_id=candidate.job_version_id,
                    derivation_profile_id=profile.id,
                    derivation_run_id=run_id,
                    attempt_count=candidate.attempt_count,
                    provider=profile.provider,
                    model=profile.model,
                    endpoint=profile.endpoint,
                    reasoning_effort=profile.reasoning_effort,
                    input_hash=candidate.input_hash,
                    request_hash=candidate.request_hash,
                    message_count=len(profile.messages(candidate.source_input)),
                    status="running",
                    started_at=self._now(),
                )
            )
        return call_id

    def succeed(
        self,
        task_id: int,
        result: StepFunResult,
        output: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            task = session.get(JobVersionDerivation, task_id, with_for_update=True)
            if task is None or task.status != "running":
                raise ValueError(f"Derivation task is not running: {task_id}")
            task.status = "succeeded"
            task.output = output
            task.provider_request_id = result.request_id
            task.finish_reason = result.finish_reason
            task.prompt_tokens = result.prompt_tokens
            task.cached_prompt_tokens = result.cached_prompt_tokens
            task.completion_tokens = result.completion_tokens
            task.total_tokens = result.total_tokens
            task.finished_at = self._now()
            task.error = None
            if call_id is not None:
                call = session.get(LLMCallLog, call_id, with_for_update=True)
                if call is None or call.status != "running":
                    raise ValueError(f"LLM call is not running: {call_id}")
                call.status = "succeeded"
                call.output = output
                call.provider_request_id = result.request_id
                call.finish_reason = result.finish_reason
                call.prompt_tokens = result.prompt_tokens
                call.cached_prompt_tokens = result.cached_prompt_tokens
                call.completion_tokens = result.completion_tokens
                call.total_tokens = result.total_tokens
                call.finished_at = task.finished_at
                call.error = None

    def fail(
        self,
        task_id: int,
        error: str,
        *,
        api_error: StepFunAPIError | None = None,
        result: StepFunResult | None = None,
        call_id: str | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            task = session.get(JobVersionDerivation, task_id, with_for_update=True)
            if task is None or task.status != "running":
                raise ValueError(f"Derivation task is not running: {task_id}")
            task.status = "failed"
            task.output = None
            audit = api_error or result
            if audit is not None:
                task.provider_request_id = audit.request_id
                task.finish_reason = audit.finish_reason
                task.prompt_tokens = audit.prompt_tokens
                task.cached_prompt_tokens = audit.cached_prompt_tokens
                task.completion_tokens = audit.completion_tokens
                task.total_tokens = audit.total_tokens
            task.finished_at = self._now()
            task.error = error[:10000]
            if call_id is not None:
                call = session.get(LLMCallLog, call_id, with_for_update=True)
                if call is None or call.status != "running":
                    raise ValueError(f"LLM call is not running: {call_id}")
                call.status = "failed"
                if result is not None:
                    call.output = result.output.model_dump(mode="json")
                if audit is not None:
                    call.provider_request_id = audit.request_id
                    call.finish_reason = audit.finish_reason
                    call.prompt_tokens = audit.prompt_tokens
                    call.cached_prompt_tokens = audit.cached_prompt_tokens
                    call.completion_tokens = audit.completion_tokens
                    call.total_tokens = audit.total_tokens
                call.finished_at = task.finished_at
                call.error = error[:10000]

    def finish_run(self, run_id: str, *, error: str | None = None) -> None:
        with Session(self.engine) as session, session.begin():
            run = session.get(DerivationRun, run_id, with_for_update=True)
            if run is None or run.status != "running":
                raise ValueError(f"Derivation run is not running: {run_id}")
            run.status = "failed" if error else "success"
            run.finished_at = self._now()
            run.error = error[:10000] if error else None

    def profile_counts(self, profile_id: str) -> dict[str, int]:
        with Session(self.engine) as session:
            rows = session.execute(
                select(
                    JobVersionDerivation.status,
                    func.count(JobVersionDerivation.id),
                )
                .where(JobVersionDerivation.derivation_profile_id == profile_id)
                .group_by(JobVersionDerivation.status)
            ).all()
        return {status: count for status, count in rows}

    def run_usage(self, run_id: str) -> dict[str, int]:
        with Session(self.engine) as session:
            row = session.execute(
                select(
                    func.count(LLMCallLog.id),
                    func.count(LLMCallLog.id).filter(LLMCallLog.status == "failed"),
                    func.count(LLMCallLog.cached_prompt_tokens),
                    func.coalesce(func.sum(LLMCallLog.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMCallLog.cached_prompt_tokens), 0),
                    func.coalesce(func.sum(LLMCallLog.completion_tokens), 0),
                    func.coalesce(func.sum(LLMCallLog.total_tokens), 0),
                ).where(LLMCallLog.derivation_run_id == run_id)
            ).one()
        return {
            "call_count": int(row[0]),
            "failed_call_count": int(row[1]),
            "cached_prompt_observations": int(row[2]),
            "prompt_tokens": int(row[3]),
            "cached_prompt_tokens": int(row[4]),
            "completion_tokens": int(row[5]),
            "total_tokens": int(row[6]),
        }


def build_source_input(payload: dict[str, Any]) -> dict[str, Any]:
    return {"source_job": copy.deepcopy(payload)}


def validate_output(
    output: JobProfileOutput,
    source_input: dict[str, Any],
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    """Validate only the response shape and controlled taxonomy keys.

    Evidence is model-generated context, not a persistence gate. It is kept in
    the stored output exactly as returned by the provider.
    """
    data = output.model_dump(mode="json")
    taxonomy_values = {
        "job_family": set(taxonomy["job_families"]),
        "specializations": set(taxonomy["specializations"]),
        "domains": set(taxonomy["domains"]),
    }
    if data["job_family"] is not None:
        _require_taxonomy_key(
            data["job_family"]["key"],
            taxonomy_values["job_family"],
            "job_family",
        )
    for field in ("specializations", "domains"):
        for item in data[field]:
            _require_taxonomy_key(item["key"], taxonomy_values[field], field)
    return data


def _require_taxonomy_key(value: str, allowed: set[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"Unknown {field} taxonomy key: {value!r}")


async def run_job_derivations(
    repository: JobDerivationRepository,
    profile: LoadedProfile,
    settings: Settings,
    *,
    limit: int,
    source_key: str | None = None,
    channel: str | None = None,
    dry_run: bool = False,
    client_factory: Callable[[Settings], StepFunChatClient] = StepFunChatClient,
) -> dict[str, Any]:
    stale_after = timedelta(minutes=settings.llm_stale_after_minutes)
    if dry_run:
        candidates = repository.preview(
            profile,
            limit=limit,
            max_attempts=settings.llm_max_attempts,
            stale_after=stale_after,
            source_key=source_key,
            channel=channel,
        )
        return {
            "dry_run": True,
            "profile_id": profile.id,
            "profile": f"{profile.name}/{profile.version}",
            "model": profile.model,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "stored_counts": repository.profile_counts(profile.id),
        }
    if not settings.llm_enabled:
        raise ValueError("LLM extraction is disabled; set LLM_ENABLED=true to run it")
    repository.ensure_profile(profile)
    client = client_factory(settings)
    recovered_runs = repository.recover_stale_runs(
        profile.id,
        stale_after=stale_after,
    )
    run_id = repository.start_run(
        profile,
        limit=limit,
        concurrency=settings.llm_concurrency,
    )
    candidates = repository.claim(
        run_id,
        profile,
        limit=limit,
        max_attempts=settings.llm_max_attempts,
        stale_after=stale_after,
        source_key=source_key,
        channel=channel,
    )
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    results: list[tuple[str, StepFunResult | None, str | None]] = []

    async def process(candidate: DerivationCandidate) -> None:
        async with semaphore:
            result: StepFunResult | None = None
            call_id: str | None = None
            try:
                call_id = await asyncio.to_thread(
                    repository.start_call,
                    candidate,
                    profile,
                    run_id,
                )
                log_event(
                    "llm_call_started",
                    run_id=run_id,
                    call_id=call_id,
                    task_id=candidate.task_id,
                    job_version_id=candidate.job_version_id,
                    source_key=candidate.source_key,
                    external_id=candidate.external_id,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    base_url=profile.endpoint,
                    reasoning_effort=profile.reasoning_effort,
                    attempt=candidate.attempt_count,
                    input_hash=candidate.input_hash,
                    request_hash=candidate.request_hash,
                    message_count=len(profile.messages(candidate.source_input)),
                    input_content_ref="job_versions.payload",
                )
                result = await asyncio.to_thread(
                    client.extract,
                    profile,
                    candidate.source_input,
                )
                output = validate_output(
                    result.output,
                    candidate.source_input,
                    profile.taxonomy,
                )
                await asyncio.to_thread(
                    repository.succeed,
                    candidate.task_id,
                    result,
                    output,
                    call_id=call_id,
                )
                log_event(
                    "llm_call_finished",
                    run_id=run_id,
                    call_id=call_id,
                    task_id=candidate.task_id,
                    job_version_id=candidate.job_version_id,
                    source_key=candidate.source_key,
                    external_id=candidate.external_id,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    base_url=profile.endpoint,
                    status="succeeded",
                    provider_request_id=result.request_id,
                    finish_reason=result.finish_reason,
                    prompt_tokens=result.prompt_tokens,
                    cached_prompt_tokens=result.cached_prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    output_content_ref="llm_call_logs.output",
                    output_hash=_json_hash(output),
                )
                results.append(("succeeded", result, None))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                await asyncio.to_thread(
                    repository.fail,
                    candidate.task_id,
                    error,
                    api_error=exc if isinstance(exc, StepFunAPIError) else None,
                    result=result,
                    call_id=call_id,
                )
                log_event(
                    "llm_call_finished",
                    level="error",
                    run_id=run_id,
                    call_id=call_id,
                    task_id=candidate.task_id,
                    job_version_id=candidate.job_version_id,
                    source_key=candidate.source_key,
                    external_id=candidate.external_id,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    base_url=profile.endpoint,
                    status="failed",
                    provider_request_id=(
                        exc.request_id if isinstance(exc, StepFunAPIError) else None
                    ),
                    finish_reason=(
                        exc.finish_reason if isinstance(exc, StepFunAPIError) else None
                    ),
                    prompt_tokens=(
                        exc.prompt_tokens if isinstance(exc, StepFunAPIError) else None
                    ),
                    cached_prompt_tokens=(
                        exc.cached_prompt_tokens
                        if isinstance(exc, StepFunAPIError)
                        else (result.cached_prompt_tokens if result is not None else None)
                    ),
                    completion_tokens=(
                        exc.completion_tokens if isinstance(exc, StepFunAPIError) else None
                    ),
                    total_tokens=(
                        exc.total_tokens if isinstance(exc, StepFunAPIError) else None
                    ),
                    error=error,
                    output_content_ref=(
                        "llm_call_logs.output" if result is not None else None
                    ),
                )
                results.append(("failed", None, error))

    await asyncio.gather(*(process(candidate) for candidate in candidates))
    counts = Counter(status for status, _, _ in results)
    errors = [error for _, _, error in results if error]
    run_error = None
    if errors:
        run_error = f"{len(errors)} of {len(results)} derivations failed: {errors[0]}"
    repository.finish_run(run_id, error=run_error)
    usage = repository.run_usage(run_id)
    return {
        "dry_run": False,
        "run_id": run_id,
        "profile_id": profile.id,
        "profile": f"{profile.name}/{profile.version}",
        "model": profile.model,
        "concurrency": settings.llm_concurrency,
        "recovered_runs": recovered_runs,
        "claimed": len(candidates),
        "succeeded": counts["succeeded"],
        "failed": counts["failed"],
        **usage,
        "stored_counts": repository.profile_counts(profile.id),
        "error_types": dict(Counter(error.split(":", maxsplit=1)[0] for error in errors)),
    }
