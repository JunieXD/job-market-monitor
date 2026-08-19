#!/usr/bin/env bash

set -uo pipefail

PROJECT_DIR=${PROJECT_DIR:-/opt/job-market-monitor}
COMPOSE_FILE=${COMPOSE_FILE:-${PROJECT_DIR}/compose.production.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
TIMEOUT_BIN=${TIMEOUT_BIN:-/usr/bin/timeout}
FLOCK_BIN=${FLOCK_BIN:-/usr/bin/flock}
CRAWL_LOCK_FILE=${CRAWL_LOCK_FILE:-/run/lock/job-market-monitor-crawl.lock}
CRAWL_CONTAINER_PREFIX=${CRAWL_CONTAINER_PREFIX:-job-market-monitor-crawl}
SOURCE_TIMEOUT_SECONDS=${SOURCE_TIMEOUT_SECONDS:-10800}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-2}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-60}
INTER_SOURCE_DELAY_SECONDS=${INTER_SOURCE_DELAY_SECONDS:-10}

log_event() {
  local event=$1
  local detail=${2:-}
  printf '{"time":"%s","event":"%s","detail":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$detail"
}

if [[ ! -d "$PROJECT_DIR" ]]; then
  log_event "batch_preflight_failed" "project_directory_missing"
  exit 1
fi
cd "$PROJECT_DIR" || exit 1

exec 9>"$CRAWL_LOCK_FILE"
if ! "$FLOCK_BIN" --nonblock 9; then
  log_event "batch_preflight_failed" "another_batch_is_running"
  exit 1
fi

compose=("$DOCKER_BIN" compose -f "$COMPOSE_FILE")

run_container() {
  "${compose[@]}" run --rm --no-deps collector "$@"
}

cleanup_source_container() {
  local container_name=$1
  if "$DOCKER_BIN" container inspect "$container_name" >/dev/null 2>&1; then
    "$DOCKER_BIN" rm -f "$container_name" >/dev/null
  fi
}

run_source() {
  local source=$1
  local container_name=$2
  "$TIMEOUT_BIN" \
    --foreground \
    --signal=TERM \
    --kill-after=30s \
    "${SOURCE_TIMEOUT_SECONDS}s" \
    "${compose[@]}" run --rm --no-deps --name "$container_name" collector \
    crawl --source "$source" --channel all
}

active_container=""
stop_active_container() {
  if [[ -n "$active_container" ]]; then
    cleanup_source_container "$active_container" || true
  fi
}
trap 'stop_active_container; exit 143' INT TERM HUP

if ! "${compose[@]}" config --quiet; then
  log_event "batch_preflight_failed" "compose_config_invalid"
  exit 1
fi
if ! run_container check-schema; then
  log_event "batch_preflight_failed" "database_schema_invalid"
  exit 1
fi
if ! run_container check-runtime; then
  log_event "batch_preflight_failed" "runtime_storage_invalid"
  exit 1
fi
if ! run_container recover-runs; then
  log_event "batch_preflight_failed" "abandoned_run_recovery_failed"
  exit 1
fi

source_output=$(run_container list-sources --format lines)
if [[ $? -ne 0 || -z "$source_output" ]]; then
  log_event "batch_preflight_failed" "source_catalog_unavailable"
  exit 1
fi

sources=()
while IFS= read -r source; do
  source=${source%$'\r'}
  [[ -z "$source" ]] && continue
  if [[ ! "$source" =~ ^[a-z0-9-]+$ ]]; then
    log_event "batch_preflight_failed" "invalid_source_alias"
    exit 1
  fi
  sources+=("$source")
done <<< "$source_output"

if [[ ${#sources[@]} -eq 0 ]]; then
  log_event "batch_preflight_failed" "source_catalog_empty"
  exit 1
fi
if [[ ! "$CRAWL_CONTAINER_PREFIX" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  log_event "batch_preflight_failed" "invalid_container_prefix"
  exit 1
fi

started_at=$SECONDS
succeeded=()
failed=()

for source in "${sources[@]}"; do
  attempt=1
  source_succeeded=false
  container_name="${CRAWL_CONTAINER_PREFIX}-${source}"
  while (( attempt <= MAX_ATTEMPTS )); do
    log_event "source_started" "${source}:attempt-${attempt}"
    if ! cleanup_source_container "$container_name"; then
      log_event "source_failed" "${source}:stale_container_cleanup_failed"
      failed+=("cleanup:${source}")
      break
    fi
    active_container=$container_name
    run_source "$source" "$container_name"
    exit_code=$?
    active_container=""
    if [[ $exit_code -eq 0 ]]; then
      source_succeeded=true
      succeeded+=("$source")
      log_event "source_succeeded" "${source}:attempt-${attempt}"
      break
    fi

    log_event "source_failed" "${source}:attempt-${attempt}:exit-${exit_code}"
    if ! cleanup_source_container "$container_name"; then
      failed+=("cleanup:${source}")
    fi
    if ! run_container recover-runs \
      --source "$source" \
      --older-than-minutes 0; then
      failed+=("recovery:${source}")
    fi
    if (( attempt < MAX_ATTEMPTS && RETRY_DELAY_SECONDS > 0 )); then
      sleep "$RETRY_DELAY_SECONDS"
    fi
    ((attempt += 1))
  done

  if [[ "$source_succeeded" != true ]]; then
    failed+=("source:${source}")
  fi
  if (( INTER_SOURCE_DELAY_SECONDS > 0 )); then
    sleep "$INTER_SOURCE_DELAY_SECONDS"
  fi
done

for check in check-runtime check-schema check-data check-source-health; do
  if run_container "$check"; then
    log_event "postflight_check_succeeded" "$check"
  else
    log_event "postflight_check_failed" "$check"
    failed+=("check:${check}")
  fi
done

duration=$((SECONDS - started_at))
succeeded_csv=$(IFS=,; printf '%s' "${succeeded[*]}")
failed_csv=$(IFS=,; printf '%s' "${failed[*]}")
log_event \
  "batch_finished" \
  "duration_seconds=${duration};succeeded=${succeeded_csv};failed=${failed_csv}"

if [[ ${#failed[@]} -gt 0 ]]; then
  exit 1
fi
