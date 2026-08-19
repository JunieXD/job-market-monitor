import json
import os
import subprocess
from pathlib import Path

SCHEDULER = Path(__file__).parents[1] / "deploy" / "run-scheduled-crawls.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    docker = tmp_path / "docker"
    timeout = tmp_path / "timeout"
    flock = tmp_path / "flock"
    _write_executable(
        docker,
        """#!/usr/bin/env bash
set -u
if [[ " $* " == *" config --quiet "* ]]; then
  exit 0
fi
if [[ "${1:-}" == "container" && "${2:-}" == "inspect" ]]; then
  [[ -e "$FAKE_CONTAINER_STATE" ]]
  exit $?
fi
if [[ "${1:-}" == "rm" && "${2:-}" == "-f" ]]; then
  printf 'cleanup:%s\n' "${3:-}" >> "$FAKE_LOG"
  rm -f "$FAKE_CONTAINER_STATE"
  exit 0
fi
command_name=""
source_name=""
due_only=false
for ((i=1; i<=$#; i++)); do
  value=${!i}
  if [[ "$value" == "collector" ]]; then
    next=$((i + 1))
    command_name=${!next}
  fi
  if [[ "$value" == "--source" ]]; then
    next=$((i + 1))
    source_name=${!next}
  fi
  if [[ "$value" == "--due-only" ]]; then
    due_only=true
  fi
done
case "$command_name" in
  list-sources)
    printf '%b' "$FAKE_SOURCES"
    ;;
  crawl)
    if [[ "$due_only" != true ]]; then
      exit 98
    fi
    if [[ "$TRACE_CONCURRENCY" == "true" ]]; then
      printf 'start:%s\\n' "$source_name" >> "$FAKE_LOG"
      sleep "$CRAWL_SLEEP_SECONDS"
      printf 'end:%s\\n' "$source_name" >> "$FAKE_LOG"
    else
      printf '%s\\n' "$source_name" >> "$FAKE_LOG"
    fi
    if [[ "$source_name" == "alpha" && "$FAIL_MODE" == "always" ]]; then
      exit 1
    fi
    if [[ "$source_name" == "alpha" && "$FAIL_MODE" == "partial" ]]; then
      exit 2
    fi
    if [[ "$source_name" == "alpha" && "$FAIL_MODE" == "once" && ! -e "$FAKE_STATE" ]]; then
      : > "$FAKE_STATE"
      exit 1
    fi
    ;;
  recover-runs)
    if [[ -n "$source_name" ]]; then
      printf 'recover:%s\\n' "$source_name" >> "$FAKE_LOG"
    fi
    if [[ "$RECOVERY_FAIL" == "true" && -n "$source_name" ]]; then
      exit 1
    fi
    ;;
  check-data)
    if [[ -n "$POSTFLIGHT_ERROR" ]]; then
      printf '%s\n' "$POSTFLIGHT_ERROR" >&2
      exit 1
    fi
    ;;
esac
""",
    )
    _write_executable(
        timeout,
        """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground|--signal=*|--kill-after=*) shift ;;
    *s) shift; break ;;
    *) break ;;
  esac
done
source_name=""
for ((i=1; i<=$#; i++)); do
  value=${!i}
  if [[ "$value" == "--source" ]]; then
    next=$((i + 1))
    source_name=${!next}
  fi
done
if [[ -n "$TIMEOUT_SOURCE" && "$source_name" == "$TIMEOUT_SOURCE" ]]; then
  : > "$FAKE_CONTAINER_STATE"
  exit 124
fi
exec "$@"
""",
    )
    _write_executable(flock, "#!/usr/bin/env bash\nexit 0\n")
    return docker, timeout, flock


def _run_scheduler(
    tmp_path: Path,
    *,
    fail_mode: str,
    attempts: int,
    timeout_source: str = "",
    max_parallel: int = 1,
    fake_sources: str = "alpha\nbeta\n",
    trace_concurrency: bool = False,
    postflight_error: str = "",
):
    docker, timeout, flock = _fake_tools(tmp_path)
    compose_file = tmp_path / "compose.production.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env = {
        **os.environ,
        "PROJECT_DIR": str(tmp_path),
        "COMPOSE_FILE": str(compose_file),
        "DOCKER_BIN": str(docker),
        "TIMEOUT_BIN": str(timeout),
        "FLOCK_BIN": str(flock),
        "CRAWL_LOCK_FILE": str(tmp_path / "crawl.lock"),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_STATE": str(tmp_path / "state"),
        "FAKE_CONTAINER_STATE": str(tmp_path / "container-state"),
        "FAIL_MODE": fail_mode,
        "TIMEOUT_SOURCE": timeout_source,
        "FAKE_SOURCES": fake_sources,
        "TRACE_CONCURRENCY": str(trace_concurrency).lower(),
        "RECOVERY_FAIL": os.getenv("RECOVERY_FAIL", "false"),
        "POSTFLIGHT_ERROR": postflight_error,
        "CRAWL_SLEEP_SECONDS": "0.2",
        "MAX_ATTEMPTS": str(attempts),
        "MAX_PARALLEL_SOURCES": str(max_parallel),
        "RETRY_DELAY_SECONDS": "0",
        "SOURCE_START_DELAY_SECONDS": "0",
    }
    result = subprocess.run(
        ["bash", str(SCHEDULER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    log_path = tmp_path / "calls.log"
    calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return result, calls


def test_scheduler_retries_failed_source_then_continues(tmp_path) -> None:
    result, calls = _run_scheduler(tmp_path, fail_mode="once", attempts=2)

    assert result.returncode == 0
    assert calls == ["alpha", "recover:alpha", "alpha", "beta"]
    assert '"event":"batch_finished"' in result.stdout


def test_scheduler_attempts_later_sources_before_reporting_failure(tmp_path) -> None:
    result, calls = _run_scheduler(tmp_path, fail_mode="always", attempts=1)

    assert result.returncode == 2
    assert calls == ["alpha", "recover:alpha", "beta"]
    assert "source:alpha" in result.stdout


def test_scheduler_removes_timed_out_container_then_continues(tmp_path) -> None:
    result, calls = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        timeout_source="alpha",
    )

    assert result.returncode == 2
    assert calls == [
        "cleanup:job-market-monitor-crawl-alpha",
        "recover:alpha",
        "beta",
    ]
    assert "source:alpha" in result.stdout


def test_scheduler_reports_partial_source_as_degraded(tmp_path) -> None:
    result, calls = _run_scheduler(tmp_path, fail_mode="partial", attempts=1)

    assert result.returncode == 2
    assert calls == ["alpha", "recover:alpha", "beta"]
    assert "partial=source:alpha" in result.stdout


def test_scheduler_runs_sources_up_to_configured_parallelism(tmp_path) -> None:
    result, calls = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        max_parallel=2,
        fake_sources="alpha\nbeta\ngamma\n",
        trace_concurrency=True,
    )

    active: set[str] = set()
    peak = 0
    for call in calls:
        event, source = call.split(":", maxsplit=1)
        if event == "start":
            active.add(source)
            peak = max(peak, len(active))
        else:
            active.remove(source)

    assert result.returncode == 0
    assert peak == 2
    assert active == set()


def test_scheduler_failure_isolated_when_sources_run_in_parallel(tmp_path) -> None:
    result, calls = _run_scheduler(
        tmp_path,
        fail_mode="always",
        attempts=1,
        max_parallel=2,
        fake_sources="alpha\nbeta\ngamma\n",
    )

    assert result.returncode == 2
    assert {call for call in calls if not call.startswith("recover:")} == {
        "alpha",
        "beta",
        "gamma",
    }
    assert "source:alpha" in result.stdout


def test_scheduler_success_after_retry_ignores_recovery_warning(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RECOVERY_FAIL", "true")
    result, calls = _run_scheduler(tmp_path, fail_mode="once", attempts=2)

    assert result.returncode == 0
    assert calls == ["alpha", "recover:alpha", "alpha", "beta"]
    assert "source_recovery_warning" in result.stdout


def test_scheduler_rejects_invalid_parallelism(tmp_path) -> None:
    result, calls = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        max_parallel=0,
    )

    assert result.returncode == 1
    assert calls == []
    assert "max_parallel_sources_must_be_a_positive_integer" in result.stdout


def test_scheduler_writes_bounded_file_log(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "logs" / "crawl.jsonl"
    monkeypatch.setenv("CRAWL_LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "2")

    result, _ = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        fake_sources="alpha\n",
    )

    assert result.returncode == 0
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert events[0]["event"] == "batch_started"
    assert events[0]["batch_id"] == events[-1]["batch_id"]
    assert events[-1]["event"] == "batch_finished"


def test_scheduler_log_is_valid_json_for_paths_and_control_characters(
    tmp_path,
    monkeypatch,
) -> None:
    log_file = tmp_path / "logs" / "crawl.jsonl"
    error = 'path=/tmp/a\\b\t"quoted"\r'
    monkeypatch.setenv("CRAWL_LOG_FILE", str(log_file))

    result, _ = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        fake_sources="alpha\n",
        postflight_error=error,
    )

    assert result.returncode == 1
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    failed = next(event for event in events if event["event"] == "postflight_check_failed")
    assert failed["detail"] == f"check-data:{error}"
