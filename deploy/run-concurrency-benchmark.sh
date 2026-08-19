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
BENCHMARK_SOURCES=${BENCHMARK_SOURCES:-"bytedance:experienced jd:experienced netease:general ant:experienced"}

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
stats_pid=""
monitor_marker="${run_dir}/.monitor-running"

cleanup() {
  local name
  rm -f "$monitor_marker"
  if [[ -n "$stats_pid" ]]; then
    kill "$stats_pid" >/dev/null 2>&1 || true
  fi
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
  local system_stats_file="${run_dir}/system-stats.tsv"
  local system_summary_file="${run_dir}/system-summary.txt"
  local name
  local mem_available_kib=0 swap_free_kib=0 swap_total_kib=0
  local pswpin pswpout oom_kill load_one
  local first_pswpin=0 first_pswpout=0 first_oom_kill=0
  local last_pswpin=0 last_pswpout=0 last_oom_kill=0
  local min_mem_available_kib=0 min_swap_free_kib=0
  local sample_count=0
  printf 'time\tname\tcpu\tmemory\tmemory_percent\tnet_io\n' >"$stats_file"
  printf 'time\tmem_available_kib\tswap_free_kib\tpswpin\tpswpout\toom_kill\tload_one\n' \
    >"$system_stats_file"
  while [[ -e "$monitor_marker" ]]; do
    if [[ -r /proc/meminfo && -r /proc/vmstat && -r /proc/loadavg ]]; then
      read -r mem_available_kib swap_free_kib swap_total_kib < <(
        awk '
          $1 == "MemAvailable:" { available = $2 }
          $1 == "SwapFree:" { free = $2 }
          $1 == "SwapTotal:" { total = $2 }
          END { print available, free, total }
        ' /proc/meminfo
      )
      read -r pswpin pswpout oom_kill < <(
        awk '
          $1 == "pswpin" { swap_in = $2 }
          $1 == "pswpout" { swap_out = $2 }
          $1 == "oom_kill" { oom = $2 }
          END { print swap_in + 0, swap_out + 0, oom + 0 }
        ' /proc/vmstat
      )
      read -r load_one _ </proc/loadavg
    else
      mem_available_kib=0
      swap_free_kib=0
      swap_total_kib=0
      pswpin=0
      pswpout=0
      oom_kill=0
      load_one=0
    fi
    if (( sample_count == 0 )); then
      first_pswpin=$pswpin
      first_pswpout=$pswpout
      first_oom_kill=$oom_kill
      min_mem_available_kib=$mem_available_kib
      min_swap_free_kib=$swap_free_kib
    fi
    (( mem_available_kib < min_mem_available_kib )) && min_mem_available_kib=$mem_available_kib
    (( swap_free_kib < min_swap_free_kib )) && min_swap_free_kib=$swap_free_kib
    last_pswpin=$pswpin
    last_pswpout=$pswpout
    last_oom_kill=$oom_kill
    ((sample_count += 1))
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mem_available_kib" "$swap_free_kib" \
      "$pswpin" "$pswpout" "$oom_kill" "$load_one" >>"$system_stats_file"
    for name in "${benchmark_names[@]}"; do
      if [[ "$($DOCKER_BIN inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]]; then
        "$DOCKER_BIN" stats --no-stream \
          --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}' \
          "$name" 2>/dev/null \
          | while IFS=$'\t' read -r name cpu memory memory_percent net_io; do
              printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$cpu" "$memory" \
                "$memory_percent" "$net_io" \
                >>"$stats_file"
            done
      fi
    done
    sleep 0.5
  done
  printf 'samples=%s\n' "$sample_count" >"$system_summary_file"
  printf 'min_mem_available_kib=%s\n' "$min_mem_available_kib" >>"$system_summary_file"
  printf 'max_swap_used_kib=%s\n' "$((swap_total_kib - min_swap_free_kib))" \
    >>"$system_summary_file"
  printf 'swap_in_pages_delta=%s\n' "$((last_pswpin - first_pswpin))" \
    >>"$system_summary_file"
  printf 'swap_out_pages_delta=%s\n' "$((last_pswpout - first_pswpout))" \
    >>"$system_summary_file"
  printf 'oom_kill_delta=%s\n' "$((last_oom_kill - first_oom_kill))" \
    >>"$system_summary_file"
  awk -F '\t' '
    BEGIN { OFS = "\t"; print "name", "peak_cpu", "peak_memory", "peak_memory_percent", "final_net_io" }
    NR > 1 {
      cpu = $3
      memory_percent = $5
      gsub(/%/, "", cpu)
      gsub(/%/, "", memory_percent)
      if (!( $2 in peak_cpu ) || cpu + 0 > peak_cpu[$2]) {
        peak_cpu[$2] = cpu + 0
      }
      if (!( $2 in peak_memory_percent ) || memory_percent + 0 > peak_memory_percent[$2]) {
        peak_memory_percent[$2] = memory_percent + 0
        peak_memory[$2] = $4
      }
      final_net_io[$2] = $6
    }
    END {
      for (name in peak_cpu) {
        printf "%s\t%.2f%%\t%s\t%.2f%%\t%s\n", name, peak_cpu[name], peak_memory[name], peak_memory_percent[name], final_net_io[name]
      }
    }
  ' "$stats_file" >"${run_dir}/container-peaks.tsv"
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

touch "$monitor_marker"
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
rm -f "$monitor_marker"
wait "$stats_pid" >/dev/null 2>&1 || true
stats_pid=""

successful_cases=0
failed_cases=0
for exit_file in "${run_dir}"/*.exit; do
  exit_code=$(<"$exit_file")
  if [[ "$exit_code" == "0" ]]; then
    ((successful_cases += 1))
  else
    ((failed_cases += 1))
  fi
done

printf 'duration_seconds=%s\n' "$((SECONDS - started_at))" >"${run_dir}/summary.txt"
printf 'parallel=%s\nmax_pages=%s\n' "$BENCHMARK_PARALLEL" "$BENCHMARK_MAX_PAGES" \
  >>"${run_dir}/summary.txt"
printf 'successful_cases=%s\nfailed_cases=%s\n' "$successful_cases" "$failed_cases" \
  >>"${run_dir}/summary.txt"
log "benchmark finished output=${run_dir}"
trap - INT TERM HUP EXIT
cleanup
if (( failed_cases > 0 )); then
  exit 1
fi
