from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.models import CrawlRun, Source, SourceChannel


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SourceHealthChecker:
    """Report whether every enabled source channel is current and succeeding."""

    def __init__(
        self,
        engine: Engine,
        *,
        stale_after: timedelta,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self.engine = engine
        self.stale_after = stale_after
        self.clock = clock

    def run(self) -> dict[str, object]:
        now = _aware_utc(self.clock())
        channels: list[dict[str, object]] = []
        with Session(self.engine) as session:
            active_channels = session.execute(
                select(Source, SourceChannel)
                .join(SourceChannel, SourceChannel.source_id == Source.id)
                .where(
                    Source.enabled.is_(True),
                    SourceChannel.status == "active",
                )
                .order_by(Source.key, SourceChannel.channel)
            ).all()
            for source, source_channel in active_channels:
                runs = session.scalars(
                    select(CrawlRun)
                    .where(
                        CrawlRun.source_id == source.id,
                        CrawlRun.channel == source_channel.channel,
                    )
                    .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
                ).all()
                channels.append(
                    self._channel_health(
                        source,
                        source_channel,
                        runs,
                        now,
                    )
                )

        unhealthy = [item for item in channels if item["status"] != "healthy"]
        return {
            "ok": not unhealthy,
            "checked_at": now.isoformat(),
            "stale_after_hours": self.stale_after.total_seconds() / 3600,
            "active_channels": len(channels),
            "unhealthy_channels": len(unhealthy),
            "channels": channels,
        }

    def _channel_health(
        self,
        source: Source,
        source_channel: SourceChannel,
        runs: list[CrawlRun],
        now: datetime,
    ) -> dict[str, object]:
        latest = runs[0] if runs else None
        latest_success = next(
            (
                run
                for run in runs
                if (
                    run.status == "success"
                    and run.complete is True
                    and run.absence_authoritative is True
                )
            ),
            None,
        )
        reasons: list[str] = []
        if latest is None:
            reasons.append("never_attempted")
        elif latest.status != "success" or not latest.complete:
            reasons.append(f"latest_attempt_{latest.status}")
        elif not latest.absence_authoritative:
            reasons.append("latest_attempt_non_authoritative")

        success_at: datetime | None = None
        age_hours: float | None = None
        if latest_success is None:
            reasons.append("never_authoritative")
        else:
            success_at = _aware_utc(
                latest_success.finished_at or latest_success.started_at
            )
            age = max(now - success_at, timedelta(0))
            age_hours = round(age.total_seconds() / 3600, 3)
            if age > self.stale_after:
                reasons.append("stale")

        consecutive_failures = 0
        for run in runs:
            if run.status == "success" and run.complete is True:
                break
            consecutive_failures += 1

        consecutive_non_authoritative = 0
        for run in runs:
            if (
                run.status == "success"
                and run.complete is True
                and run.absence_authoritative is True
            ):
                break
            consecutive_non_authoritative += 1

        return {
            "source": source.key,
            "channel": source_channel.channel,
            "status": "healthy" if not reasons else "unhealthy",
            "reasons": reasons,
            "latest_attempt_status": latest.status if latest is not None else None,
            "latest_attempt_at": (
                _aware_utc(latest.started_at).isoformat()
                if latest is not None
                else None
            ),
            "last_success_at": success_at.isoformat() if success_at else None,
            "last_success_age_hours": age_hours,
            "consecutive_failures": consecutive_failures,
            "consecutive_non_authoritative": consecutive_non_authoritative,
        }
