import os
import subprocess
from pathlib import Path

BENCHMARK = Path(__file__).parents[1] / "deploy" / "run-concurrency-benchmark.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_benchmark_records_parallel_outputs_and_stats(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    timeout = tmp_path / "timeout"
    _write_executable(
        docker,
        """#!/usr/bin/env bash
set -u
case "${1:-}" in
  stats)
    printf 'benchmark-alpha\\t10%%\\t100MiB / 1GiB\\t1MB / 100kB\\n'
    ;;
  inspect)
    printf 'true\\n'
    ;;
  rm)
    ;;
  compose)
    sleep 0.3
    printf '{"jobs": 1, "complete": false}\\n'
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
exec "$@"
""",
    )
    output_dir = tmp_path / "output"
    env = {
        **os.environ,
        "PROJECT_DIR": str(tmp_path),
        "COMPOSE_FILE": str(tmp_path / "compose.yaml"),
        "DOCKER_BIN": str(docker),
        "TIMEOUT_BIN": str(timeout),
        "BENCHMARK_OUTPUT_DIR": str(output_dir),
        "BENCHMARK_RUN_ID": "test-run",
        "BENCHMARK_PARALLEL": "2",
        "BENCHMARK_MAX_PAGES": "1",
        "BENCHMARK_SOURCES": "alpha:experienced beta:general",
    }

    result = subprocess.run(
        ["bash", str(BENCHMARK)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    run_dir = output_dir / "test-run-p2"
    assert result.returncode == 0
    assert (run_dir / "alpha.exit").read_text(encoding="utf-8").strip() == "0"
    assert (run_dir / "beta.exit").read_text(encoding="utf-8").strip() == "0"
    assert '"jobs": 1' in (run_dir / "alpha.jsonl").read_text(encoding="utf-8")
    stats = (run_dir / "docker-stats.tsv").read_text(encoding="utf-8").splitlines()
    assert stats[0] == "time\tname\tcpu\tmemory\tnet_io"
    assert len(stats) > 1
    assert "parallel=2" in (run_dir / "summary.txt").read_text(encoding="utf-8")
