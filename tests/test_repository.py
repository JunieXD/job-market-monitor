from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from job_market.models import (
    Base,
    CanonicalLocation,
    CrawlRun,
    CrawlRunFieldStat,
    DailySnapshot,
    Job,
    JobLifecycleEvent,
    JobObservation,
    JobVersion,
    JobVersionBusinessUnit,
    JobVersionLocation,
    JobVersionSourceCategory,
    Location,
    RawSnapshot,
    SourceBusinessUnit,
    SourceCategory,
    SourceChannel,
    SourceLocationMapping,
)
from job_market.quality import DataQualityChecker
from job_market.repository import Repository
from job_market.schemas import (
    SOURCE_FACT_CONTRACT_VERSION,
    BusinessUnitRecord,
    CategoryAssignmentMethod,
    Channel,
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
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
    *,
    title: str = "示例实习生",
    locations: list[LocationRecord] | None = None,
) -> JobRecord:
    return JobRecord(
        source_key="bytedance_cn",
        external_id="job-1",
        external_code="A1",
        source_url="https://jobs.bytedance.com/campus/position/job-1/detail",
        company_name="字节跳动",
        channel=Channel.CAMPUS,
        employment_type_id="202",
        employment_type_name="实习",
        recruitment_project_id="subject-1",
        recruitment_project_name="日常实习",
        title=title,
        description="直接来源的职位描述",
        requirements="直接来源的职位要求",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        categories=[
            SourceCategoryRecord(
                external_id="backend",
                name="后端",
                parent_external_id="rd",
                parent_name="研发",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=locations or [LocationRecord(code="CT_1", name="测试城")],
        source_payload={"id": "job-1"},
    )


def result(
    jobs: list[JobRecord],
    *,
    complete: bool = True,
    absence_authoritative: bool = True,
) -> CollectionResult:
    return CollectionResult(
        channel=Channel.CAMPUS,
        jobs=jobs,
        snapshots=[],
        partition_counts={"all": len(jobs)},
        pages_fetched=1,
        complete=complete,
        absence_authoritative=absence_authoritative,
    )


def make_repository(*, missing_runs_before_close: int = 2) -> tuple[Repository, Clock]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    clock = Clock()
    return (
        Repository(
            engine,
            missing_runs_before_close=missing_runs_before_close,
            clock=clock,
        ),
        clock,
    )


def test_running_progress_is_monotonic_and_stops_after_completion() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)

    assert repository.update_run_progress(
        run_id,
        discovered_count=12,
        page_count=3,
    )
    assert repository.update_run_progress(
        run_id,
        discovered_count=8,
        page_count=2,
    )
    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.discovered_count == 12
        assert run.page_count == 3

    repository.ingest(run_id, result([make_job()]))
    assert not repository.update_run_progress(
        run_id,
        discovered_count=99,
        page_count=99,
    )
    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.discovered_count == 1
        assert run.page_count == 1


def test_job_closes_across_daily_snapshots_and_reopens_with_history() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()

    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(first_run, result([make_job()]))
    assert stats["new"] == 1
    assert stats["canonical"] is True

    clock.next_day()
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(second_run, result([]))
    with Session(repository.engine) as session:
        job = session.scalar(select(Job))
        assert job is not None
        assert job.status == "active"
        assert job.missing_streak == 1
        missing_since = job.missing_since_at

    clock.next_day()
    third_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(third_run, result([]))
    assert stats["closed"] == 1
    with Session(repository.engine) as session:
        job = session.scalar(select(Job))
        assert job is not None
        assert job.status == "closed"
        assert job.closed_at == missing_since

    clock.next_day()
    fourth_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(fourth_run, result([make_job()]))
    assert stats["reopened"] == 1
    with Session(repository.engine) as session:
        job = session.scalar(select(Job))
        assert job is not None
        assert job.status == "active"
        assert job.closed_at is None
        assert job.missing_since_at is None
        event_types = session.scalars(
            select(JobLifecycleEvent.event_type).order_by(JobLifecycleEvent.id)
        ).all()
        assert event_types == ["first_seen", "missing", "closed", "reopened"]
        closed_event = session.scalar(
            select(JobLifecycleEvent).where(JobLifecycleEvent.event_type == "closed")
        )
        assert closed_event is not None
        assert closed_event.effective_at == missing_since
        assert session.query(JobVersion).count() == 1
        assert session.query(JobObservation).count() == 2
        category_link = session.scalar(select(JobVersionSourceCategory))
        assert category_link is not None
        assert category_link.assignment_method == "direct_field"
        field_stat = session.scalar(select(CrawlRunFieldStat))
        assert field_stat is not None
        assert field_stat.field_path == "id"
        assert field_stat.non_empty_count == 1


def test_same_day_rerun_replaces_daily_snapshot_without_advancing_lifecycle() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([make_job()]))

    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(second_run, result([]))

    assert stats["canonical"] is True
    assert stats["lifecycle_advanced"] is False
    with Session(repository.engine) as session:
        job = session.scalar(select(Job))
        snapshot = session.scalar(select(DailySnapshot))
        assert job is not None
        assert snapshot is not None
        assert job.missing_streak == 0
        assert snapshot.crawl_run_id == second_run
        assert session.query(JobLifecycleEvent).count() == 1


def test_due_sources_are_removed_after_all_channels_have_daily_snapshots() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source(channels={"campus": None})

    assert repository.due_source_keys() == {"bytedance_cn"}

    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(run_id, result([make_job()]))

    assert repository.due_source_keys() == set()


def test_due_source_channels_only_returns_missing_channels() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source(
        channels={"campus": None, "experienced": None}
    )

    assert repository.due_source_channels() == {
        "bytedance_cn": {"campus", "experienced"}
    }

    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(run_id, result([make_job()]))

    assert repository.due_source_channels() == {
        "bytedance_cn": {"experienced"}
    }


def test_job_without_source_location_is_valid_when_current_sets_match() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source(channels={"campus": None})
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)

    repository.ingest(run_id, result([make_job(locations=[])]))

    assert DataQualityChecker(repository.engine).run()["ok"] is True


def test_non_authoritative_run_stores_observations_without_advancing_absence() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([make_job()]))

    clock.next_day()
    live_list_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(
        live_list_run,
        result([], absence_authoritative=False),
    )

    assert stats["canonical"] is False
    assert stats["absence_authoritative"] is False
    with Session(repository.engine) as session:
        job = session.scalar(select(Job))
        run = session.get(CrawlRun, live_list_run)
        assert job is not None
        assert run is not None
        assert job.status == "active"
        assert job.missing_streak == 0
        assert run.status == "partial"
        assert run.complete is True
        assert run.absence_authoritative is False
        assert session.query(DailySnapshot).count() == 1
        assert session.query(JobLifecycleEvent).count() == 1


def test_partial_run_persists_observations_without_standard_snapshot() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    partial_result = result([make_job()], complete=False)
    partial_result.issues.append(
        CollectionIssue(
            scope="page",
            partition="root",
            page=2,
            error_type="SyntheticPageError",
            message="page remained unavailable after retries",
            retry_count=2,
        )
    )

    stats = repository.ingest(run_id, partial_result)

    assert stats["status"] == "partial"
    assert stats["canonical"] is False
    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        job = session.scalar(select(Job))
        assert run is not None
        assert run.status == "partial"
        assert run.complete is False
        assert run.absence_authoritative is False
        assert run.issues[0]["page"] == 2
        assert job is not None
        assert job.first_canonical_seen_on is None
        assert session.query(JobObservation).count() == 1
        assert session.query(DailySnapshot).count() == 0


def test_abandoned_running_run_is_marked_failed() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    clock.value += timedelta(hours=4)

    recovered = repository.fail_abandoned_runs(
        older_than=timedelta(hours=3),
        source_key="bytedance_cn",
    )

    assert recovered == 1
    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at.replace(tzinfo=UTC) == clock.value
        assert "abandoned" in run.error


def test_observation_points_to_exact_version_when_content_reverts() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    original = make_job(
        locations=[
            LocationRecord(code="CT_1", name="测试城"),
            LocationRecord(code="CT_2", name="第二城市"),
        ]
    )
    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([original]))

    clock.next_day()
    changed = original.model_copy(
        update={
            "title": "修改后的职位",
            "locations": [LocationRecord(code="CT_2", name="第二城市")],
        }
    )
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(second_run, result([changed]))

    clock.next_day()
    third_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(third_run, result([original]))

    with Session(repository.engine) as session:
        versions = session.scalars(
            select(JobVersion).order_by(JobVersion.id)
        ).all()
        observations = session.scalars(
            select(JobObservation).order_by(JobObservation.id)
        ).all()
        assert len(versions) == 2
        assert [item.job_version_id for item in observations] == [
            versions[0].id,
            versions[1].id,
            versions[0].id,
        ]
        version_location_counts = {
            version.id: session.query(JobVersionLocation)
            .filter(JobVersionLocation.job_version_id == version.id)
            .count()
            for version in versions
        }
        assert version_location_counts == {versions[0].id: 2, versions[1].id: 1}
        assert session.query(SourceCategory).count() == 2
        changed_events = session.scalars(
            select(JobLifecycleEvent).where(JobLifecycleEvent.event_type == "changed")
        ).all()
        assert [item.job_version_id for item in changed_events] == [
            versions[1].id,
            versions[0].id,
        ]


def test_daily_observations_reuse_content_until_description_changes() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    original = make_job()

    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([original]))

    clock.next_day()
    unchanged_run = repository.start_run(source_id, Channel.CAMPUS.value)
    unchanged_stats = repository.ingest(unchanged_run, result([original]))

    clock.next_day()
    changed = original.model_copy(update={"description": "官网修改后的职位描述"})
    changed_run = repository.start_run(source_id, Channel.CAMPUS.value)
    changed_stats = repository.ingest(changed_run, result([changed]))

    with Session(repository.engine) as session:
        versions = session.scalars(
            select(JobVersion).order_by(JobVersion.id)
        ).all()
        observations = session.scalars(
            select(JobObservation).order_by(JobObservation.id)
        ).all()
        assert unchanged_stats["changed"] == 0
        assert changed_stats["changed"] == 1
        assert len(versions) == 2
        assert len(observations) == 3
        assert [item.job_version_id for item in observations] == [
            versions[0].id,
            versions[0].id,
            versions[1].id,
        ]
        assert versions[0].payload["description"] == "直接来源的职位描述"
        assert versions[1].payload["description"] == "官网修改后的职位描述"


def test_changed_source_location_is_remapped_without_overwriting_history() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    first = make_job(locations=[LocationRecord(code="CT_1", name="旧城名")])
    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([first]))

    clock.next_day()
    renamed = first.model_copy(
        update={"locations": [LocationRecord(code="CT_1", name="新城名")]}
    )
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(second_run, result([renamed]))

    with Session(repository.engine) as session:
        mappings = session.scalars(
            select(SourceLocationMapping).order_by(SourceLocationMapping.id)
        ).all()
        assert len(mappings) == 2
        assert [item.is_current for item in mappings] == [False, True]
        assert mappings[0].canonical_location_id != mappings[1].canonical_location_id


def test_city_name_variants_share_one_canonical_location_without_overwriting_sources() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source(channels={"campus": None})
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(
        run_id,
        result(
            [
                make_job(
                    locations=[
                        LocationRecord(code="BJ", name="北京"),
                        LocationRecord(code="BJ-CN", name="北京市"),
                    ]
                )
            ]
        ),
    )

    with Session(repository.engine) as session:
        source_locations = session.scalars(
            select(Location).order_by(Location.id)
        ).all()
        mappings = session.scalars(
            select(SourceLocationMapping)
            .where(SourceLocationMapping.is_current.is_(True))
            .order_by(SourceLocationMapping.location_id)
        ).all()
        assert [item.name for item in source_locations] == ["北京", "北京市"]
        assert len({item.canonical_location_id for item in mappings}) == 1
        canonical = session.get(CanonicalLocation, mappings[0].canonical_location_id)
        assert canonical is not None
        assert canonical.name == "北京"


def test_source_business_unit_renames_keep_versioned_history() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    first = make_job(
        title="带业务单元的职位",
    ).model_copy(
        update={
            "business_units": [
                BusinessUnitRecord(code="unit-1", name="旧业务集团"),
            ]
        }
    )
    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([first]))

    clock.next_day()
    renamed = first.model_copy(
        update={
            "business_units": [
                BusinessUnitRecord(code="unit-1", name="新业务集团"),
            ]
        }
    )
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(second_run, result([renamed]))

    with Session(repository.engine) as session:
        units = session.scalars(
            select(SourceBusinessUnit).order_by(SourceBusinessUnit.id)
        ).all()
        assert [(item.external_code, item.name, item.valid_to is None) for item in units] == [
            ("unit-1", "旧业务集团", False),
            ("unit-1", "新业务集团", True),
        ]
        versions = session.scalars(select(JobVersion).order_by(JobVersion.id)).all()
        links = session.scalars(
            select(JobVersionBusinessUnit).order_by(JobVersionBusinessUnit.job_version_id)
        ).all()
        assert [item.source_business_unit_id for item in links] == [
            units[0].id,
            units[1].id,
        ]
        assert len(versions) == 2


def test_first_fact_contract_upgrade_is_enriched_not_a_market_change() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    legacy = make_job()

    first_run = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(first_run, result([legacy]))
    with Session(repository.engine) as session, session.begin():
        version = session.scalar(select(JobVersion))
        job = session.scalar(select(Job))
        assert version is not None
        assert job is not None
        version.fact_contract_version = "v2"
        first_changed_at = job.last_changed_at

    clock.next_day()
    enriched = legacy.model_copy(
        update={
            "business_units": [
                BusinessUnitRecord(code="unit-1", name="新增官网业务单元"),
            ]
        }
    )
    second_run = repository.start_run(source_id, Channel.CAMPUS.value)
    stats = repository.ingest(second_run, result([enriched]))

    assert stats["changed"] == 0
    assert stats["enriched"] == 1
    with Session(repository.engine) as session:
        versions = session.scalars(select(JobVersion).order_by(JobVersion.id)).all()
        events = session.scalars(
            select(JobLifecycleEvent.event_type).order_by(JobLifecycleEvent.id)
        ).all()
        assert [item.fact_contract_version for item in versions] == [
            "v2",
            SOURCE_FACT_CONTRACT_VERSION,
        ]
        assert events == ["first_seen", "enriched"]
        job = session.scalar(select(Job))
        assert job is not None
        assert job.last_changed_at == first_changed_at


def test_empty_incomplete_collection_is_recorded_without_standard_snapshot() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)

    stats = repository.ingest(run_id, result([], complete=False))

    assert stats["status"] == "partial"
    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.status == "partial"
        assert session.query(DailySnapshot).count() == 0


def test_source_channel_contract_disables_removed_coverage() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    repository.ensure_source(channels={"campus": "仅校园测试"})

    with pytest.raises(ValueError, match="active.*experienced"):
        repository.start_run(source_id, Channel.EXPERIENCED.value)
    with Session(repository.engine) as session:
        channels = session.scalars(
            select(SourceChannel).order_by(SourceChannel.channel)
        ).all()
        assert [(item.channel, item.status) for item in channels] == [
            ("campus", "active"),
            ("experienced", "disabled"),
        ]


def test_duplicate_jobs_and_cross_channel_records_are_rejected() -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    duplicate_run = repository.start_run(source_id, Channel.CAMPUS.value)
    with pytest.raises(ValueError, match="duplicate job id"):
        repository.ingest(duplicate_run, result([make_job(), make_job()]))

    channel_run = repository.start_run(source_id, Channel.CAMPUS.value)
    experienced = make_job().model_copy(update={"channel": Channel.EXPERIENCED})
    with pytest.raises(ValueError, match="Record channel"):
        repository.ingest(channel_run, result([experienced]))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"source_key": "alibaba_cn"}, "Record source"),
        ({"company_name": "错误公司"}, "Record company"),
    ],
)
def test_record_identity_must_match_crawl_source(updates: dict, message: str) -> None:
    repository, _ = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    mismatched = make_job().model_copy(update=updates)

    with pytest.raises(ValueError, match=message):
        repository.ingest(run_id, result([mismatched]))


def test_failed_run_keeps_snapshot_metadata() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    snapshot = RawSnapshotRecord(
        path=f"bytedance_cn/2026-08-17/{run_id}/campus/all-0000000.json.gz",
        sha256="a" * 64,
        byte_size=123,
        channel=Channel.CAMPUS,
        partition="all",
        offset=0,
        captured_at=clock(),
    )

    repository.fail_run(run_id, "test failure", [snapshot])

    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.status == "failed"
        stored = session.scalar(select(RawSnapshot))
        assert stored is not None
        assert stored.path == snapshot.path


def test_failed_run_deduplicates_snapshot_paths_during_error_recovery() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source()
    run_id = repository.start_run(source_id, Channel.EXPERIENCED.value)
    path = (
        f"bytedance_cn/2026-08-19/{run_id}/"
        "experienced/category-duplicate-0000000.json.gz"
    )
    snapshots = [
        RawSnapshotRecord(
            path=path,
            sha256=character * 64,
            byte_size=size,
            channel=Channel.EXPERIENCED,
            partition=partition,
            offset=0,
            captured_at=clock(),
        )
        for character, size, partition in (
            ("a", 100, "category-研发"),
            ("b", 200, "category-设计"),
        )
    ]

    repository.fail_run(run_id, "test failure", snapshots)

    with Session(repository.engine) as session:
        run = session.get(CrawlRun, run_id)
        stored = session.scalars(select(RawSnapshot)).all()
        assert run is not None
        assert run.status == "failed"
        assert len(stored) == 1
        assert stored[0].partition == "category-设计"
        assert stored[0].sha256 == "b" * 64
