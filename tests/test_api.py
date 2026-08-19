from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from job_market.api import create_app
from job_market.config import Settings
from job_market.db import create_schema
from job_market.repository import Repository
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    SourceCategoryRecord,
)


def make_api_client() -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    repository = Repository(engine)
    source_id = repository.ensure_source()
    job = JobRecord(
        source_key="bytedance_cn",
        external_id="api-job-1",
        external_code="api-job-1",
        source_url="https://jobs.bytedance.com/campus/position/api-job-1/detail",
        company_name="字节跳动",
        channel=Channel.CAMPUS,
        employment_type_id="202",
        employment_type_name="实习",
        title="后端研发实习生",
        description="负责服务开发",
        requirements="熟悉 Python",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        categories=[
            SourceCategoryRecord(
                external_id="backend",
                name="后端",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=[LocationRecord(code="BJ", name="北京")],
        source_payload={"id": "api-job-1"},
    )
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(
        run_id,
        CollectionResult(
            channel=Channel.CAMPUS,
            jobs=[job],
            snapshots=[],
            partition_counts={"all": 1},
            pages_fetched=1,
            complete=True,
        ),
    )
    return TestClient(create_app(engine=engine, settings=Settings()))


def test_api_exposes_health_overview_and_read_only_job_queries() -> None:
    with make_api_client() as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        overview = client.get("/api/v1/overview")
        assert overview.status_code == 200
        overview_body = overview.json()
        assert overview_body["meta"]["metric_definition"] == (
            "daily_company_source_breakdown"
        )
        assert overview_body["meta"]["coverage"]["standard_snapshot_count"] == 1
        assert overview_body["data"][0]["active_posting_count"] == 1

        jobs = client.get("/api/v1/jobs", params={"query": "后端"})
        assert jobs.status_code == 200
        assert jobs.json()["meta"]["pagination"]["total"] == 1
        assert jobs.json()["data"][0]["external_id"] == "api-job-1"

        fuzzy_jobs = client.get("/api/v1/jobs", params={"query": "后实"})
        assert fuzzy_jobs.json()["meta"]["pagination"]["total"] == 1

        description_jobs = client.get(
            "/api/v1/jobs",
            params={"query": "服开", "query_field": "description"},
        )
        assert description_jobs.json()["meta"]["pagination"]["total"] == 1

        wrong_field = client.get(
            "/api/v1/jobs",
            params={"query": "后端", "query_field": "description"},
        )
        assert wrong_field.json()["meta"]["pagination"]["total"] == 0

        multiple_fields = client.get(
            "/api/v1/jobs",
            params=[
                ("query", "Python"),
                ("query_fields", "title"),
                ("query_fields", "requirements"),
            ],
        )
        assert multiple_fields.status_code == 200
        assert multiple_fields.json()["meta"]["pagination"]["total"] == 1
        assert multiple_fields.json()["meta"]["filters"]["query_fields"] == [
            "title",
            "requirements",
        ]

        filtered_jobs = client.get(
            "/api/v1/jobs",
            params=[
                ("company_keys", "bytedance"),
                ("channels", "campus"),
            ],
        )
        assert filtered_jobs.json()["meta"]["pagination"]["total"] == 1

        excluded_jobs = client.get(
            "/api/v1/jobs",
            params=[("channels", "experienced")],
        )
        assert excluded_jobs.json()["meta"]["pagination"]["total"] == 0

        detail = client.get("/api/v1/jobs/bytedance_cn/api-job-1")
        assert detail.status_code == 200
        assert detail.json()["requirements"] == "熟悉 Python"

        missing = client.get("/api/v1/jobs/bytedance_cn/does-not-exist")
        assert missing.status_code == 404

        collection = client.get("/api/v1/collection/status")
        assert collection.status_code == 200
        collection_body = collection.json()
        assert collection_body["summary"]["total"] == 2
        assert collection_body["summary"]["completed"] == 1
        assert collection_body["summary"]["pending"] == 1
        assert collection_body["schedule"]["frequency"] == "daily"


def test_api_returns_analysis_envelopes_for_categories_and_cities() -> None:
    with make_api_client() as client:
        categories = client.get(
            "/api/v1/distributions/categories",
            params={"company_key": "bytedance"},
        )
        assert categories.status_code == 200
        assert categories.json()["data"][0]["source_category_name"] == (
            "后端"
        )
        assert categories.json()["meta"]["coverage"]["coverage_ratio"] == 0.5

        cities = client.get(
            "/api/v1/distributions/cities",
            params={"company_key": "bytedance"},
        )
        assert cities.status_code == 200
        assert cities.json()["data"][0]["city_name"] == "北京"

        selected_cities = client.get(
            "/api/v1/distributions/cities",
            params=[
                ("company_keys", "bytedance"),
                ("company_keys", "missing-company"),
            ],
        )
        assert selected_cities.status_code == 200
        assert selected_cities.json()["data"][0]["company_key"] == "bytedance"
        assert selected_cities.json()["meta"]["filters"]["company_keys"] == [
            "bytedance",
            "missing-company",
        ]

        sources = client.get("/api/v1/meta/sources")
        assert sources.status_code == 200
        assert {row["channel"] for row in sources.json()["data"]} == {
            "campus",
            "experienced",
        }


def test_collection_status_exposes_progress_for_running_channel() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    repository = Repository(engine)
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.update_run_progress(
        run_id,
        discovered_count=37,
        page_count=4,
    )

    with TestClient(create_app(engine=engine, settings=Settings())) as client:
        response = client.get("/api/v1/collection/status")

    assert response.status_code == 200
    running = next(
        row
        for row in response.json()["channels"]
        if row["channel"] == Channel.CAMPUS.value
    )
    assert running["state"] == "running"
    assert running["discovered_count"] == 37
    assert running["page_count"] == 4
