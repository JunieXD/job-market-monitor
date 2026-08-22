import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
OFFLINE_BUILD = ROOT / "deploy" / "build-collector-offline.sh"
DERIVATION_SCHEDULER = ROOT / "deploy" / "run-scheduled-derivations.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _collector_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("  collector:", maxsplit=1)[1].split("  api:", maxsplit=1)[0]


def test_runtime_compose_is_image_only_for_collector() -> None:
    for compose in (ROOT / "compose.yaml", ROOT / "compose.production.yaml"):
        collector = _collector_block(compose)
        assert "image:" in collector
        assert "build:" not in collector


def test_derivation_scheduler_never_builds_or_pulls_images() -> None:
    text = DERIVATION_SCHEDULER.read_text(encoding="utf-8")
    assert "deriver derive-jobs" in text
    assert "--pull never" in text
    assert "image inspect" in text
    assert " compose build" not in text
    assert "--build" not in text


def test_offline_dockerfile_has_no_network_dependency_install() -> None:
    text = (ROOT / "deploy" / "Dockerfile.offline").read_text(encoding="utf-8")
    assert "--no-deps" in text
    assert "--no-build-isolation" in text
    assert "playwright install" not in text


def test_api_dockerfile_does_not_install_chromium() -> None:
    text = (ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    assert "playwright install" not in text
    assert "--with-deps" not in text


def test_collector_network_build_requires_explicit_opt_in() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ALLOW_NETWORK_BUILD" in text
    assert 'test "$ALLOW_NETWORK_BUILD" = "1"' in text
    assert "playwright install --with-deps chromium" in text


def test_offline_build_refuses_missing_base_image(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    _write_executable(
        docker,
        """#!/usr/bin/env bash
set -u
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  exit 1
fi
exit 99
""",
    )
    result = subprocess.run(
        ["bash", str(OFFLINE_BUILD)],
        env={
            **os.environ,
            "DOCKER_BIN": str(docker),
            "PROJECT_DIR": str(ROOT),
            "BASE_IMAGE": "missing:local",
            "TARGET_IMAGE": "candidate:local",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local base image is missing" in result.stderr


def test_offline_build_forces_no_pull_and_no_network(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    calls = tmp_path / "calls"
    _write_executable(
        docker,
        f"""#!/usr/bin/env bash
set -u
if [[ "${{1:-}}" == "image" && "${{2:-}}" == "inspect" ]]; then
  exit 0
fi
printf '%s\\n' "$*" >> {calls}
""",
    )
    result = subprocess.run(
        ["bash", str(OFFLINE_BUILD)],
        env={
            **os.environ,
            "DOCKER_BIN": str(docker),
            "PROJECT_DIR": str(ROOT),
            "BASE_IMAGE": "verified:local",
            "TARGET_IMAGE": "candidate:local",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    command = calls.read_text(encoding="utf-8")
    assert "build" in command
    assert "--pull=false" in command
    assert "--network=none" in command
    assert "--build-arg BASE_IMAGE=verified:local" in command
