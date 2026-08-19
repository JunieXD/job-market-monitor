from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


class AnalyticsRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def coverage(self, snapshot_date: date | None = None) -> dict[str, Any]:
        """Return the data coverage context used by every public chart."""

        with self.engine.connect() as connection:
            if snapshot_date is None:
                snapshot_date = connection.execute(
                    text("SELECT MAX(snapshot_date) FROM daily_snapshots")
                ).scalar_one_or_none()
            if snapshot_date is None:
                return {
                    "snapshot_date": None,
                    "configured_source_channel_count": 0,
                    "standard_snapshot_count": 0,
                    "successful_source_channel_count": 0,
                    "absence_authoritative_source_channel_count": 0,
                    "non_authoritative_successful_run_count": 0,
                    "failed_run_count": 0,
                    "coverage_ratio": 0.0,
                }
            row = connection.execute(
                text(
                    """
                    SELECT
                        (
                            SELECT COUNT(*)
                            FROM source_channels AS sc
                            JOIN sources AS s ON s.id = sc.source_id
                            WHERE sc.status = 'active' AND s.enabled
                        ) AS configured_source_channel_count,
                        (
                            SELECT COUNT(*)
                            FROM daily_snapshots
                            WHERE snapshot_date = :snapshot_date
                        ) AS standard_snapshot_count,
                        (
                            SELECT COUNT(*)
                            FROM daily_snapshots AS ds
                            JOIN crawl_runs AS cr ON cr.id = ds.crawl_run_id
                            WHERE ds.snapshot_date = :snapshot_date
                              AND cr.status = 'success'
                        ) AS successful_source_channel_count,
                        (
                            SELECT COUNT(*)
                            FROM daily_snapshots AS ds
                            JOIN crawl_runs AS cr ON cr.id = ds.crawl_run_id
                            WHERE ds.snapshot_date = :snapshot_date
                              AND cr.absence_authoritative
                        ) AS absence_authoritative_source_channel_count,
                        (
                            SELECT COUNT(*)
                            FROM crawl_runs
                            WHERE snapshot_date = :snapshot_date
                              AND status = 'success'
                              AND complete
                              AND NOT absence_authoritative
                        ) AS non_authoritative_successful_run_count,
                        (
                            SELECT COUNT(*)
                            FROM crawl_runs
                            WHERE snapshot_date = :snapshot_date
                              AND status = 'failed'
                        ) AS failed_run_count
                    """
                ),
                {"snapshot_date": snapshot_date},
            ).mappings().one()
        result = dict(row)
        configured = int(result["configured_source_channel_count"])
        result["snapshot_date"] = snapshot_date
        result["coverage_ratio"] = (
            float(result["standard_snapshot_count"]) / configured
            if configured
            else 0.0
        )
        return result

    def company_trend(
        self,
        *,
        company_key: str,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._query(
            "daily_company_stats",
            company_key=company_key,
            channel=channel,
        )

    def category_distribution(
        self,
        *,
        company_key: str,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._category_query(
            market=False,
            company_key=company_key,
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def market_category_distribution(
        self,
        *,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._category_query(
            market=True,
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def city_distribution(
        self,
        *,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._city_query(
            market=company_key is None,
            company_key=company_key,
            snapshot_date=snapshot_date,
            channel=channel,
        )

    def _category_query(
        self,
        *,
        market: bool,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: dict[str, object] = {}
        if company_key is not None:
            filters.append("c.key = :company_key")
            params["company_key"] = company_key
        if snapshot_date is not None:
            filters.append("ds.snapshot_date = :snapshot_date")
            params["snapshot_date"] = snapshot_date
        if channel is not None:
            filters.append("ds.channel = :channel")
            params["channel"] = channel
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        if market:
            statement = text(
                f"""
                WITH total_counts AS (
                    SELECT ds.snapshot_date, ds.channel,
                           COUNT(DISTINCT jo.job_id) AS total_posting_count
                    FROM daily_snapshots AS ds
                    LEFT JOIN job_observations AS jo
                        ON jo.crawl_run_id = ds.crawl_run_id
                    GROUP BY ds.snapshot_date, ds.channel
                ), category_counts AS (
                    SELECT ds.snapshot_date, ds.channel,
                           jvsc.canonical_category_id,
                           CASE
                               WHEN sc.id IS NULL THEN 'unclassified'
                               WHEN jvsc.canonical_category_id IS NULL THEN 'unmapped'
                               ELSE cc.key
                           END AS canonical_category_key,
                           CASE
                               WHEN sc.id IS NULL THEN '未分类'
                               WHEN jvsc.canonical_category_id IS NULL THEN '未映射'
                               ELSE cc.name
                           END AS canonical_category_name,
                           CASE
                               WHEN sc.id IS NULL THEN 'unclassified'
                               WHEN jvsc.canonical_category_id IS NULL THEN 'unmapped'
                               ELSE 'mapped'
                           END AS category_status,
                           COUNT(DISTINCT jo.job_id) AS posting_count,
                           COUNT(DISTINCT s.company_id) AS covered_company_count
                    FROM daily_snapshots AS ds
                    JOIN sources AS s ON s.id = ds.source_id
                    JOIN job_observations AS jo
                        ON jo.crawl_run_id = ds.crawl_run_id
                    LEFT JOIN job_version_source_categories AS jvsc
                        ON jvsc.job_version_id = jo.job_version_id
                    LEFT JOIN source_categories AS sc
                        ON sc.id = jvsc.source_category_id
                    LEFT JOIN canonical_categories AS cc
                        ON cc.id = jvsc.canonical_category_id
                    {where}
                    GROUP BY ds.snapshot_date, ds.channel,
                             jvsc.canonical_category_id,
                             CASE
                                 WHEN sc.id IS NULL THEN 'unclassified'
                                 WHEN jvsc.canonical_category_id IS NULL THEN 'unmapped'
                                 ELSE cc.key
                             END,
                             CASE
                                 WHEN sc.id IS NULL THEN '未分类'
                                 WHEN jvsc.canonical_category_id IS NULL THEN '未映射'
                                 ELSE cc.name
                             END,
                             CASE
                                 WHEN sc.id IS NULL THEN 'unclassified'
                                 WHEN jvsc.canonical_category_id IS NULL THEN 'unmapped'
                                 ELSE 'mapped'
                             END
                )
                SELECT category_counts.*, 1.0 * posting_count
                    / NULLIF(total_counts.total_posting_count, 0)
                    AS market_category_share
                FROM category_counts
                JOIN total_counts
                  ON total_counts.snapshot_date = category_counts.snapshot_date
                 AND total_counts.channel = category_counts.channel
                ORDER BY category_counts.snapshot_date, canonical_category_name
                """
            )
        else:
            statement = text(
                f"""
                WITH total_counts AS (
                    SELECT ds.snapshot_date, ds.source_id, ds.channel,
                           COUNT(DISTINCT jo.job_id) AS total_posting_count
                    FROM daily_snapshots AS ds
                    LEFT JOIN job_observations AS jo
                        ON jo.crawl_run_id = ds.crawl_run_id
                    GROUP BY ds.snapshot_date, ds.source_id, ds.channel
                ), category_counts AS (
                    SELECT ds.snapshot_date, ds.source_id,
                           s.company_id, c.key AS company_key,
                           c.name AS company_name, ds.channel,
                           sc.id AS source_category_id,
                           CASE WHEN sc.id IS NULL THEN '__unclassified__'
                                ELSE sc.external_id END
                               AS source_category_external_id,
                           CASE WHEN sc.id IS NULL THEN '未分类' ELSE sc.name END
                               AS source_category_name,
                           parent.name AS source_parent_category_name,
                           jvsc.assignment_method AS category_assignment_method,
                           CASE WHEN sc.id IS NULL THEN 'unclassified'
                                WHEN jvsc.canonical_category_id IS NULL THEN 'unmapped'
                                ELSE 'mapped' END AS category_status,
                           jvsc.canonical_category_id,
                           cc.key AS canonical_category_key,
                           cc.name AS canonical_category_name,
                           COUNT(DISTINCT jo.job_id) AS posting_count
                    FROM daily_snapshots AS ds
                    JOIN sources AS s ON s.id = ds.source_id
                    JOIN companies AS c ON c.id = s.company_id
                    JOIN job_observations AS jo
                        ON jo.crawl_run_id = ds.crawl_run_id
                    LEFT JOIN job_version_source_categories AS jvsc
                        ON jvsc.job_version_id = jo.job_version_id
                    LEFT JOIN source_categories AS sc
                        ON sc.id = jvsc.source_category_id
                    LEFT JOIN source_categories AS parent
                        ON parent.id = sc.parent_id
                    LEFT JOIN canonical_categories AS cc
                        ON cc.id = jvsc.canonical_category_id
                    {where}
                    GROUP BY ds.snapshot_date, ds.source_id, s.company_id,
                             c.key, c.name, ds.channel, sc.id,
                             sc.external_id, sc.name, parent.name,
                             jvsc.assignment_method, jvsc.canonical_category_id,
                             cc.key, cc.name
                )
                SELECT category_counts.*, 1.0 * posting_count
                    / NULLIF(total_counts.total_posting_count, 0)
                    AS source_category_share
                FROM category_counts
                JOIN total_counts
                  ON total_counts.snapshot_date = category_counts.snapshot_date
                 AND total_counts.source_id = category_counts.source_id
                 AND total_counts.channel = category_counts.channel
                ORDER BY category_counts.snapshot_date, source_category_name
                """
            )
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement, params).mappings()
            ]

    def _city_query(
        self,
        *,
        market: bool,
        company_key: str | None = None,
        snapshot_date: date | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: dict[str, object] = {}
        if company_key is not None:
            filters.append("c.key = :company_key")
            params["company_key"] = company_key
        if snapshot_date is not None:
            filters.append("ds.snapshot_date = :snapshot_date")
            params["snapshot_date"] = snapshot_date
        if channel is not None:
            filters.append("ds.channel = :channel")
            params["channel"] = channel
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        company_columns = (
            ""
            if market
            else ", s.company_id, c.key AS company_key, c.name AS company_name"
        )
        company_joins = "" if market else "JOIN companies AS c ON c.id = s.company_id"
        group_company = "" if market else ", s.company_id, c.key, c.name"
        covered_company = (
            ""
            if not market
            else ", COUNT(DISTINCT s.company_id) AS covered_company_count"
        )
        statement = text(
            f"""
            WITH version_location_counts AS (
                SELECT job_version_id, COUNT(*) AS location_count
                FROM job_version_locations
                GROUP BY job_version_id
            ), city_counts AS (
                SELECT ds.snapshot_date, ds.channel{company_columns},
                       jvl.canonical_location_id,
                       cl.key AS canonical_location_key,
                       CASE WHEN cl.id IS NULL THEN '未映射' ELSE cl.name END
                           AS city_name,
                       cl.country_name, cl.state_name,
                       COUNT(DISTINCT jo.job_id) AS posting_count,
                       SUM(1.0 / vlc.location_count) AS fractional_posting_count
                       {covered_company}
                FROM daily_snapshots AS ds
                JOIN sources AS s ON s.id = ds.source_id
                {company_joins}
                JOIN job_observations AS jo
                    ON jo.crawl_run_id = ds.crawl_run_id
                JOIN job_version_locations AS jvl
                    ON jvl.job_version_id = jo.job_version_id
                JOIN version_location_counts AS vlc
                    ON vlc.job_version_id = jo.job_version_id
                LEFT JOIN canonical_locations AS cl
                    ON cl.id = jvl.canonical_location_id
                {where}
                GROUP BY ds.snapshot_date, ds.channel{group_company},
                         jvl.canonical_location_id, cl.key, cl.id, cl.name,
                         cl.country_name, cl.state_name
            )
            SELECT city_counts.*,
                   fractional_posting_count / NULLIF(
                       SUM(fractional_posting_count) OVER (
                           PARTITION BY snapshot_date, channel{
                               ', company_key' if not market else ''
                           }
                       ), 0
                   ) AS fractional_share
            FROM city_counts
            ORDER BY snapshot_date, fractional_share DESC, city_name
            """
        )
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement, params).mappings()
            ]

    def _query(self, view: str, **filters: object) -> list[dict[str, Any]]:
        allowed_views = {
            "daily_company_stats",
            "daily_category_stats",
            "daily_city_stats",
            "daily_market_category_stats",
            "daily_market_city_stats",
        }
        if view not in allowed_views:
            raise ValueError(f"Unsupported analytics view: {view}")
        active_filters = {key: value for key, value in filters.items() if value is not None}
        clauses = [f"{key} = :{key}" for key in active_filters]
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        statement = text(f"SELECT * FROM {view}{where} ORDER BY snapshot_date")
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement, active_filters).mappings()
            ]
