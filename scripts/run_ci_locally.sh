#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
JOB="${1:-all}"

run_static_analysis() {
  echo "==> Local CI: static-analysis"
  env \
    PYTHON_BIN="$PYTHON_BIN" \
    RUN_COMPILEALL="0" \
    RUN_DEPENDENCY_CHECK="0" \
    RUN_RUFF="1" \
    RUN_MYPY="1" \
    RUN_PYTEST="0" \
    REQUIRE_RUFF="1" \
    REQUIRE_MYPY="1" \
    RUFF_TARGETS="src scripts" \
    RUFF_EXTRA_ARGS="--select E9,F" \
    MYPY_TARGETS="src" \
    MYPYPATH="src" \
    ./scripts/check_quality.sh
}

run_tests() {
  echo "==> Local CI: tests"
  env \
    PYTHON_BIN="$PYTHON_BIN" \
    STRICT_DEPENDENCY_CHECK="1" \
    RUN_RUFF="0" \
    RUN_MYPY="0" \
    ./scripts/check_quality.sh
}

case "$JOB" in
  static-analysis)
    run_static_analysis
    ;;
  tests)
    run_tests
    ;;
  all)
    run_static_analysis
    run_tests
    ;;
  *)
    echo "Usage: $0 [static-analysis|tests|all]" >&2
    exit 2
    ;;
esac
