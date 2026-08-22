from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from job_market.config import Settings
from job_market.llm_derivations import (
    ChatResult,
    JobDerivationRepository,
    JobProfileOutput,
    build_source_input,
    load_profile,
    run_job_derivations,
    validate_output,
)
from job_market.models import Base, DerivationProfile, JobVersionDerivation, LLMCallLog
from job_market.repository import Repository
from job_market.schemas import Channel, CollectionResult, JobRecord


def make_job(title: str) -> JobRecord:
    return JobRecord(
        source_key="bytedance_cn",
        external_id="job-1",
        source_url="https://example.test/jobs/job-1",
        company_name="字节跳动",
        channel=Channel.EXPERIENCED,
        employment_type_id="full-time",
        employment_type_name="全职",
        title=title,
        description="负责订单系统后端服务的设计、开发和稳定性建设。",
        requirements="本科及以上学历，3年以上经验，熟练使用 Java 和 MySQL。",
        source_payload={"id": "job-1"},
    )


def collection(job: JobRecord) -> CollectionResult:
    return CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[job],
        snapshots=[],
        partition_counts={"all": 1},
        pages_fetched=1,
        complete=True,
    )


class FakeChatClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract(self, profile, source_input) -> ChatResult:
        del profile
        title = source_input["source_job"]["title"]
        self.calls.append(title)
        output = JobProfileOutput.model_validate(
            {
                "job_family": {
                    "key": "software_engineering",
                    "evidence": [{"field": "title", "quote": title}],
                },
                "specializations": [
                    {
                        "key": "backend",
                        "evidence": [
                            {
                                "field": "description",
                                "quote": "负责订单系统后端服务的设计、开发和稳定性建设。",
                            }
                        ],
                    }
                ],
                "domains": [],
                "seniority": None,
                "experience": {
                    "minimum_years": 3,
                    "maximum_years": None,
                    "requirement": "required",
                    "evidence": [
                        {
                            "field": "requirements",
                            "quote": "本科及以上学历，3年以上经验，熟练使用 Java 和 MySQL。",
                        }
                    ],
                },
                "education": {
                    "minimum_degree": "bachelor",
                    "majors": [],
                    "requirement": "required",
                    "evidence": [
                        {
                            "field": "requirements",
                            "quote": "本科及以上学历，3年以上经验，熟练使用 Java 和 MySQL。",
                        }
                    ],
                },
                "skills": [
                    {
                        "source_name": "Java",
                        "canonical_name": "Java",
                        "category": "programming_language",
                        "requirement": "required",
                        "evidence": [
                            {
                                "field": "requirements",
                                "quote": "熟练使用 Java 和 MySQL",
                            }
                        ],
                    }
                ],
                "languages": [],
                "work_conditions": [],
                "responsibilities": [],
                "qualifications": [],
            }
        )
        return ChatResult(
            request_id=f"request-{len(self.calls)}",
            finish_reason="stop",
            output=output,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cached_prompt_tokens=25,
        )


class InvalidEvidenceClient(FakeChatClient):
    def extract(self, profile, source_input) -> ChatResult:
        result = super().extract(profile, source_input)
        family = result.output.job_family
        assert family is not None
        output = result.output.model_copy(
            update={
                "job_family": family.model_copy(
                    update={
                        "evidence": [
                            family.evidence[0].model_copy(update={"quote": "不存在的标题"})
                        ]
                    }
                )
            }
        )
        return ChatResult(
            request_id=result.request_id,
            finish_reason=result.finish_reason,
            output=output,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
        )


def test_profile_uses_requested_provider_defaults() -> None:
    settings = Settings(llm_api_key="not-a-real-secret")
    profile = load_profile(settings)

    assert profile.provider == "aliyun"
    assert profile.model == "qwen3.7-flash"
    assert profile.endpoint == (
        "https://llm-pgvogg2xvi2bdy4d.cn-beijing.maas.aliyuncs.com"
        "/compatible-mode/v1/chat/completions"
    )
    assert profile.thinking_mode == "off"
    assert profile.thinking_params == {"enable_thinking": False}
    assert profile.structured_output == "json_schema"
    assert profile.max_tokens == 32768
    assert profile.version.startswith("v1+")
    assert profile.config["temperature"] == 0
    assert profile.config["prompt_sha256"] == profile.prompt_sha256
    assert profile.config["thinking_params"] == {"enable_thinking": False}
    assert len(profile.id) == 64
    assert "not-a-real-secret" not in repr(settings)


def test_messages_send_complete_job_payload_without_segmentation() -> None:
    source_input = {
        "source_job": {
            "title": "数仓工程师",
            "description": "1、设计数据模型；\n2、维护数据质量。",
            "requirements": "",
            "degree_name": "本科",
            "locations": [{"code": "beijing", "name": "北京"}],
            "categories": [{"external_id": "tech", "name": "技术"}],
        },
    }

    messages = load_profile(Settings()).messages(source_input)
    assert [message["role"] for message in messages] == ["system", "system", "user"]
    profile = load_profile(Settings())
    assert messages[0]["content"] == profile.prompt
    assert "数仓工程师" not in messages[0]["content"]
    assert "数仓工程师" not in messages[1]["content"]
    other_messages = profile.messages({"source_job": {"title": "Java 后端工程师"}})
    assert messages[:2] == other_messages[:2]
    assert messages[2] != other_messages[2]
    model_input = json.loads(messages[2]["content"].split("\n", 1)[1])

    assert model_input == source_input
    assert "evidence_catalog" not in model_input
    assert model_input["source_job"]["locations"] == [{"code": "beijing", "name": "北京"}]


def test_source_input_preserves_complete_payload_without_selection() -> None:
    payload = {
        "title": "数仓工程师",
        "description": "负责数据模型。",
        "degree_name": "本科",
        "experience_min_years": 3,
        "locations": [{"code": "beijing", "name": "北京"}],
        "categories": [{"external_id": "tech", "name": "技术"}],
        "source_url": "https://example.test/jobs/1",
    }

    source_input = build_source_input(payload)

    assert source_input == {"source_job": payload}
    assert source_input["source_job"] is not payload


def test_structured_source_field_can_supply_exact_evidence() -> None:
    source_input = {
        "source_job": {
            "title": "数仓工程师",
            "description": "负责数据模型。",
            "requirements": "",
            "categories": [{"external_id": "tech", "name": "技术"}],
        }
    }
    output = JobProfileOutput.model_validate(
        {
            "job_family": {
                "key": "software_engineering",
                "evidence": [{"field": "categories", "quote": "技术"}],
            },
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
    )

    validated = validate_output(
        output,
        source_input,
        load_profile(Settings()).taxonomy,
    )

    assert validated["job_family"]["evidence"][0]["quote"] == "技术"


def test_schema_taxonomy_enums_stay_in_sync() -> None:
    profile = load_profile(Settings())

    assert set(profile.schema["$defs"]["job_family"]["properties"]["key"]["enum"]) == set(
        profile.taxonomy["job_families"]
    )
    assert set(profile.schema["$defs"]["specialization"]["properties"]["key"]["enum"]) == set(
        profile.taxonomy["specializations"]
    )
    assert set(profile.schema["$defs"]["domain"]["properties"]["key"]["enum"]) == set(
        profile.taxonomy["domains"]
    )
    assert set(profile.schema["$defs"]["skill"]["properties"]["category"]["enum"]) == set(
        profile.taxonomy["skill_categories"]
    )
    assert set(
        profile.schema["$defs"]["recruitment_audience"]["properties"]["categories"]["items"]["enum"]
    ) == set(profile.taxonomy["recruitment_audience_categories"])


def test_taxonomy_has_chinese_labels_for_all_controlled_categories() -> None:
    profile = load_profile(Settings())
    labels = profile.taxonomy["labels_zh"]

    for category in ("job_families", "specializations", "domains", "skill_categories"):
        assert set(labels[category]) == set(profile.taxonomy[category])
        assert all(labels[category][key].strip() for key in profile.taxonomy[category])

    field_values = profile.taxonomy["field_values_zh"]
    assert set(field_values["seniority"]) == {
        "intern",
        "entry",
        "mid",
        "senior",
        "lead",
        "expert",
        "manager",
    }
    assert set(field_values["degree"]) == {
        "high_school",
        "associate",
        "bachelor",
        "master",
        "doctorate",
        "other",
    }
    assert all(value.strip() for values in field_values.values() for value in values.values())
    assert set(field_values["evidence_field"]) == set(
        profile.schema["$defs"]["evidence"]["properties"]["field"]["enum"]
    )


def test_prompt_is_general_and_delegates_specific_boundaries_to_taxonomy() -> None:
    profile = load_profile(Settings())
    system_prompt = profile.messages({"source_job": {}})[0]["content"]

    assert "taxonomy 是本任务全部预设的唯一来源" in system_prompt
    assert "按岗位的核心职责进行语义判断" in system_prompt
    assert "field_values_zh" in system_prompt
    assert "`devops_sre` 只用于" not in profile.prompt
    assert "大模型训练系统不等于数据工程" not in profile.prompt
    assert "大语言模型预训练和训练框架研发" in str(profile.taxonomy["boundaries"])
    assert "招聘面向对象" in profile.prompt
    assert "2027届校园招聘" in str(profile.taxonomy["extraction_rules_zh"]["recruitment_audience"])
    assert "不能返回minimum_years和maximum_years同时为null的对象" in str(
        profile.taxonomy["extraction_rules_zh"]["experience"]
    )


def test_other_classification_preserves_model_name() -> None:
    item = JobProfileOutput.model_validate(
        {
            "job_family": {
                "key": "other",
                "other_name": "航空运行",
                "evidence": [{"field": "title", "quote": "航空运行"}],
            },
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
    ).job_family

    assert item is not None
    assert item.other_name == "航空运行"


def test_evidence_quotes_are_preserved_without_source_validation() -> None:
    output = JobProfileOutput.model_validate(
        {
            "job_family": {
                "key": "software_engineering",
                "evidence": [{"field": "title", "quote": "not source text"}],
            },
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
    )

    validated = validate_output(output, {"source_job": {}}, load_profile(Settings()).taxonomy)
    assert validated["job_family"]["evidence"][0] == {
        "field": "title",
        "quote": "not source text",
    }


def test_evidence_quote_can_cross_punctuation_and_newlines() -> None:
    source_input = {
        "source_job": {
            "title": "数仓工程师",
            "description": "1、设计数据模型；\n2、维护数据质量。",
            "requirements": "",
        },
    }
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [],
            "domains": [],
            "seniority": None,
            "experience": None,
            "education": None,
            "skills": [],
            "languages": [],
            "work_conditions": [],
            "responsibilities": [
                {
                    "summary": "设计数据模型并维护数据质量",
                    "evidence": [
                        {
                            "field": "description",
                            "quote": "1、设计数据模型；\n2、维护数据质量。",
                        }
                    ],
                }
            ],
            "qualifications": [],
        }
    )

    validated = validate_output(
        output,
        source_input,
        load_profile(Settings()).taxonomy,
    )

    assert validated["responsibilities"][0]["evidence"][0]["quote"].count("\n") == 1


def test_skill_source_name_does_not_gate_persistence() -> None:
    source_input = {
        "source_job": {
            "title": "后端工程师",
            "description": "",
            "requirements": "熟练使用 Java。",
        },
    }
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [],
            "domains": [],
            "seniority": None,
            "experience": None,
            "education": None,
            "skills": [
                {
                    "source_name": "Python",
                    "canonical_name": "Python",
                    "category": "programming_language",
                    "requirement": "required",
                    "evidence": [{"field": "requirements", "quote": "熟练使用 Java。"}],
                }
            ],
            "languages": [],
            "work_conditions": [],
            "responsibilities": [],
            "qualifications": [],
        }
    )

    validated = validate_output(output, source_input, load_profile(Settings()).taxonomy)
    assert validated["skills"][0]["source_name"] == "Python"


def test_skill_evidence_does_not_need_to_contain_source_name() -> None:
    source_input = {
        "source_job": {
            "title": "后端工程师",
            "description": "负责系统开发。",
            "requirements": "熟练使用 Java。",
        },
    }
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [],
            "domains": [],
            "seniority": None,
            "experience": None,
            "education": None,
            "skills": [
                {
                    "source_name": "Java",
                    "canonical_name": "Java",
                    "category": "programming_language",
                    "requirement": "required",
                    "evidence": [{"field": "description", "quote": "负责系统开发。"}],
                }
            ],
            "languages": [],
            "work_conditions": [],
            "responsibilities": [],
            "qualifications": [],
        }
    )

    validated = validate_output(output, source_input, load_profile(Settings()).taxonomy)
    assert validated["skills"][0]["source_name"] == "Java"


def test_duplicate_values_are_coalesced_with_evidence() -> None:
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [
                {
                    "key": "backend",
                    "evidence": [{"field": "title", "quote": "后端"}],
                },
                {
                    "key": "backend",
                    "evidence": [{"field": "description", "quote": "后端开发"}],
                },
            ],
            "domains": [],
            "seniority": None,
            "experience": None,
            "education": None,
            "skills": [
                {
                    "source_name": "Java",
                    "canonical_name": "Java",
                    "category": "programming_language",
                    "requirement": "preferred",
                    "evidence": [{"field": "requirements", "quote": "Java"}],
                },
                {
                    "source_name": "Java",
                    "canonical_name": "java",
                    "category": "programming_language",
                    "requirement": "required",
                    "evidence": [{"field": "description", "quote": "Java"}],
                },
            ],
            "languages": [],
            "work_conditions": [],
            "responsibilities": [],
            "qualifications": [],
        }
    )

    assert len(output.specializations) == 1
    assert [(item.field, item.quote) for item in output.specializations[0].evidence] == [
        ("title", "后端"),
        ("description", "后端开发"),
    ]
    assert len(output.skills) == 1
    assert output.skills[0].requirement == "required"


def test_language_is_not_duplicated_as_a_skill() -> None:
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [],
            "domains": [],
            "seniority": None,
            "experience": None,
            "education": None,
            "skills": [
                {
                    "source_name": "英语",
                    "canonical_name": "英语",
                    "category": "other",
                    "requirement": "required",
                    "evidence": [{"field": "requirements", "quote": "英语"}],
                }
            ],
            "languages": [
                {
                    "name": "英语",
                    "level": "熟练",
                    "requirement": "required",
                    "evidence": [{"field": "requirements", "quote": "英语熟练"}],
                }
            ],
            "work_conditions": [],
            "responsibilities": [],
            "qualifications": [],
        }
    )

    assert output.skills == []
    assert [item.name for item in output.languages] == ["英语"]


def test_seniority_is_preserved_from_model_output() -> None:
    source_input = {
        "source_job": {
            "title": "产品经理",
            "description": "负责产品设计。",
            "requirements": "5年以上产品经验。",
            "categories": [{"name": "产品"}],
        },
    }
    output = JobProfileOutput.model_validate(
        {
            "job_family": None,
            "specializations": [],
            "domains": [],
            "seniority": {
                "level": "senior",
                "evidence": [{"field": "requirements", "quote": "5年以上产品经验"}],
            },
            "experience": None,
            "education": None,
            "skills": [],
            "languages": [],
            "work_conditions": [],
            "responsibilities": [
                {
                    "summary": "负责产品设计",
                    "evidence": [{"field": "description", "quote": "负责产品设计"}],
                }
            ],
            "qualifications": [],
        }
    )

    validated = validate_output(
        output,
        source_input,
        load_profile(Settings()).taxonomy,
    )

    assert validated["seniority"]["level"] == "senior"


def test_empty_model_output_is_preserved() -> None:
    source_input = {
        "source_job": {
            "title": "后端工程师",
            "description": "负责后端系统开发。",
            "requirements": "",
        },
    }
    output = JobProfileOutput.model_validate(
        {
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
    )

    validated = validate_output(output, source_input, load_profile(Settings()).taxonomy)
    assert validated["responsibilities"] == []


@pytest.mark.asyncio
async def test_unchanged_and_restored_versions_are_not_called_twice() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    crawl_repository = Repository(engine, missing_runs_before_close=2)
    source_id = crawl_repository.ensure_source()
    settings = Settings(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_concurrency=2,
    )
    profile = load_profile(settings)
    derivations = JobDerivationRepository(engine)
    client = FakeChatClient()

    def ingest(title: str) -> None:
        run_id = crawl_repository.start_run(source_id, Channel.EXPERIENCED.value)
        crawl_repository.ingest(run_id, collection(make_job(title)))

    async def derive() -> dict:
        return await run_job_derivations(
            derivations,
            profile,
            settings,
            limit=10,
            client_factory=lambda _: client,
        )

    ingest("Java 后端工程师")
    preview = await run_job_derivations(
        derivations,
        profile,
        settings,
        limit=10,
        dry_run=True,
        client_factory=lambda _: client,
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count(DerivationProfile.id))) == 0
    assert preview["candidate_count"] == 1
    assert client.calls == []

    first = await derive()
    unchanged = await derive()
    ingest("高级 Java 后端工程师")
    changed = await derive()
    ingest("Java 后端工程师")
    restored = await derive()

    assert first["succeeded"] == 1
    assert unchanged["claimed"] == 0
    assert changed["succeeded"] == 1
    assert restored["claimed"] == 0
    assert client.calls == ["Java 后端工程师", "高级 Java 后端工程师"]
    with Session(engine) as session:
        assert session.scalar(select(func.count(JobVersionDerivation.id))) == 2
        assert session.scalar(select(func.count(DerivationProfile.id))) == 1
        assert session.scalars(
            select(JobVersionDerivation.status).order_by(JobVersionDerivation.id)
        ).all() == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_partial_only_version_is_not_sent_until_authoritative() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    crawl_repository = Repository(engine, missing_runs_before_close=2)
    source_id = crawl_repository.ensure_source()
    settings = Settings(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_concurrency=2,
    )
    profile = load_profile(settings)
    derivations = JobDerivationRepository(engine)
    client = FakeChatClient()

    partial = collection(make_job("Java 后端工程师")).model_copy(
        update={"complete": False, "absence_authoritative": False}
    )
    run_id = crawl_repository.start_run(source_id, Channel.EXPERIENCED.value)
    crawl_repository.ingest(run_id, partial)
    preview = await run_job_derivations(
        derivations,
        profile,
        settings,
        limit=10,
        dry_run=True,
        client_factory=lambda _: client,
    )

    assert preview["candidate_count"] == 0
    assert client.calls == []

    authoritative_run = crawl_repository.start_run(
        source_id,
        Channel.EXPERIENCED.value,
    )
    crawl_repository.ingest(
        authoritative_run,
        collection(make_job("Java 后端工程师")),
    )
    result = await run_job_derivations(
        derivations,
        profile,
        settings,
        limit=10,
        client_factory=lambda _: client,
    )

    assert result["succeeded"] == 1
    assert client.calls == ["Java 后端工程师"]


@pytest.mark.asyncio
async def test_model_output_is_stored_without_evidence_validation_failure() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    crawl_repository = Repository(engine, missing_runs_before_close=2)
    source_id = crawl_repository.ensure_source()
    run_id = crawl_repository.start_run(source_id, Channel.EXPERIENCED.value)
    crawl_repository.ingest(run_id, collection(make_job("Java 后端工程师")))
    settings = Settings(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_concurrency=2,
    )

    summary = await run_job_derivations(
        JobDerivationRepository(engine),
        load_profile(settings),
        settings,
        limit=10,
        client_factory=lambda _: InvalidEvidenceClient(),
    )

    assert summary["succeeded"] == 1
    assert summary["call_count"] == 1
    assert summary["failed_call_count"] == 0
    assert summary["total_tokens"] == 150
    assert summary["cached_prompt_tokens"] == 25
    assert summary["cached_prompt_observations"] == 1
    with Session(engine) as session:
        task = session.scalar(select(JobVersionDerivation))
        assert task is not None
        assert task.status == "succeeded"
        assert task.provider_request_id == "request-1"
        assert task.prompt_tokens == 100
        assert task.cached_prompt_tokens == 25
        assert task.completion_tokens == 50
        assert task.total_tokens == 150
        assert task.output is not None
        call = session.scalar(select(LLMCallLog))
        assert call is not None
        assert call.status == "succeeded"
        assert call.derivation_run_id == summary["run_id"]
        assert call.derivation_profile_id == summary["profile_id"]
        assert call.provider == "aliyun"
        assert call.model == "qwen3.7-flash"
        assert call.endpoint.endswith("/compatible-mode/v1/chat/completions")
        assert call.message_count == 3
        assert call.prompt_tokens == 100
        assert call.cached_prompt_tokens == 25
        assert call.completion_tokens == 50
        assert call.total_tokens == 150
        assert call.output is not None
