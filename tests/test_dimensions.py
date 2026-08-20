from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from job_market.db import create_schema
from job_market.dimensions import DimensionRepository
from job_market.models import (
    CanonicalLocation,
    CategoryMapping,
    JobVersion,
    JobVersionLocation,
    JobVersionSourceCategory,
    Location,
    SourceCategory,
    SourceLocationMapping,
)
from job_market.repository import Repository
from job_market.schemas import (
    CategoryAssignmentMethod,
    CategoryMappingRecord,
    Channel,
    CollectionResult,
    JobRecord,
    LocationMappingRecord,
    LocationRecord,
    SourceCategoryRecord,
)


def test_canonical_city_display_name_is_normalized_without_changing_source_location() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    dimensions = DimensionRepository(engine)

    location_id = dimensions.add_canonical_location(
        key="cn-beijing-display",
        name="北京市",
        city_name="北京市",
    )

    with Session(engine) as session:
        location = session.get(CanonicalLocation, location_id)
        assert location is not None
        assert location.name == "北京"
        assert location.city_name == "北京"


def test_mapping_publications_are_versioned_and_only_one_is_current() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = Repository(engine)
    source_id = repository.ensure_source()
    record = JobRecord(
        source_key="bytedance_cn",
        external_id="job-1",
        external_code="job-1",
        source_url="https://jobs.bytedance.com/campus/position/job-1/detail",
        company_name="字节跳动",
        channel=Channel.CAMPUS,
        employment_type_id="202",
        employment_type_name="实习",
        title="研发实习生",
        description="描述",
        requirements="要求",
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
        locations=[LocationRecord(code="BJ", name="北京")],
        source_payload={"id": "job-1"},
    )
    run_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(
        run_id,
        CollectionResult(
            channel=Channel.CAMPUS,
            jobs=[record],
            snapshots=[],
            partition_counts={"all": 1},
            pages_fetched=1,
            complete=True,
        ),
    )

    dimensions = DimensionRepository(engine)
    dimensions.add_canonical_category(
        taxonomy_version="v1",
        key="engineering",
        name="研发",
    )
    dimensions.add_canonical_category(
        taxonomy_version="v1",
        key="backend",
        name="后端",
        parent_key="engineering",
    )
    with Session(engine) as session:
        source_category_id = session.scalar(
            select(SourceCategory.id).where(SourceCategory.external_id == "backend")
        )
        location = session.scalar(select(Location))
        current_location_mapping = session.scalar(
            select(SourceLocationMapping).where(
                SourceLocationMapping.is_current.is_(True)
            )
        )
        assert source_category_id is not None
        assert location is not None
        assert current_location_mapping is not None

    dimensions.publish_category_mappings(
        [
            CategoryMappingRecord(
                source_category_id=source_category_id,
                canonical_category_key="backend",
                taxonomy_version="v1",
                mapping_method="manual",
                mapping_version="map-v1",
                confidence=1,
            )
        ]
    )
    dimensions.publish_category_mappings(
        [
            CategoryMappingRecord(
                source_category_id=source_category_id,
                canonical_category_key="backend",
                taxonomy_version="v1",
                mapping_method="manual",
                mapping_version="map-v2",
                confidence=1,
            )
        ]
    )

    manual_location_id = dimensions.add_canonical_location(
        key="cn-beijing",
        name="北京",
        country_code="CN",
        country_name="中国",
    )
    del manual_location_id
    dimensions.publish_location_mappings(
        [
            LocationMappingRecord(
                location_id=location.id,
                canonical_location_key="cn-beijing",
                mapping_method="manual",
                mapping_version="map-v2",
                confidence=1,
            )
        ]
    )

    # A later crawl must not replace an explicitly published mapping with the
    # automatic normalized-name mapping.
    rerun_id = repository.start_run(source_id, Channel.CAMPUS.value)
    repository.ingest(
        rerun_id,
        CollectionResult(
            channel=Channel.CAMPUS,
            jobs=[record],
            snapshots=[],
            partition_counts={"all": 1},
            pages_fetched=1,
            complete=True,
        ),
    )

    with Session(engine) as session:
        category_mappings = session.scalars(
            select(CategoryMapping).order_by(CategoryMapping.mapping_version)
        ).all()
        assert [item.is_current for item in category_mappings] == [False, True]
        location_mappings = session.scalars(
            select(SourceLocationMapping).order_by(SourceLocationMapping.mapping_version)
        ).all()
        by_version = {item.mapping_version: item for item in location_mappings}
        assert by_version["map-v2"].is_current is True
        automatic = [
            item
            for item in location_mappings
            if item.mapping_method == "normalized_city_name"
        ]
        assert len(automatic) == 1
        assert automatic[0].is_current is False


def test_job_versions_keep_the_mapping_used_at_observation_time() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = Repository(engine)
    source_id = repository.ensure_source()
    record = JobRecord(
        source_key="bytedance_cn",
        external_id="mapping-history-job",
        external_code="mapping-history-job",
        source_url="https://jobs.bytedance.com/campus/position/mapping-history-job/detail",
        company_name="字节跳动",
        channel=Channel.CAMPUS,
        employment_type_id="202",
        employment_type_name="实习",
        title="研发实习生",
        description="描述",
        requirements="要求",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        categories=[
            SourceCategoryRecord(
                external_id="backend",
                name="后端",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=[LocationRecord(code="BJ", name="北京")],
        source_payload={"id": "mapping-history-job"},
    )

    def ingest(item: JobRecord) -> None:
        run_id = repository.start_run(source_id, Channel.CAMPUS.value)
        repository.ingest(
            run_id,
            CollectionResult(
                channel=Channel.CAMPUS,
                jobs=[item],
                snapshots=[],
                partition_counts={"all": 1},
                pages_fetched=1,
                complete=True,
            ),
        )

    ingest(record)
    with Session(engine) as session:
        source_category_id = session.scalar(
            select(SourceCategory.id).where(SourceCategory.external_id == "backend")
        )
        location_id = session.scalar(select(Location.id))
    assert source_category_id is not None
    assert location_id is not None

    dimensions = DimensionRepository(engine)
    dimensions.add_canonical_category(
        taxonomy_version="v1",
        key="backend-v1",
        name="后端研发",
    )
    dimensions.add_canonical_category(
        taxonomy_version="v1",
        key="backend-v2",
        name="后端平台",
    )
    dimensions.add_canonical_location(key="cn-beijing-v1", name="北京一类")
    dimensions.add_canonical_location(key="cn-beijing-v2", name="北京二类")
    dimensions.publish_category_mappings(
        [
            CategoryMappingRecord(
                source_category_id=source_category_id,
                canonical_category_key="backend-v1",
                taxonomy_version="v1",
                mapping_method="manual",
                mapping_version="category-map-v1",
                confidence=1,
            )
        ]
    )
    dimensions.publish_location_mappings(
        [
            LocationMappingRecord(
                location_id=location_id,
                canonical_location_key="cn-beijing-v1",
                mapping_method="manual",
                mapping_version="location-map-v1",
                confidence=1,
            )
        ]
    )
    ingest(record.model_copy(update={"title": "研发实习生（二）"}))

    dimensions.publish_category_mappings(
        [
            CategoryMappingRecord(
                source_category_id=source_category_id,
                canonical_category_key="backend-v2",
                taxonomy_version="v1",
                mapping_method="manual",
                mapping_version="category-map-v2",
                confidence=1,
            )
        ]
    )
    dimensions.publish_location_mappings(
        [
            LocationMappingRecord(
                location_id=location_id,
                canonical_location_key="cn-beijing-v2",
                mapping_method="manual",
                mapping_version="location-map-v2",
                confidence=1,
            )
        ]
    )
    ingest(record.model_copy(update={"title": "研发实习生（三）"}))

    with Session(engine) as session:
        versions = session.scalars(select(JobVersion).order_by(JobVersion.id)).all()
        category_snapshots = [
            session.scalar(
                select(JobVersionSourceCategory).where(
                    JobVersionSourceCategory.job_version_id == version.id
                )
            )
            for version in versions
        ]
        location_snapshots = [
            session.scalar(
                select(JobVersionLocation).where(
                    JobVersionLocation.job_version_id == version.id
                )
            )
            for version in versions
        ]

    assert [item.mapping_version for item in category_snapshots] == [
        None,
        "category-map-v1",
        "category-map-v2",
    ]
    location_versions = [item.mapping_version for item in location_snapshots]
    assert location_versions[0] is not None
    assert location_versions[0].startswith("auto-city-name-v5-")
    assert location_versions[1:] == ["location-map-v1", "location-map-v2"]
