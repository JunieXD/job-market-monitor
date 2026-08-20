from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

import job_market.db
from job_market.db import check_schema, create_schema
from job_market.models import (
    Base,
    CanonicalLocation,
    Company,
    CrawlRun,
    DailySnapshot,
    Job,
    JobLifecycleEvent,
    JobObservation,
    JobVersion,
    JobVersionLocation,
    JobVersionSourceCategory,
    Location,
    Source,
    SourceCategory,
    SourceChannel,
    SourceLocationMapping,
    Topic,
)


def migration_config(connection: sa.Connection) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(job_market.db.__file__).with_name("migrations")),
    )
    config.attributes["connection"] = connection
    return config


def test_fresh_migrations_match_models_and_create_analysis_views() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        drift = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert revision == "0016"
    assert drift == []
    columns = {item["name"]: item for item in inspect(engine).get_columns("jobs")}
    run_columns = {
        item["name"]: item for item in inspect(engine).get_columns("crawl_runs")
    }
    assert run_columns["absence_authoritative"]["nullable"] is False
    assert run_columns["issues"]["nullable"] is False
    assert columns["external_code"]["nullable"] is True
    assert columns["published_at"]["nullable"] is True
    assert columns["is_hot"]["nullable"] is True
    assert columns["source_updated_at"]["nullable"] is True
    assert columns["source_status"]["nullable"] is True
    assert columns["description"]["nullable"] is True
    assert columns["requirements"]["nullable"] is True
    assert columns["recruitment_count"]["nullable"] is True
    assert columns["category_id"]["nullable"] is True
    assert columns["category_name"]["nullable"] is True
    assert columns["interview_location_names"]["nullable"] is False
    assert {
        "source_channels",
        "job_version_source_categories",
        "crawl_run_field_stats",
    }.issubset(inspect(engine).get_table_names())
    assert set(inspect(engine).get_view_names()) == {
        "daily_category_stats",
        "daily_city_stats",
        "daily_company_stats",
        "daily_market_category_stats",
        "daily_market_city_stats",
    }
    assert check_schema(engine)["ok"] is True


def test_city_mapping_migration_unifies_sources_with_different_metadata() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "0006")

    observed_at = datetime(2026, 8, 18, 0, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        byte_company = Company(
            key="bytedance",
            name="字节跳动",
            created_at=observed_at,
        )
        ali_company = Company(
            key="alibaba",
            name="阿里巴巴集团",
            created_at=observed_at,
        )
        session.add_all([byte_company, ali_company])
        session.flush()
        legacy_sources = sa.Table(
            "sources",
            sa.MetaData(),
            autoload_with=session.connection(),
        )
        byte_source_id = session.execute(
            legacy_sources.insert()
            .values(
                company_id=byte_company.id,
                key="bytedance_cn",
                company_name="字节跳动",
                base_url="https://jobs.bytedance.com",
                enabled=True,
                timezone="Asia/Shanghai",
            )
            .returning(legacy_sources.c.id)
        ).scalar_one()
        ali_source_id = session.execute(
            legacy_sources.insert()
            .values(
                company_id=ali_company.id,
                key="alibaba_cn",
                company_name="阿里巴巴集团",
                base_url="https://campus-talent.alibaba.com",
                enabled=True,
                timezone="Asia/Shanghai",
            )
            .returning(legacy_sources.c.id)
        ).scalar_one()
        byte_city = Location(
            source_id=byte_source_id,
            code="CT_11",
            name="北京",
            country_name="中国大陆",
            state_name="北京",
        )
        ali_city = Location(
            source_id=ali_source_id,
            code="city:北京",
            name="北京",
        )
        session.add_all([byte_city, ali_city])
        session.flush()
        byte_canonical = CanonicalLocation(
            key="legacy-byte-city",
            level="city",
            name="北京",
            country_name="中国大陆",
            state_name="北京",
            city_name="北京",
            created_at=observed_at,
        )
        ali_canonical = CanonicalLocation(
            key="legacy-ali-city",
            level="city",
            name="北京",
            city_name="北京",
            created_at=observed_at,
        )
        session.add_all([byte_canonical, ali_canonical])
        session.flush()
        session.add_all(
            [
                SourceLocationMapping(
                    location_id=byte_city.id,
                    canonical_location_id=byte_canonical.id,
                    mapping_method="exact_source_fields",
                    mapping_version="v1",
                    is_current=True,
                    confidence=1,
                    created_at=observed_at,
                ),
                SourceLocationMapping(
                    location_id=ali_city.id,
                    canonical_location_id=ali_canonical.id,
                    mapping_method="exact_source_fields",
                    mapping_version="v1",
                    is_current=True,
                    confidence=1,
                    created_at=observed_at,
                ),
            ]
        )

    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "0007")

    with Session(engine) as session:
        current = session.execute(
            select(
                Source.key.label("source_key"),
                CanonicalLocation.key.label("canonical_key"),
                SourceLocationMapping.mapping_method,
            )
            .join(Location, Location.source_id == Source.id)
            .join(
                SourceLocationMapping,
                SourceLocationMapping.location_id == Location.id,
            )
            .join(
                CanonicalLocation,
                CanonicalLocation.id
                == SourceLocationMapping.canonical_location_id,
            )
            .where(SourceLocationMapping.is_current.is_(True))
            .order_by(Source.key)
        ).all()
        assert len(current) == 2
        assert len({row.canonical_key for row in current}) == 1
        assert {row.mapping_method for row in current} == {"normalized_city_name"}
        assert session.query(SourceLocationMapping).filter(
            SourceLocationMapping.mapping_method == "exact_source_fields",
            SourceLocationMapping.is_current.is_(False),
        ).count() == 2
        canonical = session.scalar(
            select(CanonicalLocation).where(
                CanonicalLocation.key == current[0].canonical_key
            )
        )
        assert canonical is not None
        assert canonical.country_name == "中国大陆"
        assert canonical.state_name == "北京"


def test_city_display_migration_preserves_raw_names_and_repoints_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    observed_at = datetime(2026, 8, 19, 0, tzinfo=UTC)
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "0015")

    with Session(engine) as session, session.begin():
        company = Company(key="example", name="示例公司", created_at=observed_at)
        session.add(company)
        session.flush()
        source = Source(
            company_id=company.id,
            key="example_cn",
            display_name="示例招聘官网",
            source_type="company_career_portal",
            scope_name="示例公司",
            company_name="示例公司",
            base_url="https://example.invalid",
            timezone="Asia/Shanghai",
            enabled=True,
        )
        session.add(source)
        session.flush()
        session.add(
            SourceChannel(
                source_id=source.id,
                channel="experienced",
                status="active",
                coverage_note=None,
                created_at=observed_at,
                updated_at=observed_at,
            )
        )
        canonical_beijing = CanonicalLocation(
            key="legacy-beijing",
            level="city",
            name="北京",
            city_name="北京",
            created_at=observed_at,
        )
        canonical_beijing_city = CanonicalLocation(
            key="legacy-beijing-city",
            level="city",
            name="北京市",
            city_name="北京市",
            created_at=observed_at,
        )
        session.add_all([canonical_beijing, canonical_beijing_city])
        session.flush()
        locations = [
            Location(source_id=source.id, code="BJ", name="北京"),
            Location(source_id=source.id, code="BJ-CN", name="北京市"),
        ]
        session.add_all(locations)
        session.flush()
        session.add_all(
            [
                SourceLocationMapping(
                    location_id=locations[0].id,
                    canonical_location_id=canonical_beijing.id,
                    mapping_method="exact_source_fields",
                    mapping_version="auto-city-name-v2-legacy-beijing",
                    is_current=True,
                    confidence=1,
                    created_at=observed_at,
                ),
                SourceLocationMapping(
                    location_id=locations[1].id,
                    canonical_location_id=canonical_beijing_city.id,
                    mapping_method="exact_source_fields",
                    mapping_version="auto-city-name-v2-legacy-beijing-city",
                    is_current=True,
                    confidence=1,
                    created_at=observed_at,
                ),
            ]
        )
        runs = [
            CrawlRun(
                id="legacy-run-bj",
                source_id=source.id,
                channel="experienced",
                snapshot_date=observed_at.date(),
                status="success",
                started_at=observed_at,
                finished_at=observed_at,
                discovered_count=1,
                page_count=1,
                partition_counts={"all": 1},
                complete=True,
                absence_authoritative=True,
                issues=[],
            ),
            CrawlRun(
                id="legacy-run-bj-city",
                source_id=source.id,
                channel="experienced",
                snapshot_date=observed_at.date(),
                status="success",
                started_at=observed_at,
                finished_at=observed_at,
                discovered_count=1,
                page_count=1,
                partition_counts={"all": 1},
                complete=True,
                absence_authoritative=True,
                issues=[],
            ),
        ]
        session.add_all(runs)
        session.flush()
        jobs = [
            Job(
                source_id=source.id,
                external_id="job-bj",
                source_url="https://example.invalid/jobs/job-bj",
                channel="experienced",
                employment_type_id="full_time",
                employment_type_name="正式",
                title="北京研发工程师",
                status="active",
                missing_streak=0,
                content_hash="b" * 64,
                first_seen_at=observed_at,
                first_canonical_seen_on=observed_at.date(),
                last_seen_at=observed_at,
                last_changed_at=observed_at,
            ),
            Job(
                source_id=source.id,
                external_id="job-bj-city",
                source_url="https://example.invalid/jobs/job-bj-city",
                channel="experienced",
                employment_type_id="full_time",
                employment_type_name="正式",
                title="北京市研发工程师",
                status="active",
                missing_streak=0,
                content_hash="c" * 64,
                first_seen_at=observed_at,
                first_canonical_seen_on=observed_at.date(),
                last_seen_at=observed_at,
                last_changed_at=observed_at,
            ),
        ]
        session.add_all(jobs)
        session.flush()
        versions = [
            JobVersion(
                job_id=jobs[0].id,
                crawl_run_id=runs[0].id,
                content_hash=jobs[0].content_hash,
                fact_contract_version="v3",
                payload={"locations": [{"name": "北京"}]},
                observed_at=observed_at,
            ),
            JobVersion(
                job_id=jobs[1].id,
                crawl_run_id=runs[1].id,
                content_hash=jobs[1].content_hash,
                fact_contract_version="v3",
                payload={"locations": [{"name": "北京市"}]},
                observed_at=observed_at,
            ),
        ]
        session.add_all(versions)
        session.flush()
        session.add_all(
            [
                JobObservation(
                    job_id=jobs[0].id,
                    job_version_id=versions[0].id,
                    crawl_run_id=runs[0].id,
                    observed_at=observed_at,
                ),
                JobObservation(
                    job_id=jobs[1].id,
                    job_version_id=versions[1].id,
                    crawl_run_id=runs[1].id,
                    observed_at=observed_at,
                ),
                JobVersionLocation(
                    job_version_id=versions[0].id,
                    location_id=locations[0].id,
                    canonical_location_id=canonical_beijing.id,
                    mapping_method="exact_source_fields",
                    mapping_version="auto-city-name-v2-legacy-beijing",
                    mapping_confidence=1,
                ),
                JobVersionLocation(
                    job_version_id=versions[1].id,
                    location_id=locations[1].id,
                    canonical_location_id=canonical_beijing_city.id,
                    mapping_method="exact_source_fields",
                    mapping_version="auto-city-name-v2-legacy-beijing-city",
                    mapping_confidence=1,
                ),
                DailySnapshot(
                    source_id=source.id,
                    channel="experienced",
                    snapshot_date=observed_at.date(),
                    crawl_run_id=runs[0].id,
                    is_baseline=True,
                    created_at=observed_at,
                ),
            ]
        )

    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")

    with Session(engine) as session:
        assert session.scalars(select(Location.name).order_by(Location.id)).all() == [
            "北京",
            "北京市",
        ]
        history = session.scalars(
            select(JobVersionLocation).order_by(JobVersionLocation.job_version_id)
        ).all()
        assert len({item.canonical_location_id for item in history}) == 1
        assert {item.mapping_method for item in history} == {"normalized_city_name"}
        assert len({item.mapping_version for item in history}) == 1
        assert next(iter({item.mapping_version for item in history})).startswith(
            "auto-city-name-v3-city-name-"
        )


def test_topic_experiment_is_retired_without_deleting_audit_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0004")
        metadata = sa.MetaData()
        runs = sa.Table("derivation_runs", metadata, autoload_with=connection)
        connection.execute(
            runs.insert().values(
                id="experimental-topic-run",
                kind="rule",
                extractor_name="topic-keywords",
                extractor_version="v1",
                status="success",
                is_current=True,
                config={"experimental": True},
                started_at=datetime(2026, 8, 17, 0, tzinfo=UTC),
                finished_at=datetime(2026, 8, 17, 1, tzinfo=UTC),
                error=None,
            )
        )

        command.upgrade(config, "0005")

        assert connection.execute(
            text("SELECT COUNT(*) FROM topics")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM topics WHERE active")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM derivation_runs")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM derivation_runs WHERE is_current")
        ).scalar_one() == 0
        assert not {
            "daily_topic_stats",
            "daily_market_topic_stats",
        } & set(inspect(connection).get_view_names())


def test_legacy_database_is_stamped_migrated_and_backfilled_without_data_loss() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "0001")
        metadata = sa.MetaData()
        sources = sa.Table("sources", metadata, autoload_with=connection)
        runs = sa.Table("crawl_runs", metadata, autoload_with=connection)
        jobs = sa.Table("jobs", metadata, autoload_with=connection)
        versions = sa.Table("job_versions", metadata, autoload_with=connection)
        observations = sa.Table("job_observations", metadata, autoload_with=connection)
        locations = sa.Table("locations", metadata, autoload_with=connection)
        job_locations = sa.Table("job_locations", metadata, autoload_with=connection)
        observed_at = datetime(2026, 8, 17, 0, tzinfo=UTC)
        source_id = connection.execute(
            sources.insert()
            .values(
                key="bytedance_cn",
                company_name="字节跳动",
                base_url="https://jobs.bytedance.com",
                enabled=True,
            )
            .returning(sources.c.id)
        ).scalar_one()
        run_id = "legacy-run"
        connection.execute(
            runs.insert().values(
                id=run_id,
                source_id=source_id,
                channel="campus",
                status="success",
                started_at=observed_at,
                finished_at=observed_at,
                discovered_count=1,
                page_count=1,
                partition_counts={"all": 1},
                complete=True,
            )
        )
        job_id = connection.execute(
            jobs.insert()
            .values(
                source_id=source_id,
                external_id="job-1",
                external_code="A1",
                source_url="https://jobs.bytedance.com/campus/position/job-1/detail",
                channel="campus",
                employment_type_id="202",
                employment_type_name="实习",
                recruitment_project_id=None,
                recruitment_project_name=None,
                title="Agent研发工程师",
                description="职位描述",
                requirements="职位要求",
                published_at=observed_at,
                category_id="backend",
                category_name="后端",
                category_parent_id="rd",
                category_parent_name="研发",
                is_hot=False,
                status="active",
                missing_streak=0,
                content_hash="a" * 64,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                last_changed_at=observed_at,
                closed_at=None,
            )
            .returning(jobs.c.id)
        ).scalar_one()
        payload = {
            "category_id": "backend",
            "category_name": "后端",
            "category_parent_id": "rd",
            "category_parent_name": "研发",
            "locations": [{"code": "BJ", "name": "北京"}],
            "title": "Agent研发工程师",
            "description": "职位描述",
            "requirements": "职位要求",
        }
        version_id = connection.execute(
            versions.insert()
            .values(
                job_id=job_id,
                crawl_run_id=run_id,
                content_hash="a" * 64,
                payload=payload,
                observed_at=observed_at,
            )
            .returning(versions.c.id)
        ).scalar_one()
        del version_id
        connection.execute(
            observations.insert().values(
                job_id=job_id,
                crawl_run_id=run_id,
                observed_at=observed_at,
            )
        )
        location_id = connection.execute(
            locations.insert()
            .values(
                source_id=source_id,
                code="BJ",
                name="北京",
                country_code=None,
                country_name=None,
                state_code=None,
                state_name=None,
                district_code=None,
                district_name=None,
                address=None,
            )
            .returning(locations.c.id)
        ).scalar_one()
        connection.execute(
            job_locations.insert().values(job_id=job_id, location_id=location_id)
        )
        connection.execute(text("DROP TABLE alembic_version"))

    create_schema(engine)

    with Session(engine) as session:
        assert session.query(Job).count() == 1
        assert session.query(JobVersion).count() == 1
        assert session.query(JobObservation).count() == 1
        assert session.query(JobVersionLocation).count() == 1
        assert session.query(JobVersionSourceCategory).count() == 1
        assert session.query(SourceCategory).count() == 2
        assert session.query(SourceChannel).count() == 2
        assert session.query(SourceLocationMapping).count() == 3
        assert session.query(SourceLocationMapping).filter(
            SourceLocationMapping.is_current.is_(True)
        ).count() == 1
        assert session.query(DailySnapshot).count() == 1
        assert session.query(Topic).filter(Topic.active.is_(True)).count() == 0
        observation = session.scalar(select(JobObservation))
        version = session.scalar(select(JobVersion))
        job = session.scalar(select(Job))
        snapshot = session.scalar(select(DailySnapshot))
        event = session.scalar(select(JobLifecycleEvent))
        run = session.scalar(select(CrawlRun))
        assert observation is not None and version is not None
        assert observation.job_version_id == version.id
        assert job is not None and snapshot is not None
        assert job.first_canonical_seen_on == snapshot.snapshot_date
        assert job.source_updated_at is None
        assert job.source_status is None
        assert job.interview_location_names == []
        source = session.scalar(select(Source))
        assert source is not None
        assert source.display_name == "字节跳动中国招聘官网"
        assert event is not None and event.event_type == "first_seen"
        assert run is not None and run.absence_authoritative is True
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT active_posting_count FROM daily_company_stats")
        ).scalar_one() == 1


def test_fact_contract_migration_reclassifies_v3_transition() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    observed_at = datetime(2026, 8, 18, 0, tzinfo=UTC)
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "0009")
        metadata = sa.MetaData()
        companies = sa.Table("companies", metadata, autoload_with=connection)
        sources = sa.Table("sources", metadata, autoload_with=connection)
        runs = sa.Table("crawl_runs", metadata, autoload_with=connection)
        jobs = sa.Table("jobs", metadata, autoload_with=connection)
        versions = sa.Table("job_versions", metadata, autoload_with=connection)
        events = sa.Table("job_lifecycle_events", metadata, autoload_with=connection)

        company_id = connection.execute(
            companies.insert()
            .values(key="example", name="示例公司", created_at=observed_at)
            .returning(companies.c.id)
        ).scalar_one()
        source_id = connection.execute(
            sources.insert()
            .values(
                company_id=company_id,
                key="example_source",
                display_name="示例来源",
                source_type="company_career_portal",
                scope_name="示例公司",
                company_name="示例公司",
                base_url="https://example.invalid",
                timezone="Asia/Shanghai",
                enabled=True,
            )
            .returning(sources.c.id)
        ).scalar_one()
        run_id = "v3-upgrade-run"
        connection.execute(
            runs.insert().values(
                id=run_id,
                source_id=source_id,
                channel="campus",
                snapshot_date=observed_at.date(),
                status="success",
                started_at=observed_at,
                finished_at=observed_at,
                discovered_count=1,
                page_count=1,
                partition_counts={"all": 1},
                complete=True,
            )
        )
        job_id = connection.execute(
            jobs.insert()
            .values(
                source_id=source_id,
                external_id="job-1",
                external_code="CODE-1",
                source_url="https://example.invalid/job-1",
                channel="campus",
                employment_type_id="campus",
                employment_type_name="校园招聘",
                title="示例岗位",
                description="示例描述",
                requirements="示例要求",
                interview_location_names=[],
                is_hot=False,
                status="active",
                missing_streak=0,
                content_hash="b" * 64,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                last_changed_at=datetime(2026, 8, 20, 0, tzinfo=UTC),
            )
            .returning(jobs.c.id)
        ).scalar_one()
        legacy_version_id = connection.execute(
            versions.insert()
            .values(
                job_id=job_id,
                crawl_run_id=run_id,
                source_category_id=None,
                content_hash="a" * 64,
                payload={"category_id": "engineering"},
                observed_at=observed_at,
            )
            .returning(versions.c.id)
        ).scalar_one()
        v3_version_id = connection.execute(
            versions.insert()
            .values(
                job_id=job_id,
                crawl_run_id=run_id,
                source_category_id=None,
                content_hash="b" * 64,
                payload={"categories": []},
                observed_at=observed_at,
            )
            .returning(versions.c.id)
        ).scalar_one()
        connection.execute(
            events.insert(),
            [
                {
                    "job_id": job_id,
                    "crawl_run_id": run_id,
                    "job_version_id": legacy_version_id,
                    "event_type": "first_seen",
                    "effective_at": observed_at,
                    "observed_at": observed_at,
                    "details": {},
                },
                {
                    "job_id": job_id,
                    "crawl_run_id": run_id,
                    "job_version_id": v3_version_id,
                    "event_type": "changed",
                    "effective_at": observed_at,
                    "observed_at": observed_at,
                    "details": {},
                },
            ],
        )

    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        versions = sa.Table("job_versions", sa.MetaData(), autoload_with=connection)
        events = sa.Table(
            "job_lifecycle_events",
            sa.MetaData(),
            autoload_with=connection,
        )
        assert connection.execute(
            select(versions.c.fact_contract_version).order_by(versions.c.id)
        ).scalars().all() == ["v2", "v3"]
        event = connection.execute(
            select(events).where(events.c.event_type == "enriched")
        ).mappings().one()
        assert event["event_type"] == "enriched"
        assert event["details"] == {
            "previous_fact_contract_version": "v2",
            "fact_contract_version": "v3",
        }
        stored_job = connection.execute(
            select(sa.Table("jobs", sa.MetaData(), autoload_with=connection).c.last_changed_at)
        ).scalar_one()
        assert stored_job == observed_at.replace(tzinfo=None)


def test_unknown_existing_schema_is_rejected_instead_of_stamped() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))

    with pytest.raises(RuntimeError, match="unknown database schema"):
        create_schema(engine)
