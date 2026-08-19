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
done
case "$command_name" in
  list-sources)
    printf 'alpha\\nbeta\\n'
    ;;
  crawl)
    printf '%s\\n' "$source_name" >> "$FAKE_LOG"
    if [[ "$source_name" == "alpha" && "$FAIL_MODE" == "always" ]]; then
      exit 1
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
        "MAX_ATTEMPTS": str(attempts),
        "RETRY_DELAY_SECONDS": "0",
        "INTER_SOURCE_DELAY_SECONDS": "0",
    }
    result = subprocess.run(
        ["bash", str(SCHEDULER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    return result, calls


def test_scheduler_retries_failed_source_then_continues(tmp_path) -> None:
    result, calls = _run_scheduler(tmp_path, fail_mode="once", attempts=2)

    assert result.returncode == 0
    assert calls == ["alpha", "recover:alpha", "alpha", "beta"]
    assert '"event":"batch_finished"' in result.stdout


def test_scheduler_attempts_later_sources_before_reporting_failure(tmp_path) -> None:
    result, calls = _run_scheduler(tmp_path, fail_mode="always", attempts=1)

    assert result.returncode == 1
    assert calls == ["alpha", "recover:alpha", "beta"]
    assert "source:alpha" in result.stdout


def test_scheduler_removes_timed_out_container_then_continues(tmp_path) -> None:
    result, calls = _run_scheduler(
        tmp_path,
        fail_mode="never",
        attempts=1,
        timeout_source="alpha",
    )

    assert result.returncode == 1
    assert calls == [
        "cleanup:job-market-monitor-crawl-alpha",
        "recover:alpha",
        "beta",
    ]
    assert "source:alpha" in result.stdout
