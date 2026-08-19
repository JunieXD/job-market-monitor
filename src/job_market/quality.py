from collections.abc import Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy import case as sa_case
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.models import (
    Company,
    CrawlRun,
    CrawlRunFieldStat,
    DailySnapshot,
    Job,
    JobLifecycleEvent,
    JobLocation,
    JobObservation,
    JobVersion,
    JobVersionLocation,
    JobVersionSourceCategory,
    Location,
    RawSnapshot,
    Source,
    SourceCategory,
    SourceChannel,
    SourceLocationMapping,
)


class DataQualityChecker:
    def __init__(self, engine: Engine):
        self.engine = engine

    def run(self) -> dict[str, object]:
        with Session(self.engine) as session:
            counts = {
                "companies": self._count(session, Company),
                "sources": self._count(session, Source),
                "crawl_runs": self._count(session, CrawlRun),
                "crawl_run_field_stats": self._count(session, CrawlRunFieldStat),
                "daily_snapshots": self._count(session, DailySnapshot),
                "jobs": self._count(session, Job),
                "job_versions": self._count(session, JobVersion),
                "job_version_source_categories": self._count(
                    session,
                    JobVersionSourceCategory,
                ),
                "job_observations": self._count(session, JobObservation),
                "job_lifecycle_events": self._count(session, JobLifecycleEvent),
                "locations": self._count(session, Location),
                "job_version_locations": self._count(session, JobVersionLocation),
                "raw_snapshots": self._count(session, RawSnapshot),
            }
            checks: dict[str, Callable[[Session], int]] = {
                "observation_job_version_mismatch": self._observation_version_mismatch,
                "observation_run_scope_mismatch": self._observation_run_scope_mismatch,
                "invalid_daily_snapshot": self._invalid_daily_snapshot,
                "successful_run_count_mismatch": self._successful_run_count_mismatch,
                "invalid_baseline_snapshot": self._invalid_baseline_snapshot,
                "current_location_set_mismatch": self._current_location_set_mismatch,
                "job_hash_without_version": self._job_hash_without_version,
                "category_source_mismatch": self._category_source_mismatch,
                "legacy_category_link_missing": self._legacy_category_link_missing,
                "version_category_primary_missing": (
                    self._version_category_primary_missing
                ),
                "field_stats_run_count_mismatch": (
                    self._field_stats_run_count_mismatch
                ),
                "run_without_source_channel": self._run_without_source_channel,
                "job_without_exactly_one_first_seen_event": self._invalid_first_seen_count,
                "location_without_one_current_mapping": self._invalid_location_mapping_count,
                "canonical_seen_date_missing": self._canonical_seen_date_missing,
                "canonical_seen_date_mismatch": self._canonical_seen_date_mismatch,
                "lifecycle_event_job_version_mismatch": (
                    self._lifecycle_event_job_version_mismatch
                ),
                "invalid_closed_state": self._invalid_closed_state,
            }
            violations = {
                name: count for name, check in checks.items() if (count := check(session))
            }
        return {"ok": not violations, "counts": counts, "violations": violations}

    @staticmethod
    def _count(session: Session, model: type) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    @staticmethod
    def _observation_version_mismatch(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(JobObservation)
            .join(JobVersion, JobVersion.id == JobObservation.job_version_id)
            .where(JobObservation.job_id != JobVersion.job_id)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _observation_run_scope_mismatch(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(JobObservation)
            .join(Job, Job.id == JobObservation.job_id)
            .join(CrawlRun, CrawlRun.id == JobObservation.crawl_run_id)
            .where(
                or_(
                    Job.source_id != CrawlRun.source_id,
                    Job.channel != CrawlRun.channel,
                )
            )
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _invalid_daily_snapshot(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(DailySnapshot)
            .join(CrawlRun, CrawlRun.id == DailySnapshot.crawl_run_id)
            .where(
                or_(
                    CrawlRun.status != "success",
                    CrawlRun.complete.is_(False),
                    CrawlRun.absence_authoritative.is_(False),
                    CrawlRun.source_id != DailySnapshot.source_id,
                    CrawlRun.channel != DailySnapshot.channel,
                    CrawlRun.snapshot_date != DailySnapshot.snapshot_date,
                )
            )
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _successful_run_count_mismatch(session: Session) -> int:
        observation_counts = (
            select(
                JobObservation.crawl_run_id.label("run_id"),
                func.count(JobObservation.id).label("observed_count"),
            )
            .group_by(JobObservation.crawl_run_id)
            .subquery()
        )
        statement = (
            select(func.count())
            .select_from(CrawlRun)
            .outerjoin(
                observation_counts,
                observation_counts.c.run_id == CrawlRun.id,
            )
            .where(
                CrawlRun.status.in_(("success", "partial")),
                CrawlRun.discovered_count
                != func.coalesce(observation_counts.c.observed_count, 0),
            )
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _invalid_baseline_snapshot(session: Session) -> int:
        groups = session.execute(
            select(
                DailySnapshot.source_id,
                DailySnapshot.channel,
                func.min(DailySnapshot.snapshot_date).label("first_date"),
                func.sum(
                    sa_case((DailySnapshot.is_baseline.is_(True), 1), else_=0)
                ).label("baseline_count"),
                func.min(
                    sa_case(
                        (
                            DailySnapshot.is_baseline.is_(True),
                            DailySnapshot.snapshot_date,
                        ),
                        else_=None,
                    )
                ).label("baseline_date"),
            ).group_by(DailySnapshot.source_id, DailySnapshot.channel)
        )
        return sum(
            row.baseline_count != 1 or row.baseline_date != row.first_date
            for row in groups
        )

    @staticmethod
    def _current_location_set_mismatch(session: Session) -> int:
        current_version_locations = (
            select(
                JobVersion.job_id.label("job_id"),
                JobVersionLocation.location_id.label("location_id"),
            )
            .join(
                Job,
                and_(
                    Job.id == JobVersion.job_id,
                    Job.content_hash == JobVersion.content_hash,
                ),
            )
            .join(
                JobVersionLocation,
                JobVersionLocation.job_version_id == JobVersion.id,
            )
            .subquery()
        )
        missing_from_current = session.scalar(
            select(func.count())
            .select_from(current_version_locations)
            .outerjoin(
                JobLocation,
                and_(
                    JobLocation.job_id == current_version_locations.c.job_id,
                    JobLocation.location_id
                    == current_version_locations.c.location_id,
                ),
            )
            .where(JobLocation.job_id.is_(None))
        ) or 0
        extra_in_current = session.scalar(
            select(func.count())
            .select_from(JobLocation)
            .outerjoin(
                current_version_locations,
                and_(
                    current_version_locations.c.job_id == JobLocation.job_id,
                    current_version_locations.c.location_id
                    == JobLocation.location_id,
                ),
            )
            .where(current_version_locations.c.job_id.is_(None))
        ) or 0
        return missing_from_current + extra_in_current

    @staticmethod
    def _job_hash_without_version(session: Session) -> int:
        matching_version = (
            select(JobVersion.id)
            .where(
                JobVersion.job_id == Job.id,
                JobVersion.content_hash == Job.content_hash,
            )
            .exists()
        )
        return (
            session.scalar(
                select(func.count()).select_from(Job).where(~matching_version)
            )
            or 0
        )

    @staticmethod
    def _category_source_mismatch(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(JobVersionSourceCategory)
            .join(
                JobVersion,
                JobVersion.id == JobVersionSourceCategory.job_version_id,
            )
            .join(Job, Job.id == JobVersion.job_id)
            .join(
                SourceCategory,
                SourceCategory.id == JobVersionSourceCategory.source_category_id,
            )
            .where(Job.source_id != SourceCategory.source_id)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _legacy_category_link_missing(session: Session) -> int:
        matching_link = (
            select(JobVersionSourceCategory.job_version_id)
            .where(
                JobVersionSourceCategory.job_version_id == JobVersion.id,
                JobVersionSourceCategory.source_category_id
                == JobVersion.source_category_id,
            )
            .exists()
        )
        statement = (
            select(func.count())
            .select_from(JobVersion)
            .where(JobVersion.source_category_id.is_not(None), ~matching_link)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _version_category_primary_missing(session: Session) -> int:
        category_link = (
            select(JobVersionSourceCategory.job_version_id)
            .where(JobVersionSourceCategory.job_version_id == JobVersion.id)
            .exists()
        )
        statement = (
            select(func.count())
            .select_from(JobVersion)
            .where(JobVersion.source_category_id.is_(None), category_link)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _field_stats_run_count_mismatch(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(CrawlRunFieldStat)
            .join(CrawlRun, CrawlRun.id == CrawlRunFieldStat.crawl_run_id)
            .where(CrawlRunFieldStat.row_count != CrawlRun.discovered_count)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _run_without_source_channel(session: Session) -> int:
        matching_contract = (
            select(SourceChannel.source_id)
            .where(
                SourceChannel.source_id == CrawlRun.source_id,
                SourceChannel.channel == CrawlRun.channel,
            )
            .exists()
        )
        statement = select(func.count()).select_from(CrawlRun).where(~matching_contract)
        return session.scalar(statement) or 0

    @staticmethod
    def _invalid_first_seen_count(session: Session) -> int:
        event_counts = (
            select(
                JobLifecycleEvent.job_id,
                func.count().label("event_count"),
            )
            .where(JobLifecycleEvent.event_type == "first_seen")
            .group_by(JobLifecycleEvent.job_id)
            .subquery()
        )
        statement = (
            select(func.count())
            .select_from(Job)
            .outerjoin(event_counts, event_counts.c.job_id == Job.id)
            .where(func.coalesce(event_counts.c.event_count, 0) != 1)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _invalid_location_mapping_count(session: Session) -> int:
        mapping_counts = (
            select(
                SourceLocationMapping.location_id,
                func.count().label("mapping_count"),
            )
            .where(SourceLocationMapping.is_current.is_(True))
            .group_by(SourceLocationMapping.location_id)
            .subquery()
        )
        statement = (
            select(func.count())
            .select_from(Location)
            .outerjoin(mapping_counts, mapping_counts.c.location_id == Location.id)
            .where(func.coalesce(mapping_counts.c.mapping_count, 0) != 1)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _canonical_seen_date_missing(session: Session) -> int:
        canonical_observation = (
            select(JobObservation.id)
            .join(
                DailySnapshot,
                DailySnapshot.crawl_run_id == JobObservation.crawl_run_id,
            )
            .where(JobObservation.job_id == Job.id)
            .exists()
        )
        statement = (
            select(func.count())
            .select_from(Job)
            .where(Job.first_canonical_seen_on.is_(None), canonical_observation)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _canonical_seen_date_mismatch(session: Session) -> int:
        first_dates = (
            select(
                JobObservation.job_id,
                func.min(DailySnapshot.snapshot_date).label("first_date"),
            )
            .join(
                DailySnapshot,
                DailySnapshot.crawl_run_id == JobObservation.crawl_run_id,
            )
            .group_by(JobObservation.job_id)
            .subquery()
        )
        statement = (
            select(func.count())
            .select_from(Job)
            .join(first_dates, first_dates.c.job_id == Job.id)
            .where(Job.first_canonical_seen_on != first_dates.c.first_date)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _lifecycle_event_job_version_mismatch(session: Session) -> int:
        statement = (
            select(func.count())
            .select_from(JobLifecycleEvent)
            .join(JobVersion, JobVersion.id == JobLifecycleEvent.job_version_id)
            .where(JobLifecycleEvent.job_id != JobVersion.job_id)
        )
        return session.scalar(statement) or 0

    @staticmethod
    def _invalid_closed_state(session: Session) -> int:
        statement = select(func.count()).select_from(Job).where(
            or_(
                and_(
                    Job.status == "closed",
                    or_(Job.closed_at.is_(None), Job.missing_since_at.is_(None)),
                ),
                and_(Job.status == "active", Job.closed_at.is_not(None)),
            )
        )
        return session.scalar(statement) or 0
