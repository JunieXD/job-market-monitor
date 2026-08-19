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
MAX_PARALLEL_SOURCES=${MAX_PARALLEL_SOURCES:-2}
SOURCE_START_DELAY_SECONDS=${SOURCE_START_DELAY_SECONDS:-3}

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
    crawl --source "$source" --channel all --due-only
}

active_pids=()
active_sources=()
active_count=0

stop_active_workers() {
  local index
  local source
  for index in "${!active_pids[@]}"; do
    kill -TERM "${active_pids[$index]}" >/dev/null 2>&1 || true
  done
  for index in "${!active_sources[@]}"; do
    source=${active_sources[$index]}
    cleanup_source_container "${CRAWL_CONTAINER_PREFIX}-${source}" || true
  done
  for index in "${!active_pids[@]}"; do
    wait "${active_pids[$index]}" >/dev/null 2>&1 || true
  done
}
trap 'stop_active_workers; exit 143' INT TERM HUP

validate_positive_integer() {
  local name=$1
  local value=$2
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    log_event "batch_preflight_failed" "${name}_must_be_a_positive_integer"
    exit 1
  fi
}

validate_nonnegative_integer() {
  local name=$1
  local value=$2
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    log_event "batch_preflight_failed" "${name}_must_be_a_nonnegative_integer"
    exit 1
  fi
}

validate_positive_integer "source_timeout_seconds" "$SOURCE_TIMEOUT_SECONDS"
validate_positive_integer "max_attempts" "$MAX_ATTEMPTS"
validate_positive_integer "max_parallel_sources" "$MAX_PARALLEL_SOURCES"
validate_nonnegative_integer "retry_delay_seconds" "$RETRY_DELAY_SECONDS"
validate_nonnegative_integer "source_start_delay_seconds" "$SOURCE_START_DELAY_SECONDS"
if (( MAX_PARALLEL_SOURCES > 32 )); then
  log_event "batch_preflight_failed" "max_parallel_sources_exceeds_32"
  exit 1
fi

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

source_output=$(run_container list-sources --format lines --due-only)
if [[ $? -ne 0 ]]; then
  log_event "batch_preflight_failed" "source_catalog_unavailable"
  exit 1
fi
if [[ -z "$source_output" ]]; then
  log_event "batch_skipped" "all_sources_already_collected_today"
  exit 0
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
log_event \
  "batch_started" \
  "sources=${#sources[@]};max_parallel=${MAX_PARALLEL_SOURCES};start_delay_seconds=${SOURCE_START_DELAY_SECONDS}"

run_source_worker() {
  local source=$1
  local attempt=1
  local source_succeeded=false
  local infrastructure_succeeded=true
  local container_name="${CRAWL_CONTAINER_PREFIX}-${source}"
  local exit_code

  while (( attempt <= MAX_ATTEMPTS )); do
    log_event "source_started" "${source}:attempt-${attempt}"
    if ! cleanup_source_container "$container_name"; then
      log_event "source_failed" "${source}:stale_container_cleanup_failed"
      return 1
    fi
    run_source "$source" "$container_name"
    exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
      source_succeeded=true
      log_event "source_succeeded" "${source}:attempt-${attempt}"
      break
    fi

    log_event "source_failed" "${source}:attempt-${attempt}:exit-${exit_code}"
    if ! cleanup_source_container "$container_name"; then
      infrastructure_succeeded=false
      log_event "source_failed" "${source}:container_cleanup_failed"
    fi
    if ! run_container recover-runs \
      --source "$source" \
      --older-than-minutes 0; then
      infrastructure_succeeded=false
      log_event "source_failed" "${source}:run_recovery_failed"
    fi
    if (( attempt < MAX_ATTEMPTS && RETRY_DELAY_SECONDS > 0 )); then
      sleep "$RETRY_DELAY_SECONDS"
    fi
    ((attempt += 1))
  done

  if [[ "$source_succeeded" == true && "$infrastructure_succeeded" == true ]]; then
    return 0
  fi
  return 1
}

job_is_running() {
  local expected_pid=$1
  local running_pid
  while IFS= read -r running_pid; do
    if [[ "$running_pid" == "$expected_pid" ]]; then
      return 0
    fi
  done < <(jobs -pr)
  return 1
}

reap_one_worker() {
  local index
  local pid
  local source
  while :; do
    for index in "${!active_pids[@]}"; do
      pid=${active_pids[$index]}
      source=${active_sources[$index]}
      if job_is_running "$pid"; then
        continue
      fi
      if wait "$pid"; then
        succeeded+=("$source")
      else
        failed+=("source:${source}")
      fi
      unset 'active_pids[index]'
      unset 'active_sources[index]'
      ((active_count -= 1))
      return
    done
    sleep 0.2
  done
}

for source in "${sources[@]}"; do
  while (( active_count >= MAX_PARALLEL_SOURCES )); do
    reap_one_worker
  done
  run_source_worker "$source" &
  active_pids+=("$!")
  active_sources+=("$source")
  ((active_count += 1))
  if (( SOURCE_START_DELAY_SECONDS > 0 )); then
    sleep "$SOURCE_START_DELAY_SECONDS"
  fi
done

while (( active_count > 0 )); do
  reap_one_worker
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
