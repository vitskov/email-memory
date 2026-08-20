#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

JOB="${1:-all}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required; install it from https://docs.astral.sh/uv/" >&2
  exit 1
fi

./scripts/bootstrap.sh --dev --accelerator cpu

run_in_project() {
  uv run --locked --extra dev --no-sync -- "$@"
}

run_static_analysis() {
  echo "==> Local CI: static-analysis"
  run_in_project env \
    PYTHON_BIN=python \
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
    ./scripts/check_quality.sh
}

run_tests() {
  echo "==> Local CI: tests"
  run_in_project env \
    PYTHON_BIN=python \
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
