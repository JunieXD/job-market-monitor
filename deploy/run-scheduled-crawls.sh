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
CRAWL_LOG_FILE=${CRAWL_LOG_FILE:-}
LOG_MAX_BYTES=${LOG_MAX_BYTES:-20971520}
LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-14}
TEE_BIN=${TEE_BIN:-/usr/bin/tee}
STAT_BIN=${STAT_BIN:-/usr/bin/stat}
GZIP_BIN=${GZIP_BIN:-/usr/bin/gzip}
FIND_BIN=${FIND_BIN:-/usr/bin/find}
BATCH_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"

log_event() {
  local event=$1
  local detail=${2:-}
  detail=${detail//\/\\}
  detail=${detail//\"/\\\"}
  detail=${detail//$'\n'/\\n}
  printf '{"time":"%s","batch_id":"%s","event":"%s","detail":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BATCH_ID" "$event" "$detail"
}

prepare_log_output() {
  [[ -z "$CRAWL_LOG_FILE" ]] && return 0
  local log_dir
  local current_size=0
  local rotated_file
  log_dir=$(dirname "$CRAWL_LOG_FILE")
  if [[ "$log_dir" != /* || "$log_dir" == "/" ]]; then
    return 1
  fi
  mkdir -p "$log_dir" || return 1
  if [[ -f "$CRAWL_LOG_FILE" ]]; then
    current_size=$("$STAT_BIN" --format=%s "$CRAWL_LOG_FILE") || return 1
  fi
  if (( current_size >= LOG_MAX_BYTES )); then
    rotated_file="${CRAWL_LOG_FILE}.$(date -u +%Y%m%dT%H%M%SZ).$$"
    mv "$CRAWL_LOG_FILE" "$rotated_file" || return 1
    "$GZIP_BIN" "$rotated_file" || true
  fi
  "$FIND_BIN" "$log_dir" -maxdepth 1 -type f \
    -name "$(basename "$CRAWL_LOG_FILE").*.gz" \
    -mtime "+${LOG_RETENTION_DAYS}" -delete || true
  exec > >("$TEE_BIN" -a "$CRAWL_LOG_FILE") 2>&1
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
if [[ ! "$LOG_MAX_BYTES" =~ ^[1-9][0-9]*$ ]] \
  || [[ ! "$LOG_RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  log_event "batch_preflight_failed" "invalid_log_retention_configuration"
  exit 1
fi
if ! prepare_log_output; then
  log_event "batch_preflight_failed" "log_output_unavailable"
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
partial=()
failed=()
log_event \
  "batch_started" \
  "sources=${#sources[@]};max_parallel=${MAX_PARALLEL_SOURCES};start_delay_seconds=${SOURCE_START_DELAY_SECONDS}"

run_source_worker() {
  local source=$1
  local attempt=1
  local source_succeeded=false
  local container_name="${CRAWL_CONTAINER_PREFIX}-${source}"
  local exit_code
  local final_exit_code=1

  while (( attempt <= MAX_ATTEMPTS )); do
    log_event "source_started" "${source}:attempt-${attempt}"
    if ! cleanup_source_container "$container_name"; then
      log_event "source_failed" "${source}:stale_container_cleanup_failed"
      return 1
    fi
    run_source "$source" "$container_name"
    exit_code=$?
    final_exit_code=$exit_code
    if [[ $exit_code -eq 0 ]]; then
      source_succeeded=true
      log_event "source_succeeded" "${source}:attempt-${attempt}"
      break
    fi

    if [[ $exit_code -eq 2 ]]; then
      log_event "source_attempt_partial" "${source}:attempt-${attempt}"
    else
      log_event "source_attempt_failed" "${source}:attempt-${attempt}:exit-${exit_code}"
    fi
    if ! cleanup_source_container "$container_name"; then
      log_event "source_recovery_warning" "${source}:container_cleanup_failed"
    fi
    if ! run_container recover-runs \
      --source "$source" \
      --older-than-minutes 0; then
      log_event "source_recovery_warning" "${source}:run_recovery_failed"
    fi
    if (( attempt < MAX_ATTEMPTS && RETRY_DELAY_SECONDS > 0 )); then
      sleep "$RETRY_DELAY_SECONDS"
    fi
    ((attempt += 1))
  done

  if [[ "$source_succeeded" == true ]]; then
    return 0
  fi
  if [[ $final_exit_code -eq 2 ]]; then
    return 2
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
  local worker_exit_code
  while :; do
    for index in "${!active_pids[@]}"; do
      pid=${active_pids[$index]}
      source=${active_sources[$index]}
      if job_is_running "$pid"; then
        continue
      fi
      wait "$pid"
      worker_exit_code=$?
      case "$worker_exit_code" in
        0) succeeded+=("$source") ;;
        2) partial+=("source:${source}") ;;
        *) failed+=("source:${source}") ;;
      esac
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

critical_checks_failed=()
for check in check-runtime check-schema check-data; do
  check_output=$(run_container "$check" 2>&1)
  if [[ $? -eq 0 ]]; then
    log_event "postflight_check_succeeded" "$check"
  else
    log_event "postflight_check_failed" "${check}:${check_output:0:2000}"
    critical_checks_failed+=("check:${check}")
  fi
done
health_degraded=false
health_output=$(run_container check-source-health 2>&1)
if [[ $? -eq 0 ]]; then
  log_event "postflight_check_succeeded" "check-source-health"
else
  health_degraded=true
  log_event "postflight_check_degraded" "check-source-health"
fi

duration=$((SECONDS - started_at))
succeeded_csv=$(IFS=,; printf '%s' "${succeeded[*]}")
partial_csv=$(IFS=,; printf '%s' "${partial[*]}")
failed_csv=$(IFS=,; printf '%s' "${failed[*]}")
critical_csv=$(IFS=,; printf '%s' "${critical_checks_failed[*]}")
batch_status=success
if [[ ${#critical_checks_failed[@]} -gt 0 ]]; then
  batch_status=failed
elif [[ ${#partial[@]} -gt 0 || ${#failed[@]} -gt 0 || "$health_degraded" == true ]]; then
  batch_status=degraded
fi
log_event \
  "batch_finished" \
  "status=${batch_status};duration_seconds=${duration};succeeded=${succeeded_csv};partial=${partial_csv};failed=${failed_csv};critical=${critical_csv}"

if [[ ${#critical_checks_failed[@]} -gt 0 ]]; then
  exit 1
fi
if [[ "$batch_status" == degraded ]]; then
  exit 2
fi
