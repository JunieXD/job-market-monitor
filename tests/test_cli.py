import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import job_market.cli as cli
from job_market.cli import category_summary, collection_hash, track_parsed_jobs
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    SourceCategoryRecord,
)


def test_category_summary_keeps_unclassified_and_multi_category_counts() -> None:
    def job(external_id: str, category_ids: list[str]) -> JobRecord:
        return JobRecord(
            source_key="example",
            external_id=external_id,
            source_url=f"https://example.com/jobs/{external_id}",
            company_name="示例公司",
            channel=Channel.EXPERIENCED,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title="示例岗位",
            description="描述",
            requirements="要求",
            locations=[LocationRecord(code="city:test", name="测试城")],
            categories=[
                SourceCategoryRecord(
                    external_id=category_id,
                    name=f"分类 {category_id}",
                    assignment_method=CategoryAssignmentMethod.FILTER_MEMBERSHIP,
                )
                for category_id in category_ids
            ],
            source_payload={"id": external_id},
        )

    result = CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[job("multi", ["a", "b"]), job("none", [])],
        snapshots=[],
        partition_counts={"all": 2},
        pages_fetched=1,
        complete=True,
    )

    assert category_summary(result) == {
        "classified_jobs": 1,
        "unclassified_jobs": 1,
        "multi_category_jobs": 1,
        "category_assignments": 2,
        "assignment_methods": {"filter_membership": 2},
    }


def test_kuaishou_keeps_initialization_resources_unblocked() -> None:
    assert cli.SOURCE_SPECS["kuaishou"]["block_nonessential_resources"] is False


def test_incomplete_collection_is_never_absence_authoritative() -> None:
    result = CollectionResult(
        channel=Channel.CAMPUS,
        jobs=[],
        snapshots=[],
        partition_counts={"all": 100, "collected-unique": 10},
        pages_fetched=1,
        complete=False,
    )

    assert result.absence_authoritative is False


def test_collection_hash_is_stable_across_job_order() -> None:
    def job(external_id: str, title: str) -> JobRecord:
        return JobRecord(
            source_key="example",
            external_id=external_id,
            source_url=f"https://example.test/{external_id}",
            company_name="示例公司",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=title,
            source_payload={"id": external_id},
        )

    first = CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[job("b", "岗位 B"), job("a", "岗位 A")],
        snapshots=[],
        partition_counts={"all": 2},
        pages_fetched=1,
        complete=True,
    )
    second = first.model_copy(update={"jobs": list(reversed(first.jobs))})

    assert collection_hash(first) == collection_hash(second)


def test_connector_progress_tracks_unique_parsed_jobs() -> None:
    class Connector:
        @staticmethod
        def parse_job(external_id: str) -> JobRecord:
            return JobRecord(
                source_key="example",
                external_id=external_id,
                source_url=f"https://example.test/{external_id}",
                company_name="示例公司",
                channel=Channel.EXPERIENCED,
                employment_type_id="social",
                employment_type_name="社会招聘",
                title="示例岗位",
                source_payload={"id": external_id},
            )

    connector = Connector()
    discovered_ids = track_parsed_jobs(connector)

    connector.parse_job("a")
    connector.parse_job("b")
    connector.parse_job("a")

    assert discovered_ids == {"a", "b"}


def test_source_summary_is_derived_from_registry(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["job-market", "list-sources", "--format", "summary"],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "companies": len(
            {spec["company_key"] for spec in cli.SOURCE_SPECS.values()}
        ),
        "sources": len(cli.SOURCE_SPECS),
        "channels": sum(
            len(spec["channels"]) for spec in cli.SOURCE_SPECS.values()
        ),
    }


class FakeRepository:
    def __init__(self, due_channels: set[str] | None = None) -> None:
        self.failed: list[tuple[str, str, list]] = []
        self.ingested: list[str] = []
        self.progress: list[tuple[str, int, int]] = []
        self.due_channels = due_channels

    def ensure_source(self, **kwargs) -> int:
        return 1

    def start_run(self, source_id: int, channel: str) -> str:
        return f"run-{channel}"

    def ingest(self, run_id: str, result: CollectionResult) -> dict[str, int]:
        self.ingested.append(run_id)
        return {"discovered": len(result.jobs)}

    def update_run_progress(
        self,
        run_id: str,
        *,
        discovered_count: int,
        page_count: int,
    ) -> bool:
        self.progress.append((run_id, discovered_count, page_count))
        return True

    def fail_run(
        self,
        run_id: str,
        error: str,
        snapshots: list,
        *,
        error_type: str | None = None,
    ) -> None:
        self.failed.append((run_id, error, snapshots))

    def due_source_channels(self) -> dict[str, set[str]]:
        channels = self.due_channels
        if channels is None:
            channels = {Channel.EXPERIENCED.value}
        return {"test_source": channels} if channels else {}


class SuccessfulConnector:
    def __init__(self, page, settings, raw_store) -> None:
        self.snapshots = []

    async def collect(self, channel: Channel, *, max_pages: int | None):
        return CollectionResult(
            channel=channel,
            jobs=[],
            snapshots=[],
            partition_counts={"all": 0},
            pages_fetched=1,
            complete=True,
        )


class TimeoutConnector(SuccessfulConnector):
    async def collect(self, channel: Channel, *, max_pages: int | None):
        raise TimeoutError("synthetic timeout")


class PartialConnector(SuccessfulConnector):
    async def collect(self, channel: Channel, *, max_pages: int | None):
        return CollectionResult(
            channel=channel,
            jobs=[],
            snapshots=[],
            partition_counts={"all": 1, "collected-unique": 0},
            pages_fetched=1,
            complete=False,
        )


async def run_crawl_case(
    monkeypatch,
    tmp_path: Path,
    connector_type,
    *,
    launch_error: Exception | None = None,
    context_close_error: Exception | None = None,
    due_only: bool = False,
    due_channels: set[str] | None = None,
    dry_run: bool = False,
    block_nonessential_resources: bool = True,
):
    repository = FakeRepository(due_channels)
    state: list[str] = []

    class Context:
        async def new_page(self):
            return object()

        async def close(self) -> None:
            state.append("context.close")
            if context_close_error is not None:
                raise context_close_error

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self) -> None:
            state.append("browser.close")

    class Chromium:
        async def launch(self, **kwargs):
            state.append("chromium.launch")
            if launch_error is not None:
                raise launch_error
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            state.append("playwright.stop")

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setitem(
        cli.SOURCE_SPECS,
        "test-source",
        {
            "key": "test_source",
            "company_key": "test_company",
            "company_name": "测试公司",
            "display_name": "测试招聘站",
            "source_type": "company_career_portal",
            "scope_name": "测试公司",
            "base_url": "https://example.test",
            "connector": connector_type,
            "block_nonessential_resources": block_nonessential_resources,
            "channels": {Channel.EXPERIENCED: "公开社会招聘岗位"},
        },
    )
    monkeypatch.setattr(cli, "make_engine", lambda settings: object())
    monkeypatch.setattr(cli, "create_schema", lambda engine: None)
    monkeypatch.setattr(cli, "Repository", lambda engine, missing: repository)
    monkeypatch.setattr(cli, "async_playwright", lambda: Starter())

    class NetworkMetrics:
        async def install_policy(self, context) -> None:
            state.append("network.install_policy")

        async def attach_page(self, page) -> None:
            return None

        def watch_new_pages(self, context) -> None:
            return None

        async def snapshot(self) -> dict[str, int]:
            return {}

    monkeypatch.setattr(cli, "BrowserNetworkMetrics", NetworkMetrics)

    args = argparse.Namespace(
        source="test-source",
        channel=Channel.EXPERIENCED.value,
        full=False,
        dry_run=dry_run,
        max_pages=None,
        timeout_seconds=60,
        due_only=due_only,
    )
    settings = SimpleNamespace(
        crawl_channel_timeout_seconds=60,
        missing_runs_before_close=2,
        raw_data_dir=tmp_path,
        headless=True,
        crawl_block_nonessential_resources=True,
        crawl_block_service_workers=True,
    )
    return await cli.crawl(args, settings), repository, state


async def test_channel_timeout_records_failed_run(monkeypatch, tmp_path: Path) -> None:
    exit_code, repository, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        TimeoutConnector,
    )

    assert exit_code == 1
    assert repository.ingested == []
    assert repository.failed[0][0] == "run-experienced"
    assert "exceeded 60 seconds" in repository.failed[0][1]
    assert state[-3:] == ["context.close", "browser.close", "playwright.stop"]


async def test_browser_startup_failure_records_failed_run(monkeypatch, tmp_path: Path) -> None:
    exit_code, repository, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        SuccessfulConnector,
        launch_error=RuntimeError("browser unavailable"),
    )

    assert exit_code == 1
    assert repository.ingested == []
    assert repository.failed[0][0] == "run-experienced"
    assert "browser unavailable" in repository.failed[0][1]
    assert state == ["chromium.launch", "playwright.stop"]


async def test_partial_channel_is_persisted_and_returns_degraded_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    exit_code, repository, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        PartialConnector,
    )

    assert exit_code == 2
    assert repository.ingested == ["run-experienced"]
    assert repository.failed == []
    assert state[-3:] == ["context.close", "browser.close", "playwright.stop"]


async def test_partial_dry_run_returns_degraded_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    exit_code, repository, _ = await run_crawl_case(
        monkeypatch,
        tmp_path,
        PartialConnector,
        dry_run=True,
    )

    assert exit_code == 2
    assert repository.ingested == []


async def test_source_can_keep_resources_required_by_its_frontend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    exit_code, _, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        SuccessfulConnector,
        block_nonessential_resources=False,
    )

    assert exit_code == 0
    assert "network.install_policy" not in state


async def test_cleanup_error_does_not_override_successful_run(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    exit_code, repository, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        SuccessfulConnector,
        context_close_error=RuntimeError("context cleanup failed"),
    )

    assert exit_code == 0
    assert repository.ingested == ["run-experienced"]
    assert repository.failed == []
    assert state[-3:] == ["context.close", "browser.close", "playwright.stop"]
    assert "cleanup_warnings" in capsys.readouterr().err


async def test_due_only_skips_source_without_missing_channels(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    exit_code, repository, state = await run_crawl_case(
        monkeypatch,
        tmp_path,
        SuccessfulConnector,
        due_only=True,
        due_channels=set(),
    )

    assert exit_code == 0
    assert repository.ingested == []
    assert state == []
    assert "all_channels_already_collected_today" in capsys.readouterr().out
