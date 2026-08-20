#!/bin/bash -p
# Privacy-safe nightly maintenance for a locally configured deployment.
set -euo pipefail
unset BASH_ENV ENV LLM_PREFLIGHT_EXPECTED_RESPONSE \
  EMAIL_MEMORY_INTERNAL_VERIFIED_DEFAULT_ACCOUNT
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
  -m email_memory_store.local_config --profile maintenance --shell)"; then
  printf '%s\n' 'email-memory maintenance configuration could not be loaded' >&2
  exit 2
fi
eval "$config_exports"
unset config_exports

ROOT="$EMAIL_MEMORY_ROOT"
MAIL_CLIENT="$EMAIL_MEMORY_MAIL_CLIENT_EXECUTABLE"
HERMES="$EMAIL_MEMORY_HERMES_EXECUTABLE"
ALERT_TARGET="$HERMES_ALERT_TARGET"
unset HERMES_ALERT_TARGET
LOG_DIR="$ROOT/reports"
ALERT_DIR="$LOG_DIR/nightly_alerts"
FLUSH_LOCK="$ALERT_DIR/.flush.lock"
ARTIFACT_PYTHON="$EMAIL_MEMORY_OPERATIONAL_PYTHON"
ARTIFACT_MODULE="email_memory_store.operational_artifacts"
REPORT_RETENTION_DAYS="${EMAIL_MEMORY_REPORT_RETENTION_DAYS:-30}"
RUN_ID="${EMAIL_MEMORY_RUN_ID:-run-$(date -u +%Y%m%dT%H%M%S)-$$-${RANDOM}}"
LOGFILE="$LOG_DIR/nightly_${RUN_ID#run-}.jsonl"
CURRENT_STEP="startup"
FAILURE_LOGGED=0
SCRIPT_START=$SECONDS

export EMAIL_MEMORY_STORE_FACT_STORE_PROVIDER
export EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT

artifact() {
  "${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" -m "$ARTIFACT_MODULE" "$@"
}

artifact secure --directory "$LOG_DIR"
artifact secure --directory "$ALERT_DIR"
artifact secure --directory "$LOG_DIR" --file "$LOGFILE"
artifact secure --directory "$ALERT_DIR" --file "$FLUSH_LOCK"
artifact prune --directory "$LOG_DIR" --pattern 'nightly_*.jsonl' --days "$REPORT_RETENTION_DAYS"

TRANSIENT_DIR="$(mktemp -d "$LOG_DIR/.transient.XXXXXX")"
chmod 700 "$TRANSIENT_DIR"
cleanup() {
  rm -rf -- "$TRANSIENT_DIR"
}
trap cleanup EXIT

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
  append_event "$LOGFILE" "$event_code" "$severity" "$@"
  printf 'event=%s severity=%s run_id=%s\n' "$event_code" "$severity" "$RUN_ID"
}

on_error() {
  local status=$?
  if [[ "$FAILURE_LOGGED" == '0' ]]; then
    append_event "$LOGFILE" run_failed error --stage "$CURRENT_STEP" --exit-code "$status" --retryable true || true
  fi
  exit "$status"
}
trap on_error ERR

validate_batch_path() {
  local batch_file=${EMAIL_MEMORY_ALERT_BATCH_FILE:-}
  [[ -z "$batch_file" ]] && return 0
  if [[ "${EMAIL_MEMORY_TEST_MODE:-0}" == '1' ]]; then
    return 0
  fi
  case "$batch_file" in
    "$ALERT_DIR"/*.jsonl) return 0 ;;
    *) return 1 ;;
  esac
}

send_operational_alert() {
  local event_code=$1
  local severity=$2
  local stage=$3
  shift 3
  local batch_file=${EMAIL_MEMORY_ALERT_BATCH_FILE:-}
  local event_file render_file transport_output

  if [[ -n "$batch_file" ]]; then
    if ! validate_batch_path; then
      log_event alert_batch_path_rejected error --stage "$stage" --retryable false
      return 1
    fi
    exec 8>>"$FLUSH_LOCK"
    flock 8
    append_event "$batch_file" "$event_code" "$severity" --stage "$stage" "$@"
    flock -u 8
    exec 8>&-
    log_event alert_queued info --stage "$stage"
    return 0
  fi

  event_file="$(mktemp "$TRANSIENT_DIR/event.XXXXXX")"
  render_file="$(mktemp "$TRANSIENT_DIR/render.XXXXXX")"
  transport_output="$(mktemp "$TRANSIENT_DIR/transport.XXXXXX")"
  append_event "$event_file" "$event_code" "$severity" --stage "$stage" "$@"
  artifact render --input "$event_file" --output "$render_file"
  if "${CLEAN_ENV[@]}" "$HERMES" send \
    --to "$ALERT_TARGET" \
    --subject "[email-memory-store] operational alert" \
    --file "$render_file" >"$transport_output" 2>&1; then
    log_event alert_delivered info --stage "$stage"
  else
    log_event alert_delivery_failed warning --stage "$stage" --retryable true
  fi
}

run_himalaya_preflight() {
  CURRENT_STEP="mail-preflight"
  local account_file="$TRANSIENT_DIR/mail-account-preflight.out"
  local output_file="$TRANSIENT_DIR/mail-preflight.out"
  local error_file="$TRANSIENT_DIR/mail-preflight.err"
  local status
  log_event stage_started info --stage "$CURRENT_STEP"

  if "${CLEAN_ENV[@]}" "$MAIL_CLIENT" account list --output json >"$account_file" 2>"$error_file"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" != '0' ]]; then
    FAILURE_LOGGED=1
    log_event mail_preflight_failed error --stage "$CURRENT_STEP" --exit-code "$status" --retryable true
    send_operational_alert mail_preflight_failed error "$CURRENT_STEP" --exit-code "$status" --retryable true
    return "$status"
  fi
  if ! "${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" - "$account_file" <<'PY' 2>"$error_file"
import json
import os
import sys

try:
    payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1) from None
selected_name = os.environ.get("EMAIL_MEMORY_ACCOUNT_NAME", "")
if not isinstance(payload, list):
    raise SystemExit(1)
records = [item for item in payload if isinstance(item, dict)]
selected = [item for item in records if item.get("name") == selected_name]
defaults = [item for item in records if item.get("default") is True]
if (
    len(records) != len(payload)
    or len(selected) != 1
    or len(defaults) != 1
    or selected[0] is not defaults[0]
):
    raise SystemExit(1)
PY
  then
    FAILURE_LOGGED=1
    log_event mail_account_selection_failed error --stage "$CURRENT_STEP" --retryable false
    send_operational_alert mail_account_selection_failed error "$CURRENT_STEP" --retryable false
    return 1
  fi
  EMAIL_MEMORY_INTERNAL_VERIFIED_DEFAULT_ACCOUNT=1
  export EMAIL_MEMORY_INTERNAL_VERIFIED_DEFAULT_ACCOUNT
  if "${CLEAN_ENV[@]}" "$MAIL_CLIENT" folder list --output json >"$output_file" 2>"$error_file"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" != '0' ]]; then
    FAILURE_LOGGED=1
    log_event mail_preflight_failed error --stage "$CURRENT_STEP" --exit-code "$status" --retryable true
    send_operational_alert mail_preflight_failed error "$CURRENT_STEP" --exit-code "$status" --retryable true
    return "$status"
  fi
  if ! "${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" -m json.tool "$output_file" >/dev/null 2>"$error_file"; then
    FAILURE_LOGGED=1
    log_event mail_preflight_invalid_response error --stage "$CURRENT_STEP" --retryable true
    send_operational_alert mail_preflight_invalid_response error "$CURRENT_STEP" --retryable true
    return 1
  fi
  log_event stage_completed info --stage "$CURRENT_STEP"
}

run_llm_preflight() {
  CURRENT_STEP="llm-preflight"
  local output_file="$TRANSIENT_DIR/llm-preflight.out"
  local error_file="$TRANSIENT_DIR/llm-preflight.err"
  local response status
  log_event stage_started info --stage "$CURRENT_STEP"

  if "${CLEAN_ENV[@]}" "$HERMES" chat -Q -q "Reply with exactly OK." >"$output_file" 2>"$error_file"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" != '0' ]]; then
    FAILURE_LOGGED=1
    log_event llm_preflight_failed error --stage "$CURRENT_STEP" --exit-code "$status" --retryable true
    send_operational_alert llm_preflight_failed error "$CURRENT_STEP" --exit-code "$status" --retryable true
    return "$status"
  fi
  response="$(tail -n 1 "$output_file" | tr -d '\r')"
  if [[ "$response" != 'OK' ]]; then
    FAILURE_LOGGED=1
    log_event llm_preflight_unexpected_response error --stage "$CURRENT_STEP" --retryable true
    send_operational_alert llm_preflight_unexpected_response error "$CURRENT_STEP" --retryable true
    return 1
  fi
  log_event stage_completed info --stage "$CURRENT_STEP"
}

run_core_stage() {
  local stage=$1
  local output_file=$2
  shift 2
  local error_file="$TRANSIENT_DIR/${stage}.err"
  local started=$SECONDS
  local status
  CURRENT_STEP="$stage"
  log_event stage_started info --stage "$stage"
  if "$@" >"$output_file" 2>"$error_file"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" != '0' ]]; then
    FAILURE_LOGGED=1
    log_event stage_failed error --stage "$stage" --exit-code "$status" --elapsed-seconds "$((SECONDS - started))" --retryable true
    return "$status"
  fi
  log_event stage_completed info --stage "$stage" --elapsed-seconds "$((SECONDS - started))"
}

run_promotion_stage() {
  local stage="run-llm-promotions"
  local output_file="$TRANSIENT_DIR/run-llm-promotions.out"
  local error_file="$TRANSIENT_DIR/run-llm-promotions.err"
  local started=$SECONDS
  local status promotion_errors
  CURRENT_STEP="$stage"
  log_event stage_started info --stage "$stage"
  if "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" \
    run-llm-promotions --limit 50 --embed >"$output_file" 2>"$error_file"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" != '0' ]]; then
    FAILURE_LOGGED=1
    log_event stage_failed error --stage "$stage" --exit-code "$status" \
      --elapsed-seconds "$((SECONDS - started))" --retryable true
    send_operational_alert llm_promotions_failed error "$stage" \
      --exit-code "$status" --retryable true
    return "$status"
  fi

  if promotion_errors="$("${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" - "$output_file" <<'PY' 2>/dev/null
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
starts = [index for index, character in enumerate(text) if character == "{" and (index == 0 or text[index - 1] == "\n")]
for start in reversed(starts):
    try:
        summary, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        continue
    if text[start + end :].strip():
        continue
    errors = summary.get("errors") if isinstance(summary, dict) else None
    if isinstance(errors, int) and not isinstance(errors, bool) and errors >= 0:
        print(errors)
        raise SystemExit(0)
raise SystemExit(1)
PY
)"; then
    :
  else
    FAILURE_LOGGED=1
    log_event promotion_output_invalid error --stage "$stage" --retryable false
    send_operational_alert promotion_output_invalid error "$stage" --retryable false
    return 1
  fi
  if [[ "$promotion_errors" =~ ^[1-9][0-9]*$ ]]; then
    FAILURE_LOGGED=1
    log_event llm_promotions_reported_errors error --stage "$stage" \
      --count "$promotion_errors" --elapsed-seconds "$((SECONDS - started))" --retryable true
    send_operational_alert llm_promotions_reported_errors error "$stage" \
      --count "$promotion_errors" --retryable true
    return 1
  fi
  log_event stage_completed info --stage "$stage" \
    --elapsed-seconds "$((SECONDS - started))"
}

log_event maintenance_started info
run_core_stage runtime-doctor "$TRANSIENT_DIR/runtime-doctor.out" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" \
  runtime-doctor --require mail --require selected-llm
run_himalaya_preflight
run_llm_preflight

if [[ "${EMAIL_MEMORY_PREFLIGHT_ONLY:-0}" == '1' ]]; then
  log_event maintenance_preflight_only_completed info
  exit 0
fi

nightly_output="$TRANSIENT_DIR/nightly-update.out"
run_core_stage nightly-update "$nightly_output" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" nightly-update \
  --embed

if folder_failure_count="$("${CLEAN_ENV[@]}" "$ARTIFACT_PYTHON" - "$nightly_output" <<'PY' 2>/dev/null
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
start = text.find("{")
if start < 0:
    raise SystemExit(1)
summary, _ = json.JSONDecoder().raw_decode(text[start:])
if not isinstance(summary, dict) or "folder_fetch_failures" not in summary:
    raise SystemExit(1)
failures = summary["folder_fetch_failures"]
if not isinstance(failures, list):
    raise SystemExit(1)
print(len(failures))
PY
)"; then
  :
else
  FAILURE_LOGGED=1
  log_event nightly_output_invalid error --stage nightly-update --retryable false
  send_operational_alert nightly_output_invalid error nightly-update --retryable false
  exit 1
fi
if [[ "$folder_failure_count" =~ ^[1-9][0-9]*$ ]]; then
  log_event mail_folder_fetch_partial warning --stage nightly-update --count "$folder_failure_count" --retryable true
  send_operational_alert mail_folder_fetch_partial warning nightly-update \
    --count "$folder_failure_count" --retryable true
fi

run_core_stage extract-threads "$TRANSIENT_DIR/extract-threads.out" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" extract-threads --limit 200 --embed
if [[ -n "${EMAIL_MEMORY_STORE_PRIVATE_FACT_STORE_ROOT:-}" ]]; then
  run_promotion_stage
else
  log_event stage_skipped info --stage run-llm-promotions
fi
run_core_stage vector-reconcile "$TRANSIENT_DIR/vector-reconcile.out" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" embed-backfill
run_core_stage cursor-reconcile "$TRANSIENT_DIR/cursor-reconcile.out" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" reconcile-ingestion-cursors --apply
run_core_stage cleanup-expired "$TRANSIENT_DIR/cleanup-expired.out" \
  "${CLEAN_ENV[@]}" "$EMAIL_MEMORY_STORE_COMMAND" cleanup-expired --apply

CURRENT_STEP="complete"
log_event maintenance_completed info --elapsed-seconds "$((SECONDS - SCRIPT_START))"
