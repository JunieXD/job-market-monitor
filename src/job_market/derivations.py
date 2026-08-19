import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, tuple_, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.models import (
    DerivationRun,
    JobDerivedAttribute,
    JobTopicMention,
    JobVersion,
    Topic,
)
from job_market.schemas import DerivedAttributeRecord, TopicMentionRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DerivationRepository:
    def __init__(
        self,
        engine: Engine,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.engine = engine
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Derivation clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def start_run(
        self,
        *,
        kind: str,
        extractor_name: str,
        extractor_version: str,
        config: dict | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        with Session(self.engine) as session, session.begin():
            session.add(
                DerivationRun(
                    id=run_id,
                    kind=kind,
                    extractor_name=extractor_name,
                    extractor_version=extractor_version,
                    status="running",
                    is_current=False,
                    config=config or {},
                    started_at=self._now(),
                )
            )
        return run_id

    def add_topic_mentions(
        self,
        run_id: str,
        mentions: Iterable[TopicMentionRecord],
    ) -> int:
        records = list(mentions)
        with Session(self.engine) as session, session.begin():
            run = self._require_running_run(session, run_id)
            del run
            version_ids = {record.job_version_id for record in records}
            self._validate_versions(session, version_ids)
            topic_keys = {
                (record.taxonomy_version, record.topic_key) for record in records
            }
            topics = {
                (topic.taxonomy_version, topic.key): topic.id
                for topic in session.scalars(
                    select(Topic).where(
                        tuple_(Topic.taxonomy_version, Topic.key).in_(topic_keys)
                    )
                )
            }
            missing_topics = topic_keys - topics.keys()
            if missing_topics:
                raise ValueError(f"Unknown topics: {sorted(missing_topics)}")
            for record in records:
                session.add(
                    JobTopicMention(
                        job_version_id=record.job_version_id,
                        topic_id=topics[(record.taxonomy_version, record.topic_key)],
                        derivation_run_id=run_id,
                        relevance=record.relevance,
                        confidence=Decimal(str(record.confidence)),
                        matched_fields=record.matched_fields,
                        evidence=[
                            item.model_dump(mode="json") for item in record.evidence
                        ],
                        created_at=self._now(),
                    )
                )
        return len(records)

    def add_attributes(
        self,
        run_id: str,
        attributes: Iterable[DerivedAttributeRecord],
    ) -> int:
        records = list(attributes)
        with Session(self.engine) as session, session.begin():
            self._require_running_run(session, run_id)
            self._validate_versions(
                session,
                {record.job_version_id for record in records},
            )
            for record in records:
                session.add(
                    JobDerivedAttribute(
                        job_version_id=record.job_version_id,
                        attribute_key=record.attribute_key,
                        value=record.value,
                        derivation_run_id=run_id,
                        confidence=Decimal(str(record.confidence)),
                        evidence=[
                            item.model_dump(mode="json") for item in record.evidence
                        ],
                        created_at=self._now(),
                    )
                )
        return len(records)

    def complete_run(self, run_id: str, *, publish: bool = True) -> None:
        with Session(self.engine) as session, session.begin():
            run = self._require_running_run(session, run_id)
            if publish:
                session.execute(
                    update(DerivationRun)
                    .where(
                        DerivationRun.extractor_name == run.extractor_name,
                        DerivationRun.id != run.id,
                        DerivationRun.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
            run.status = "success"
            run.is_current = publish
            run.finished_at = self._now()

    def fail_run(self, run_id: str, error: str) -> None:
        with Session(self.engine) as session, session.begin():
            run = session.get(DerivationRun, run_id)
            if run is None:
                raise ValueError(f"Unknown derivation run: {run_id}")
            run.status = "failed"
            run.is_current = False
            run.finished_at = self._now()
            run.error = error[:10000]

    @staticmethod
    def _require_running_run(session: Session, run_id: str) -> DerivationRun:
        run = session.scalar(
            select(DerivationRun)
            .where(DerivationRun.id == run_id)
            .with_for_update()
        )
        if run is None:
            raise ValueError(f"Unknown derivation run: {run_id}")
        if run.status != "running":
            raise ValueError(f"Derivation run is not running: {run_id}")
        return run

    @staticmethod
    def _validate_versions(session: Session, version_ids: set[int]) -> None:
        if not version_ids:
            return
        existing = set(
            session.scalars(select(JobVersion.id).where(JobVersion.id.in_(version_ids)))
        )
        missing = version_ids - existing
        if missing:
            raise ValueError(f"Unknown job versions: {sorted(missing)}")
