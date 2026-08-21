from collections.abc import Callable

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy import case as sa_case
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.china_cities import china_city_name_sql, china_city_values_sql
from job_market.models import (
    Company,
    CrawlRun,
    CrawlRunFieldStat,
    DailySnapshot,
    DailySnapshotCityStat,
    Job,
    JobLifecycleEvent,
    JobLocation,
    JobObservation,
    JobSearchDocument,
    JobVersion,
    JobVersionLocation,
    JobVersionLocationCity,
    JobVersionSourceCategory,
    Location,
    RawSnapshot,
    Source,
    SourceCategory,
    SourceChannel,
    SourceLocationMapping,
)
from job_market.normalization import normalize_city_names

AUTO_LOCATION_MAPPING_METHODS = {"exact_source_fields", "normalized_city_name"}


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
                "daily_snapshot_city_stats": self._count(
                    session, DailySnapshotCityStat
                ),
                "jobs": self._count(session, Job),
                "job_search_documents": self._count(session, JobSearchDocument),
                "job_versions": self._count(session, JobVersion),
                "job_version_source_categories": self._count(
                    session,
                    JobVersionSourceCategory,
                ),
                "job_observations": self._count(session, JobObservation),
                "job_lifecycle_events": self._count(session, JobLifecycleEvent),
                "locations": self._count(session, Location),
                "job_version_locations": self._count(session, JobVersionLocation),
                "job_version_location_cities": self._count(
                    session,
                    JobVersionLocationCity,
                ),
                "raw_snapshots": self._count(session, RawSnapshot),
            }
            checks: dict[str, Callable[[Session], int]] = {
                "observation_job_version_mismatch": self._observation_version_mismatch,
                "observation_run_scope_mismatch": self._observation_run_scope_mismatch,
                "invalid_daily_snapshot": self._invalid_daily_snapshot,
                "daily_snapshot_rollup_mismatch": (
                    self._daily_snapshot_rollup_mismatch
                ),
                "daily_snapshot_city_stat_mismatch": (
                    self._daily_snapshot_city_stat_mismatch
                ),
                "job_search_document_mismatch": (
                    self._job_search_document_mismatch
                ),
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
    def _daily_snapshot_rollup_mismatch(session: Session) -> int:
        statement = text(
            """
            WITH observation_counts AS (
                SELECT ds.id AS snapshot_id,
                       COUNT(jo.id) AS active_count,
                       SUM(
                           CASE
                               WHEN NOT ds.is_baseline
                                AND j.first_canonical_seen_on = ds.snapshot_date
                               THEN 1 ELSE 0
                           END
                       ) AS new_count
                FROM daily_snapshots AS ds
                LEFT JOIN job_observations AS jo
                  ON jo.crawl_run_id = ds.crawl_run_id
                LEFT JOIN jobs AS j ON j.id = jo.job_id
                GROUP BY ds.id, ds.snapshot_date, ds.is_baseline
            ), event_counts AS (
                SELECT crawl_run_id,
                       SUM(CASE WHEN event_type = 'changed' THEN 1 ELSE 0 END)
                           AS changed_count,
                       SUM(CASE WHEN event_type = 'missing' THEN 1 ELSE 0 END)
                           AS missing_count,
                       SUM(CASE WHEN event_type = 'closed' THEN 1 ELSE 0 END)
                           AS closed_count,
                       SUM(CASE WHEN event_type = 'reopened' THEN 1 ELSE 0 END)
                           AS reopened_count
                FROM job_lifecycle_events
                WHERE crawl_run_id IS NOT NULL
                GROUP BY crawl_run_id
            )
            SELECT COUNT(*)
            FROM daily_snapshots AS ds
            JOIN observation_counts AS oc ON oc.snapshot_id = ds.id
            LEFT JOIN event_counts AS ec ON ec.crawl_run_id = ds.crawl_run_id
            WHERE ds.active_posting_count != oc.active_count
               OR ds.new_posting_count != oc.new_count
               OR ds.changed_posting_count != COALESCE(ec.changed_count, 0)
               OR ds.first_missing_posting_count != COALESCE(ec.missing_count, 0)
               OR ds.closed_posting_count != COALESCE(ec.closed_count, 0)
               OR ds.reopened_posting_count != COALESCE(ec.reopened_count, 0)
               OR ds.active_posting_count < 0
               OR ds.new_posting_count < 0
               OR ds.changed_posting_count < 0
               OR ds.first_missing_posting_count < 0
               OR ds.closed_posting_count < 0
               OR ds.reopened_posting_count < 0
            """
        )
        return int(session.execute(statement).scalar_one())

    @staticmethod
    def _daily_snapshot_city_stat_mismatch(session: Session) -> int:
        standard_city_name = china_city_name_sql("cl.name")
        statement = text(
            f"""
            WITH china_city_links AS (
                SELECT DISTINCT jvlc.job_version_id,
                       {standard_city_name} AS city_name
                FROM job_version_location_cities AS jvlc
                JOIN canonical_locations AS cl
                  ON cl.id = jvlc.canonical_location_id
                WHERE {standard_city_name} IN ({china_city_values_sql()})
            ), version_location_counts AS (
                SELECT job_version_id, COUNT(*) AS location_count
                FROM china_city_links
                GROUP BY job_version_id
            ), expected AS (
                SELECT ds.id AS daily_snapshot_id,
                       city.city_name,
                       COUNT(DISTINCT jo.job_id) AS posting_count,
                       SUM(1.0 / vlc.location_count)
                           AS fractional_posting_count
                FROM daily_snapshots AS ds
                JOIN job_observations AS jo
                  ON jo.crawl_run_id = ds.crawl_run_id
                JOIN china_city_links AS city
                  ON city.job_version_id = jo.job_version_id
                JOIN version_location_counts AS vlc
                  ON vlc.job_version_id = jo.job_version_id
                GROUP BY ds.id, city.city_name
            ), mismatches AS (
                SELECT expected.daily_snapshot_id, expected.city_name
                FROM expected
                LEFT JOIN daily_snapshot_city_stats AS actual
                  ON actual.daily_snapshot_id = expected.daily_snapshot_id
                 AND actual.city_name = expected.city_name
                WHERE actual.daily_snapshot_id IS NULL
                   OR actual.posting_count != expected.posting_count
                   OR ABS(
                       actual.fractional_posting_count
                       - expected.fractional_posting_count
                   ) > 0.000000000001
                UNION ALL
                SELECT actual.daily_snapshot_id, actual.city_name
                FROM daily_snapshot_city_stats AS actual
                LEFT JOIN expected
                  ON expected.daily_snapshot_id = actual.daily_snapshot_id
                 AND expected.city_name = actual.city_name
                WHERE expected.daily_snapshot_id IS NULL
            )
            SELECT COUNT(*) FROM mismatches
            """
        )
        return int(session.execute(statement).scalar_one())

    @staticmethod
    def _job_search_document_mismatch(session: Session) -> int:
        rows = session.execute(
            select(
                Job.title,
                Job.description,
                Job.requirements,
                JobSearchDocument.job_id,
                JobSearchDocument.title_characters,
                JobSearchDocument.description_characters,
                JobSearchDocument.requirements_characters,
            ).outerjoin(JobSearchDocument, JobSearchDocument.job_id == Job.id)
        )
        mismatches = 0
        for row in rows:
            if row.job_id is None:
                mismatches += 1
                continue
            expected = (
                DataQualityChecker._search_character_codes(row.title),
                DataQualityChecker._search_character_codes(row.description),
                DataQualityChecker._search_character_codes(row.requirements),
            )
            actual = (
                row.title_characters,
                row.description_characters,
                row.requirements_characters,
            )
            mismatches += actual != expected
        return mismatches

    @staticmethod
    def _search_character_codes(value: str | None) -> list[int]:
        return sorted(
            {
                ord(character)
                for character in (value or "").lower()
                if not character.isspace() or character in "\u00a0\u202f"
            }
        )

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
        mapping_rows = session.execute(
            select(
                SourceLocationMapping.location_id,
                func.count().label("mapping_count"),
            )
            .where(SourceLocationMapping.is_current.is_(True))
            .group_by(SourceLocationMapping.location_id)
        )
        mapping_counts = {
            location_id: mapping_count
            for location_id, mapping_count in mapping_rows
        }
        current_methods: dict[int, list[str]] = {}
        for location_id, mapping_method in session.execute(
            select(
                SourceLocationMapping.location_id,
                SourceLocationMapping.mapping_method,
            ).where(SourceLocationMapping.is_current.is_(True))
        ):
            current_methods.setdefault(location_id, []).append(mapping_method)
        locations = session.execute(select(Location.id, Location.name))
        return sum(
            count
            != (
                1
                if any(
                    method not in AUTO_LOCATION_MAPPING_METHODS
                    for method in current_methods.get(location_id, [])
                )
                else len(normalize_city_names(name))
            )
            for location_id, name in locations
            for count in (mapping_counts.get(location_id, 0),)
        )

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
