#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
BASE_IMAGE=${BASE_IMAGE:-job-market-monitor-api:latest}
TARGET_IMAGE=${TARGET_IMAGE:-job-market-monitor-api:candidate}
DOCKERFILE=${DOCKERFILE:-${PROJECT_DIR}/deploy/Dockerfile.api-offline}

fail() {
  printf 'offline API build refused: %s\n' "$1" >&2
  exit 1
}

[[ -d "$PROJECT_DIR" ]] || fail "project directory does not exist: $PROJECT_DIR"
[[ -f "$DOCKERFILE" ]] || fail "offline Dockerfile does not exist: $DOCKERFILE"
[[ "$BASE_IMAGE" != "$TARGET_IMAGE" ]] || fail "base and target images must be different"
"$DOCKER_BIN" image inspect "$BASE_IMAGE" >/dev/null 2>&1 \
  || fail "local base image is missing: $BASE_IMAGE"
RUNTIME_USER=$("$DOCKER_BIN" image inspect \
  --format '{{.Config.User}}' "$BASE_IMAGE")
[[ -n "$RUNTIME_USER" && "$RUNTIME_USER" != "root" ]] \
  || fail "base image must declare a non-root runtime user"

exec "$DOCKER_BIN" build \
  --pull=false \
  --network=none \
  --file "$DOCKERFILE" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "RUNTIME_USER=$RUNTIME_USER" \
  --tag "$TARGET_IMAGE" \
  "$PROJECT_DIR"
