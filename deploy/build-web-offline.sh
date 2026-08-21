#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
BASE_IMAGE=${BASE_IMAGE:-job-market-monitor-web:latest}
TARGET_IMAGE=${TARGET_IMAGE:-job-market-monitor-web:candidate}
DOCKERFILE=${DOCKERFILE:-${PROJECT_DIR}/frontend/Dockerfile.offline}
EXPECTED_API_INTERNAL_URL=${EXPECTED_API_INTERNAL_URL:-http://api:8000}

fail() {
  printf 'offline Web build refused: %s\n' "$1" >&2
  exit 1
}

[[ -d "$PROJECT_DIR" ]] || fail "project directory does not exist: $PROJECT_DIR"
[[ -f "$DOCKERFILE" ]] || fail "offline Dockerfile does not exist: $DOCKERFILE"
[[ -f "$PROJECT_DIR/frontend/.next/standalone/server.js" ]] \
  || fail "frontend production build is missing; run npm run build first"
[[ -d "$PROJECT_DIR/frontend/.next/static" ]] \
  || fail "frontend static build output is missing"
grep -Fq "${EXPECTED_API_INTERNAL_URL}/api/:path*" \
  "$PROJECT_DIR/frontend/.next/standalone/.next/routes-manifest.json" \
  || fail "frontend build does not target ${EXPECTED_API_INTERNAL_URL}; rebuild it with API_INTERNAL_URL set"
[[ "$BASE_IMAGE" != "$TARGET_IMAGE" ]] || fail "base and target images must be different"
"$DOCKER_BIN" image inspect "$BASE_IMAGE" >/dev/null 2>&1 \
  || fail "local base image is missing: $BASE_IMAGE"

BUILD_CONTEXT=$(mktemp -d "${TMPDIR:-/tmp}/job-market-web-offline.XXXXXX")
trap 'rm -rf -- "$BUILD_CONTEXT"' EXIT
mkdir -p "$BUILD_CONTEXT/public" "$BUILD_CONTEXT/.next/standalone/.next" \
  "$BUILD_CONTEXT/.next/static"
cp -a "$PROJECT_DIR/frontend/public/." "$BUILD_CONTEXT/public/"
cp -a "$PROJECT_DIR/frontend/.next/standalone/server.js" \
  "$PROJECT_DIR/frontend/.next/standalone/package.json" \
  "$BUILD_CONTEXT/.next/standalone/"
cp -a "$PROJECT_DIR/frontend/.next/standalone/.next/." \
  "$BUILD_CONTEXT/.next/standalone/.next/"
cp -a "$PROJECT_DIR/frontend/.next/static/." "$BUILD_CONTEXT/.next/static/"
cp "$DOCKERFILE" "$BUILD_CONTEXT/Dockerfile"

exec "$DOCKER_BIN" build \
  --pull=false \
  --network=none \
  --file "$BUILD_CONTEXT/Dockerfile" \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --tag "$TARGET_IMAGE" \
  "$BUILD_CONTEXT"
