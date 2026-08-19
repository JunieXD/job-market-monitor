from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from job_market.analytics import AnalyticsRepository
from job_market.db import create_schema
from job_market.quality import DataQualityChecker
from job_market.repository import Repository
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    SourceCategoryRecord,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 17, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def next_day(self) -> None:
        self.value += timedelta(days=1)


def make_job(
    external_id: str,
    *,
    title: str,
    category_id: str,
    category_name: str,
    locations: list[LocationRecord],
) -> JobRecord:
    return JobRecord(
        source_key="bytedance_cn",
        external_id=external_id,
        external_code=external_id,
        source_url=f"https://jobs.bytedance.com/campus/position/{external_id}/detail",
        company_name="字节跳动",
        channel=Channel.CAMPUS,
        employment_type_id="202",
        employment_type_name="实习",
        title=title,
        description="职位描述",
        requirements="职位要求",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        categories=[
            SourceCategoryRecord(
                external_id=category_id,
                name=category_name,
                parent_external_id="rd",
                parent_name="研发",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=locations,
        source_payload={"id": external_id},
    )


def collection(jobs: list[JobRecord]) -> CollectionResult:
    return CollectionResult(
        channel=Channel.CAMPUS,
        jobs=jobs,
        snapshots=[],
        partition_counts={"all": len(jobs)},
        pages_fetched=1,
        complete=True,
    )


def test_daily_views_use_baselines_versioned_dimensions_and_fractional_cities() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    clock = Clock()
    repository = Repository(engine, clock=clock)
    source_id = repository.ensure_source()
    city_a = LocationRecord(code="A", name="甲城")
    city_b = LocationRecord(code="B", name="乙城")
    backend_job = make_job(
        "job-backend",
        title="后端研发工程师",
        category_id="backend",
        category_name="后端",
        locations=[city_a, city_b],
    )
    product_job = make_job(
        "job-product",
        title="产品实习生",
        category_id="product",
        category_name="产品",
        locations=[city_a],
    )

    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, collection([backend_job, product_job]))
    clock.next_day()
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(second_run, collection([backend_job]))

    analytics = AnalyticsRepository(engine)
    company = analytics.company_trend(
        company_key="bytedance",
        channel=Channel.CAMPUS.value,
    )
    assert [row["active_posting_count"] for row in company] == [2, 1]
    assert [row["new_posting_count"] for row in company] == [0, 0]
    assert bool(company[0]["is_baseline"]) is True
    assert company[1]["first_missing_posting_count"] == 1

    coverage = analytics.coverage(snapshot_date=clock().date() - timedelta(days=1))
    assert coverage["configured_source_channel_count"] == 2
    assert coverage["standard_snapshot_count"] == 1
    assert coverage["successful_source_channel_count"] == 1
    assert coverage["absence_authoritative_source_channel_count"] == 1
    assert coverage["coverage_ratio"] == pytest.approx(0.5)

    categories = analytics.category_distribution(
        company_key="bytedance",
        snapshot_date=clock().date() - timedelta(days=1),
        channel=Channel.CAMPUS.value,
    )
    assert {row["source_category_name"] for row in categories} == {"后端", "产品"}
    assert {float(row["source_category_share"]) for row in categories} == {0.5}

    cities = analytics.city_distribution(
        company_key="bytedance",
        snapshot_date=clock().date() - timedelta(days=1),
        channel=Channel.CAMPUS.value,
    )
    by_city = {row["city_name"]: row for row in cities}
    assert by_city["甲城"]["posting_count"] == 2
    assert float(by_city["甲城"]["fractional_posting_count"]) == pytest.approx(1.5)
    assert float(by_city["甲城"]["fractional_share"]) == pytest.approx(0.75)
    assert float(by_city["乙城"]["fractional_share"]) == pytest.approx(0.25)

    assert DataQualityChecker(engine).run()["ok"] is True


def test_market_city_view_unifies_same_city_across_sources() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    clock = Clock()
    repository = Repository(engine, clock=clock)
    byte_source_id = repository.ensure_source()
    ali_source_id = repository.ensure_source(
        key="alibaba_cn",
        company_key="alibaba",
        company_name="阿里巴巴集团",
        base_url="https://campus-talent.alibaba.com",
        display_name="阿里巴巴集团统一校园招聘",
        source_type="group_campus_portal",
        scope_name="阿里巴巴集团校园招聘",
        channels={"campus": "测试覆盖"},
    )
    byte_job = make_job(
        "byte-job",
        title="字节示例岗位",
        category_id="engineering",
        category_name="研发",
        locations=[
            LocationRecord(
                code="CT_11",
                name="北京",
                country_name="中国大陆",
                state_name="北京",
            )
        ],
    )
    ali_job = JobRecord(
        source_key="alibaba_cn",
        external_id="ali-job",
        external_code=None,
        source_url="https://campus-talent.alibaba.com/campus/position/ali-job",
        company_name="阿里巴巴集团",
        channel=Channel.CAMPUS,
        employment_type_id="freshman",
        employment_type_name="应届生",
        title="阿里示例岗位",
        description="职位描述",
        requirements="职位要求",
        published_at=None,
        categories=[
            SourceCategoryRecord(
                external_id="11",
                name="技术类",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=[LocationRecord(code="city:北京", name="北京")],
        source_payload={"id": "ali-job"},
    )

    byte_run = repository.start_run(byte_source_id, Channel.CAMPUS.value)
    repository.ingest(byte_run, collection([byte_job]))
    ali_run = repository.start_run(ali_source_id, Channel.CAMPUS.value)
    repository.ingest(ali_run, collection([ali_job]))

    rows = AnalyticsRepository(engine).city_distribution(
        snapshot_date=clock().date(),
        channel=Channel.CAMPUS.value,
    )
    beijing = [row for row in rows if row["city_name"] == "北京"]
    assert len(beijing) == 1
    assert beijing[0]["posting_count"] == 2
    assert beijing[0]["covered_company_count"] == 2

    selected_rows = AnalyticsRepository(engine).city_distribution(
        company_keys=["bytedance", "alibaba"],
        snapshot_date=clock().date(),
        channel=Channel.CAMPUS.value,
    )
    assert {row["company_key"] for row in selected_rows} == {
        "bytedance",
        "alibaba",
    }
    assert [row["city_name"] for row in selected_rows] == ["北京", "北京"]
    assert DataQualityChecker(engine).run()["ok"] is True


def test_multi_category_and_unclassified_jobs_remain_distinct() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    clock = Clock()
    repository = Repository(engine, clock=clock)
    source_id = repository.ensure_source(
        key="alibaba_taotian_social",
        company_key="alibaba",
        company_name="阿里巴巴集团",
        base_url="https://talent.taotian.com",
        display_name="淘天集团社会招聘",
        source_type="business_unit_career_portal",
        scope_name="淘天集团",
        channels={"experienced": "测试覆盖"},
    )

    def taotian_job(external_id: str, categories: list[SourceCategoryRecord]) -> JobRecord:
        return JobRecord(
            source_key="alibaba_taotian_social",
            external_id=external_id,
            external_code=external_id,
            source_url=(
                "https://talent.taotian.com/off-campus/position-detail"
                f"?positionId={external_id}"
            ),
            company_name="阿里巴巴集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=f"示例岗位 {external_id}",
            description="职位描述",
            requirements="职位要求",
            locations=[LocationRecord(code="city:测试城", name="测试城")],
            categories=categories,
            source_payload={"id": external_id, "categories": None},
        )

    filter_category = CategoryAssignmentMethod.FILTER_MEMBERSHIP
    categorized = taotian_job(
        "categorized",
        [
            SourceCategoryRecord(
                external_id="130",
                name="技术类",
                assignment_method=filter_category,
            ),
            SourceCategoryRecord(
                external_id="97",
                name="产品类",
                assignment_method=filter_category,
            ),
        ],
    )
    unclassified = taotian_job("unclassified", [])
    run_id = repository.start_run(source_id, Channel.EXPERIENCED.value)
    repository.ingest(
        run_id,
        CollectionResult(
            channel=Channel.EXPERIENCED,
            jobs=[categorized, unclassified],
            snapshots=[],
            partition_counts={"all": 2},
            pages_fetched=1,
            complete=True,
        ),
    )

    rows = AnalyticsRepository(engine).category_distribution(
        company_key="alibaba",
        snapshot_date=clock().date(),
        channel=Channel.EXPERIENCED.value,
    )
    by_name = {row["source_category_name"]: row for row in rows}
    assert set(by_name) == {"技术类", "产品类", "未分类"}
    assert float(by_name["技术类"]["source_category_share"]) == 0.5
    assert float(by_name["产品类"]["source_category_share"]) == 0.5
    assert float(by_name["未分类"]["source_category_share"]) == 0.5
    assert by_name["技术类"]["category_assignment_method"] == "filter_membership"
    assert by_name["未分类"]["category_assignment_method"] is None
    assert DataQualityChecker(engine).run()["ok"] is True
