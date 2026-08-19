import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.models import (
    CanonicalLocation,
    CategoryMapping,
    Company,
    CrawlRun,
    CrawlRunFieldStat,
    DailySnapshot,
    Job,
    JobLifecycleEvent,
    JobLocation,
    JobObservation,
    JobVersion,
    JobVersionBusinessUnit,
    JobVersionLocation,
    JobVersionSourceCategory,
    Location,
    RawSnapshot,
    Source,
    SourceBusinessUnit,
    SourceCategory,
    SourceChannel,
    SourceLocationMapping,
)
from job_market.normalization import canonical_location_key
from job_market.profiling import profile_source_fields
from job_market.schemas import (
    SOURCE_FACT_CONTRACT_VERSION,
    BusinessUnitRecord,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
)

AUTO_LOCATION_MAPPING_METHODS = {"exact_source_fields", "normalized_city_name"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Repository:
    def __init__(
        self,
        engine: Engine,
        missing_runs_before_close: int = 2,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.engine = engine
        self.missing_runs_before_close = missing_runs_before_close
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Repository clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def ensure_source(
        self,
        key: str = "bytedance_cn",
        *,
        company_key: str = "bytedance",
        company_name: str = "字节跳动",
        base_url: str = "https://jobs.bytedance.com",
        display_name: str = "字节跳动中国招聘官网",
        source_type: str = "company_career_portal",
        scope_name: str = "字节跳动",
        channels: dict[str, str | None] | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> int:
        if channels is None:
            channels = {"campus": None, "experienced": None}
        now = self._now()
        with Session(self.engine) as session, session.begin():
            company = session.scalar(select(Company).where(Company.key == company_key))
            if company is None:
                company = Company(key=company_key, name=company_name, created_at=now)
                session.add(company)
                session.flush()
            elif company.name != company_name:
                company.name = company_name

            source = session.scalar(select(Source).where(Source.key == key))
            if source is None:
                source = Source(
                    company_id=company.id,
                    key=key,
                    display_name=display_name,
                    source_type=source_type,
                    scope_name=scope_name,
                    company_name=company_name,
                    base_url=base_url,
                    timezone=timezone,
                )
                session.add(source)
                session.flush()
            else:
                if source.company_id != company.id:
                    raise ValueError(
                        f"Source {key!r} already belongs to another company"
                    )
                source.display_name = display_name
                source.source_type = source_type
                source.scope_name = scope_name
                source.company_name = company_name
                source.base_url = base_url
                source.timezone = timezone
            configured_channels = set(channels)
            existing_channels = session.scalars(
                select(SourceChannel).where(SourceChannel.source_id == source.id)
            ).all()
            for source_channel in existing_channels:
                if source_channel.channel not in configured_channels:
                    source_channel.status = "disabled"
                    source_channel.updated_at = now
            for channel, coverage_note in channels.items():
                source_channel = session.get(SourceChannel, (source.id, channel))
                if source_channel is None:
                    session.add(
                        SourceChannel(
                            source_id=source.id,
                            channel=channel,
                            status="active",
                            coverage_note=coverage_note,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    source_channel.status = "active"
                    source_channel.coverage_note = coverage_note
                    source_channel.updated_at = now
            return source.id

    def start_run(self, source_id: int, channel: str) -> str:
        run_id = str(uuid.uuid4())
        now = self._now()
        with Session(self.engine) as session, session.begin():
            source = session.get(Source, source_id)
            if source is None:
                raise ValueError(f"Unknown source: {source_id}")
            if not source.enabled:
                raise ValueError(f"Source {source.key!r} is disabled")
            source_channel = session.get(SourceChannel, (source_id, channel))
            if source_channel is None or source_channel.status != "active":
                raise ValueError(
                    f"Source {source.key!r} does not have an active {channel!r} channel"
                )
            session.add(
                CrawlRun(
                    id=run_id,
                    source_id=source_id,
                    channel=channel,
                    snapshot_date=now.astimezone(ZoneInfo(source.timezone)).date(),
                    status="running",
                    started_at=now,
                )
            )
        return run_id

    def fail_abandoned_runs(
        self,
        *,
        older_than: timedelta,
        source_key: str | None = None,
    ) -> int:
        if older_than < timedelta(0):
            raise ValueError("older_than cannot be negative")
        now = self._now()
        cutoff = now - older_than
        with Session(self.engine) as session, session.begin():
            statement = (
                select(CrawlRun)
                .join(Source, Source.id == CrawlRun.source_id)
                .where(
                    CrawlRun.status == "running",
                    CrawlRun.started_at <= cutoff,
                )
            )
            if source_key is not None:
                statement = statement.where(Source.key == source_key)
            runs = session.scalars(statement.with_for_update()).all()
            for run in runs:
                run.status = "failed"
                run.finished_at = now
                run.error = (
                    "The collector process exited without finalizing this run; "
                    "the scheduler marked it abandoned."
                )
            return len(runs)

    def ingest(self, run_id: str, result: CollectionResult) -> dict[str, int | bool]:
        if not result.complete:
            raise ValueError("Refusing to ingest an incomplete collection")
        now = self._now()
        seen_ids: set[str] = set()
        new_count = 0
        changed_count = 0
        enriched_count = 0
        reopened_count = 0

        with Session(self.engine) as session, session.begin():
            run = session.get(CrawlRun, run_id)
            if run is None:
                raise ValueError(f"Unknown crawl run: {run_id}")
            if run.channel != result.channel.value:
                raise ValueError(
                    f"Run channel {run.channel!r} does not match result {result.channel.value!r}"
                )
            source_id = run.source_id
            source = session.execute(
                select(Source).where(Source.id == source_id).with_for_update()
            ).scalar_one()
            daily_snapshot = result.absence_authoritative
            advances_lifecycle = daily_snapshot and self._set_latest_daily_snapshot(
                session,
                run,
                now,
            )

            for record in result.jobs:
                if record.source_key != source.key:
                    raise ValueError(
                        f"Record source {record.source_key!r} does not match "
                        f"run source {source.key!r}"
                    )
                if record.company_name != source.company_name:
                    raise ValueError(
                        f"Record company {record.company_name!r} does not match "
                        f"run company {source.company_name!r}"
                    )
                if record.channel.value != run.channel:
                    raise ValueError(
                        f"Record channel {record.channel.value!r} does not match "
                        f"run channel {run.channel!r}"
                    )
                if record.external_id in seen_ids:
                    raise ValueError(
                        f"Collection contains duplicate job id {record.external_id!r}"
                    )
                seen_ids.add(record.external_id)
                source_categories = self._ensure_source_categories(
                    session,
                    source_id,
                    record,
                    now,
                )
                locations = self._upsert_locations(session, source_id, record.locations, now)
                business_units = self._upsert_business_units(
                    session,
                    source_id,
                    record.business_units,
                    now,
                )
                job = session.scalar(
                    select(Job).where(
                        Job.source_id == source_id,
                        Job.external_id == record.external_id,
                    )
                )
                payload = record.model_dump(mode="json")
                payload.pop("source_payload", None)
                content_hash = record.content_hash()

                if job is None:
                    job = Job(
                        source_id=source_id,
                        external_id=record.external_id,
                        first_seen_at=now,
                        first_canonical_seen_on=(
                            run.snapshot_date if daily_snapshot else None
                        ),
                        last_changed_at=now,
                        status="active",
                        missing_streak=0,
                        missing_since_at=None,
                        closed_at=None,
                        content_hash=content_hash,
                    )
                    session.add(job)
                    self._copy_job_fields(job, record)
                    job.last_seen_at = now
                    session.flush()
                    version = self._create_version(
                        session,
                        job,
                        run_id,
                        source_categories[0][0].id if source_categories else None,
                        content_hash,
                        SOURCE_FACT_CONTRACT_VERSION,
                        payload,
                        now,
                    )
                    self._add_lifecycle_event(
                        session,
                        job=job,
                        run_id=run_id,
                        version=version,
                        event_type="first_seen",
                        effective_at=now,
                        observed_at=now,
                    )
                    new_count += 1
                else:
                    previous_missing_streak = job.missing_streak
                    previous_missing_since = job.missing_since_at
                    was_closed = job.status == "closed"
                    version = session.scalar(
                        select(JobVersion).where(
                            JobVersion.job_id == job.id,
                            JobVersion.content_hash == content_hash,
                        )
                    )
                    if job.content_hash != content_hash:
                        previous_version = session.scalar(
                            select(JobVersion).where(
                                JobVersion.job_id == job.id,
                                JobVersion.content_hash == job.content_hash,
                            )
                        )
                        if previous_version is None:
                            raise RuntimeError(
                                f"Job {job.id} references content hash without a stored version"
                            )
                        is_contract_enrichment = (
                            previous_version.fact_contract_version
                            != SOURCE_FACT_CONTRACT_VERSION
                        )
                        job.content_hash = content_hash
                        if version is None:
                            version = self._create_version(
                                session,
                                job,
                                run_id,
                                source_categories[0][0].id if source_categories else None,
                                content_hash,
                                SOURCE_FACT_CONTRACT_VERSION,
                                payload,
                                now,
                            )
                        if is_contract_enrichment:
                            enriched_count += 1
                            self._add_lifecycle_event(
                                session,
                                job=job,
                                run_id=run_id,
                                version=version,
                                event_type="enriched",
                                effective_at=now,
                                observed_at=now,
                                details={
                                    "previous_fact_contract_version": (
                                        previous_version.fact_contract_version
                                    ),
                                    "fact_contract_version": SOURCE_FACT_CONTRACT_VERSION,
                                },
                            )
                        else:
                            changed_count += 1
                            job.last_changed_at = now
                            self._add_lifecycle_event(
                                session,
                                job=job,
                                run_id=run_id,
                                version=version,
                                event_type="changed",
                                effective_at=now,
                                observed_at=now,
                            )
                    elif version is None:
                        raise RuntimeError(
                            f"Job {job.id} references content hash without a stored version"
                        )

                    if daily_snapshot and was_closed:
                        reopened_count += 1
                        self._add_lifecycle_event(
                            session,
                            job=job,
                            run_id=run_id,
                            version=version,
                            event_type="reopened",
                            effective_at=now,
                            observed_at=now,
                            details={"previous_closed_at": _isoformat(job.closed_at)},
                        )
                        job.status = "active"
                        job.closed_at = None
                    elif daily_snapshot and previous_missing_streak:
                        self._add_lifecycle_event(
                            session,
                            job=job,
                            run_id=run_id,
                            version=version,
                            event_type="recovered",
                            effective_at=now,
                            observed_at=now,
                            details={
                                "missing_since_at": _isoformat(previous_missing_since),
                                "previous_missing_streak": previous_missing_streak,
                            },
                        )
                    if daily_snapshot:
                        if job.first_canonical_seen_on is None:
                            job.first_canonical_seen_on = run.snapshot_date
                        job.missing_streak = 0
                        job.missing_since_at = None

                self._copy_job_fields(job, record)
                job.last_seen_at = now
                self._sync_current_locations(session, job.id, locations)
                self._ensure_version_locations(session, version.id, locations)
                self._ensure_version_business_units(session, version.id, business_units)
                self._ensure_version_categories(session, version.id, source_categories)
                session.add(
                    JobObservation(
                        job_id=job.id,
                        job_version_id=version.id,
                        crawl_run_id=run_id,
                        observed_at=now,
                    )
                )

            closed_count = 0
            if advances_lifecycle:
                closed_count = self._reconcile_missing(
                    session,
                    source_id=source_id,
                    channel=result.channel.value,
                    seen_ids=seen_ids,
                    run_id=run_id,
                    observed_at=now,
                )

            self._add_snapshots(session, run_id, result.snapshots)
            self._add_field_stats(session, run_id, result.jobs)
            run.status = "success"
            run.finished_at = now
            run.discovered_count = len(result.jobs)
            run.page_count = result.pages_fetched
            run.partition_counts = result.partition_counts
            run.complete = result.complete
            run.absence_authoritative = result.absence_authoritative

        return {
            "discovered": len(result.jobs),
            "new": new_count,
            "changed": changed_count,
            "enriched": enriched_count,
            "reopened": reopened_count,
            "closed": closed_count,
            "canonical": daily_snapshot,
            "lifecycle_advanced": advances_lifecycle,
            "absence_authoritative": result.absence_authoritative,
        }

    def fail_run(
        self,
        run_id: str,
        error: str,
        snapshots: list[RawSnapshotRecord] | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            run = session.get(CrawlRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = self._now()
                run.error = error[:10000]
                self._add_snapshots(session, run_id, snapshots or [])

    @staticmethod
    def _set_latest_daily_snapshot(
        session: Session,
        run: CrawlRun,
        created_at: datetime,
    ) -> bool:
        existing = session.scalar(
            select(DailySnapshot).where(
                DailySnapshot.source_id == run.source_id,
                DailySnapshot.channel == run.channel,
                DailySnapshot.snapshot_date == run.snapshot_date,
            )
        )
        if existing is not None:
            existing.crawl_run_id = run.id
            existing.created_at = created_at
            return False
        is_baseline = (
            session.scalar(
                select(func.count(DailySnapshot.id)).where(
                    DailySnapshot.source_id == run.source_id,
                    DailySnapshot.channel == run.channel,
                )
            )
            == 0
        )
        session.add(
            DailySnapshot(
                source_id=run.source_id,
                channel=run.channel,
                snapshot_date=run.snapshot_date,
                crawl_run_id=run.id,
                is_baseline=is_baseline,
                created_at=created_at,
            )
        )
        return True

    def due_source_keys(self) -> set[str]:
        """Return enabled sources missing at least one standard snapshot today."""

        now = self._now()
        due: set[str] = set()
        with Session(self.engine) as session:
            sources = session.scalars(
                select(Source).where(Source.enabled.is_(True)).order_by(Source.key)
            ).all()
            for source in sources:
                snapshot_date = now.astimezone(ZoneInfo(source.timezone)).date()
                active_channels = set(
                    session.scalars(
                        select(SourceChannel.channel).where(
                            SourceChannel.source_id == source.id,
                            SourceChannel.status == "active",
                        )
                    )
                )
                completed_channels = set(
                    session.scalars(
                        select(DailySnapshot.channel).where(
                            DailySnapshot.source_id == source.id,
                            DailySnapshot.snapshot_date == snapshot_date,
                        )
                    )
                )
                if active_channels - completed_channels:
                    due.add(source.key)
        return due

    @staticmethod
    def _create_version(
        session: Session,
        job: Job,
        run_id: str,
        source_category_id: int | None,
        content_hash: str,
        fact_contract_version: str,
        payload: dict,
        observed_at: datetime,
    ) -> JobVersion:
        version = JobVersion(
            job_id=job.id,
            crawl_run_id=run_id,
            source_category_id=source_category_id,
            content_hash=content_hash,
            fact_contract_version=fact_contract_version,
            payload=payload,
            observed_at=observed_at,
        )
        session.add(version)
        session.flush()
        return version

    @staticmethod
    def _add_lifecycle_event(
        session: Session,
        *,
        job: Job,
        run_id: str,
        version: JobVersion | None,
        event_type: str,
        effective_at: datetime,
        observed_at: datetime,
        details: dict | None = None,
    ) -> None:
        session.add(
            JobLifecycleEvent(
                job_id=job.id,
                crawl_run_id=run_id,
                job_version_id=version.id if version is not None else None,
                event_type=event_type,
                effective_at=effective_at,
                observed_at=observed_at,
                details=details or {},
            )
        )

    @staticmethod
    def _add_snapshots(
        session: Session,
        run_id: str,
        snapshots: list[RawSnapshotRecord],
    ) -> None:
        for snapshot in snapshots:
            session.add(
                RawSnapshot(
                    crawl_run_id=run_id,
                    path=snapshot.path,
                    sha256=snapshot.sha256,
                    byte_size=snapshot.byte_size,
                    channel=snapshot.channel.value,
                    partition=snapshot.partition,
                    offset=snapshot.offset,
                    captured_at=snapshot.captured_at,
                )
            )

    @staticmethod
    def _copy_job_fields(job: Job, record: JobRecord) -> None:
        data = record.model_dump(mode="python", exclude={"source_payload", "source_key"})
        data.pop("locations", None)
        data.pop("business_units", None)
        data.pop("categories", None)
        data.pop("company_name", None)
        data["source_url"] = str(record.source_url)
        data["channel"] = record.channel.value
        data["published_at"] = record.published_at
        for field, value in data.items():
            if hasattr(job, field):
                setattr(job, field, value)
        primary_category = record.categories[0] if record.categories else None
        job.category_id = (
            primary_category.external_id if primary_category is not None else None
        )
        job.category_name = primary_category.name if primary_category is not None else None
        job.category_parent_id = (
            primary_category.parent_external_id
            if primary_category is not None
            else None
        )
        job.category_parent_name = (
            primary_category.parent_name if primary_category is not None else None
        )

    @staticmethod
    def _ensure_source_categories(
        session: Session,
        source_id: int,
        record: JobRecord,
        now: datetime,
    ) -> list[tuple[SourceCategory, str]]:
        categories: list[tuple[SourceCategory, str]] = []
        seen_ids: set[str] = set()
        ordered = sorted(
            record.categories,
            key=lambda item: (item.external_id, item.assignment_method.value),
        )
        for item in ordered:
            if item.external_id in seen_ids:
                raise ValueError(
                    f"Job {record.external_id} has duplicate source category "
                    f"{item.external_id!r}"
                )
            seen_ids.add(item.external_id)
            parent: SourceCategory | None = None
            if (
                item.parent_external_id
                and item.parent_external_id != item.external_id
            ):
                parent = Repository._upsert_source_category(
                    session,
                    source_id,
                    item.parent_external_id,
                    item.parent_name or item.parent_external_id,
                    None,
                    now,
                )
            category = Repository._upsert_source_category(
                session,
                source_id,
                item.external_id,
                item.name,
                parent.id if parent is not None else None,
                now,
            )
            categories.append((category, item.assignment_method.value))
        return categories

    @staticmethod
    def _ensure_version_categories(
        session: Session,
        version_id: int,
        categories: list[tuple[SourceCategory, str]],
    ) -> None:
        existing = {
            item.source_category_id: item.assignment_method
            for item in session.scalars(
                select(JobVersionSourceCategory).where(
                    JobVersionSourceCategory.job_version_id == version_id
                )
            )
        }
        expected = {category.id: method for category, method in categories}
        unexpected = set(existing) - set(expected)
        if unexpected:
            raise RuntimeError(
                f"Stored job version {version_id} has unexpected source categories"
            )
        for category_id, method in expected.items():
            stored_method = existing.get(category_id)
            if stored_method is None:
                mapping = session.scalar(
                    select(CategoryMapping).where(
                        CategoryMapping.source_category_id == category_id,
                        CategoryMapping.is_current.is_(True),
                    )
                )
                session.add(
                    JobVersionSourceCategory(
                        job_version_id=version_id,
                        source_category_id=category_id,
                        assignment_method=method,
                        canonical_category_id=(
                            None if mapping is None else mapping.canonical_category_id
                        ),
                        mapping_method=None if mapping is None else mapping.mapping_method,
                        mapping_version=None if mapping is None else mapping.mapping_version,
                        mapping_confidence=(
                            None if mapping is None else mapping.confidence
                        ),
                    )
                )
            elif stored_method != method:
                raise RuntimeError(
                    f"Stored job version {version_id} changed category assignment method"
                )

    @staticmethod
    def _add_field_stats(
        session: Session,
        run_id: str,
        jobs: list[JobRecord],
    ) -> None:
        for stat in profile_source_fields(jobs):
            session.add(
                CrawlRunFieldStat(
                    crawl_run_id=run_id,
                    field_path=stat.field_path,
                    row_count=stat.row_count,
                    present_count=stat.present_count,
                    non_null_count=stat.non_null_count,
                    non_empty_count=stat.non_empty_count,
                    type_counts=stat.type_counts,
                )
            )

    @staticmethod
    def _upsert_source_category(
        session: Session,
        source_id: int,
        external_id: str,
        name: str,
        parent_id: int | None,
        now: datetime,
    ) -> SourceCategory:
        category = session.scalar(
            select(SourceCategory).where(
                SourceCategory.source_id == source_id,
                SourceCategory.external_id == external_id,
            )
        )
        if category is None:
            category = SourceCategory(
                source_id=source_id,
                external_id=external_id,
                name=name,
                parent_id=parent_id,
                created_at=now,
                updated_at=now,
            )
            session.add(category)
            session.flush()
        elif category.name != name or category.parent_id != parent_id:
            category.name = name
            category.parent_id = parent_id
            category.updated_at = now
        return category

    @staticmethod
    def _upsert_locations(
        session: Session,
        source_id: int,
        records: list[LocationRecord],
        now: datetime,
    ) -> list[Location]:
        locations: list[Location] = []
        for item in records:
            location = session.scalar(
                select(Location).where(
                    Location.source_id == source_id,
                    Location.code == item.code,
                )
            )
            if location is None:
                location = Location(source_id=source_id, code=item.code, name=item.name)
                session.add(location)
                session.flush()
            location.name = item.name
            location.country_code = item.country_code
            location.country_name = item.country_name
            location.state_code = item.state_code
            location.state_name = item.state_name
            location.district_code = item.district_code
            location.district_name = item.district_name
            location.address = item.address
            Repository._ensure_canonical_location(session, location, item, now)
            locations.append(location)
        return locations

    @staticmethod
    def _ensure_canonical_location(
        session: Session,
        location: Location,
        record: LocationRecord,
        now: datetime,
    ) -> None:
        key = canonical_location_key(record)
        mapping = session.scalar(
            select(SourceLocationMapping).where(
                SourceLocationMapping.location_id == location.id,
                SourceLocationMapping.is_current.is_(True),
            )
        )
        if mapping is not None:
            mapped_location = session.get(
                CanonicalLocation,
                mapping.canonical_location_id,
            )
            if mapped_location is not None and mapped_location.key == key:
                Repository._enrich_canonical_location(mapped_location, record)
                return
            if mapping.mapping_method not in AUTO_LOCATION_MAPPING_METHODS:
                # A published manual/model mapping always wins over automatic
                # name matching until another mapping is explicitly published.
                return
            mapping.is_current = False
            session.flush()
        canonical = session.scalar(
            select(CanonicalLocation).where(CanonicalLocation.key == key)
        )
        if canonical is None:
            canonical = CanonicalLocation(
                key=key,
                level="city",
                name=record.name,
                country_code=record.country_code,
                country_name=record.country_name,
                state_code=record.state_code,
                state_name=record.state_name,
                city_name=record.name,
                created_at=now,
            )
            session.add(canonical)
            session.flush()
        else:
            Repository._enrich_canonical_location(canonical, record)
        mapping_version = f"auto-city-name-v2-{key}"
        replacement = session.scalar(
            select(SourceLocationMapping).where(
                SourceLocationMapping.location_id == location.id,
                SourceLocationMapping.mapping_version == mapping_version,
            )
        )
        if replacement is None:
            replacement = SourceLocationMapping(
                location_id=location.id,
                canonical_location_id=canonical.id,
                mapping_method="normalized_city_name",
                mapping_version=mapping_version,
                is_current=True,
                confidence=Decimal("0.9900"),
                created_at=now,
            )
            session.add(replacement)
        else:
            if replacement.canonical_location_id != canonical.id:
                raise RuntimeError(
                    "Automatic location mapping version points to a different city"
                )
            replacement.is_current = True

    @staticmethod
    def _enrich_canonical_location(
        canonical: CanonicalLocation,
        record: LocationRecord,
    ) -> None:
        for field in ("country_code", "country_name", "state_code", "state_name"):
            if getattr(canonical, field) is None and (value := getattr(record, field)):
                setattr(canonical, field, value)

    @staticmethod
    def _sync_current_locations(
        session: Session,
        job_id: int,
        locations: list[Location],
    ) -> None:
        session.execute(delete(JobLocation).where(JobLocation.job_id == job_id))
        for location in locations:
            session.add(JobLocation(job_id=job_id, location_id=location.id))

    @staticmethod
    def _ensure_version_locations(
        session: Session,
        version_id: int,
        locations: list[Location],
    ) -> None:
        existing_ids = set(
            session.scalars(
                select(JobVersionLocation.location_id).where(
                    JobVersionLocation.job_version_id == version_id
                )
            )
        )
        for location in locations:
            if location.id not in existing_ids:
                mapping = session.scalar(
                    select(SourceLocationMapping).where(
                        SourceLocationMapping.location_id == location.id,
                        SourceLocationMapping.is_current.is_(True),
                    )
                )
                session.add(
                    JobVersionLocation(
                        job_version_id=version_id,
                        location_id=location.id,
                        canonical_location_id=(
                            None if mapping is None else mapping.canonical_location_id
                        ),
                        mapping_method=None if mapping is None else mapping.mapping_method,
                        mapping_version=None if mapping is None else mapping.mapping_version,
                        mapping_confidence=(
                            None if mapping is None else mapping.confidence
                        ),
                    )
                )

    @staticmethod
    def _upsert_business_units(
        session: Session,
        source_id: int,
        records: list[BusinessUnitRecord],
        now: datetime,
    ) -> list[SourceBusinessUnit]:
        units: list[SourceBusinessUnit] = []
        for record in records:
            current = session.scalar(
                select(SourceBusinessUnit).where(
                    SourceBusinessUnit.source_id == source_id,
                    SourceBusinessUnit.external_code == record.code,
                    SourceBusinessUnit.valid_to.is_(None),
                )
            )
            if current is None:
                current = SourceBusinessUnit(
                    source_id=source_id,
                    external_code=record.code,
                    name=record.name,
                    valid_from=now,
                    valid_to=None,
                    created_at=now,
                )
                session.add(current)
                session.flush()
            elif current.name != record.name:
                # Never overwrite a source fact that belongs to an older job
                # version. Close it and publish a new dimension version.
                current.valid_to = now
                session.flush()
                current = SourceBusinessUnit(
                    source_id=source_id,
                    external_code=record.code,
                    name=record.name,
                    valid_from=now,
                    valid_to=None,
                    created_at=now,
                )
                session.add(current)
                session.flush()
            units.append(current)
        return units

    @staticmethod
    def _ensure_version_business_units(
        session: Session,
        version_id: int,
        units: list[SourceBusinessUnit],
    ) -> None:
        existing_ids = set(
            session.scalars(
                select(JobVersionBusinessUnit.source_business_unit_id).where(
                    JobVersionBusinessUnit.job_version_id == version_id
                )
            )
        )
        for unit in units:
            if unit.id not in existing_ids:
                session.add(
                    JobVersionBusinessUnit(
                        job_version_id=version_id,
                        source_business_unit_id=unit.id,
                    )
                )

    def _reconcile_missing(
        self,
        session: Session,
        *,
        source_id: int,
        channel: str,
        seen_ids: set[str],
        run_id: str,
        observed_at: datetime,
    ) -> int:
        jobs = session.scalars(
            select(Job).where(
                Job.source_id == source_id,
                Job.channel == channel,
                Job.status == "active",
            )
        ).all()
        closed = 0
        for job in jobs:
            if job.external_id in seen_ids:
                continue
            version = session.scalar(
                select(JobVersion).where(
                    JobVersion.job_id == job.id,
                    JobVersion.content_hash == job.content_hash,
                )
            )
            if job.missing_streak == 0:
                job.missing_since_at = observed_at
                self._add_lifecycle_event(
                    session,
                    job=job,
                    run_id=run_id,
                    version=version,
                    event_type="missing",
                    effective_at=observed_at,
                    observed_at=observed_at,
                )
            job.missing_streak += 1
            if job.missing_streak >= self.missing_runs_before_close:
                effective_at = job.missing_since_at or observed_at
                job.status = "closed"
                job.closed_at = effective_at
                self._add_lifecycle_event(
                    session,
                    job=job,
                    run_id=run_id,
                    version=version,
                    event_type="closed",
                    effective_at=effective_at,
                    observed_at=observed_at,
                    details={"confirmation_streak": job.missing_streak},
                )
                closed += 1
        return closed


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
