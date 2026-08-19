#!/usr/bin/env bash

set -uo pipefail

PROJECT_DIR=${PROJECT_DIR:-/opt/job-market-monitor}
COMPOSE_FILE=${COMPOSE_FILE:-${PROJECT_DIR}/compose.yaml}
DOCKER_BIN=${DOCKER_BIN:-/usr/bin/docker}
TIMEOUT_BIN=${TIMEOUT_BIN:-/usr/bin/timeout}
BENCHMARK_MAX_PAGES=${BENCHMARK_MAX_PAGES:-20}
BENCHMARK_TIMEOUT_SECONDS=${BENCHMARK_TIMEOUT_SECONDS:-3600}
BENCHMARK_PARALLEL=${BENCHMARK_PARALLEL:-1}
BENCHMARK_OUTPUT_DIR=${BENCHMARK_OUTPUT_DIR:-/tmp/job-market-monitor-benchmark}
BENCHMARK_RUN_ID=${BENCHMARK_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
BENCHMARK_SOURCES=${BENCHMARK_SOURCES:-"bytedance:experienced jd:experienced netease:general alibaba:campus"}

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

validate_positive_integer() {
  local name=$1
  local value=$2
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    log "invalid ${name}: ${value}"
    exit 1
  fi
}

validate_positive_integer "benchmark_max_pages" "$BENCHMARK_MAX_PAGES"
validate_positive_integer "benchmark_timeout_seconds" "$BENCHMARK_TIMEOUT_SECONDS"
validate_positive_integer "benchmark_parallel" "$BENCHMARK_PARALLEL"
if (( BENCHMARK_PARALLEL > 4 )); then
  log "benchmark_parallel must not exceed 4"
  exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  log "project directory is missing: $PROJECT_DIR"
  exit 1
fi
cd "$PROJECT_DIR" || exit 1

run_dir="${BENCHMARK_OUTPUT_DIR}/${BENCHMARK_RUN_ID}-p${BENCHMARK_PARALLEL}"
mkdir -p "$run_dir"
compose=("$DOCKER_BIN" compose -f "$COMPOSE_FILE")
active_names=()
active_pids=()
active_sources=()
active_count=0
benchmark_names=()

cleanup() {
  local name
  for name in "${active_names[@]}"; do
    "$DOCKER_BIN" rm -f "$name" >/dev/null 2>&1 || true
  done
}
trap cleanup INT TERM HUP EXIT

run_case() {
  local source=$1
  local channel=$2
  local safe_name="${source//[^a-zA-Z0-9_.-]/-}"
  local container_name="job-market-benchmark-${BENCHMARK_RUN_ID}-${safe_name}"
  local output_file="${run_dir}/${safe_name}.jsonl"
  local exit_file="${run_dir}/${safe_name}.exit"
  log "start source=${source} channel=${channel} container=${container_name}"
  "$TIMEOUT_BIN" \
    --foreground \
    --signal=TERM \
    --kill-after=30s \
    "${BENCHMARK_TIMEOUT_SECONDS}s" \
    "${compose[@]}" run --rm --no-deps --name "$container_name" collector \
    crawl --source "$source" --channel "$channel" --dry-run \
    --max-pages "$BENCHMARK_MAX_PAGES" >"$output_file" 2>&1
  local exit_code=$?
  printf '%s\n' "$exit_code" >"$exit_file"
  log "finish source=${source} exit=${exit_code} output=${output_file}"
  return "$exit_code"
}

monitor_stats() {
  local stats_file="${run_dir}/docker-stats.tsv"
  local any_running
  local name
  printf 'time\tname\tcpu\tmemory\tnet_io\n' >"$stats_file"
  while :; do
    any_running=false
    "$DOCKER_BIN" stats --no-stream \
      --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}' \
      "${benchmark_names[@]}" 2>/dev/null \
      | while IFS=$'\t' read -r name cpu memory net_io; do
          printf '%s\t%s\t%s\t%s\t%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$cpu" "$memory" "$net_io" \
            >>"$stats_file"
        done
    for name in "${benchmark_names[@]}"; do
      if [[ "$($DOCKER_BIN inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]]; then
        any_running=true
        break
      fi
    done
    [[ "$any_running" == true ]] || break
    sleep 1
  done
}

job_is_running() {
  local expected_pid=$1
  local running_pid
  while IFS= read -r running_pid; do
    [[ "$running_pid" == "$expected_pid" ]] && return 0
  done < <(jobs -pr)
  return 1
}

reap_one() {
  local index pid source
  while :; do
    for index in "${!active_pids[@]}"; do
      pid=${active_pids[$index]}
      source=${active_sources[$index]}
      if job_is_running "$pid"; then
        continue
      fi
      wait "$pid" >/dev/null 2>&1 || true
      unset 'active_pids[index]'
      unset 'active_sources[index]'
      ((active_count -= 1))
      return
    done
    sleep 0.2
  done
}

started_at=$SECONDS
for spec in $BENCHMARK_SOURCES; do
  source=${spec%%:*}
  channel=${spec#*:}
  if [[ -z "$source" || -z "$channel" || "$spec" != *:* ]]; then
    log "invalid source specification: $spec"
    exit 1
  fi
  safe_name="${source//[^a-zA-Z0-9_.-]/-}"
  benchmark_names+=("job-market-benchmark-${BENCHMARK_RUN_ID}-${safe_name}")
  active_names+=("job-market-benchmark-${BENCHMARK_RUN_ID}-${safe_name}")
done

monitor_stats &
stats_pid=$!
for spec in $BENCHMARK_SOURCES; do
  source=${spec%%:*}
  channel=${spec#*:}
  while (( active_count >= BENCHMARK_PARALLEL )); do
    reap_one
  done
  run_case "$source" "$channel" &
  active_pids+=("$!")
  active_sources+=("$spec")
  ((active_count += 1))
done
while (( active_count > 0 )); do
  reap_one
done
wait "$stats_pid" >/dev/null 2>&1 || true

printf 'duration_seconds=%s\n' "$((SECONDS - started_at))" >"${run_dir}/summary.txt"
printf 'parallel=%s\nmax_pages=%s\n' "$BENCHMARK_PARALLEL" "$BENCHMARK_MAX_PAGES" \
  >>"${run_dir}/summary.txt"
log "benchmark finished output=${run_dir}"
