#!/bin/bash -p
# Cron entrypoint for privacy-safe email-memory maintenance telemetry.
set -euo pipefail
unset BASH_ENV ENV
for variable in "${!PYTHON@}"; do unset "$variable"; done
for variable in "${!HIMALAYA_@}" "${!HERMES_@}"; do unset "$variable"; done
CLEAN_ENV=(/usr/bin/env -u BASH_ENV -u ENV -u BASHOPTS -u SHELLOPTS)
umask 077
PATH='/usr/bin:/bin'
export PATH

SCRIPT_DIR="$(cd "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/email_memory_environment.sh"
unset EMAIL_MEMORY_ROOT EMAIL_MEMORY_STORE_RUNTIME_CONFIG \
  EMAIL_MEMORY_ACCOUNT_NAME EMAIL_MEMORY_ACCOUNT_EMAIL \
  EMAIL_MEMORY_INCLUDE_FOLDERS_JSON EMAIL_MEMORY_EXCLUDE_FOLDERS_JSON \
  EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT \
  EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER \
  EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE EMAIL_MEMORY_HERMES_EXECUTABLE \
  HERMES_ALERT_TARGET
if ! config_exports="$("${CLEAN_ENV[@]}" "$EMAIL_MEMORY_OPERATIONAL_PYTHON" \
  -m email_memory_store.local_config --profile cron --shell)"; then
  printf '%s\n' 'email-memory cron configuration could not be loaded' >&2
  exit 2
fi
eval "$config_exports"
unset config_exports

ROOT="$EMAIL_MEMORY_ROOT"
REPORTS_DIR="$ROOT/reports"
ALERT_DIR="$REPORTS_DIR/nightly_alerts"
LOCK_FILE="$ROOT/nightly_maintenance.lock"
DEFAULT_MAINTENANCE_SCRIPT="$SCRIPT_DIR/nightly_maintenance.sh"
MAINTENANCE_SCRIPT="${EMAIL_MEMORY_MAINTENANCE_SCRIPT:-$DEFAULT_MAINTENANCE_SCRIPT}"
ALERT_TARGET="$HERMES_ALERT_TARGET"
unset HERMES_ALERT_TARGET
HERMES="$EMAIL_MEMORY_HERMES_EXECUTABLE"
ARTIFACT_PYTHON="$EMAIL_MEMORY_OPERATIONAL_PYTHON"
ARTIFACT_MODULE="email_memory_store.operational_artifacts"
LOCK_BUSY_EXIT=75
WEEKLY_ALERT_DAY="${EMAIL_MEMORY_WEEKLY_ALERT_DAY:-7}"
REPORT_RETENTION_DAYS="${EMAIL_MEMORY_REPORT_RETENTION_DAYS:-30}"
DELIVERED_ALERT_RETENTION_DAYS="${EMAIL_MEMORY_DELIVERED_ALERT_RETENTION_DAYS:-8}"
PENDING_ALERT_RETENTION_DAYS="${EMAIL_MEMORY_PENDING_ALERT_RETENTION_DAYS:-35}"
FLUSH_ONLY=0
STAMP="$(date -u +%Y%m%dT%H%M%S)-$$-${RANDOM}"
RUN_ID="run-${STAMP}"
WEEK_KEY="$(date +%G-W%V)"
RUN_LOG="$REPORTS_DIR/nightly_cron_${STAMP}.jsonl"
BATCH_FILE="$ALERT_DIR/${WEEK_KEY}.jsonl"
FLUSH_LOCK="$ALERT_DIR/.flush.lock"

artifact() {
  "${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" -m "$ARTIFACT_MODULE" "$@"
}

die() {
  printf '%s\n' "$1" >&2
  exit 2
}

validate_days() {
  local name=$1
  local value=$2
  [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 && "$value" -le 3650 ]] \
    || die "$name must be an integer between 1 and 3650"
}

if [[ $# -gt 1 ]]; then
  die "usage: $0 [--flush-alerts-only]"
fi
if [[ ${1:-} == '--flush-alerts-only' ]]; then
  FLUSH_ONLY=1
elif [[ $# -eq 1 ]]; then
  die "usage: $0 [--flush-alerts-only]"
fi
[[ "$WEEKLY_ALERT_DAY" =~ ^[1-7]$ ]] || die "EMAIL_MEMORY_WEEKLY_ALERT_DAY must be between 1 and 7"
validate_days EMAIL_MEMORY_REPORT_RETENTION_DAYS "$REPORT_RETENTION_DAYS"
validate_days EMAIL_MEMORY_DELIVERED_ALERT_RETENTION_DAYS "$DELIVERED_ALERT_RETENTION_DAYS"
validate_days EMAIL_MEMORY_PENDING_ALERT_RETENTION_DAYS "$PENDING_ALERT_RETENTION_DAYS"
if [[ "$MAINTENANCE_SCRIPT" != "$DEFAULT_MAINTENANCE_SCRIPT" && "${EMAIL_MEMORY_TEST_MODE:-0}" != '1' ]]; then
  die "EMAIL_MEMORY_MAINTENANCE_SCRIPT is available only in explicit test mode"
fi

artifact secure --directory "$REPORTS_DIR"
artifact secure --directory "$ALERT_DIR"
artifact secure --directory "$REPORTS_DIR" --file "$RUN_LOG"
artifact secure --directory "$ALERT_DIR" --file "$FLUSH_LOCK"

append_event() {
  local path=$1
  local event_code=$2
  local severity=$3
  shift 3
  artifact append --path "$path" --event-code "$event_code" --run-id "$RUN_ID" --severity "$severity" "$@"
}

log_event() {
  local event_code=$1
  local severity=$2
  shift 2
  append_event "$RUN_LOG" "$event_code" "$severity" "$@"
}

record_alert() {
  local event_code=$1
  local severity=$2
  shift 2
  exec 8>>"$FLUSH_LOCK"
  flock 8
  append_event "$BATCH_FILE" "$event_code" "$severity" "$@"
  flock -u 8
  exec 8>&-
}

prune_artifacts() {
  artifact prune --directory "$REPORTS_DIR" \
    --pattern 'nightly_cron_*.log' --pattern 'nightly_*.log' --days 0
  artifact prune --directory "$ALERT_DIR" --pattern '*.log' --days 0
  artifact harden --directory "$REPORTS_DIR" \
    --pattern 'nightly_cron_*.jsonl' --pattern 'nightly_*.jsonl'
  artifact harden --directory "$ALERT_DIR" --pattern '*.jsonl' --pattern '*.sent'
  artifact prune --directory "$REPORTS_DIR" \
    --pattern 'nightly_cron_*.jsonl' --pattern 'nightly_*.jsonl' \
    --days "$REPORT_RETENTION_DAYS"
  artifact prune --directory "$ALERT_DIR" --pattern '*.jsonl' \
    --days "$DELIVERED_ALERT_RETENTION_DAYS" --companion-suffix '.sent'
  artifact prune --directory "$ALERT_DIR" --pattern '*.jsonl' \
    --days "$PENDING_ALERT_RETENTION_DAYS"
  artifact prune --directory "$ALERT_DIR" --pattern '*.sent' \
    --days "$PENDING_ALERT_RETENTION_DAYS"
  artifact prune --directory "$ALERT_DIR" \
    --pattern '.render.*' --pattern '.transport.*' --days 1
  artifact prune --directory "$REPORTS_DIR" \
    --pattern '.maintenance.*' --days 1
}

flush_weekly_alerts() {
  local force_mode=${1:-0}
  local batch_file sent_marker week_key render_file delivered_file
  local pending=()
  local flush_result=0

  [[ "$force_mode" == '1' || "$(date +%u)" == "$WEEKLY_ALERT_DAY" ]] || return 0
  exec 9>>"$FLUSH_LOCK"
  flock -n 9 || return 0

  shopt -s nullglob
  pending=("$ALERT_DIR"/*.jsonl)
  shopt -u nullglob

  for batch_file in "${pending[@]}"; do
    sent_marker="${batch_file}.sent"
    [[ -s "$batch_file" && ! -e "$sent_marker" ]] || continue
    week_key="$(basename "${batch_file%.jsonl}")"
    week_key="${week_key:0:8}"
    render_file="$(mktemp "$ALERT_DIR/.render.XXXXXX")"
    if ! artifact render --input "$batch_file" --output "$render_file"; then
      rm -f -- "$render_file"
      log_event alert_batch_rejected error --stage "$week_key" --retryable false
      flush_result=1
      continue
    fi
    if "${CLEAN_ENV[@]}" "$HERMES" send --to "$ALERT_TARGET" \
      --subject "[email-memory-store] weekly maintenance alerts ($week_key)" \
      --file "$render_file" >/dev/null 2>&1; then
      delivered_file="$ALERT_DIR/${week_key}.${RUN_ID}.jsonl"
      mv -- "$batch_file" "$delivered_file"
      sent_marker="${delivered_file}.sent"
      artifact secure --directory "$ALERT_DIR" --file "$sent_marker"
      log_event alert_batch_delivered info --stage "$week_key"
    else
      log_event alert_batch_delivery_failed error --stage "$week_key" --retryable true
      flush_result=1
    fi
    rm -f -- "$render_file"
  done

  return "$flush_result"
}

prune_artifacts
log_event cron_started info

if [[ "$FLUSH_ONLY" == '1' ]]; then
  if flush_weekly_alerts 1; then
    result=0
  else
    result=$?
  fi
  if [[ "$result" == '0' ]]; then
    log_event cron_flush_completed info
  else
    log_event cron_flush_failed error --exit-code "$result" --retryable true
  fi
  prune_artifacts
  exit "$result"
fi

if EMAIL_MEMORY_ALERT_BATCH_FILE="$BATCH_FILE" \
  EMAIL_MEMORY_RUN_ID="$RUN_ID" \
  /usr/bin/flock -n -E "$LOCK_BUSY_EXIT" "$LOCK_FILE" \
    "${CLEAN_ENV[@]}" "$MAINTENANCE_SCRIPT" \
  >/dev/null 2>&1; then
  result=0
else
  result=$?
fi

case "$result" in
  0)
    log_event maintenance_completed info
    ;;
  "$LOCK_BUSY_EXIT")
    record_alert maintenance_lock_busy warning --stage maintenance --retryable true
    log_event maintenance_lock_busy warning --stage maintenance --retryable true
    ;;
  *)
    record_alert maintenance_failed error --stage maintenance --exit-code "$result" --retryable true
    log_event maintenance_failed error --stage maintenance --exit-code "$result" --retryable true
    ;;
esac

if flush_weekly_alerts; then
  flush_result=0
else
  flush_result=$?
fi
prune_artifacts
if [[ "$result" == '0' && "$flush_result" != '0' ]]; then
  exit "$flush_result"
fi
exit "$result"
