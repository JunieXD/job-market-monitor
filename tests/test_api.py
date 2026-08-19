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

        sources = client.get("/api/v1/meta/sources")
        assert sources.status_code == 200
        assert {row["channel"] for row in sources.json()["data"]} == {
            "campus",
            "experienced",
        }
