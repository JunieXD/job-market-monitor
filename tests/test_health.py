from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from job_market.health import SourceHealthChecker
from job_market.models import Base
from job_market.repository import Repository
from job_market.schemas import Channel, CollectionResult


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def empty_result() -> CollectionResult:
    return CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[],
        snapshots=[],
        partition_counts={"all": 0},
        pages_fetched=1,
        complete=True,
    )


def make_repository() -> tuple[Repository, Clock]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    clock = Clock()
    repository = Repository(engine, clock=clock)
    repository.ensure_source(channels={Channel.EXPERIENCED.value: "test"})
    return repository, clock


def test_source_health_reports_never_attempted_channel() -> None:
    repository, clock = make_repository()

    report = SourceHealthChecker(
        repository.engine,
        stale_after=timedelta(hours=36),
        clock=clock,
    ).run()

    assert report["ok"] is False
    assert report["unhealthy_channels"] == 1
    channel = report["channels"][0]
    assert channel["reasons"] == ["never_attempted", "never_authoritative"]


def test_source_health_tracks_latest_failure_and_staleness() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source(
        channels={Channel.EXPERIENCED.value: "test"}
    )
    successful_run = repository.start_run(source_id, Channel.EXPERIENCED.value)
    repository.ingest(successful_run, empty_result())

    clock.value += timedelta(hours=1)
    failed_run = repository.start_run(source_id, Channel.EXPERIENCED.value)
    repository.fail_run(failed_run, "synthetic failure")
    report = SourceHealthChecker(
        repository.engine,
        stale_after=timedelta(hours=36),
        clock=clock,
    ).run()
    channel = report["channels"][0]
    assert channel["reasons"] == ["latest_attempt_failed"]
    assert channel["consecutive_failures"] == 1

    clock.value += timedelta(hours=40)
    stale = SourceHealthChecker(
        repository.engine,
        stale_after=timedelta(hours=36),
        clock=clock,
    ).run()["channels"][0]
    assert stale["reasons"] == ["latest_attempt_failed", "stale"]


def test_source_health_reports_latest_non_authoritative_walk() -> None:
    repository, clock = make_repository()
    source_id = repository.ensure_source(
        channels={Channel.EXPERIENCED.value: "test"}
    )
    first_run = repository.start_run(source_id, Channel.EXPERIENCED.value)
    repository.ingest(first_run, empty_result())

    clock.value += timedelta(hours=1)
    live_walk = empty_result().model_copy(update={"absence_authoritative": False})
    second_run = repository.start_run(source_id, Channel.EXPERIENCED.value)
    repository.ingest(second_run, live_walk)

    channel = SourceHealthChecker(
        repository.engine,
        stale_after=timedelta(hours=36),
        clock=clock,
    ).run()["channels"][0]
    assert channel["reasons"] == ["latest_attempt_non_authoritative"]
    assert channel["consecutive_failures"] == 0
    assert channel["consecutive_non_authoritative"] == 1
