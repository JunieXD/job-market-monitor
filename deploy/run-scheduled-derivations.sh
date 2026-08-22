#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-/opt/job-market-monitor}
COMPOSE_FILE=${COMPOSE_FILE:-${PROJECT_DIR}/compose.production.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
TIMEOUT_BIN=${TIMEOUT_BIN:-/usr/bin/timeout}
FLOCK_BIN=${FLOCK_BIN:-/usr/bin/flock}
DERIVATION_LOCK_FILE=${DERIVATION_LOCK_FILE:-/run/lock/job-market-monitor-derive.lock}
DERIVATION_CONTAINER_NAME=${DERIVATION_CONTAINER_NAME:-job-market-monitor-derive}
DERIVATION_BATCH_LIMIT=${DERIVATION_BATCH_LIMIT:-100}
DERIVATION_TIMEOUT_SECONDS=${DERIVATION_TIMEOUT_SECONDS:-21600}

fail() {
  printf 'scheduled derivation refused: %s\n' "$1" >&2
  exit 1
}

[[ -d "$PROJECT_DIR" ]] || fail "project directory does not exist: $PROJECT_DIR"
[[ -f "$COMPOSE_FILE" ]] || fail "compose file does not exist: $COMPOSE_FILE"
[[ "$DERIVATION_BATCH_LIMIT" =~ ^[1-9][0-9]*$ ]] \
  || fail "DERIVATION_BATCH_LIMIT must be a positive integer"
[[ "$DERIVATION_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "DERIVATION_TIMEOUT_SECONDS must be a positive integer"

cd "$PROJECT_DIR"
exec 9>"$DERIVATION_LOCK_FILE"
"$FLOCK_BIN" --nonblock 9 || fail "another derivation batch is running"

compose=("$DOCKER_BIN" compose -f "$COMPOSE_FILE")
"${compose[@]}" config --quiet || fail "compose configuration is invalid"
"$DOCKER_BIN" image inspect \
  "${COLLECTOR_IMAGE:-job-market-monitor-collector:latest}" >/dev/null 2>&1 \
  || fail "collector image is missing"

exit_code=0
"$TIMEOUT_BIN" \
  --foreground \
  --signal=TERM \
  --kill-after=30s \
  "${DERIVATION_TIMEOUT_SECONDS}s" \
  "${compose[@]}" run --pull never --rm --no-deps \
  --name "$DERIVATION_CONTAINER_NAME" \
  deriver derive-jobs --limit "$DERIVATION_BATCH_LIMIT" \
  || exit_code=$?

if "$DOCKER_BIN" container inspect "$DERIVATION_CONTAINER_NAME" >/dev/null 2>&1; then
  "$DOCKER_BIN" rm -f "$DERIVATION_CONTAINER_NAME" >/dev/null
fi
exit "$exit_code"
