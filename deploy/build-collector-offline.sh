#!/usr/bin/env bash

# Build a collector image from an already verified local image. This script
# must stay network-independent: it refuses a missing base and passes both
# --pull=false and --network=none to Docker.
set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
BASE_IMAGE=${BASE_IMAGE:-job-market-monitor-collector:vm-base}
TARGET_IMAGE=${TARGET_IMAGE:-job-market-monitor-collector:latest}
DOCKERFILE=${DOCKERFILE:-${PROJECT_DIR}/deploy/Dockerfile.offline}

fail() {
  printf 'offline collector build refused: %s\n' "$1" >&2
  exit 1
}

[[ -d "$PROJECT_DIR" ]] || fail "project directory does not exist: $PROJECT_DIR"
[[ -f "$DOCKERFILE" ]] || fail "offline Dockerfile does not exist: $DOCKERFILE"
[[ "$BASE_IMAGE" != "$TARGET_IMAGE" ]] || fail "base and target images must be different"

if ! "$DOCKER_BIN" image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  fail "local base image is missing: $BASE_IMAGE (build and verify it once with Dockerfile first)"
fi

exec "$DOCKER_BIN" build \
  --pull=false \
  --network=none \
  --file "$DOCKERFILE" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --tag "$TARGET_IMAGE" \
  "$PROJECT_DIR"
