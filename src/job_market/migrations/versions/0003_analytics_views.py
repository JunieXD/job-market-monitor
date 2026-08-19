"""Add rebuildable daily analysis views.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COMPANY_VIEW = """
CREATE VIEW daily_company_stats AS
WITH event_counts AS (
    SELECT
        crawl_run_id,
        SUM(CASE WHEN event_type = 'changed' THEN 1 ELSE 0 END) AS changed_count,
        SUM(CASE WHEN event_type = 'missing' THEN 1 ELSE 0 END) AS first_missing_count,
        SUM(CASE WHEN event_type = 'closed' THEN 1 ELSE 0 END) AS closed_count,
        SUM(CASE WHEN event_type = 'reopened' THEN 1 ELSE 0 END) AS reopened_count
    FROM job_lifecycle_events
    WHERE crawl_run_id IS NOT NULL
    GROUP BY crawl_run_id
)
SELECT
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key AS company_key,
    c.name AS company_name,
    ds.channel,
    ds.is_baseline,
    COUNT(jo.job_id) AS active_posting_count,
    CASE
        WHEN ds.is_baseline THEN 0
        ELSE SUM(
            CASE WHEN j.first_canonical_seen_on = ds.snapshot_date THEN 1 ELSE 0 END
        )
    END AS new_posting_count,
    COALESCE(MAX(ec.changed_count), 0) AS changed_posting_count,
    COALESCE(MAX(ec.first_missing_count), 0) AS first_missing_posting_count,
    COALESCE(MAX(ec.closed_count), 0) AS closed_posting_count,
    COALESCE(MAX(ec.reopened_count), 0) AS reopened_posting_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN companies AS c ON c.id = s.company_id
LEFT JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
LEFT JOIN jobs AS j ON j.id = jo.job_id
LEFT JOIN event_counts AS ec ON ec.crawl_run_id = ds.crawl_run_id
GROUP BY
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key,
    c.name,
    ds.channel,
    ds.is_baseline
"""


CATEGORY_VIEW = """
CREATE VIEW daily_category_stats AS
WITH category_counts AS (
    SELECT
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key AS company_key,
        c.name AS company_name,
        ds.channel,
        sc.id AS source_category_id,
        sc.external_id AS source_category_external_id,
        sc.name AS source_category_name,
        parent.name AS source_parent_category_name,
        cc.id AS canonical_category_id,
        cc.key AS canonical_category_key,
        cc.name AS canonical_category_name,
        COUNT(jo.job_id) AS posting_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN companies AS c ON c.id = s.company_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    JOIN job_versions AS jv ON jv.id = jo.job_version_id
    JOIN source_categories AS sc ON sc.id = jv.source_category_id
    LEFT JOIN source_categories AS parent ON parent.id = sc.parent_id
    LEFT JOIN category_mappings AS cm
        ON cm.source_category_id = sc.id AND cm.is_current
    LEFT JOIN canonical_categories AS cc ON cc.id = cm.canonical_category_id
    GROUP BY
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key,
        c.name,
        ds.channel,
        sc.id,
        sc.external_id,
        sc.name,
        parent.name,
        cc.id,
        cc.key,
        cc.name
)
SELECT
    category_counts.*,
    1.0 * posting_count
        / NULLIF(
            SUM(posting_count) OVER (
                PARTITION BY snapshot_date, source_id, channel
            ),
            0
        ) AS source_category_share
FROM category_counts
"""


CITY_VIEW = """
CREATE VIEW daily_city_stats AS
WITH version_location_counts AS (
    SELECT job_version_id, COUNT(*) AS location_count
    FROM job_version_locations
    GROUP BY job_version_id
),
city_counts AS (
    SELECT
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key AS company_key,
        c.name AS company_name,
        ds.channel,
        cl.id AS canonical_location_id,
        cl.key AS canonical_location_key,
        cl.name AS city_name,
        cl.country_name,
        cl.state_name,
        COUNT(DISTINCT jo.job_id) AS posting_count,
        SUM(1.0 / vlc.location_count) AS fractional_posting_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN companies AS c ON c.id = s.company_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    JOIN job_version_locations AS jvl ON jvl.job_version_id = jo.job_version_id
    JOIN version_location_counts AS vlc ON vlc.job_version_id = jo.job_version_id
    JOIN source_location_mappings AS slm
        ON slm.location_id = jvl.location_id AND slm.is_current
    JOIN canonical_locations AS cl ON cl.id = slm.canonical_location_id
    GROUP BY
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key,
        c.name,
        ds.channel,
        cl.id,
        cl.key,
        cl.name,
        cl.country_name,
        cl.state_name
)
SELECT
    city_counts.*,
    fractional_posting_count
        / NULLIF(
            SUM(fractional_posting_count) OVER (
                PARTITION BY snapshot_date, source_id, channel
            ),
            0
        ) AS fractional_share
FROM city_counts
"""


TOPIC_VIEW = """
CREATE VIEW daily_topic_stats AS
SELECT
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key AS company_key,
    c.name AS company_name,
    ds.channel,
    t.id AS topic_id,
    t.taxonomy_version,
    t.key AS topic_key,
    t.name AS topic_name,
    COUNT(DISTINCT jo.job_id) AS active_posting_count,
    CASE
        WHEN ds.is_baseline THEN 0
        ELSE COUNT(
            DISTINCT CASE
                WHEN j.first_canonical_seen_on = ds.snapshot_date THEN jo.job_id
                ELSE NULL
            END
        )
    END AS new_posting_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN companies AS c ON c.id = s.company_id
JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
JOIN jobs AS j ON j.id = jo.job_id
JOIN job_topic_mentions AS jtm ON jtm.job_version_id = jo.job_version_id
JOIN derivation_runs AS dr
    ON dr.id = jtm.derivation_run_id
    AND dr.status = 'success'
    AND dr.is_current
JOIN topics AS t ON t.id = jtm.topic_id
GROUP BY
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key,
    c.name,
    ds.channel,
    ds.is_baseline,
    t.id,
    t.taxonomy_version,
    t.key,
    t.name
"""


MARKET_CATEGORY_VIEW = """
CREATE VIEW daily_market_category_stats AS
WITH category_counts AS (
    SELECT
        ds.snapshot_date,
        ds.channel,
        cc.id AS canonical_category_id,
        COALESCE(cc.key, 'unmapped') AS canonical_category_key,
        COALESCE(cc.name, '未映射') AS canonical_category_name,
        COUNT(jo.job_id) AS posting_count,
        COUNT(DISTINCT s.company_id) AS covered_company_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    JOIN job_versions AS jv ON jv.id = jo.job_version_id
    JOIN source_categories AS sc ON sc.id = jv.source_category_id
    LEFT JOIN category_mappings AS cm
        ON cm.source_category_id = sc.id AND cm.is_current
    LEFT JOIN canonical_categories AS cc ON cc.id = cm.canonical_category_id
    GROUP BY
        ds.snapshot_date,
        ds.channel,
        cc.id,
        cc.key,
        cc.name
)
SELECT
    category_counts.*,
    1.0 * posting_count
        / NULLIF(
            SUM(posting_count) OVER (PARTITION BY snapshot_date, channel),
            0
        ) AS market_category_share
FROM category_counts
"""


MARKET_CITY_VIEW = """
CREATE VIEW daily_market_city_stats AS
WITH version_location_counts AS (
    SELECT job_version_id, COUNT(*) AS location_count
    FROM job_version_locations
    GROUP BY job_version_id
),
city_counts AS (
    SELECT
        ds.snapshot_date,
        ds.channel,
        cl.id AS canonical_location_id,
        cl.key AS canonical_location_key,
        cl.name AS city_name,
        cl.country_name,
        cl.state_name,
        COUNT(DISTINCT jo.job_id) AS posting_count,
        SUM(1.0 / vlc.location_count) AS fractional_posting_count,
        COUNT(DISTINCT s.company_id) AS covered_company_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    JOIN job_version_locations AS jvl ON jvl.job_version_id = jo.job_version_id
    JOIN version_location_counts AS vlc ON vlc.job_version_id = jo.job_version_id
    JOIN source_location_mappings AS slm
        ON slm.location_id = jvl.location_id AND slm.is_current
    JOIN canonical_locations AS cl ON cl.id = slm.canonical_location_id
    GROUP BY
        ds.snapshot_date,
        ds.channel,
        cl.id,
        cl.key,
        cl.name,
        cl.country_name,
        cl.state_name
)
SELECT
    city_counts.*,
    fractional_posting_count
        / NULLIF(
            SUM(fractional_posting_count) OVER (
                PARTITION BY snapshot_date, channel
            ),
            0
        ) AS fractional_share
FROM city_counts
"""


MARKET_TOPIC_VIEW = """
CREATE VIEW daily_market_topic_stats AS
SELECT
    ds.snapshot_date,
    ds.channel,
    t.id AS topic_id,
    t.taxonomy_version,
    t.key AS topic_key,
    t.name AS topic_name,
    COUNT(DISTINCT jo.job_id) AS active_posting_count,
    COUNT(
        DISTINCT CASE
            WHEN NOT ds.is_baseline
                AND j.first_canonical_seen_on = ds.snapshot_date
            THEN jo.job_id
            ELSE NULL
        END
    ) AS new_posting_count,
    COUNT(DISTINCT s.company_id) AS covered_company_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
JOIN jobs AS j ON j.id = jo.job_id
JOIN job_topic_mentions AS jtm ON jtm.job_version_id = jo.job_version_id
JOIN derivation_runs AS dr
    ON dr.id = jtm.derivation_run_id
    AND dr.status = 'success'
    AND dr.is_current
JOIN topics AS t ON t.id = jtm.topic_id
GROUP BY
    ds.snapshot_date,
    ds.channel,
    t.id,
    t.taxonomy_version,
    t.key,
    t.name
"""


def upgrade() -> None:
    op.execute(COMPANY_VIEW)
    op.execute(CATEGORY_VIEW)
    op.execute(CITY_VIEW)
    op.execute(TOPIC_VIEW)
    op.execute(MARKET_CATEGORY_VIEW)
    op.execute(MARKET_CITY_VIEW)
    op.execute(MARKET_TOPIC_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS daily_market_topic_stats")
    op.execute("DROP VIEW IF EXISTS daily_market_city_stats")
    op.execute("DROP VIEW IF EXISTS daily_market_category_stats")
    op.execute("DROP VIEW IF EXISTS daily_topic_stats")
    op.execute("DROP VIEW IF EXISTS daily_city_stats")
    op.execute("DROP VIEW IF EXISTS daily_category_stats")
    op.execute("DROP VIEW IF EXISTS daily_company_stats")
