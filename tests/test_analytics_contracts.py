from datetime import date

from job_market.analytics_contracts import AnalyticsEnvelope, CoverageSummary


def test_analytics_envelope_requires_coverage_and_metric_definition() -> None:
    envelope = AnalyticsEnvelope(
        data=[{"active_posting_count": 3}],
        meta={
            "snapshot_date": date(2026, 8, 19),
            "filters": {"channel": "campus"},
            "coverage": CoverageSummary(
                snapshot_date=date(2026, 8, 19),
                configured_source_channel_count=4,
                standard_snapshot_count=3,
                successful_source_channel_count=3,
                absence_authoritative_source_channel_count=2,
                non_authoritative_successful_run_count=1,
                failed_run_count=0,
                coverage_ratio=0.75,
            ),
            "metric_definition": "active_posting_count",
        },
    )

    assert envelope.meta.timezone == "Asia/Shanghai"
    assert envelope.meta.coverage.coverage_ratio == 0.75
